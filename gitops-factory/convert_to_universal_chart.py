#!/usr/bin/env python3
"""
convert_to_universal_chart.py
==============================
Converts raw Kubernetes/OpenShift manifests dumped per-namespace/per-microservice

    namespaces/<namespace>/<microservice>.yaml   (multi-document)
    namespaces/<namespace>/shared.yaml           (multi-document, optional)

into:

    1. Two Universal Helm Chart values files PER microservice:
         base/<slug>.yaml                     (comprehensive - everything
                                                EXCEPT the image and replicas
                                                - written ONCE and reused by
                                                every namespace that runs
                                                this microservice)
         <namespace>/values/<slug>.yaml       (image + what differs ONLY -
                                                exclusively here, not duplicated
                                                in the comprehensive file - one
                                                per namespace)
       plus <namespace>/defaults.yaml (what every release there shares).
       <namespace> may be a variant folder (dev/yellow, dev/yellow/eu —
       --env-group, or already present in the input) deploying into its first
       path segment's namespace; values/ may hold grouping sub-folders. With
       --env-group / --env-suffixes, values differing only by a group token or
       an environment word are merged into one templated base value
       ({{ .Values.color }}, {{ .Values.environment }}, {{ .Release.Namespace }})
       whose per-folder value lives in defaults.yaml. The chart runs every value
       through `tpl`, so a literal '{{' from the source is written escaped as
       {{ "{{" }} (helm_safe_values). NetworkPolicy and ServiceMonitor are
       deliberately NOT converted (DROPPED_KINDS; one report line each).

    2. One namespace-shared "release" (workload.type: none) that owns any
       resources living in shared.yaml, so they are never double-declared
       (and therefore never double-owned) by every microservice that uses them.
       It is an ordinary <namespace>/values/shared.yaml like every other
       release — which is why <namespace>/defaults.yaml is computed with it in
       the pool (see Pass 3 in main()).

    3. (Always.) Two files at the tree ROOT, the ENTIRE ArgoCD wiring:
         rootApplication.yaml.txt   the one object you apply by hand
                                    (`kubectl apply -f rootApplication.yaml.txt`;
                                    .txt so no directory apply, ArgoCD's
                                    included, ever deploys or manages it)
         rootApplicationSet.yaml    one Application per namespace folder
                                    (variant folders included), each deploying
                                    the ms-applicationSet chart from the
                                    universal-chart repo
       That chart renders the per-folder ApplicationSet which actually fans
       out over <folder>/values/**/*.yaml and layers three values files:
       base/<file> -> <folder>/defaults.yaml -> <folder>/values/<file>.
       Adding a namespace is adding a directory; adding a microservice is
       adding a file. Neither needs a new manifest or another kubectl apply.

    4. One cluster-scoped "shared" values file + Application for anything
       that is cluster-scoped in Kubernetes (ClusterRole, ClusterRoleBinding,
       StorageClass, PersistentVolume, SecurityContextConstraints) — these can
       never be safely owned by more than one Helm release, so they are
       deduplicated globally across the *entire* run, not just per namespace.

    5. (Unless --skip-verify) Proof that the output is actually safe to
       apply: every generated release is rendered for real with `helm
       template` and checked, on the RENDERED manifests (not just the values
       files), that no two releases claim the same (kind, namespace, name)
       for a namespaced resource and no two releases in the whole run claim
       the same (kind, name) for a cluster-scoped one. This is the same
       static analysis as steps 1-4 above, just verified against the real
       chart instead of trusted blindly — pass --chart to point at a
       non-default Universal Chart location, or --skip-verify to skip it
       (e.g. if `helm` isn't installed, or for a quick dry run).

Conflict-free by construction
------------------------------
Several kinds render with their RAW map key as the Kubernetes resource name
(no release-name prefix) per the Universal Chart's templates: ConfigMap,
Secret, PersistentVolumeClaim, Role, RoleBinding, Route (extra),
Service (extra), SecretStore, ExternalSecret and (cluster-scoped) ClusterRole,
ClusterRoleBinding, StorageClass, PersistentVolume, SecurityContextConstraints,
ClusterSecretStore. Those are exactly the ones that can collide once multiple
microservices/namespaces are deployed as separate Helm releases. This script
resolves collisions deterministically:

  * A name first declared in shared.yaml is "owned" by the namespace-shared
    release. Any microservice that also happens to (re)declare it locally
    drops its own copy and simply references the shared name — no ownership
    fight, no "already exists" error.
  * A name first declared by microservice A, and later declared independently
    (by coincidence, not via shared.yaml) by microservice B, is automatically
    renamed to "<B>-<name>" for B, and every reference to it inside B's own
    values (env, envFrom, volumes, rbac roleRef/subjects) is rewritten to
    match. This is logged, not silently swallowed.
  * Cluster-scoped kinds are deduplicated by (kind, name) across the whole
    run. If two different namespaces define the same name with different
    content, the first one wins and the conflict is written to the report
    for a human to resolve — cluster-scoped identity can't be safely renamed
    (StorageClass/SCC names are referenced by exact string elsewhere).

Usage
-----
    python convert_to_universal_chart.py --input namespaces --output output

    # --chart defaults to '..' (this repo's root IS the Universal Chart); pass
    # --skip-verify to skip the helm-template render-verification step entirely
    # (e.g. helm isn't installed):
    python convert_to_universal_chart.py --input namespaces --output output --chart ..

See README.md for the full end-to-end verification workflow.
"""
from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from yaml_style import _ForcedBlockEmitterMixin

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Resources that are transient / controller-owned / generated by the cluster
# itself. A raw dump from `oc get all -o yaml` typically includes these; they
# are not desired-state and must never be converted.
TRANSIENT_KINDS = {
    "ReplicaSet", "Pod", "Endpoints", "EndpointSlice", "Event",
    "ControllerRevision", "Binding",
}

# Kinds deliberately NOT converted. NetworkPolicy and ServiceMonitor are
# platform concerns on the migration target (network isolation and scrape
# config are owned there, not by each application's release), so carrying the
# source cluster's copies would install policy nobody asked the release to own.
# They are dropped at load time by filter_transient — one line per object in
# conflicts_and_warnings.txt — and, because the round-trip fidelity check reads
# the raw side through the same filter_transient, never counted as lost there.
# The chart still has networkPolicies / serviceMonitor for hand-written values.
DROPPED_KINDS = {"NetworkPolicy", "ServiceMonitor"}

CLUSTER_SCOPED_KINDS = {
    "ClusterRole", "ClusterRoleBinding", "StorageClass",
    "PersistentVolume", "SecurityContextConstraints", "ClusterSecretStore",
}

WORKLOAD_KINDS = {"Deployment", "StatefulSet", "DaemonSet"}

# Kinds whose Universal Chart template renders the resource name as the RAW
# values map-key (no `<release>-` prefix) — see templates/*.yaml. These are
# exactly the ones the NamespaceRegistry / ClusterRegistry must dedupe.
NS_RAW_NAME_KINDS = [
    "ConfigMap", "Secret", "PersistentVolumeClaim",
    "Role", "RoleBinding", "ServiceAccount", "Route", "Service",
    "SecretStore", "ExternalSecret",
]

# OpenShift's integrated-registry controller names the Secrets it generates for
# a ServiceAccount `<sa>-dockercfg-XXXXX` / `<sa>-token-XXXXX`. Both are
# cluster-generated artifacts that do not exist on a vanilla migration target,
# so a reference to one is a reference to nothing. Anything NOT matching this is
# a credential a human added and has to survive the conversion.
#
# `{5,}` rather than `{5}`: the suffix is 5 characters in every sample in this
# repo, but nothing documents it as fixed-width, and an under-matching pattern
# here means carrying a dead reference rather than dropping a live one.
AUTO_SA_SECRET_RE = re.compile(r".+-(dockercfg|token)-[a-z0-9]{5,}$")

# Recognized environment-suffix tokens for env_normalized_ms_name() below.
KNOWN_ENV_SUFFIXES = {
    "dev", "development", "tst", "test", "testing", "stg", "stage", "staging",
    "prd", "prod", "production", "qa", "uat", "roi",
}


def env_normalized_ms_name(namespace: str, ms_name: str) -> str:
    """A namespace like 'myapp-tst' and a microservice file 'jenkins-tst.yaml'
    are, in real orgs, usually the SAME logical microservice (jenkins) as
    'myapp-prd'/'jenkins-prd.yaml' and 'myapp-dev'/'jenkins-dev.yaml' — not
    three unrelated ones that happen to share a naming coincidence. Only when
    the microservice filename's trailing '-<suffix>' matches the CONTAINING
    namespace's own trailing '-<suffix>' (both recognized environment
    tokens, and the SAME token) is it stripped, so 'jenkins-tst' in
    namespace 'myapp-tst' normalizes to 'jenkins' — shared release identity,
    one base/jenkins.yaml, across every such environment's copy. A
    coincidental match (e.g. a genuinely-named 'payments-tst' microservice
    sitting in namespace 'myapp-prd', suffixes differing) is left alone.

    --env-suffixes and --env-group switch on a second, broader rule (see
    NAME_TOKENS): every listed token standing alone ANYWHERE in the name is
    stripped, so 'filter-service-black-b2b-tst' and 'filter-service-yellow-b2b'
    both become 'filter-service-b2b'. Opt-in, because it merges releases the
    default rule deliberately keeps apart."""
    if NAME_TOKENS and ms_name not in TOKEN_EXEMPT_STEMS:
        ms_name = strip_name_tokens(ms_name, NAME_TOKENS)
    namespace = k8s_ns(namespace)
    ns_suffix = namespace.rsplit("-", 1)[-1] if "-" in namespace else None
    if ns_suffix not in KNOWN_ENV_SUFFIXES:
        return ms_name
    ms_base, _, ms_suffix = ms_name.rpartition("-")
    if ms_base and ms_suffix == ns_suffix:
        return ms_base
    return ms_name


# Tokens stripped from anywhere in a microservice name, before the
# namespace-suffix rule above. Empty by default; main() fills it from every
# --env-group token and from --env-suffixes (which also empties
# KNOWN_ENV_SUFFIXES, replacing that rule).
NAME_TOKENS: set[str] = set()

# File stems NAME_TOKENS must leave alone: ones whose stripped name no OTHER
# stem in the run shares (see token_exempt_stems). Filled once per run, before
# any name is normalized, so every caller — the build, the folder assignment,
# the fidelity check — agrees on a release's name.
TOKEN_EXEMPT_STEMS: set[str] = set()


def token_exempt_stems(stems, tokens) -> set[str]:
    """Stems a token is NOT stripped from, because stripping it folds nothing.

    The tokens exist to bring the copies of ONE microservice together across
    environments and variants: 'pay-dev' + 'pay-prd' -> 'pay', 'ms1-yellow' +
    'ms1-black' -> 'ms1'. A token that only happens to be a word of the name —
    'project-api.yaml', the same in every namespace, while 'project' is also an
    env suffix — produced a release called 'api' (and, as a group token, a
    'project/' variant folder) with no second file to share it with. So a strip
    is kept only when at least two DIFFERENT stems in the run land on the same
    name; a stem alone in its family keeps the name it has, whatever namespaces
    it appears in."""
    if not tokens:
        return set()
    families: dict[str, set[str]] = defaultdict(set)
    for stem in stems:
        families[strip_name_tokens(stem, tokens)].add(stem)
    return {stem for norm, members in families.items() if len(members) == 1 and norm != SHARED_DIR_NAME
            for stem in members if stem != norm}


def sibling_env_suffixes(namespace_dirs) -> set[str]:
    """The environment words a run's namespaces carry without being told.

    Sibling namespaces like goldenbell-ui and goldenbell-next usually hold ONE
    app, named after its namespace: goldenbell-ui.yaml there, goldenbell-next
    .yaml here (web-ui / web-next likewise). 'ui' and 'next' are no known
    environment word, so the copies stayed two unrelated releases — two base/
    files, nothing shared. A namespace's last word (after its last '-') is
    taken as an environment word when a file there carries it and a file in
    another namespace, carrying THAT namespace's last word, comes down to the
    same name — the evidence that the two are one app's copies. Returns those
    words; main() treats them as --env-suffixes would, on top of the default
    rule."""
    families: dict[str, set[str]] = defaultdict(set)
    for key, ns_dir in namespace_dirs:
        ns = k8s_ns(key)
        if "-" not in ns:
            continue
        suffix = ns.rsplit("-", 1)[1]
        for f in ns_dir.glob("*.yaml"):
            norm = strip_name_tokens(f.stem, {suffix})
            if norm != f.stem:
                families[norm].add(suffix)
    return {s for suffixes in families.values() if len(suffixes) > 1 for s in suffixes}


def _token_re(tokens) -> "re.Pattern":
    # A token counts only standing alone — delimited by a non-alphanumeric or
    # the string edge — so 'dev' is never found inside 'devtools' and 'tst'
    # never inside 'attest'. Longest first, so 'prod' does not eat 'production'.
    alt = "|".join(re.escape(t) for t in sorted(tokens, key=len, reverse=True))
    return re.compile(r"(?<![A-Za-z0-9])(?:" + alt + r")(?![A-Za-z0-9])")


def strip_name_tokens(name: str, tokens) -> str:
    """'filter-service-black-b2b-tst' minus {black, tst} -> 'filter-service-b2b'.
    Returns `name` unchanged if nothing would be left."""
    stripped = _token_re(tokens).sub("", name)
    stripped = re.sub(r"[-_.]{2,}", lambda m: m.group(0)[0], stripped).strip("-_.")
    return stripped or name


# ---------------------------------------------------------------------------
# Env groups (--env-group) — one base value instead of N per-namespace copies
# ---------------------------------------------------------------------------
# The same release in several namespaces often differs only by environment
# words: a route host 'api.fac1.black.example.org' next to
# 'api.fac2.yellow.example.org'. Without groups each namespace keeps its own
# concrete copy in values/. With `--env-group color=black,yellow` both collapse
# into ONE templated base value
#
#     api.{{ .Release.Namespace }}.{{ .Values.color }}.example.org
#
# and each release's values/ file carries its own `color: black`. The chart
# tpl's every value, so this renders back to exactly the source string.
#
# The namespace is always a group once any --env-group is given; it needs no
# injection because Helm already knows it. `--env-group namespace` turns on
# just that axis.
#
# Placeholders are held as sentinels until the file is written (see
# helm_safe_values): a source string's own '{{' must be escaped for the
# chart's tpl pass, while ours must not be, and the sentinel keeps them apart.

NAMESPACE_GROUP = "namespace"
ENVIRONMENT_GROUP = "environment"
_PH = "\x00{}\x00"
_PH_RE = re.compile(r"\x00([^\x00]+)\x00")


@dataclass
class EnvGroup:
    name: str
    tokens: dict  # token found in a value -> value injected as .Values.<name>
    # A folder group's token in a release's file name puts the release in a
    # sub-folder of its namespace named after it (dev/ms1-yellow.yaml ->
    # dev/yellow/). The implicit 'environment' group (--env-suffixes) never
    # does: its token is already the namespace.
    folder: bool = True

    def token_in(self, text: str) -> "str | None":
        found = [t for t in self.tokens if _token_re([t]).search(text)]
        return found[0] if len(found) == 1 else None

    @property
    def placeholder(self) -> str:
        return _PH.format(".Values." + self.name)


def parse_env_group(spec: str, reserved: set) -> EnvGroup:
    """'NAME=tok[:VALUE],...' — VALUE defaults to the token itself."""
    name, _, right = spec.partition("=")
    name = name.strip()
    if not re.fullmatch(r"[a-z][A-Za-z0-9_]*", name):
        sys.exit(f"--env-group {spec!r}: the group name must be a Helm value key "
                 f"(a lowercase letter, then letters, digits or '_')")
    if name in reserved:
        sys.exit(f"--env-group {spec!r}: '{name}' is one of the chart's own top-level "
                 f"values; injecting it would override that setting")
    tokens: dict = {}
    for piece in filter(None, (p.strip() for p in right.split(","))):
        tok, _, val = piece.partition(":")
        tok = tok.strip()
        if not tok or tok in tokens:
            sys.exit(f"--env-group {spec!r}: empty or duplicate token {tok!r}")
        tokens[tok] = val.strip() or tok
    if not tokens and name != NAMESPACE_GROUP:
        sys.exit(f"--env-group {spec!r}: no tokens — use '{name}=tok1,tok2'")
    return EnvGroup(name, tokens)


def folder_values(key: str, groups: list) -> dict:
    """The group values a folder stands for, written to its defaults.yaml:
    'dev/yellow' -> {color: yellow, environment: dev}. A folder group's value
    comes from the folder name, a non-folder group's (environment) from its
    token in the namespace name."""
    ns, _, rest = key.partition("/")
    parts = set(rest.split("/")) if rest else set()
    out = {}
    for g in groups:
        if g.name == NAMESPACE_GROUP:
            continue
        tok = next((t for t in g.tokens if t in parts), None) if g.folder else g.token_in(ns)
        if tok:
            out[g.name] = g.tokens[tok]
    return out


def combine_values(per_ns: dict, groups: list) -> "tuple[str, dict] | None":
    """Express per-namespace strings {namespace: value} as one template.

    Each namespace's value has its OWN namespace name replaced by
    {{ .Release.Namespace }} — never another namespace's, so a URL pointing at
    a neighbour namespace stays literal — and the one token of each group it
    carries by that group's placeholder. A group is used only where its token
    actually differs between namespaces: a word every copy shares is not an
    environment difference and stays literal. Returns (template,
    {group: {namespace: injected value}}) when every namespace ends up with the
    same template, else None. Rendering the template with a namespace's
    injected values gives back exactly that namespace's original string.
    """
    hits: dict = {}  # group -> {namespace: token}
    for g in groups:
        found = {}
        for ns, val in per_ns.items():
            present = [t for t in g.tokens if _token_re([t]).search(val)]
            if len(present) > 1:
                return None  # two of one group's tokens: ambiguous
            if present:
                found[ns] = present[0]
        if len(set(found.values())) > 1:
            hits[g.name] = found
    templates, injection = set(), {}
    for ns, val in per_ns.items():
        subs = {k8s_ns(ns): _PH.format(".Release.Namespace")}
        for g in groups:
            tok = hits.get(g.name, {}).get(ns)
            if tok:
                subs[tok] = g.placeholder
                injection.setdefault(g.name, {})[ns] = g.tokens[tok]
        # One regex pass, so a placeholder is never itself re-substituted.
        templates.add(_token_re(subs).sub(lambda m: subs[m.group(0)], val))
    if len(templates) != 1:
        return None
    template = templates.pop()
    injection = {g: m for g, m in injection.items() if _PH.format(".Values." + g) in template}
    if any(len(m) != len(per_ns) for m in injection.values()):
        return None  # a group placeholder some namespace could not fill
    return template, injection


def _string_leaves(d: dict, path: tuple = ()) -> dict:
    """Every string and every list under d, by path. A list (args, a sidecar's
    env) is compared as one JSON string, so a token anywhere inside it is found
    and the whole list is templated at once — lists are never merged by item."""
    out = {}
    for k, v in d.items():
        if isinstance(v, dict):
            out.update(_string_leaves(v, path + (k,)))
        elif isinstance(v, str):
            out[path + (k,)] = v
        elif isinstance(v, list) and k != "extraDeploy":
            out[path + (k,)] = "" + json.dumps(v, ensure_ascii=False)
    return out


def _leaf_paths(d: dict, prefix: str = "") -> list:
    """['env.test', 'resources.requests.memory'] — what actually differs, not
    just the top-level key it sits under (the rest of that key is in base/)."""
    out = []
    for k, v in d.items():
        p = prefix + _PH_RE.sub(r"{{ \1 }}", str(k))
        out +=_leaf_paths(v, p + ".") if isinstance(v, dict) and v else [p]
    return sorted(out)


def _leaf_value(text: str):
    """Undo _string_leaves' list encoding. strict=False: the placeholder
    sentinel is a control character, which strict JSON refuses in a string."""
    return json.loads(text[1:], strict=False) if text.startswith("") else text


def _pop_nested(d: dict, path: tuple) -> None:
    """Delete d[path...] and any dict it leaves empty on the way up."""
    if len(path) == 1:
        d.pop(path[0], None)
        return
    child = d.get(path[0])
    if isinstance(child, dict):
        _pop_nested(child, path[1:])
        if not child:
            del d[path[0]]


# A map key can carry a group token as much as a value can: a volume
# 'certs-black' next to 'certs-yellow', a CronJob, a sidecar, a ConfigMap, an
# env var name. As two different KEYS they never reach the leaf pass of
# combine_env_groups, so every copy stayed in values/. unify_group_keys renames
# them to one templated key first; the chart tpl's keys too
# (universal-chart.tplWalk). Top-level keys are the chart's own and never
# renamed; extraDeploy is a list, so it is never walked.


def _rename_tracked(ctx, path: tuple, new: str) -> None:
    """Move what ctx tracks by path (the per-namespace image/env/raw-image of a
    job, cronjob, sidecar or init container, and who owns a sub key) along with
    a key renamed at `path`, and rename it in a sibling initContainerOrder."""
    n = len(path)
    for tracker in (ctx.sub_full_image, ctx.sub_full_env, ctx.sub_raw_image, ctx.sub_key_owner):
        for key in [k for k in tracker if k[:n] == path]:
            tracker[path[:-1] + (new,) + key[n:]] = tracker.pop(key)
    if path[-2:-1] == ("initContainers",):
        holder = ctx.values
        for k in path[:-2]:
            holder = holder.get(k, {})
        order = holder.get("initContainerOrder")
        if isinstance(order, list):
            holder["initContainerOrder"] = [new if o == path[-1] else o for o in order]


def unify_group_keys(entries: list, groups: list, injections: dict,
                     path: tuple = (), maps: "list | None" = None) -> None:
    """Rename, in place, map keys that differ between one release's namespaces
    only by group tokens to their shared template, so common_subtree and
    combine_env_groups see one key. Run before both; the group values each
    namespace needs land in injections[ns]."""
    names = [ns for ns, _ in entries]
    if maps is None:
        maps = [ctx.values for _, ctx in entries]
    if path:
        # Same shape once every token is blanked -> candidate for one key.
        shapes: dict = {}
        for i, (ns, m) in enumerate(zip(names, maps)):
            subs = {k8s_ns(ns): ""} | {t: "" for g in groups for t in g.tokens}
            for k in m:
                if isinstance(k, str):
                    shapes.setdefault(_token_re(subs).sub("", k), {}).setdefault(i, []).append(k)
        for hit in shapes.values():
            if len(hit) != len(maps) or any(len(ks) != 1 for ks in hit.values()):
                continue
            per_ns = {names[i]: ks[0] for i, ks in hit.items()}
            if len(set(per_ns.values())) < 2:
                continue
            res = combine_values(per_ns, groups)
            if res is None:
                continue
            for i, ((_, ctx), m) in enumerate(zip(entries, maps)):
                old = per_ns[names[i]]
                m[res[0]] = m.pop(old)
                _rename_tracked(ctx, path + (old,), res[0])
            for g, m in res[1].items():
                for ns, v in m.items():
                    injections.setdefault(ns, {})[g] = v
    for k in set.intersection(*(set(m) for m in maps)):
        if all(isinstance(m[k], dict) for m in maps):
            unify_group_keys(entries, groups, injections, path + (k,), [m[k] for m in maps])


def combine_env_groups(name: str, entries: list, groups: list, base: dict,
                       interactive: bool = False) -> "tuple[dict, list]":
    """Lift every value that differs between one release's namespaces only by
    group tokens into `base` as a template, dropping the concrete copies from
    each instance. Candidates are string leaves of ctx.values plus, under
    --env-in-values, the env list (it lives in ctx.full_env and never reaches
    ctx.values).

    Returns ({namespace: {group: value}}, [applied combination records])."""
    def candidates(ctx) -> dict:
        out = {p: ("values", v) for p, v in _string_leaves(ctx.values).items()}
        if ctx.full_env:
            out.update({p: ("env", v) for p, v in _string_leaves({"env": ctx.full_env}).items()})
        return out

    per_instance = [(ns, ctx, candidates(ctx)) for ns, ctx in entries]
    common = set.intersection(*(set(c) for _, _, c in per_instance))
    injections: dict = {ns: {} for ns, _ in entries}
    applied = []
    for path in sorted(common):
        kinds = {c[path][0] for _, _, c in per_instance}
        per_ns = {ns: c[path][1] for ns, _, c in per_instance}
        if len(kinds) != 1 or len(set(per_ns.values())) < 2:
            continue
        res = combine_values(per_ns, groups)
        if res is None:
            continue
        template, injection = res
        shown = _PH_RE.sub(r"{{ \1 }}", template.lstrip("\x02"))
        if interactive and sys.stdin.isatty():
            try:
                if input(f"[{name}] {'.'.join(path)}: {per_ns} -> {shown!r}  combine? [Y/n] "
                         ).strip().lower().startswith("n"):
                    continue
            except EOFError:
                pass
        if kinds == {"env"}:
            base["env"] = _leaf_value(template)
            for _, ctx, _ in per_instance:
                ctx.full_env = []
        else:
            set_nested(base, path, _leaf_value(template))
            for _, ctx, _ in per_instance:
                _pop_nested(ctx.values, path)
        for g, m in injection.items():
            for ns, v in m.items():
                injections[ns][g] = v
        applied.append({"release": name, "path": ".".join(path), "template": shown,
                        "injection": injection})
    return injections, applied


def helm_safe_values(data):
    """Make converter output survive the chart's tpl pass unchanged.

    The chart renders every value with `tpl`, so a source string's own '{{'
    (a Prometheus alert, a Go template in a ConfigMap) would be evaluated — or
    fail to parse. Escaping the opening delimiter makes tpl emit it literally;
    env-group placeholders (sentinels until now) become real templates.
    extraDeploy is skipped: the chart tpl's it separately and as_extra_deploy
    already escaped it."""
    def walk(v):
        if isinstance(v, str):
            return _PH_RE.sub(r"{{ \1 }}", v.replace("{{", '{{ "{{" }}'))
        if isinstance(v, dict):
            return {walk(k): walk(x) for k, x in v.items()}
        if isinstance(v, list):
            return [walk(x) for x in v]
        return v
    if not isinstance(data, dict):
        return walk(data)
    return {k: (v if k == "extraDeploy" else walk(v)) for k, v in data.items()}

# Directory/release names must be valid Helm release names & K8s resource
# names (lowercase alphanumeric + '-', no leading underscore) — hence "shared"
# rather than "_shared" on disk, even though the internal registry sentinel
# owner label stays "_shared" (never itself written to a path).
SHARED_DIR_NAME = "shared"
CLUSTER_SHARED_DIR_NAME = "cluster-shared"

# Output layout:
#   <output>/base/<release-slug>.yaml            comprehensive, environment-
#                                                 agnostic — NEVER an image
#                                                 tag. Written ONCE
#                                                 and reused by every
#                                                 namespace that runs this
#                                                 microservice (see the base
#                                                 registry / release_slugs in
#                                                 main()).
#   <output>/<namespace>/values/<slug>.yaml       image tag + what differs +
#                                                 minor per-namespace tweaks
#                                                 ONLY — this is the hand-
#                                                 editable, per-environment
#                                                 file. Its EXISTENCE is what
#                                                 deploys the release: the
#                                                 ms-applicationSet chart's git
#                                                 "files" generator globs this
#                                                 directory, and the filename
#                                                 is what base/<file> resolves
#                                                 against.
#   <output>/<namespace>/defaults.yaml            what is identical across every
#                                                 release in this namespace.
#   <output>/rootApplication.yaml.txt             apply this, once, by hand.
#   <output>/rootApplicationSet.yaml              one Application per namespace
#                                                 directory — the whole wiring.
#
# release-slug is normally just the microservice's own name. It only becomes
# "<namespace>-<microservice>" when two namespaces declare a microservice by
# the same name but with genuinely DIFFERENT base content — see the base
# registry in main(), which mirrors the same claim/rename-on-conflict
# philosophy NamespaceRegistry already applies to individual K8s object names
# (module docstring, "Conflict-free by construction").
#
# The base/ and values/ filenames for one release are always the SAME name,
# which is what keeps the chart's chain self-consistent even when a slug had
# to be disambiguated.
#
# cluster-shared is the one release outside the per-namespace fan-out:
# <output>/cluster-shared/values/, excluded from the root ApplicationSet's
# directory generator, deployed by its own Application at the tree root. It is
# NOT base-shared — it's already global. See
# build_cluster_shared_application for why it cannot take the uniform chain.
#
# <namespace>/defaults.yaml holds the folder's group values plus the labels
# byte-identical across the releases they render on: workload/pod labels
# (WORKLOAD_LABEL_KEYS) across every microservice with a Deployment,
# StatefulSet or DaemonSet; commonLabels (DEFAULTS_KEYS) across EVERY
# release in that namespace, the shared release included — same "each layer
# only has what's different" idea as examples/gitops/STRUCTURE.md's
# _base/defaults.yaml, just computed automatically instead of hand-written.
# It's layered between base/ and values/ (base -> defaults -> values), and it's
# always safe: a release that never had a
# given key is never given it by defaults, since only keys present with an
# IDENTICAL value in every release qualify. It is deliberately NOT subtracted
# out of base/ content, since base/ is shared across namespaces and a delta
# specific to one namespace's release mix may not hold for every other
# namespace that reuses the same base file. There is no global defaults layer
# — see Pass 2 in main() for why adding one back would be unsafe rather than
# merely redundant.
BASE_DIR_NAME = "base"
VALUES_DIR_NAME = "values"
DEFAULTS_FILE_NAME = "defaults.yaml"
# The only computed keys a defaults.yaml carries — see Pass 3 in main().
# commonLabels lands on every object of every release, so it is computed over
# every release there, the shared one included.
DEFAULTS_KEYS = ("commonLabels",)
# The workload's own labels and its pods'. The chart renders these on a
# Deployment/StatefulSet/DaemonSet only, so a release without one (`shared`, a
# CronJob-only release) is not touched by them and does not get a say: a label
# every microservice with a workload carries goes to defaults.yaml even when
# the namespace's shared release has no labels at all.
WORKLOAD_LABEL_KEYS = ("labels", "podLabels")
LABELLED_WORKLOAD_TYPES = frozenset({"deployment", "statefulset", "daemonset"})
# The two files at the tree root that are the ENTIRE ArgoCD wiring. Applying
# ROOT_APP_FILE_NAME by hand once deploys ROOT_APPSET_FILE_NAME, which deploys
# one ms-applicationSet release per namespace directory, which fans out over
# that namespace's values/*.yaml. Nothing else in the tree is a manifest.
#
# The root Application ends in .txt ON PURPOSE: it is the object a human
# applies by hand, and nothing in git should deploy or manage it. ArgoCD's
# directory source and `kubectl apply -f <dir>` both read only .yaml/.yml/.json,
# so the .txt keeps it out of every directory apply; `kubectl apply -f
# rootApplication.yaml.txt` still works because kubectl reads a file named
# explicitly whatever its extension (it filters extensions only while walking a
# directory). Written on every run — the flat tree plus these two files.
ROOT_APP_FILE_NAME = "rootApplication.yaml.txt"
ROOT_APPSET_FILE_NAME = "rootApplicationSet.yaml"
# The names earlier runs used. Reported stale (and removed by --prune): a
# leftover root-application.yaml would still deploy the old ApplicationSet.
LEGACY_ROOT_FILE_NAMES = ("root-application.yaml", "root-applicationSet.yaml")
# Written only under --cluster-scoped, and deployed by the root Application
# alongside the root ApplicationSet rather than through the per-namespace
# fan-out — see build_cluster_shared_application.
CLUSTER_SHARED_APP_FILE_NAME = "cluster-shared-application.yaml"
# Top-level entries under the output tree that are NOT a namespace: skipped by
# verify_no_conflicts, find_stale_output and verify_raw_fidelity alike.
# ERRORS_ANALYSIS is the defect-triage tree (one folder per known conversion
# defect, each with its own test/fixture/ of raw manifests). Those fixtures are
# INPUT to a test, never generated output — left unlisted here, find_stale_output
# reads ERRORS_ANALYSIS as a namespace that vanished from the input and reports
# every fixture under it as a stale generated file.
NON_NAMESPACE_OUTPUT_DIRS = {"report", "ERRORS_ANALYSIS"}

# Variant folders: a namespace folder may hold sub-folders that are themselves
# sets of releases deploying into the SAME namespace — prd/yellow/, prd/black/,
# nested as deep as there are folder groups (prd/yellow/eu/), all sharing the
# one base/. Created by folder --env-group tokens, or already present in the
# input. A folder is keyed by its path ('prd/yellow') everywhere a key or a
# folder is needed; k8s_ns() gives the Kubernetes namespace ('prd').
def k8s_ns(namespace: str) -> str:
    return namespace.split("/", 1)[0]


def folder_suffix(namespace: str) -> str:
    """'prd/yellow/eu' -> 'yellow-eu', '' for a plain namespace folder."""
    return namespace.partition("/")[2].replace("/", "-")


def app_name_suffix(namespace: str) -> str:
    """What ms-applicationSet appends to a release's file name to name its
    Application: the namespace's last '-' token, plus the variant folder
    ('devops-team-dev' -> 'dev', 'prd/yellow' -> 'prd-yellow')."""
    return k8s_ns(namespace).rsplit("-", 1)[-1] + (f"-{folder_suffix(namespace)}" if "/" in namespace else "")


def is_input_ns(d: Path) -> bool:
    return any(d.glob("*.yaml"))


def is_output_ns(d: Path) -> bool:
    return (d / VALUES_DIR_NAME).is_dir() or (d / DEFAULTS_FILE_NAME).is_file()


def namespace_dirs_of(root: Path, is_ns=is_output_ns) -> list[tuple[str, Path]]:
    """(key, directory) for every namespace folder under `root`, variant
    sub-folders included: each top-level folder is a namespace, and any folder
    below it that `is_ns` accepts is a variant. values/ is never descended."""
    out = []
    def walk(d: Path, top: bool) -> None:
        if top or is_ns(d):
            out.append((d.relative_to(root).as_posix(), d))
        for c in sorted(d.iterdir()):
            if c.is_dir() and c.name != VALUES_DIR_NAME:
                walk(c, False)
    for top in sorted(p for p in root.iterdir() if p.is_dir()
                      and p.name not in (CLUSTER_SHARED_DIR_NAME, BASE_DIR_NAME)
                      and p.name not in NON_NAMESPACE_OUTPUT_DIRS):
        walk(top, True)
    return out


def values_files(values_dir: Path) -> list[Path]:
    """Every release file under values/, sub-folders included: they are only
    for grouping (values/group1/ms1.yaml), the release is still the file name."""
    return sorted(values_dir.rglob("*.yaml")) if values_dir.is_dir() else []


def base_file_for(base_dir: Path, values_dir: Path, vf: Path) -> Path:
    """A release's base file: base/ mirrors its sub-folder under values/
    (values/group1/ms1.yaml -> base/group1/ms1.yaml), as the chart reads it."""
    return base_dir / vf.relative_to(values_dir)

VOLATILE_ANNOTATION_PREFIXES = (
    "kubectl.kubernetes.io/last-applied-configuration",
    "kubernetes.io/created-by",
    "openshift.io/generated-by",
    "openshift.io/deployment.phase",
    "image.openshift.io/triggers",
    "openshift.io/host.generated",
    # A server-maintained rollout counter, meaningless on the target cluster and
    # different on every source. Carried through it produces a permanent
    # non-empty ArgoCD diff, which trains operators to ignore drift.
    "deployment.kubernetes.io/revision",
    # The SOURCE ArgoCD Application's ownership stamp. Carried through, the
    # object claims to belong to an Application that is not the one deploying
    # it now; ArgoCD writes its own on apply.
    "argocd.argoproj.io/tracking-id",
)

VOLATILE_METADATA_KEYS = (
    "creationTimestamp", "resourceVersion", "uid", "generation",
    "selfLink", "managedFields", "namespace", "ownerReferences",
    "generateName", "finalizers",
)


# ---------------------------------------------------------------------------
# YAML I/O helpers
# ---------------------------------------------------------------------------

def load_multidoc(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8")
    try:
        return [d for d in yaml.safe_load_all(text) if d]
    except yaml.YAMLError:
        # A trailing TAB after a plain scalar: legal to Go's YAML (Helm renders
        # it), fatal to PyYAML. See namespace_importer.parse_yaml_stream.
        return [d for d in yaml.safe_load_all(re.sub(r"[ \t]+$", "", text, flags=re.M)) if d]


# Keys pinned to the top of a generated values file, in this order, ahead of
# the alphabetically-sorted remainder. `workload` decides what the release
# fundamentally IS (deployment / statefulset / daemonset / none), so reading it
# first is what makes the rest of the file make sense — and sorted purely
# alphabetically it lands at the very BOTTOM, under every incidental key.
#
# The rest of the list runs by importance — what it runs, how many, then its
# container, networking, config, scaling, and its names/stamps near the end —
# and mirrors devops-portal's src/client/modules/argocd/catalog.ts (FEATURES
# registration order), so a tree converted here and one written by the portal
# lay a file out the same way. Change both together.
TOP_LEVEL_KEY_ORDER = [
    "workload", "image", "imagePullSecrets", "replicaCount", "revisionHistoryLimit",
    "progressDeadlineSeconds", "minReadySeconds", "podManagementPolicy", "serviceName", "strategy",
    "containerName", "command", "args", "workingDir", "restartPolicy", "terminationGracePeriodSeconds",
    "containerRestartPolicy", "terminationMessagePath", "terminationMessagePolicy",
    "env", "envFrom", "envFromOrder", "ports", "portOrder", "resources", "livenessProbe", "readinessProbe", "startupProbe",
    "containerRestartRules", "lifecycle",
    "volumes", "volumeOrder", "volumeMounts", "pvc", "volumeClaimTemplates", "persistentVolumeClaimRetentionPolicy",
    "persistentVolumes", "storageClasses",
    "service", "services", "ingress", "route", "routes", "networkPolicies",
    "configMaps", "secrets", "secretStores", "clusterSecretStores", "externalSecretsApiVersion",
    "externalSecrets", "checksums", "cronjobs", "jobs", "hpa", "vpa", "pdb",
    "nodeSelector", "tolerations", "topologySpreadConstraints", "priorityClassName", "runtimeClassName",
    "schedulerName", "hostNetwork", "affinity", "podSecurityContext", "securityContext", "scc", "dnsPolicy", "dnsConfig",
    "hostAliases", "automountServiceAccountToken", "serviceAccount", "rbac", "serviceMonitor", "extraDeploy",
    "nameOverride", "fullnameOverride", "commonLabels", "commonAnnotations", "chartLabels",
    "selectorLabels", "podAnnotations", "podLabels", "labels", "annotations", "sidecars", "sidecarOrder", "initContainers", "initContainerOrder",
]


def _sorted_deep(obj: Any) -> Any:
    """Recursively sort dict keys, so dumping with sort_keys=False (needed to
    control the top-level order) still produces byte-stable output everywhere
    below the top level."""
    if isinstance(obj, dict):
        return {k: _sorted_deep(obj[k]) for k in sorted(obj)}
    if isinstance(obj, list):
        # A list entry's `name` first (`- name: X`), as Kubernetes writes env
        # vars and volume mounts; the rest stay sorted.
        return [{"name": d.pop("name"), **d} if isinstance(d, dict) and "name" in d else d
                for d in (_sorted_deep(i) for i in obj)]
    return obj


# A string that PyYAML emits BARE but Go's yaml.v3 reads back as a NUMBER.
#
# PyYAML resolves scalars with YAML **1.1** rules, which require a decimal point
# before an exponent: `1e+09` is therefore not a float to PyYAML, so its emitter
# sees no reason to quote it. Helm parses values files with Go's yaml.v3, which
# uses the YAML **1.2** core schema, where `1e+09` IS a float. The string comes
# out of the raw manifest as a string, crosses the values file as a bare scalar,
# and arrives in the template as the float 1e9 — which Helm then renders as
# `1000000000`.
#
# That is not the cosmetic difference the report calls it. `ConfigMap.data` is
# `map[string]string`, `EnvVar.value` is a string, and a resource quantity is a
# string-typed `Quantity`: an int in any of them is rejected by the API server's
# decoder. The report's own escape clause is the accurate description of what
# was happening — "if the pipeline is parsing quantity strings as numbers
# anywhere, that is a real bug of the same family and far more damaging".
#
# Everything PyYAML and Go agree is numeric (`0.10`, `1.20`, big integers) was
# already quoted by PyYAML's emitter and never had this problem. This pattern is
# exactly the 1.1/1.2 gap, and nothing else:
#   1e+09  1E9  -1.5e-3  +2E+10       exponent form, with or without a point
#   0o17                              YAML 1.2 octal; `017` is 1.1 octal and is
#                                     already quoted by PyYAML
YAML12_ONLY_NUMERIC_RE = re.compile(
    r"""^[-+]?(?:
          (?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)[eE][-+]?[0-9]+   # exponent form
        | 0o[0-7]+                                          # 1.2 octal
        )$""", re.VERBOSE)


# ...and the YAML 1.1 side of the same gap. `helm template` (v3.16, checked
# by dumping .Values) reads values files the way go-yaml v2 does: `y`/`n`/`Y`/`N`
# are booleans, `08` is the number 8, `0x1F`/`0o17`/`1_000`/`+12` are integers
# and `.inf`/`.nan` cannot be rendered at all. PyYAML treats several of those
# (`y`, `n`, `08`) as plain strings and emits them bare, so an env value `"y"`
# reached the chart as `true` and the API server refused the Deployment.
_GO_BOOL_RE = re.compile(r"^(?:[yYnN]|yes|Yes|YES|no|No|NO|on|On|ON|off|Off|OFF|true|True|TRUE|false|False|FALSE)$")
_GO_NULL_RE = re.compile(r"^(?:~|null|Null|NULL|)$")
_GO_SPECIAL_FLOAT_RE = re.compile(r"^[-+]?\.(?:inf|Inf|INF)$|^\.(?:nan|NaN|NAN)$")
_GO_FLOAT_RE = re.compile(r"^[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)(?:[eE][-+]?[0-9]+)?$")
_GO_INT_RE = re.compile(r"^[-+]?(?:0[xX][0-9a-fA-F_]+|0[oO][0-7_]+|0[bB][01_]+|[0-9][0-9_]*)$")


def go_yaml_retypes(value: str) -> bool:
    """Whether go-yaml v2 (Helm's reader) would read this string, written
    bare, as something other than a string."""
    return bool(_GO_NULL_RE.match(value) or _GO_BOOL_RE.match(value) or _GO_SPECIAL_FLOAT_RE.match(value)
                or _GO_INT_RE.match(value) or _GO_FLOAT_RE.match(value))


def _represent_str_quoting_go_numerics(dumper, value):
    """Quote a string only when leaving it bare would change its TYPE in Go.

    Scoped as tightly as the evidence: PyYAML's own quoting is left entirely
    alone, and only the strings it would emit bare while Go reads them as
    numbers are forced into quotes. Broadening this to "quote every string"
    would work too and would be much worse — every script, every certificate and
    every `grafana.ini` in the corpus would lose its literal block and turn into
    one long quoted line.
    """
    if YAML12_ONLY_NUMERIC_RE.match(value) or go_yaml_retypes(value):
        style = "'"
    elif "\n" in value and not value[:1].isspace():
        # Multi-line strings (scripts, certs, grafana.ini, extraDeploy entries)
        # as a literal `|` block rather than one long \n-escaped quoted line.
        # A leading space would need an indentation indicator, so those are
        # left to the default. PyYAML itself degrades to double quotes when a
        # literal block cannot represent the value (e.g. trailing spaces).
        style = "|"
    else:
        style = None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


HELM_RELEASE_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$")


class _HelmOutputLoader(yaml.SafeLoader):
    """SafeLoader for `helm template` output. Go's YAML writer emits a string
    `=` or `<<` bare (they are plain strings to Go), and PyYAML's 1.1 resolver
    tags those `!!value` / `!!merge`, which SafeLoader cannot construct as a
    value — the verifier crashed on a perfectly valid env var `value: =`."""


for _tag in ("tag:yaml.org,2002:value", "tag:yaml.org,2002:merge"):
    _HelmOutputLoader.add_constructor(_tag, lambda loader, node: loader.construct_scalar(node))


class _ValuesDumper(_ForcedBlockEmitterMixin, yaml.SafeDumper):
    """SafeDumper plus the one representer above. Subclassed rather than
    registered on SafeDumper itself so importing this module cannot change YAML
    output for anything else in the process. The mixin keeps the literal block
    for a multi-line string with trailing spaces (see yaml_style)."""


_ValuesDumper.add_representer(str, _represent_str_quoting_go_numerics)


def dump_yaml(data: Any, spaced: bool = False) -> str:
    """Emit values YAML.

    `allow_unicode=True` is the second half of the encoding fix (the first is
    encoding="utf-8" on the helm subprocess). Without it PyYAML escapes every
    non-ASCII character as `\\uXXXX` and, because a string containing an escape
    cannot be a literal block, collapses the whole value into one long
    double-quoted scalar. Both round-trip correctly — Go's yaml.v3 reads the
    escapes back — so this is not corruption, but these files are described a
    few lines below as "the hand-editable, per-environment file", and a shell
    script that arrives as
    `"#!/bin/sh\\n# Maintainer: \\u013Dubo\\u0161 Nov\\xE1k \\u2014 ..."`
    is not hand-editable in any useful sense. With it, the script stays a
    literal block and the name stays a name.
    """
    if not data:
        return "{}\n"
    if not isinstance(data, dict):
        return yaml.dump(data, Dumper=_ValuesDumper, sort_keys=True,
                         default_flow_style=False, width=100, allow_unicode=True)
    keys = [k for k in TOP_LEVEL_KEY_ORDER if k in data]
    keys += sorted(k for k in data if k not in TOP_LEVEL_KEY_ORDER)
    chunks = [yaml.dump({k: _sorted_deep(data[k])}, Dumper=_ValuesDumper, sort_keys=False,
                        default_flow_style=False, width=100, allow_unicode=True)
              for k in keys]
    # A values file gets a blank line between top-level keys, so each section
    # reads as its own block; manifests (extraDeploy, Argo apps) stay tight.
    return ("\n" if spaced else "").join(chunks)


# ---------------------------------------------------------------------------
# Sanitization
# ---------------------------------------------------------------------------

def clean_annotations(ann: Optional[dict]) -> dict:
    if not ann:
        return {}
    return {k: v for k, v in ann.items() if not k.startswith(VOLATILE_ANNOTATION_PREFIXES)}


def filter_transient(docs: list[dict], warnings: Optional[list[str]] = None) -> list[dict]:
    """Drop controller-owned / generated objects that are not desired state,
    and the DROPPED_KINDS (logged to `warnings` when given — the fidelity
    check's raw index passes none, it only needs them gone)."""
    out = []
    for d in docs:
        kind = d.get("kind")
        if kind in TRANSIENT_KINDS:
            continue
        if kind in DROPPED_KINDS:
            if warnings is not None:
                warnings.append(f"{kind}/{(d.get('metadata') or {}).get('name')}: dropped — "
                                f"{kind} is deliberately not converted (no values, no extraDeploy)")
            continue
        if kind == "Job":
            owners = (d.get("metadata") or {}).get("ownerReferences") or []
            if any(o.get("kind") == "CronJob" for o in owners):
                continue  # a CronJob-spawned Job instance, not standalone desired state
        out.append(d)
    return out


# Invisible characters that a copy-paste or a rich-text editor can leave inside
# a manifest. They survive YAML parsing as ordinary content, so `Se<ZWNJ>cretStore`
# is a different string from `SecretStore` to every lookup that matters, while
# looking identical in a terminal, a diff and a code review.
ZERO_WIDTH_CHARS = (
    "​",  # ZERO WIDTH SPACE
    "‌",  # ZERO WIDTH NON-JOINER   <- the one in the report
    "‍",  # ZERO WIDTH JOINER
    "⁠",  # WORD JOINER
    "﻿",  # ZERO WIDTH NO-BREAK SPACE / BOM
    "­",  # SOFT HYPHEN
)


def strip_zero_width(value):
    """Remove invisible characters from an identifier. Non-strings pass through."""
    if not isinstance(value, str):
        return value
    for ch in ZERO_WIDTH_CHARS:
        value = value.replace(ch, "")
    return value


# Keys whose VALUE is an identifier — a name, a kind, or a reference to one.
# A zero-width character in any of these breaks a lookup while looking correct;
# in anything else it may be legitimate payload. Broader than the three fields
# originally covered, because error.txt is right that "only secretStoreRef.kind
# surfaced because that is where a mismatch happened to be detectable — the same
# paste could have landed anywhere".
IDENTIFIER_KEYS = frozenset({
    "name", "kind", "secretName", "serviceAccountName", "serviceAccount",
    "claimName", "storageClassName", "className", "ingressClassName",
    "priorityClassName", "runtimeClassName", "schedulerName", "eventSourceName",
})

# ...except where the key name is reused for something that is NOT an identifier.
# `data.name` in a ConfigMap is content; a container's `env[].name` is a variable
# name, which Kubernetes does permit to be odd. Keyed on the PARENT path segment
# so the rule stays readable.
NON_IDENTIFIER_PARENTS = frozenset({"data", "stringData", "labels", "annotations",
                                    "matchLabels", "selector", "env"})


def find_zero_width(doc: Any, path: str = "") -> list[tuple[str, str]]:
    """Every string in `doc` containing an invisible character, as (path, chars).

    Scans the WHOLE document, including ConfigMap/Secret data, because the
    question this answers is "is the source of truth clean?" — not "what may I
    safely rewrite?". Those are different questions with different answers, and
    conflating them is how a defensive strip turns into silent corruption of
    payload. Nothing here mutates anything.
    """
    out: list[tuple[str, str]] = []
    if isinstance(doc, str):
        hits = [c for c in doc if c in ZERO_WIDTH_CHARS]
        if hits:
            out.append((path or "<root>", ", ".join(sorted({"U+%04X" % ord(c) for c in hits}))))
    elif isinstance(doc, dict):
        for k, v in doc.items():
            out += find_zero_width(v, f"{path}.{k}" if path else str(k))
    elif isinstance(doc, list):
        for i, v in enumerate(doc):
            out += find_zero_width(v, f"{path}[{i}]")
    return out


def _strip_identifiers_deep(node: Any, notes: list, kind: str, path: str = "",
                            parent_key: str = "") -> None:
    """Strip zero-width characters from identifier-valued keys, anywhere."""
    if isinstance(node, dict):
        for k, v in list(node.items()):
            child = f"{path}.{k}" if path else k
            if (isinstance(v, str) and k in IDENTIFIER_KEYS
                    and parent_key not in NON_IDENTIFIER_PARENTS):
                cleaned = strip_zero_width(v)
                if cleaned != v:
                    node[k] = cleaned
                    notes.append(
                        f"{kind}: {child} contained "
                        f"{len(v) - len(cleaned)} invisible character(s) "
                        f"({', '.join('U+%04X' % ord(c) for c in v if c in ZERO_WIDTH_CHARS)}) "
                        f"- stripped. Fix the raw manifest at source; until then any "
                        f"lookup by this name would silently fail.")
            else:
                _strip_identifiers_deep(v, notes, kind, child, k)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _strip_identifiers_deep(v, notes, kind, f"{path}[{i}]", parent_key)


def _sanitize_identifiers(doc: dict, notes: list | None = None) -> None:
    """Strip zero-width characters from the fields that are IDENTIFIERS — the
    object's own name, and the references that have to resolve to another
    object's name or kind.

    Deliberately narrow. A blanket strip over the whole document would also
    rewrite ConfigMap/Secret *data*, where a zero-width character can be
    legitimate content (a test fixture, a template, a translation string), and
    silently corrupting payload to tidy up a name is a worse bug than the one
    being fixed. Identifiers have no such ambiguity: no Kubernetes name or kind
    may contain one, so removing it can only ever repair.

    Every removal is recorded in `notes` so it appears in
    conflicts_and_warnings.txt rather than happening invisibly — the raw
    manifest is still wrong and somebody should fix it at source.
    """
    meta = doc.get("metadata") or {}
    for holder, key, label in (
        (meta, "name", "metadata.name"),
        ((doc.get("spec") or {}).get("secretStoreRef") or {}, "name",
         "spec.secretStoreRef.name"),
        ((doc.get("spec") or {}).get("secretStoreRef") or {}, "kind",
         "spec.secretStoreRef.kind"),
    ):
        original = holder.get(key)
        cleaned = strip_zero_width(original)
        if isinstance(original, str) and cleaned != original:
            holder[key] = cleaned
            if notes is not None:
                # The OBJECT's name, not the cleaned value. Using `cleaned` here
                # printed "ExternalSecret/SecretStore" for a kind-field repair —
                # which reads as an object name and names the wrong thing, in a
                # warning whose entire purpose is to point at a specific line.
                notes.append(
                    f"{doc.get('kind')}/{meta.get('name')}: {label} contained "
                    f"{len(original) - len(cleaned)} invisible character(s) "
                    f"({', '.join('U+%04X' % ord(c) for c in original if c in ZERO_WIDTH_CHARS)}) "
                    f"- stripped. Fix the raw manifest at source; until then any "
                    f"lookup by this name would silently fail."
                )

    # The three fields above are the ones the report actually observed. They are
    # not the ones a stray paste can reach: error.txt asks to "search wider than
    # this one field", and an identifier anywhere in the document has the same
    # failure mode — invisible, and fatal to any lookup.
    if notes is not None:
        _strip_identifiers_deep(doc, notes, doc.get("kind", "?"))

        # Report what is NOT stripped, separately and without touching it.
        # ConfigMap/Secret data may legitimately contain a zero-width character
        # — a test fixture, a translation string, a template — so rewriting it
        # to tidy up a name would be a worse bug than the one being fixed. But
        # the report's complaint is that "the chart's auto-stripping works but
        # is a workaround, not a fix", and silence is what makes it a workaround.
        # Surfacing it on every run is what turns the strip into a stopgap
        # somebody can actually close.
        for where, chars in find_zero_width(doc):
            if where.startswith(("data.", "stringData.")):
                notes.append(
                    f"{doc.get('kind')}/{(doc.get('metadata') or {}).get('name')}: {where} "
                    f"contains invisible character(s) ({chars}) in CONTENT, not in an "
                    f"identifier - deliberately NOT stripped, since payload may legitimately "
                    f"contain them. Confirm it is intentional; if it is a stray paste, fix "
                    f"the raw manifest at source.")


def _pod_template_metadatas(kind: str, spec: Any) -> list[dict]:
    """Every pod-template `metadata` block an object of this kind can hold.

    One for a Deployment/StatefulSet/DaemonSet/Job at `spec.template`, one for a
    CronJob at `spec.jobTemplate.spec.template`. Returns the live dicts, so a
    caller edits the document in place.

    A named seam rather than an inline expression because three different rules
    need the same set and getting the CronJob nesting wrong is silent — the code
    still runs, it just never reaches the field.
    """
    if not isinstance(spec, dict):
        return []
    out = []
    if kind in WORKLOAD_KINDS or kind == "Job":
        meta = (spec.get("template") or {}).get("metadata")
        if isinstance(meta, dict):
            out.append(meta)
    elif kind == "CronJob":
        job_spec = (spec.get("jobTemplate") or {}).get("spec") or {}
        meta = (job_spec.get("template") or {}).get("metadata")
        if isinstance(meta, dict):
            out.append(meta)
    return out


def sanitize_object(doc: dict, notes: list | None = None) -> dict:
    """Strip OpenShift/Kubernetes runtime & cluster-specific noise."""
    doc = copy.deepcopy(doc)
    doc.pop("status", None)
    _sanitize_identifiers(doc, notes)

    meta = doc.get("metadata", {}) or {}
    for key in VOLATILE_METADATA_KEYS:
        meta.pop(key, None)
    ann = clean_annotations(meta.get("annotations"))
    if ann:
        meta["annotations"] = ann
    else:
        meta.pop("annotations", None)
    doc["metadata"] = meta

    kind = doc.get("kind")
    spec = doc.get("spec")
    if kind == "Service" and isinstance(spec, dict):
        if spec.get("clusterIP") != "None":
            spec.pop("clusterIP", None)
        for k in ("clusterIPs", "ipFamilies", "ipFamilyPolicy", "internalTrafficPolicy"):
            spec.pop(k, None)
    if kind == "Route" and isinstance(spec, dict):
        spec.pop("host", None) if spec.get("host", "").endswith(".apps.cluster-generated.local") else None

    # A pod TEMPLATE's metadata is embedded too, and the strip above only reaches
    # the outer document — so `name`, `namespace` and `creationTimestamp: null`
    # arrive inside spec.template.metadata on anything dumped from a live
    # cluster or written by an operator.
    #
    # All four are inert, and for four different reasons:
    #   name       Kubernetes IGNORES it entirely. Pod names come from the
    #              controller's name plus a hash; a name on the template has no
    #              effect whatsoever.
    #   namespace  inherited from the object's own namespace. Setting it is
    #              redundant, and setting it to a DIFFERENT namespace is invalid.
    #   creationTimestamp  server-populated, same as the outer one.
    #   ownerReferences  the outer object's have always been stripped
    #              (VOLATILE_METADATA_KEYS); the pod template's were not, and
    #              they are the more dangerous half. An ownerReference IS a
    #              `uid`, and a uid from the SOURCE cluster does not exist in the
    #              target — but pod template metadata is stamped onto every pod
    #              the controller creates, so carrying it gives each new pod an
    #              owner that is already gone and the garbage collector deletes
    #              the pods about as fast as the controller makes them. The
    #              report rates this benign "(operator re-creates them)", which
    #              is a dependency on the operator being deployed rather than a
    #              property of the field. The verdict happens to be right, but
    #              not for that reason: even with no operator anywhere, carrying
    #              the source cluster's uid is the outcome to avoid.
    #
    # Deliberately still an explicit list rather than VOLATILE_METADATA_KEYS.
    # Every key here has a reason written against POD TEMPLATES specifically;
    # the rest of that tuple (resourceVersion, uid, generation, selfLink,
    # managedFields, finalizers) has never been observed on a pod template and
    # widening a rule past its evidence is what folder 17 exists to warn about.
    #
    # Removed rather than suppressed in the checker, matching how the outer
    # object and the volumeClaimTemplates below are handled: one rule, at the
    # boundary, so the junk never reaches the generated values either.
    # Batch kinds carry pod templates too, one level deeper for a CronJob. The
    # rule was scoped to WORKLOAD_KINDS while only Deployments had been observed
    # losing these fields; a Job's pod template is the same PodTemplateSpec with
    # the same inert keys, and an operator-written CronJob arrives with the same
    # `uid` in the same place. Widened here rather than waiting for a second
    # report to say so.
    for pod_meta in _pod_template_metadatas(kind, spec):
        for key in ("name", "namespace", "creationTimestamp", "ownerReferences"):
            pod_meta.pop(key, None)

    # A StatefulSet's volumeClaimTemplates are EMBEDDED objects, and the runtime
    # noise stripped above only reaches the outer document — so a dump scraped
    # from a live cluster arrives with `status: {phase: Pending}`, `kind`,
    # `apiVersion` and `creationTimestamp: null` sitting inside each template.
    # None of that belongs in a chart: status is server-populated, and kind /
    # apiVersion on an embedded PVC template are redundant.
    #
    # Removed here rather than suppressed in _fidelity_ignored_path, for two
    # reasons. It is the same treatment the outer object already gets four lines
    # up, so the rule stays in one place; and a checker-side suppression would
    # have to name the whole volumeClaimTemplates subtree, which is exactly the
    # blunt instrument that would bury the LABELS defect sitting in the same
    # bucket (see handle_workload — the labels are a real loss and are carried).
    if kind == "StatefulSet" and isinstance(spec, dict):
        for vct in (spec.get("volumeClaimTemplates") or []):
            if not isinstance(vct, dict):
                continue
            vct.pop("status", None)
            vct.pop("kind", None)
            vct.pop("apiVersion", None)
            vct_meta = vct.get("metadata")
            if isinstance(vct_meta, dict):
                for key in VOLATILE_METADATA_KEYS:
                    vct_meta.pop(key, None)

    return doc


def is_auto_sa_secret(doc: dict) -> bool:
    if doc.get("type") in ("kubernetes.io/service-account-token", "kubernetes.io/dockercfg"):
        return True
    name = (doc.get("metadata") or {}).get("name", "")
    return bool(AUTO_SA_SECRET_RE.match(name))


def content_hash(doc: dict) -> str:
    """Identity hash ignoring cosmetic metadata (labels/annotations)."""
    d = copy.deepcopy(doc)
    meta = d.get("metadata", {})
    meta.pop("labels", None)
    meta.pop("annotations", None)
    return hashlib.sha256(json.dumps(d, sort_keys=True, default=str).encode()).hexdigest()[:16]


def run_fingerprint(input_dir: Path) -> tuple[str, str, str]:
    """Identify a converter RUN as (converter_hash, input_hash, run_id).

    Two fidelity reports may only be compared to each other when their run_ids
    match. This exists because they were not: a hand-maintained error summary
    and a generated fidelity_report.txt were used interchangeably to set
    priorities while describing different runs, and eight error types the
    summary rated significant — including its own #1 HIGH and #1 MEDIUM — did
    not appear in the report at all. Nothing in either document said which run
    it came from, so the contradiction was invisible and every count in every
    downstream analysis inherited it.

    The id is deliberately content-derived rather than a timestamp or a
    sequence number. A timestamp distinguishes two runs that are identical and
    fails to distinguish the case that actually matters: the same input read by
    a converter that changed underneath it. Hashing the converter's own source
    plus every input manifest means the id moves if and only if a count could
    legitimately move, so an unchanged id is a real guarantee that two reports
    are about the same thing.
    """
    converter_h = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:12]
    h = hashlib.sha256()
    # Sorted relative paths, so the id does not depend on filesystem ordering,
    # and the path is hashed alongside the bytes — a manifest that MOVED is a
    # different input even when its content is untouched.
    for p in sorted(input_dir.rglob("*.yaml")):
        h.update(str(p.relative_to(input_dir)).replace("\\", "/").encode())
        h.update(p.read_bytes())
    input_h = h.hexdigest()[:12]
    return converter_h, input_h, f"{converter_h}-{input_h}"


def split_image(image: str) -> tuple[str, str, str]:
    """Split a container image reference into (repository, tag, digest).

    A digest pin (`repo@sha256:...`) is the STRONGEST form of image reference —
    it names exactly one immutable blob. It used to be discarded here, returning
    an empty tag, and the chart's image helper then fell through to
    `repository:{{ .Chart.AppVersion }}`. So an immutable pin silently became the
    mutable tag `:2.0.0`, and the workload could deploy an entirely different
    binary from the one the manifest specified. That compounds with any
    imagePullPolicy downgrade: pinned-by-digest becomes pinned-to-a-moving-tag
    AND stops re-pulling.

    Returns three parts rather than folding the digest into the repository so
    the digest can be routed like a tag — into the per-namespace minimal values
    rather than the shared base (see build_minimal_values). The chart renders it
    from `.Values.image.digest` (universal-chart.image in templates/_helpers.tpl).

    The `/` check on the tag candidate is load-bearing: it keeps a registry HOST
    WITH A PORT (`registry:5000/ns/app`) from being read as a tag.
    """
    if not image:
        return "", "", ""
    if "@" in image:
        repo, _, digest = image.partition("@")
        return repo, "", digest
    if ":" in image:
        repo, _, maybe_tag = image.rpartition(":")
        if "/" not in maybe_tag:
            return repo, maybe_tag, ""
    return image, "", ""


# ---------------------------------------------------------------------------
# Name registries — this is the conflict-free core of the converter
# ---------------------------------------------------------------------------

class NamespaceRegistry:
    """Dedupes raw-named (release-unprefixed) resources within one namespace."""

    def __init__(self, namespace: str):
        self.namespace = namespace
        self.claims: dict[tuple[str, str], dict] = {}
        # Every collision rename this registry performed, as structured data
        # rather than only as prose in conflicts_and_warnings.txt.
        #
        # The name-fidelity check compares raw names against rendered names and
        # calls anything with no raw counterpart INVENTED — a hard FAIL, on the
        # reasoning that "anything that referenced them by name will break". A
        # collision rename produces exactly that shape, deliberately: two
        # microservices declared different objects under one name and the second
        # had to move. Without a record, every legitimate rename leaves the gate
        # permanently red, and a gate that always fails is one nobody reads.
        self.renames: list[dict] = []
        # Identical duplicates lifted into the shared release before any build
        # (promote_shared_candidates) — written to renames.json alongside renames.
        self.promotions: list[dict] = []

    def claim(self, kind: str, name: str, content_h: str, owner: str):
        """Returns (final_name, action, note). action in own|reference_shared|renamed."""
        key = (kind, name)
        existing = self.claims.get(key)

        if existing is None:
            self.claims[key] = {"owner": owner, "hash": content_h}
            return name, "own", None

        if existing["owner"] == owner:
            return name, "own", None

        if existing["owner"] == "_shared":
            return name, "reference_shared", (
                f"{kind}/{name}: already owned by the namespace-shared release; "
                f"'{owner}' dropped its duplicate declaration and now references the shared resource"
            )

        if owner == "_shared":
            self.claims[key] = {"owner": "_shared", "hash": content_h}
            return name, "own", (
                f"{kind}/{name}: promoted to namespace-shared even though microservice "
                f"'{existing['owner']}' already privately declared it — verify '{existing['owner']}' "
                f"is updated to reference the shared copy instead of owning its own"
            )

        # IDEMPOTENCE. `f"{owner}-{name}"` unconditionally produced
        # `interconn-kafka-kafbat-interconn-kafka-kafbat` whenever a
        # microservice declared an object under its own name — a prefix applied
        # to a name that already carried it. The result is correct (it is
        # unique, and every reference follows it) but it is twice as long as it
        # needs to be, reads as a bug to everyone who sees it, and pushes
        # against the 63-character name limit that validate_resource_names
        # enforces later.
        #
        # So the prefix is added only when it actually distinguishes anything.
        # When the name already begins with the owner, prefixing adds length
        # and no information, and a numeric suffix is both shorter and honest
        # about what it is: a disambiguation, not a namespace.
        #
        # The loop is what keeps this SAFE. Skipping the prefix could otherwise
        # hand back a name that is already claimed — the exact collision this
        # function exists to resolve — so the chosen base is walked until it is
        # actually free. The ordinary case (`owner` unrelated to `name`)
        # produces the same result as before, so this renames nothing that was
        # already sensibly named.
        base = name if (name == owner or name.startswith(f"{owner}-")) else f"{owner}-{name}"
        new_name = base
        n = 2
        while (kind, new_name) in self.claims:
            new_name = f"{base}-{n}"
            n += 1
        same = existing["hash"] == content_h
        self.claims[(kind, new_name)] = {"owner": owner, "hash": content_h}
        # A same-hash collision reaching here is now DEGENERATE: a namespace-
        # scoped name is unique within its namespace, so identical declarations
        # are one source object, and promote_shared_candidates has already lifted
        # them into the shared release before any build. What still arrives is
        # a set promotion deliberately skipped (a Service/Route, or a name the
        # raw shared file already owns). The identical branch survives as a
        # safety net, with advice rather than a silent merge. Differing content
        # has no choice — the rename is the only correct outcome.
        if same:
            advice = (
                f" -> renamed '{owner}' copy to '{new_name}'. The two copies are BYTE-IDENTICAL, "
                f"so this is probably one shared object in the source cluster, split into two. "
                f"To share it instead: move {kind}/{name} into this namespace's shared.yaml and "
                f"delete it from both microservice files; every referring release will then point "
                f"at the one copy. Left as a rename because nothing in the raw manifests can prove "
                f"they were shared rather than coincidentally equal."
            )
        else:
            advice = (
                f" -> renamed '{owner}' copy to '{new_name}'. Content DIFFERS, so these are two "
                f"different objects that collided on a name; the rename is correct and no action "
                f"is needed beyond checking that nothing OUTSIDE these releases referred to "
                f"{kind}/{name} by its original name."
            )
        self.renames.append({"namespace": self.namespace, "kind": kind,
                             "from": name, "to": new_name, "owner": owner,
                             "identical_content": bool(same)})
        return new_name, "renamed", (
            f"{kind}/{name}: name collision between microservices '{existing['owner']}' and "
            f"'{owner}' in namespace '{self.namespace}' "
            f"({'identical' if same else 'DIFFERING'} content){advice}"
        )


# Kinds promotion skips: the shared release renders its PRIMARY Service with the
# shared release's own selector, so a promoted Service (and the Route in front of
# it) would stop matching the declarers' pods.
_NEVER_PROMOTE_KINDS = frozenset({"Service", "Route"})


def promote_shared_candidates(namespace: str, ms_docs: list[tuple[str, list[dict]]],
                              shared_docs: list[dict], ns_registry: NamespaceRegistry) -> list[dict]:
    """Lift objects several microservices declare IDENTICALLY into the shared release.

    Sound because a namespace-scoped name is unique within a namespace: byte-
    identical declarations of one (kind, name) in several microservice files are
    views of ONE source object that the dump captured once per consumer. Without
    this the second copy was renamed '<owner>-<name>', splitting one object in two.

    The promoted copy is appended to the shared docs under its ORIGINAL name.
    Declarers keep their docs: the shared release is built first, so
    NamespaceRegistry.claim hands each of them 'reference_shared' and they drop
    their private copy and reference the shared one — no reference rewrite.

    Deliberately NOT promoted (they take the existing rename path): names the raw
    shared file already declares, sets whose content differs, mixed match/differ
    sets, auto-generated SA secrets, and _NEVER_PROMOTE_KINDS.
    Returns the shared doc list, with promotions recorded on ns_registry.
    """
    in_shared = {(d.get("kind"), (d.get("metadata") or {}).get("name")) for d in shared_docs}
    declarers: dict[tuple[str, str], dict[str, dict]] = defaultdict(dict)
    for ms_name, docs in ms_docs:
        for d in docs:
            kind, name = d.get("kind"), (d.get("metadata") or {}).get("name")
            if kind not in NS_RAW_NAME_KINDS or kind in _NEVER_PROMOTE_KINDS or not name:
                continue
            if (kind, name) in in_shared or (kind == "Secret" and is_auto_sa_secret(d)):
                continue
            declarers[(kind, name)].setdefault(ms_name, d)

    out = list(shared_docs)
    for (kind, name), by_ms in sorted(declarers.items()):
        if len(by_ms) < 2 or len({content_hash(d) for d in by_ms.values()}) != 1:
            continue
        out.append(copy.deepcopy(next(iter(by_ms.values()))))
        ns_registry.promotions.append({
            "namespace": namespace, "kind": kind, "from": name, "to": name,
            "owner": "_shared", "promotion": True, "promoted_from": sorted(by_ms),
        })
    return out


class ClusterRegistry:
    """Dedupes cluster-scoped kinds globally, across every namespace in the run."""

    def __init__(self):
        self.claims: dict[tuple[str, str], dict] = {}

    def claim(self, kind: str, name: str, content_h: str, namespace: str, microservice: str):
        """Returns (should_emit, note)."""
        key = (kind, name)
        existing = self.claims.get(key)
        if existing is None:
            self.claims[key] = {"first_owner": (namespace, microservice), "hash": content_h}
            return True, None
        if existing["hash"] == content_h:
            return False, None
        return False, (
            f"{kind}/{name}: cluster-scoped resource defined with DIFFERING content in "
            f"{existing['first_owner']} and ({namespace}, {microservice}) — keeping the first "
            f"definition; the second occurrence was skipped and needs manual review"
        )


# ---------------------------------------------------------------------------
# Per-microservice conversion context
# ---------------------------------------------------------------------------

@dataclass
class MicroserviceContext:
    namespace: str
    name: str
    values: dict = field(default_factory=dict)
    extra_deploy: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    name_map: dict = field(default_factory=lambda: defaultdict(dict))
    claim_actions: dict = field(default_factory=lambda: defaultdict(dict))
    saw_workload: bool = False
    saw_service: bool = False
    primary_sa_set: bool = False
    # ServiceAccount names the pods in this file run as ('default' when a pod
    # names none); None when the file has no pods. See handle_serviceaccount.
    pod_sa_names: Optional[set] = None
    primary_route_set: bool = False
    primary_service_set: bool = False
    # Each Service's raw selector, by final name, with whether it is the primary
    # one — read by rewire_service_selectors once the whole namespace is built.
    raw_selectors: dict = field(default_factory=dict)
    # The pod spec's own automountServiceAccountToken (None when unset), and
    # whether the ServiceAccount document set its own — see handle_workload.
    pod_automount: Any = None
    sa_automount_from_doc: bool = False
    # Full (untrimmed) image, kept only so build_minimal_values can pull the
    # tag out of it — ctx.values["image"] deliberately EXCLUDES the tag (see
    # handle_main_container) so it isn't duplicated in both files. full_env is
    # the env list under --env-in-values, written to the minimal override
    # instead of ctx.values.
    full_image: dict = field(default_factory=dict)
    full_env: list = field(default_factory=list)
    # How many replicas to run is a per-environment decision, not a property
    # of the microservice: prd and tst legitimately differ, and base/ is
    # shared by every namespace running this microservice. Tracked here rather
    # than in ctx.values so it lands in the namespace's own minimal values
    # file, the same treatment image tags and literal env already get.
    replica_count: Any = None
    # Same idea as full_image/full_env above, but for CronJob/Job
    # sub-resources (handle_cronjob/handle_job) — these live nested under
    # ctx.values["cronjobs"/"jobs"][sub_name], not at the microservice's top
    # level, so they need their own keyed-by-(top_key, sub_name) tracking
    # rather than reusing the single top-level full_image/full_env.
    sub_full_image: dict = field(default_factory=dict)
    sub_full_env: dict = field(default_factory=dict)
    # Init containers and sidecars are raw, pass-through Kubernetes container
    # specs (see remap_container_refs docstring / _helpers.tpl) — their
    # `image` stays a plain "repo:tag" string, toYaml'd through as-is, so it
    # can't be split into {repository, tag} the way the main/cronjob/job
    # container's can. The whole string is pulled out to minimal instead
    # (same day-2-tuning-belongs-in-minimal rule, just a whole-value move).
    # Keyed by the FULL path of dict keys needed to reach it in values,
    # e.g. ("sidecars", "logger") or ("cronjobs", "db-vacuum", "jobTemplate",
    # "initContainers", "wait-for-db").
    sub_raw_image: dict = field(default_factory=dict)
    # (top_key, map key) -> (raw metadata.name, whether the chart reproduces
    # that name from this key). Used only by claim_subresource_key, to decide
    # which of two colliding CronJobs/Jobs is entitled to the short key without
    # depending on the order the documents happen to appear in the file.
    sub_key_owner: dict = field(default_factory=dict)
    # NetworkAttachmentDefinition names this release carries (handle_nad), so
    # cross_reference_checks can tell whether a pod's networks annotation is
    # satisfied inside the dump.
    nad_names: set = field(default_factory=set)


def env_list(env, ctx: "MicroserviceContext | None" = None) -> list:
    """The container's env as a LIST, in the deployment's own order.

    Kept a list (not a name-keyed map) so the chart renders it exactly as the
    deployment had it: no reorder — so no pod restart on the cutover sync — and
    `$(VAR)` still expands, since Kubernetes resolves only vars defined EARLIER.
    ConfigMap/Secret references follow ctx.name_map's renames.
    """
    out = []
    for e in env or []:
        entry = copy.deepcopy(e)
        # `- name: FOO` with nothing else, or `value: {}`, both reduce to an
        # empty body — and an empty body has neither a value nor a valueFrom, so
        # it renders as a malformed env var. Normalised to `value: ""` rather
        # than dropped: an env var explicitly set to the empty string is
        # meaningful (it is how a program is told to use its own default), and
        # removing the variable is NOT the same as setting it empty.
        if set(entry) == {"name"} or entry.get("value") == {}:
            entry = {"name": entry["name"], "value": ""}
        out.append(entry)
    if ctx is not None:
        remap_container_refs({"env": out}, ctx)
    return out


def envfrom_list_to_map(envfrom_list) -> dict:
    out = {}
    for e in envfrom_list or []:
        if "configMapRef" in e:
            ref, typ = e["configMapRef"], "configMapRef"
        elif "secretRef" in e:
            ref, typ = e["secretRef"], "secretRef"
        else:
            continue
        entry = {"type": typ}
        if ref.get("optional") is not None:
            entry["optional"] = ref["optional"]
        # `prefix` sits beside the ref, not inside it, and renames every key
        # the pod sees — dropped, the app read none of the variables it expects.
        if e.get("prefix"):
            entry["prefix"] = e["prefix"]
        key = ref["name"]
        # A ConfigMap and a Secret of one name (Harbor's harbor-core) are two
        # entries; keyed by name alone the second replaced the first.
        if key in out:
            entry["name"] = key
            key = f"{key}-{'secret' if typ == 'secretRef' else 'configmap'}"
        out[key] = entry
    return out


def ports_list_to_map(ports_list) -> dict:
    """Index container ports by name for the map-based values.

    A container port does not have to be named, but a values MAP has to have a
    key — so an unnamed port still needs one, and `port-<number>` is it. What it
    must NOT do is put that invented name into the rendered manifest: a Service
    `targetPort: http` or a probe `port: metrics` resolves by name, and a port
    that acquires a name the raw manifest never gave it is a different object.
    `unnamed: true` records that the key is bookkeeping, and
    universal-chart.mapToList drops both the marker and the `name:` line.
    """
    out = {}
    for p in ports_list or []:
        entry = {k: v for k, v in p.items() if k != "name"}
        if p.get("name"):
            key = p["name"]
        else:
            entry["unnamed"] = True
            # The synthetic key MUST carry the protocol. Keyed on the number
            # alone, a container declaring 9125/TCP and 9125/UDP — the standard
            # statsd-exporter shape, and legal Kubernetes — collapsed both onto
            # `port-9125` and the second silently overwrote the first. That is
            # the report's `Deployment/b2b ports[#port:9125/TCP]`, whose root
            # cause it records as UNKNOWN: dict insertion order meant the UDP
            # entry landed last and the TCP one ceased to exist.
            #
            # `_item_name` (the fidelity checker) already uses exactly this
            # identity and says why in its own docstring: "a container may not
            # declare the same containerPort/protocol pair twice". The two
            # halves of the pipeline simply disagreed about what identifies an
            # unnamed port, and the checker was the half that was right.
            #
            # The key never reaches the rendered manifest — universal-chart.mapToList
            # omits both `unnamed` and `name` — so this is free to be as
            # specific as correctness requires.
            key = f"port-{p['containerPort']}-{str(p.get('protocol', 'TCP')).lower()}"
        if key in out:
            # Kubernetes forbids a duplicate name and a duplicate
            # containerPort/protocol pair, so reaching here means the input is
            # already invalid. Raising beats the previous behaviour — silently
            # keeping the last one — because a port that vanishes between raw
            # and rendered is exactly the defect this function just caused.
            raise ValueError(
                f"duplicate container port key '{key}' in {ports_list!r}: two ports "
                f"share a name, or a containerPort/protocol pair, which Kubernetes "
                f"does not allow. Refusing to drop one silently."
            )
        out[key] = entry
    return out


def named_list_to_map(items) -> dict:
    """Index a list of named Kubernetes entries by name, for map-based values.

    A pod's VOLUME names are unique by API rule, so for `volumes` the name is a
    safe key. A container's VOLUME MOUNTS are not: mounting one volume several
    times with different `subPath`s is the standard way to project individual
    ConfigMap keys onto individual file paths, and every one of those mounts
    carries the same `name`. Keyed naively, all but the last were silently
    overwritten and the container came up missing config files.

    So a name that appears more than once switches to a composite key,
    '<name>:<subPath or mountPath>', and the entry carries an explicit `name`
    field recording what the key no longer says on its own. The chart reads that
    field back (universal-chart.mapToList / .validateVolumeMounts in
    templates/_helpers.tpl, `$spec.name | default $key`).

    Unique names — the overwhelmingly common case — keep their bare name as the
    key and gain no `name` field, so existing values files and any GitOps
    override addressing `volumeMounts.<name>` are unaffected.
    """
    items = list(items or [])
    counts = Counter(it["name"] for it in items)
    out: dict = {}
    for it in items:
        name = it["name"]
        entry = {k: v for k, v in it.items() if k != "name"}
        if counts[name] == 1:
            out[name] = entry
            continue
        # subPath is what actually distinguishes these mounts semantically;
        # mountPath is the fallback for a repeat with no subPath.
        discriminator = it.get("subPath") or it.get("mountPath") or str(len(out))
        key = f"{name}:{discriminator}"
        # Same name AND same discriminator is a malformed manifest rather than a
        # pattern, but it must still not silently drop an entry.
        if key in out:
            suffix = 2
            while f"{key}#{suffix}" in out:
                suffix += 1
            key = f"{key}#{suffix}"
        entry["name"] = name
        out[key] = entry
    return out


def set_nested(d: dict, path: tuple, value) -> None:
    for key in path[:-1]:
        d = d.setdefault(key, {})
    d[path[-1]] = value


def named_list_to_map_split_image(items, ctx: MicroserviceContext, path_prefix: tuple) -> tuple[dict, list]:
    """Same as named_list_to_map, but for init containers/sidecars: pulls
    each container's raw image string out into ctx.sub_raw_image (see its
    field comment on MicroserviceContext) instead of leaving it in the
    returned map, so it lands in the per-namespace minimal override instead
    of base/.

    Returns (map, order). The chart ranges a map in SORTED key order, and init
    containers run in list order — so an init container that must run before an
    alphabetically-earlier one was reordered and crashed the pod. Callers write
    `order` as initContainerOrder, which the chart renders first."""
    out = {}
    for it in items or []:
        name = it["name"]
        entry = {k: v for k, v in it.items() if k != "name"}
        if entry.get("image"):
            ctx.sub_raw_image[path_prefix + (name, "image")] = entry.pop("image")
        out[name] = entry
    return out, list(out)


def envfrom_order(ef_map: dict) -> Optional[list]:
    """The raw order of envFrom refs, when it is not the sorted order the chart
    would otherwise use. Later refs win a key both define, so it is meaning."""
    keys = list(ef_map)
    return keys if len(keys) > 1 and keys != sorted(keys) else None


def remap_envfrom(ef_map: dict, ctx: MicroserviceContext) -> dict:
    out = {}
    for name, entry in ef_map.items():
        kind = "ConfigMap" if entry.get("type") == "configMapRef" else "Secret"
        if "name" in entry:  # keyed apart from its object (envfrom_list_to_map)
            entry = {**entry, "name": ctx.name_map[kind].get(entry["name"], entry["name"])}
            out[name] = entry
        else:
            out[ctx.name_map[kind].get(name, name)] = entry
    return out


def remap_container_refs(container: dict, ctx: MicroserviceContext) -> dict:
    """Rewrite configMapKeyRef/secretKeyRef/configMapRef/secretRef names inside
    a raw (list-shaped) container spec's env/envFrom, in place, using the same
    ctx.name_map collision-resolution table applied to the main container via
    env_list/remap_envfrom. Init containers and sidecars are stored as native
    Kubernetes container specs (toYaml'd through as-is by the chart), so this
    walks the list entries directly — without it, a ConfigMap/Secret that gets
    renamed by the collision-avoidance pass leaves these containers pointing at
    a name that no longer exists. The main container's env (env_list) is
    remapped through here too.
    """
    for e in container.get("env") or []:
        vf = e.get("valueFrom") or {}
        if "configMapKeyRef" in vf and vf["configMapKeyRef"].get("name"):
            n = vf["configMapKeyRef"]["name"]
            vf["configMapKeyRef"]["name"] = ctx.name_map["ConfigMap"].get(n, n)
        if "secretKeyRef" in vf and vf["secretKeyRef"].get("name"):
            n = vf["secretKeyRef"]["name"]
            vf["secretKeyRef"]["name"] = ctx.name_map["Secret"].get(n, n)
    for ef in container.get("envFrom") or []:
        if "configMapRef" in ef and ef["configMapRef"].get("name"):
            n = ef["configMapRef"]["name"]
            ef["configMapRef"]["name"] = ctx.name_map["ConfigMap"].get(n, n)
        if "secretRef" in ef and ef["secretRef"].get("name"):
            n = ef["secretRef"]["name"]
            ef["secretRef"]["name"] = ctx.name_map["Secret"].get(n, n)
    return container


def remap_volumes(vol_map: dict, ctx: MicroserviceContext) -> dict:
    for vol_name, entry in vol_map.items():
        if "emptyDir" in entry and not (entry["emptyDir"] or {}).get("sizeLimit"):
            ctx.warnings.append(
                f"[P2] volume '{vol_name}': emptyDir has no sizeLimit — unbounded, it can fill "
                f"the node's disk and get every pod on that node evicted"
            )
        if "persistentVolumeClaim" in entry and entry["persistentVolumeClaim"].get("claimName"):
            n = entry["persistentVolumeClaim"]["claimName"]
            entry["persistentVolumeClaim"]["claimName"] = ctx.name_map["PersistentVolumeClaim"].get(n, n)
        if "configMap" in entry and entry["configMap"].get("name"):
            n = entry["configMap"]["name"]
            entry["configMap"]["name"] = ctx.name_map["ConfigMap"].get(n, n)
        if "secret" in entry and entry["secret"].get("secretName"):
            n = entry["secret"]["secretName"]
            entry["secret"]["secretName"] = ctx.name_map["Secret"].get(n, n)
    return vol_map


# ---------------------------------------------------------------------------
# Per-kind handlers -> mutate ctx.values
# ---------------------------------------------------------------------------

def set_order(v: dict, key: str, order_key: str) -> None:
    """Record the raw LIST order of the map-based `v[key]` under `v[order_key]`.

    The chart renders these maps as Kubernetes lists, and Helm ranges a map in
    sorted key order — so without this, ports / volumes / envFrom / sidecars
    came out alphabetised. That is a pod-template change: migrating a workload
    onto the chart rolled every pod on the first sync for no other reason. The
    maps here are built in raw order (dicts keep insertion order), so their
    keys ARE the raw order. (env and volumeMounts are written as lists, which
    the chart renders as written, so they need no order key.)
    A single entry has no order to keep, so nothing is written for it.

    The cosmetic ones (COSMETIC_ORDER_KEYS — only the restart cares) are
    written under --exact-copy alone; envFrom/initContainer order is meaning
    and always kept.
    """
    if order_key in COSMETIC_ORDER_KEYS and not EXACT_COPY:
        return
    m = v.get(key)
    if isinstance(m, dict) and len(m) > 1:
        v[order_key] = list(m)


def handle_main_container(c: dict, ctx: MicroserviceContext) -> None:
    v = ctx.values
    # The container's own name. The chart used to name its managed container
    # after the release, which still runs the same image but renames the
    # container — and a container is addressed BY NAME by everything outside
    # the pod spec: `kubectl logs -c` / `exec -c`, log-pipeline selectors
    # (container="nginx"), and the `container` label on every kubelet metric.
    # A raw `nginx` silently becoming `storefront` breaks all of them, so the
    # real name is carried and templates/_pod-spec.tpl falls back to the
    # release name only when it is absent.
    if c.get("name"):
        v["containerName"] = c["name"]
    repo, tag, digest = split_image(c.get("image", ""))
    image = {"repository": repo}
    if tag:
        image["tag"] = tag
    if digest:
        image["digest"] = digest
    if c.get("imagePullPolicy"):
        image["pullPolicy"] = c["imagePullPolicy"]
    ctx.full_image = image
    # The comprehensive base/ file omits the repository, tag AND digest — all
    # three are the minimal override's job (see build_minimal_values), so a
    # day-2 version bump only ever touches the namespace's own
    # values/<slug>.yaml, not base/. A digest is the same kind of value as a
    # tag here: the version pin. The repository travels with it — written once,
    # beside its tag, rather than in base and again in every values file.
    # pullPolicy is structural and stays in base.
    if image.get("pullPolicy"):
        v["image"] = {"pullPolicy": image["pullPolicy"]}

    for k in ("command", "args", "workingDir"):
        if c.get(k):
            v[k] = c[k]
    if c.get("ports"):
        v["ports"] = ports_list_to_map(c["ports"])
        set_order(v, "ports", "portOrder")
    if c.get("env"):
        # See ENV_IN_VALUES: under that flag the env list goes to the
        # minimal override instead of here.
        if ENV_IN_VALUES:
            ctx.full_env = env_list(c["env"], ctx)
        else:
            v["env"] = env_list(c["env"], ctx)
    if c.get("envFrom"):
        v["envFrom"] = remap_envfrom(envfrom_list_to_map(c["envFrom"]), ctx)
        set_order(v, "envFrom", "envFromOrder")
    if c.get("resources"):
        v["resources"] = c["resources"]
    for probe in ("livenessProbe", "readinessProbe", "startupProbe"):
        if c.get(probe):
            v[probe] = c[probe]
    if c.get("lifecycle"):
        v["lifecycle"] = c["lifecycle"]
    # Under --exact-copy, `in` rather than truthiness: an explicit empty
    # `securityContext: {}` is stored by the API server as given, and dropping
    # it changes the pod template, which restarts the pods on the first sync
    # after migrating. Otherwise an empty one says nothing and is left out.
    if c.get("securityContext") or (EXACT_COPY and isinstance(c.get("securityContext"), dict)):
        v["securityContext"] = c["securityContext"]
    if c.get("volumeMounts"):
        # A list in the deployment's own order (see env_list).
        v["volumeMounts"] = copy.deepcopy(c["volumeMounts"])
    if c.get("containerRestartRules"):
        v["containerRestartRules"] = c["containerRestartRules"]
    # The CONTAINER-level restartPolicy, which is a different field from the
    # pod-level one already captured in handle_workload — hence the distinct
    # values key, since .Values.restartPolicy is the pod's. On an initContainer
    # `Always` is how Kubernetes 1.28+ declares a native SIDECAR; on a regular
    # container (1.33+) it restarts that container in place rather than failing
    # the pod. Dropping it turns a sidecar back into an ordinary init container
    # the workload waits to COMPLETE, which hangs a Job forever.
    if c.get("restartPolicy"):
        v["containerRestartPolicy"] = c["restartPolicy"]
    for flag in ("stdin", "stdinOnce", "tty"):
        if c.get(flag):
            v[flag] = c[flag]
    for k in ("terminationMessagePath", "terminationMessagePolicy"):
        if c.get(k):
            v[k] = c[k]


def handle_workload(doc: dict, ctx: MicroserviceContext, kind: str) -> None:
    ctx.saw_workload = True
    v = ctx.values
    spec = doc.get("spec", {}) or {}
    v.setdefault("workload", {})["type"] = {
        "Deployment": "deployment", "StatefulSet": "statefulset", "DaemonSet": "daemonset",
    }[kind]

    if "replicas" in spec:
        ctx.replica_count = spec["replicas"]  # -> minimal values, never base
    if "revisionHistoryLimit" in spec:
        v["revisionHistoryLimit"] = spec["revisionHistoryLimit"]
    # Deployment-only: how long Kubernetes waits before marking a rollout
    # failed. The Kubernetes default (600) is harmless to lose, which is why
    # the report rated this LOW — but a Deployment that deliberately shortens
    # it, say to 120 for fast rollback detection, silently gets ten minutes
    # instead of two. Captured whatever the value is: deciding which values are
    # "worth" keeping is how fields go missing.
    if "progressDeadlineSeconds" in spec:
        v["progressDeadlineSeconds"] = spec["progressDeadlineSeconds"]
    # minReadySeconds sits one line from progressDeadlineSeconds and was missed
    # when that one was fixed — the report's bulk suppression pass then swept it
    # into a "Pod template defaults" bucket alongside genuinely inert fields
    # (enableServiceLinks: false, shareProcessNamespace: false) purely because
    # they sit near each other in the pod spec.
    #
    # Its own line-by-line pass had it right: "MEDIUM impact [...] Should be
    # preserved." The Kubernetes default is 0, so there is no default worth
    # losing — an explicit 10 is a deliberate rollout-safety setting, and
    # dropping it means the next pod starts rolling the instant the current one
    # reports Ready. Captured with `in` rather than truthiness so an explicit 0
    # survives too (see Subtract_Defaults_Removes_Explicit_Zero).
    if "minReadySeconds" in spec:
        v["minReadySeconds"] = spec["minReadySeconds"]

    if kind == "StatefulSet":
        if spec.get("podManagementPolicy"):
            v["podManagementPolicy"] = spec["podManagementPolicy"]
        # The GOVERNING Service — the one that gives each pod its stable DNS
        # name, <pod>.<serviceName>.<ns>.svc. Clustered systems (Elasticsearch,
        # ZooKeeper, Redis, Postgres replicas) point it at a dedicated HEADLESS
        # Service, conventionally '<name>-headless', because only a headless
        # Service publishes per-pod A records; the regular ClusterIP Service
        # next to it resolves to one load-balanced VIP and peer discovery over
        # it cannot work at all.
        #
        # Captured unconditionally rather than only when it differs from the
        # workload name. Storing it on mismatch alone would lean on the
        # chart's fullname resolving back to the raw workload name, which is
        # true today only because fullnameOverride is pinned below — and this
        # defect exists precisely because an implicit "the names will match"
        # assumption went unstated. Pinning the real value retires the
        # assumption instead of moving it somewhere else.
        if spec.get("serviceName"):
            v["serviceName"] = spec["serviceName"]
        # persistentVolumeClaimRetentionPolicy is the one field here whose
        # loss is IRREVERSIBLE. Kubernetes defaults both halves to `Delete`,
        # so a StatefulSet that declared `Retain` and converts without it
        # comes back looking identical and then destroys its PVCs — and the
        # data on them — the first time it is deleted, scaled down, or
        # recreated by an ArgoCD sync. Copied verbatim rather than normalised:
        # each half is independently optional, and a half that is absent in
        # the raw manifest must stay absent here so the render doesn't invent
        # a policy the source never stated.
        if spec.get("persistentVolumeClaimRetentionPolicy"):
            v["persistentVolumeClaimRetentionPolicy"] = \
                spec["persistentVolumeClaimRetentionPolicy"]
        vcts = spec.get("volumeClaimTemplates") or []
        if vcts:
            vct_map = {}
            for vct in vcts:
                name = vct["metadata"]["name"]
                vspec = vct.get("spec", {}) or {}
                entry = {"accessModes": vspec.get("accessModes", ["ReadWriteOnce"])}
                size = ((vspec.get("resources") or {}).get("requests") or {}).get("storage")
                if size:
                    entry["size"] = size
                if vspec.get("storageClassName"):
                    entry["storageClassName"] = vspec["storageClassName"]
                if vspec.get("volumeMode"):
                    entry["volumeMode"] = vspec["volumeMode"]
                # The PVCs a StatefulSet creates inherit the TEMPLATE's labels
                # and annotations, and nothing else gives them any. Uncaptured,
                # every generated volume arrives with no team / environment /
                # cost-centre metadata, which is not cosmetic: it is what quota
                # enforcement and cost allocation select on, and a PVC is the one
                # object here that outlives its workload.
                #
                # The chart template already renders both — this was a converter
                # gap alone. Deliberately kept separate from the VCT runtime
                # boilerplate (status/kind/apiVersion/creationTimestamp) that
                # sanitize_object drops: the report bundles them into one
                # "volumeClaimTemplates metadata ~25" row, and suppressing that
                # row wholesale would have buried this loss inside it.
                vct_meta = vct.get("metadata") or {}
                if pod_template_labels(vct_meta):
                    entry["labels"] = pod_template_labels(vct_meta)
                vct_ann = clean_annotations(vct_meta.get("annotations"))
                if vct_ann:
                    entry["annotations"] = vct_ann
                vct_map[name] = entry
            v["volumeClaimTemplates"] = vct_map

    strategy = spec.get("strategy") or spec.get("updateStrategy")
    if strategy:
        v["strategy"] = strategy

    template = spec.get("template", {}) or {}
    pod_meta = template.get("metadata", {}) or {}
    pod_spec = template.get("spec", {}) or {}

    # automountServiceAccountToken is settable in TWO places — on the
    # ServiceAccount (handled in handle_serviceaccount) and on the pod spec,
    # which is where hardening usually sets it because it is what actually
    # stops the token being mounted into this workload's pods. The chart
    # renders both from the single .Values.serviceAccount key
    # (templates/_pod-spec.tpl and templates/serviceaccount.yaml), so the
    # pod-level value has to feed the same key. Read only from the
    # ServiceAccount, a raw `automountServiceAccountToken: false` on the pod
    # was silently dropped and the chart then rendered the default `true` —
    # quietly undoing the hardening.
    #
    # The pod's value is also remembered on its own: hardened charts set the SA
    # to false and the pod to true, and once the ServiceAccount document has
    # said its own value the pod's must not overwrite it (that flipped the SA
    # object). build_microservice then writes the chart's top-level pod value
    # wherever the two disagree.
    if "automountServiceAccountToken" in pod_spec:
        ctx.pod_automount = pod_spec["automountServiceAccountToken"]
        if not ctx.sa_automount_from_doc:
            v.setdefault("serviceAccount", {})["automountServiceAccountToken"] = \
                pod_spec["automountServiceAccountToken"]

    if raw_labels(doc):
        v["labels"] = raw_labels(doc)
    ann = clean_annotations((doc.get("metadata") or {}).get("annotations"))
    if ann:
        v["annotations"] = ann

    # The live selector, verbatim: it is immutable, so anything else fails sync.
    selector = (spec.get("selector") or {}).get("matchLabels") or {}
    if (spec.get("selector") or {}).get("matchExpressions"):
        ctx.warnings.append(
            f"[P1] {kind}: spec.selector.matchExpressions cannot be expressed by the chart "
            f"(selectorLabels is matchLabels only) — the selector will change, and the "
            f"workload must be recreated on migration")
    if selector:
        v["selectorLabels"] = selector
    pod_labels = {k: val for k, val in pod_template_labels(pod_meta).items()
                  if k not in selector}
    if pod_labels:
        v["podLabels"] = pod_labels
    pod_ann = pod_template_annotations(pod_meta)
    if pod_ann:
        v["podAnnotations"] = pod_ann
    # The `checksum/*` annotations themselves are correctly dropped — they are
    # sha256s of the SOURCE cluster's ConfigMap/Secret content, and carrying a
    # stale hash into a chart is worse than carrying none: it pins the pod
    # template to a value that no longer describes anything.
    #
    # But their PRESENCE is information, and dropping that too is the actual
    # regression. A workload whose raw manifest carries them is a workload that
    # was being restarted when its config changed. The Universal Chart can do
    # that — universal-chart.checksumAnnotations, hashing .Values.configMaps and
    # .Values.secrets — but it ships `checksums.enabled: false` and nothing
    # turned it on, so the behaviour was being lost silently.
    #
    # That is the failure mode error.txt describes: "the ConfigMap updates,
    # ArgoCD reports Synced and Healthy, and the pods keep serving stale
    # configuration until something unrelated restarts them." Nothing reports it,
    # and it is very hard to diagnose after the fact.
    #
    # So: drop the stale VALUES, preserve the INTENT.
    if any(k.startswith("checksum/") for k in (pod_meta.get("annotations") or {})):
        v.setdefault("checksums", {})["enabled"] = True

    if pod_spec.get("securityContext"):
        v["podSecurityContext"] = pod_spec["securityContext"]

    sa_name = pod_spec.get("serviceAccountName") or pod_spec.get("serviceAccount")
    if sa_name == "default":
        # The pod runs as the namespace's `default` SA, which the chart's own
        # default (create: false) already leaves alone. Name `default` only when
        # the raw manifest spelled it out — rendering a serviceAccountName the
        # template never carried changes the pod template.
        v.setdefault("serviceAccount", {}).setdefault("name", "default")
    if sa_name and sa_name != "default":
        final = ctx.name_map["ServiceAccount"].get(sa_name, sa_name)
        # Only create=True when THIS microservice actually owns a
        # ServiceAccount object under that name (action "own"/"renamed"). If
        # the name is unclaimed here, or claimed by the namespace-shared
        # release ("reference_shared"), this pod is merely a CONSUMER of a
        # ServiceAccount created elsewhere — create=True in that case would
        # make this release fight the real owner over the same object; the
        # chart's default (create: false) just names it. Order-independent:
        # claim_actions is fully populated in pass 1, before any doc
        # (ServiceAccount or workload) is handled here.
        action = ctx.claim_actions["ServiceAccount"].get(sa_name)
        v.setdefault("serviceAccount", {})
        v["serviceAccount"]["name"] = final
        if action not in (None, "reference_shared"):
            v["serviceAccount"].setdefault("create", True)

    # enableServiceLinks joins this list, and it is the odd one out in a way
    # worth stating: every other boolean here defaults to FALSE in Kubernetes,
    # so losing an explicit `false` costs nothing. enableServiceLinks defaults to
    # TRUE. The report's benign rating says "false is the K8s default, omitted by
    # chart", which is factually backwards — an explicit `false` is a deliberate
    # opt-out, and dropping it re-injects the {SVCNAME}_SERVICE_HOST/_PORT
    # variables the source switched off. Teams disable those for two reasons:
    # env bloat in namespaces with many Services, and because an injected name
    # can collide with the application's own variable and silently override it.
    #
    # error.txt asks for a collision check on the affected workloads before
    # suppressing. That needs manifests this repo does not have — so the field is
    # carried and the question does not need answering.
    for k in ("terminationGracePeriodSeconds", "hostNetwork", "hostIPC", "hostPID",
              "shareProcessNamespace", "enableServiceLinks", "priorityClassName",
              "runtimeClassName", "schedulerName", "dnsPolicy", "restartPolicy"):
        if k in pod_spec:
            v[k] = pod_spec[k]
    for k in ("dnsConfig", "nodeSelector", "tolerations", "affinity",
              "topologySpreadConstraints", "imagePullSecrets", "hostAliases",
              "readinessGates", "overhead"):
        if pod_spec.get(k):
            v[k] = pod_spec[k]

    containers = pod_spec.get("containers", []) or []
    if containers:
        handle_main_container(containers[0], ctx)
        for extra in containers[1:]:
            remap_container_refs(extra, ctx)
            entry = {k: val for k, val in extra.items() if k != "name"}
            if entry.get("image"):
                ctx.sub_raw_image[("sidecars", extra["name"], "image")] = entry.pop("image")
            v.setdefault("sidecars", {})[extra["name"]] = entry
        set_order(v, "sidecars", "sidecarOrder")
    init_containers = pod_spec.get("initContainers", []) or []
    if init_containers:
        for ic in init_containers:
            remap_container_refs(ic, ctx)
        v["initContainers"], order = named_list_to_map_split_image(init_containers, ctx, ("initContainers",))
        if len(order) > 1:
            v["initContainerOrder"] = order

    volumes = pod_spec.get("volumes", []) or []
    if volumes:
        v["volumes"] = remap_volumes(named_list_to_map(volumes), ctx)
        set_order(v, "volumes", "volumeOrder")


# Only the ReplicaSet controller's hash. name/instance/app used to be stripped
# here and re-derived by the chart from Release.Name, which rewrote a migrated
# pod's labels (and, via the selector, broke an immutable field). They are now
# carried; handle_workload moves the selector keys to selectorLabels.
POD_TEMPLATE_CHART_LABELS = frozenset({"pod-template-hash", "helm.sh/chart"})


def pod_template_labels(pod_meta: dict) -> dict:
    """A pod TEMPLATE's labels, minus the identity ones the chart computes.

    Extracted into a seam because it was inline in handle_workload and nowhere
    else: handle_cronjob and handle_job never read pod-template labels at all,
    so every CronJob lost {critical, environment, operational, operationalPurpose,
    team} — 30 errors, and a real runtime risk rather than lost metadata, since
    pod-template labels are what admission webhooks and sidecar injectors match
    on. A CronJob pod stripped of them can miss an injection it depends on, or
    receive one it should have been excluded from.

    Copying the filter a third time would have kept the three handlers free to
    drift apart; a shared function means the next handler that needs pod labels
    gets the same exclusions by construction.
    """
    return {k: v for k, v in (pod_meta.get("labels") or {}).items()
            if k not in POD_TEMPLATE_CHART_LABELS}


def pod_template_annotations(pod_meta: dict) -> dict:
    """A pod TEMPLATE's annotations, minus `checksum/*`.

    Those are content hashes a deploying tool regenerates (see folder 19); the
    rest — `sidecar.istio.io/inject`, `prometheus.io/scrape`, vault agent
    annotations — are load-bearing and were being dropped from every CronJob and
    Job for the same reason the labels were.
    """
    return {k: v for k, v in (pod_meta.get("annotations") or {}).items()
            if not k.startswith("checksum/") and k not in RESTART_STAMP_ANNOTATIONS}


# Stamped on the pod template by `oc rollout restart` / `kubectl rollout
# restart`. It records WHEN someone last restarted the workload — an action,
# not configuration. Carried into the chart, it pins a stale timestamp: on the
# first sync the template no longer matches the live one, so every pod restarts
# again, and every later manual restart shows up as drift.
RESTART_STAMP_ANNOTATIONS = frozenset({
    "kubectl.kubernetes.io/restartedAt",
    "openshift.openshift.io/restartedAt",
})


def as_extra_deploy(doc: dict) -> str:
    """Serialise a doc for .Values.extraDeploy, neutralising Go template syntax.

    templates/extra-deploy.yaml renders each entry through `tpl`, which is a
    deliberate feature for hand-written entries ("inject any raw Kubernetes
    YAML, with full Helm templating support"). It is exactly wrong for the
    entries THIS converter produces: those are raw manifests copied verbatim
    from a cluster, and a `{{ ... }}` inside one is the source object's own
    syntax, not a Helm expression.

    Argo Events makes this concrete rather than theoretical. A Sensor whose
    trigger passes a parameter to an Argo Workflow carries
    `value: "{{workflow.parameters.target}}"` — completely ordinary — and `tpl`
    tries to evaluate it as Helm:

        Error: ... error calling tpl: cannot parse template

    That is not a mangled value, it is a HARD RENDER FAILURE of the whole
    release. Anything reaching extraDeploy is by definition a kind the chart
    does not model, so this is the least-tested path in the chart and the one
    most likely to carry foreign templating syntax.

    Escaping only the OPENING delimiter is sufficient — Go's parser treats a
    bare `}}` as ordinary text — and it round-trips: `tpl` emits the literal
    `{{` back, so the rendered manifest is byte-identical to the raw one.
    """
    return dump_yaml(doc).replace("{{", '{{ "{{" }}')


def raw_labels(doc: dict) -> dict:
    """An object's OWN metadata.labels, minus the ones the chart derives itself.

    Almost no handler read this. The chart rendered `universal-chart.labels`
    alone on every object, so `team`, `environment`, `app.kubernetes.io/part-of`,
    `argocd.argoproj.io/*` and the rest were dropped from everything the
    converter emitted — the single highest-count defect in the fidelity report.
    Pod-TEMPLATE labels survived (handle_workload -> podLabels); the objects'
    own labels did not.

    The identity labels are excluded rather than carried, for the same reason
    handle_workload already excludes them from podLabels: the chart computes
    them from Release.Name and they must stay consistent with selectorLabels.
    Carrying the raw copies would either be redundant or, worse, contradict what
    the selector matches on. `app` is excluded too — it is the pre-Helm
    convention for the same thing and the fidelity checker already treats it as
    chart-owned.
    """
    labels = ((doc.get("metadata") or {}).get("labels") or {})
    return {k: v for k, v in labels.items() if k not in CHART_OWNED_LABELS}


# name/instance/version/managed-by/app are CARRIED now: the chart's default
# chartLabels: false renders an object's labels exactly as the
# live object had them instead of re-deriving identity labels from this release
# (a different instance, a new version and a missing name label all showed up
# as ArgoCD diffs).
CHART_OWNED_LABELS = frozenset({
    "pod-template-hash",
    # The chart that USED to deploy the object (e.g. super-universal-chart-1.0.0).
    # Carried verbatim it stamped that chart+version onto every object of every
    # microservice forever, frozen at whatever version was live at conversion.
    "helm.sh/chart",
    # ArgoCD stamps this on every object it syncs, naming the Application that
    # owns it. It is present in a dump scraped from a live cluster and must NOT
    # come back out of a chart: the chart cannot know which Application will
    # deploy it, and rendering a guess would fight ArgoCD's own tracking on the
    # next sync. The report's field table reaches the same verdict on its own
    # — "Unavoidable — ArgoCD injects at deploy" — and it is the ONE row there
    # marked suppressible. The other ~520 lost labels are custom governance
    # metadata and are carried; see build_route_entry.
    "argocd.argoproj.io/instance",
})


def build_route_entry(doc: dict, ctx: MicroserviceContext) -> dict:
    spec = doc.get("spec", {}) or {}
    entry = {"enabled": True}
    if spec.get("host"):
        entry["host"] = spec["host"]
    if spec.get("path"):
        entry["path"] = spec["path"]
    tp = (spec.get("port") or {}).get("targetPort")
    if tp:
        entry["targetPort"] = tp
    if spec.get("tls"):
        entry["tls"] = spec["tls"]
    if spec.get("wildcardPolicy") and spec["wildcardPolicy"] != "None":
        entry["wildcardPolicy"] = spec["wildcardPolicy"]
    to_name = (spec.get("to") or {}).get("name")
    if to_name:
        # Remap through the Service name-claiming pass, same as env/envFrom/volumes,
        # in case the target Service got renamed by collision resolution.
        entry["serviceName"] = ctx.name_map["Service"].get(to_name, to_name)
    ann = clean_annotations((doc.get("metadata") or {}).get("annotations"))
    if ann:
        entry["annotations"] = ann
    # THIS LINE IS THE DEFECT. Every other handler that emits an object calls
    # raw_labels — Service, ConfigMap, Secret, ExternalSecret, PVC,
    # Role, RoleBinding, ServiceAccount, CronJob,
    # Job, workloads, cluster-scoped. Route received a COMMENT saying labels
    # were handled here, and not the call. Both Route templates already render
    # `.labels` through universal-chart.objectLabels, so the chart was waiting
    # for a value the converter never produced, and every Route in the source
    # lost team / environment / critical / operational / operational.purpose /
    # app.openshift.io/runtime / opentelemetry.io/enabled — 613-705 occurrences,
    # the single largest error class in the report.
    if raw_labels(doc):
        entry["labels"] = raw_labels(doc)
    return entry


def handle_route(doc: dict, ctx: MicroserviceContext, final_name: str) -> None:
    entry = build_route_entry(doc, ctx)
    if not ctx.primary_route_set:
        ctx.primary_route_set = True
        # The Route's own name. Source manifests conventionally suffix it
        # ('<app>-route'), and the chart used to render every object as the
        # release fullname — so the Route and the Service came out sharing one
        # name and the '-route' name simply ceased to exist for anything
        # referencing it. serviceName is deliberately NOT dropped any more:
        # the primary Route targets whatever Service the raw manifest named,
        # which is the '-service' object, not the release fullname.
        entry["name"] = final_name
        ctx.values["route"] = entry
    else:
        # Extra routes DO honor serviceName (defaults to the fullname Service
        # only when absent) — an extra route fronting a DIFFERENT backend
        # (e.g. an admin Service) depends on this being preserved.
        ctx.values.setdefault("routes", {})[final_name] = entry


def build_service_entry(doc: dict) -> dict:
    spec = doc.get("spec", {}) or {}
    entry = {"type": spec.get("type", "ClusterIP")}
    ports = spec.get("ports") or []
    if ports:
        pmap = {}
        for p in ports:
            name = p.get("name") or f"port-{p['port']}"
            e = {"port": p["port"]}
            if p.get("targetPort") is not None:
                e["targetPort"] = p["targetPort"]
            if p.get("protocol"):
                e["protocol"] = p["protocol"]
            # appProtocol is what a service mesh reads to choose an L7 handler.
            # `protocol` above is L4 (TCP/UDP/SCTP) and says nothing about it, so
            # a port that declared `grpc` and lost it is treated as opaque TCP:
            # gRPC-aware routing, retries and telemetry all quietly degrade.
            if p.get("appProtocol"):
                e["appProtocol"] = p["appProtocol"]
            if p.get("nodePort"):
                e["nodePort"] = p["nodePort"]
            pmap[name] = e
        entry["ports"] = pmap
    if spec.get("clusterIP") == "None":
        entry["clusterIP"] = "None"
    if spec.get("type") == "ExternalName" and spec.get("externalName"):
        entry["externalName"] = spec["externalName"]
    # Both are rendered by service.yaml and services.yaml; not carrying them
    # silently re-addressed a LoadBalancer and opened it to every source range.
    if spec.get("loadBalancerIP"):
        entry["loadBalancerIP"] = spec["loadBalancerIP"]
    if spec.get("loadBalancerSourceRanges"):
        entry["loadBalancerSourceRanges"] = spec["loadBalancerSourceRanges"]
    if spec.get("externalTrafficPolicy"):
        entry["externalTrafficPolicy"] = spec["externalTrafficPolicy"]
    if spec.get("sessionAffinity") and spec["sessionAffinity"] != "None":
        entry["sessionAffinity"] = spec["sessionAffinity"]
        if spec.get("sessionAffinityConfig"):
            entry["sessionAffinityConfig"] = spec["sessionAffinityConfig"]
    if spec.get("publishNotReadyAddresses") is not None:
        entry["publishNotReadyAddresses"] = spec["publishNotReadyAddresses"]
    if spec.get("trafficDistribution"):
        entry["trafficDistribution"] = spec["trafficDistribution"]
    # `is not None`, not truthiness — and that is the whole risk in this field.
    # The Kubernetes default is TRUE, so losing an explicit `true` is harmless
    # and losing an explicit `false` silently flips a LoadBalancer into
    # allocating node ports it was configured not to allocate. A truthy check
    # would preserve exactly the harmless case and drop the dangerous one.
    #
    # Captured for every Service rather than only for type LoadBalancer. The
    # report splits this field into 62 benign occurrences on Redis "announce"
    # Services plus 3 real LoadBalancers, and proposes suppressing the 62 — but
    # its own field table offers the alternative, "Suppressible or explicitly
    # set". Setting it is strictly better: one rule, no kind-scoping to get
    # wrong, nothing suppressed, and the 62 inert copies cost a rendered line
    # each while remaining byte-accurate to the source.
    if spec.get("allocateLoadBalancerNodePorts") is not None:
        entry["allocateLoadBalancerNodePorts"] = spec["allocateLoadBalancerNodePorts"]
    if spec.get("selector"):
        entry["selector"] = spec["selector"]
    ann = clean_annotations((doc.get("metadata") or {}).get("annotations"))
    if ann:
        entry["annotations"] = ann
    meta_labels = (doc.get("metadata") or {}).get("labels")
    if meta_labels:
        entry["labels"] = raw_labels(doc)
    return entry


def handle_service(doc: dict, ctx: MicroserviceContext, final_name: str) -> None:
    ctx.saw_service = True
    # A Service with no selector is a deliberate pattern: its Endpoints are
    # managed by hand, pointing at an external or legacy backend the cluster
    # cannot select for. This chart cannot express that — every non-ExternalName
    # Service it renders gets the release's own selectorLabels — so the
    # migrated Service starts matching pods and silently replaces those
    # hand-managed Endpoints. Nothing else reports it: the fidelity checker
    # deliberately ignores Service.spec.selector, because the chart deriving it
    # is normally correct. Warn, since the conversion cannot be faithful here.
    if not (doc.get("spec") or {}).get("selector"):
        ctx.warnings.append(
            f"Service/{final_name}: the raw manifest declares NO selector, but the chart "
            f"renders one from the release's own labels. If this Service is backed by a "
            f"hand-managed Endpoints object, the injected selector will override it."
        )
    svc_spec = doc.get("spec") or {}
    if svc_spec.get("type", "ClusterIP") == "ClusterIP" and \
            any(p.get("nodePort") for p in svc_spec.get("ports") or []):
        ctx.warnings.append(
            f"[P0] Service/{final_name}: nodePort is set on a ClusterIP Service — Kubernetes "
            f"ignores it, so nothing is exposed on the nodes. Set type: NodePort if node "
            f"exposure was intended."
        )
    entry = build_service_entry(doc)
    entry["enabled"] = True
    ctx.raw_selectors[final_name] = (not ctx.primary_service_set, (doc.get("spec") or {}).get("selector") or {})
    if not ctx.primary_service_set:
        ctx.primary_service_set = True
        # The Service's own name. In-cluster DNS, Routes, Ingresses and any
        # other release's config all address a Service by name, so rendering it
        # as the release fullname silently deletes '<app>-service' from the
        # namespace and breaks every one of those references.
        entry["name"] = final_name
        ctx.values["service"] = entry
    else:
        ctx.values.setdefault("services", {})[final_name] = entry


SERVICE_SELECTOR_LABEL_PREFIX = "svc.universal-chart.io/"


def rewire_service_selectors(releases: list[tuple["MicroserviceContext", list[dict]]]) -> list[str]:
    """Point every Service at the pods it selected in the raw manifests.

    The chart relabels pods: app.kubernetes.io/name and /instance become the
    RELEASE's, so a raw selector naming the source chart's values
    (`instance: redis, name: redis`) matches nothing once converted. A primary
    Service of a release that has its own pods is fine — the chart's own
    selector is that release's pods. Everything else is resolved here, across
    the whole namespace: a headless Service kept in `shared` for two
    StatefulSets, Kafka's client Service, an extra Service of a release.

    The pods the raw selector matched are found from the raw pod templates; if
    the raw selector still holds in what they render (all its labels survive in
    podLabels) it is kept, otherwise each matched workload gets one stable
    label, `svc.universal-chart.io/<service>: "true"`, and the Service selects
    that. Returns report lines.
    """
    pods = []
    for ctx, docs in releases:
        for d in docs:
            if d.get("kind") in WORKLOAD_KINDS:
                labels = ((((d.get("spec") or {}).get("template") or {}).get("metadata") or {})
                          .get("labels") or {})
                pods.append((ctx, d, labels))
    notes = []
    for ctx, _ in releases:
        for svc, (primary, sel) in sorted(ctx.raw_selectors.items()):
            entry = ctx.values.get("service") if primary else (ctx.values.get("services") or {}).get(svc)
            if not sel or not isinstance(entry, dict):
                continue
            matched = [(c, d) for c, d, labels in pods
                       if all(labels.get(k) == v for k, v in sel.items())]
            if not matched:
                continue
            if primary and ctx.saw_workload and all(c is ctx for c, _ in matched):
                continue
            # What the pods render with: the live selector (selectorLabels) plus
            # the rest of their template labels (podLabels).
            def rendered(c):
                return {**(c.values.get("selectorLabels") or {}), **(c.values.get("podLabels") or {})}
            survives = all(all(rendered(c).get(k) == v for k, v in sel.items()) for c, _ in matched)
            if survives:
                entry["selector"] = dict(sel)
                continue
            key = SERVICE_SELECTOR_LABEL_PREFIX + svc
            for c, _ in matched:
                c.values.setdefault("podLabels", {})[key] = "true"
            entry["selector"] = {key: "true"}
            targets = sorted({f"{d.get('kind')}/{(d.get('metadata') or {}).get('name')}" for _, d in matched})
            notes.append(f"Service/{svc}: its raw selector names labels the chart rewrites, so it "
                         f"selects {', '.join(targets)} through the pod label '{key}'")
    return notes


def handle_configmap(doc: dict, ctx: MicroserviceContext, name: str) -> None:
    entry = {}
    if doc.get("data"):
        entry["data"] = doc["data"]
    if doc.get("binaryData"):
        entry["binaryData"] = doc["binaryData"]
    meta = doc.get("metadata", {}) or {}
    if meta.get("labels"):
        entry["labels"] = raw_labels(doc)
    ann = clean_annotations(meta.get("annotations"))
    if ann:
        entry["annotations"] = ann
    ctx.values.setdefault("configMaps", {})[name] = entry
    _flag_redis_config_issues(doc, ctx, name)


_REDIS_TMP_DIR_RE = re.compile(r"""^\s*dir\s+["']?(/tmp|/var/tmp)(?=["'/\s]|$)""", re.MULTILINE)
_SENTINEL_DOWN_AFTER_RE = re.compile(r"^\s*sentinel\s+down-after-milliseconds\s+\S+\s+(\d+)", re.MULTILINE)


def _flag_redis_config_issues(doc: dict, ctx: MicroserviceContext, name: str) -> None:
    """Advisory lint for a redis.conf / sentinel.conf carried in a ConfigMap.
    Report-only: the value is converted unchanged either way."""
    for key, text in (doc.get("data") or {}).items():
        if not isinstance(text, str):
            continue
        m = _REDIS_TMP_DIR_RE.search(text)
        if m:
            ctx.warnings.append(
                f"[P1] ConfigMap/{name} key '{key}': redis `dir {m.group(1)}` is ephemeral "
                f"tmpfs — RDB/AOF data is lost on every restart")
        for m in _SENTINEL_DOWN_AFTER_RE.finditer(text):
            if int(m.group(1)) < 5000:
                ctx.warnings.append(
                    f"[P1] ConfigMap/{name} key '{key}': sentinel down-after-milliseconds "
                    f"{m.group(1)} < 5000 — aggressive failover on a brief GC or network pause")


def handle_secret(doc: dict, ctx: MicroserviceContext, name: str) -> None:
    entry = {"type": doc.get("type", "Opaque")}
    if doc.get("data"):
        entry["data"] = doc["data"]
    if doc.get("stringData"):
        entry["stringData"] = doc["stringData"]
    meta = doc.get("metadata", {}) or {}
    if meta.get("labels"):
        entry["labels"] = raw_labels(doc)
    ann = clean_annotations(meta.get("annotations"))
    if ann:
        entry["annotations"] = ann
    ctx.values.setdefault("secrets", {})[name] = entry


# Set from --external-secrets-api-version. Empty means "carry each source
# manifest's own apiVersion through", which is right when the manifests came
# from the same cluster they are going back to. When the TARGET cluster runs a
# different External Secrets Operator than the source did — manifests exported
# from a v1 cluster being deployed to an on-prem one that only serves v1beta1,
# or the reverse — the source's value is the wrong one to keep, and this pins
# every ESO resource in the run to what the target actually serves.
ESO_API_VERSION_OVERRIDE = ""

# TRUE by default; --cluster-scoped opts out. Keeps output inside the
# namespace boundary: no global defaults.yaml at the tree root, and no
# separate cluster-shared release — cluster-scoped objects are emitted into
# the namespace that declared them instead. See handle_cluster_scoped.
NAMESPACE_SCOPED_ONLY = True
# --argocd-manifests: write the root ApplicationSet / Application (and the
# cluster-shared Application). Off = the values tree only.
ARGOCD_MANIFESTS = False
# Written into the ArgoCD wiring when --values-repo-url is not given: an obvious
# "fill me in" rather than a plausible URL that silently points nowhere.
VALUES_REPO_PLACEHOLDER = "<values-repo-url>"
# The env list lives in the release's values like everything else, so one the
# same in every namespace lands in base/ once and only a namespace whose list
# differs carries it in values/. --env-in-values writes every namespace's env
# list into its own values/ file instead.
ENV_IN_VALUES = False
# --exact-copy: render every migrated pod template byte-for-byte as it is live,
# so the cutover sync restarts no pods. That costs readability — list-order
# keys nobody wrote (portOrder, volumeOrder, sidecarOrder) and an empty
# `securityContext: {}` kept only because the API server stored it — so it is
# for workloads that cannot take a restart. Off, the values say what the app
# needs and the first sync rolls the pods once. Orders that carry MEANING are
# kept either way: initContainerOrder (run order) and envFromOrder (the later
# ref wins a shared key). env and volumeMounts are lists, always in the
# deployment's own order.
EXACT_COPY = False
# The --exact-copy flag as given: True / False for every release, None (the
# default) for the stateful ones only (is_stateful). build_microservice sets
# EXACT_COPY from it per release.
EXACT_COPY_FLAG: bool | None = None
# Order keys whose only job is the byte-identical pod template above.
COSMETIC_ORDER_KEYS = frozenset({"portOrder", "volumeOrder", "sidecarOrder"})

# --exclude / --keep: case-insensitive substrings matched against an object's
# metadata.name and its label/annotation VALUES — deliberately never its data
# payload, so a kept microservice whose ConfigMap merely carries a kafka URL is
# not dropped by `--exclude kafka`. --keep wins over --exclude.
#
# --include is the allow-list the other two lack: a microservice file is
# converted only when something in it matches. --exclude still wins over it.
#
# A pattern starting with `=` is an exact object NAME instead of a substring
# (`=web` picks Deployment/web, not web-worker) — what a picker listing
# workloads by name needs, and what typing `--exclude web` could never say.
# `key=value` is one exact label (`app.kubernetes.io/part-of=payments`).
EXCLUDE_PATTERNS: list[str] = []
KEEP_PATTERNS: list[str] = []
INCLUDE_PATTERNS: list[str] = []


def matches_patterns(doc: dict, patterns: list[str]) -> bool:
    meta = doc.get("metadata") or {}
    name = str(meta.get("name") or "")
    strings = [name]
    strings += [str(v) for v in (meta.get("labels") or {}).values()]
    strings += [str(v) for v in clean_annotations(meta.get("annotations")).values()]
    strings = [s.lower() for s in strings]
    labels = meta.get("labels") or {}
    for p in patterns:
        if p.startswith("="):
            if p[1:] == name:
                return True
        elif "=" in p:
            # `key=value`: that exact label, the way a selector would ask for it.
            key, _, value = p.partition("=")
            if str(labels.get(key.strip(), "\x00")) == value.strip():
                return True
        elif any(p.lower() in s for s in strings):
            return True
    return False


def is_excluded(doc: dict) -> bool:
    if not EXCLUDE_PATTERNS:
        return False
    return matches_patterns(doc, EXCLUDE_PATTERNS) and not matches_patterns(doc, KEEP_PATTERNS)


ROUTING_KINDS = ("Ingress", "Route")


def _object_refs(doc: dict) -> set[tuple[str, str]]:
    """Every (kind, name) one object points at by name — the forward edges of
    place_shared_by_use's graph."""
    refs = {(k, n) for k, n, _ in collect_object_references(doc)}
    spec = doc.get("spec") or {}
    kind = doc.get("kind")

    def walk(node):
        if isinstance(node, dict):
            claim = node.get("persistentVolumeClaim")
            if isinstance(claim, dict) and isinstance(claim.get("claimName"), str):
                refs.add(("PersistentVolumeClaim", claim["claimName"]))
            for s in node.get("imagePullSecrets") or []:
                if isinstance(s, dict) and isinstance(s.get("name"), str):
                    refs.add(("Secret", s["name"]))
            svc = node.get("service")
            if kind == "Ingress" and isinstance(svc, dict) and isinstance(svc.get("name"), str):
                refs.add(("Service", svc["name"]))
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(spec)
    if kind in ("RoleBinding", "ClusterRoleBinding"):
        ref = doc.get("roleRef") or {}
        if ref.get("name"):
            refs.add((ref.get("kind") or "Role", ref["name"]))
    if kind == "ExternalSecret":
        store = spec.get("secretStoreRef") or {}
        if store.get("name"):
            refs.add((store.get("kind") or "SecretStore", store["name"]))
    if kind in ("SecretStore", "ClusterSecretStore"):
        refs |= {(k, n) for k, n, _ in _collect_ref_targets(spec)}
        sa = ((((spec.get("provider") or {}).get("vault") or {}).get("auth") or {})
              .get("kubernetes") or {}).get("serviceAccountRef") or {}
        if sa.get("name"):
            refs.add(("ServiceAccount", sa["name"]))
    return refs


def place_shared_by_use(shared_docs: list[dict], kept: list[tuple[str, list[dict]]],
                        skipped: list[list[dict]]) -> tuple[list[dict], list[str]]:
    """With --include / --exclude, `shared` holds only what at least TWO of the
    microservices still being converted use. Each shared object is placed by
    who uses it among the kept ones — two or more: stays shared; one: moves
    into that microservice (appended to `kept` in place); none, but a skipped
    one does: dropped. One nobody uses at all (argocd-cm, read through the
    API) stays when the run includes it by pattern, or includes nothing and
    excludes something else; otherwise it is dropped.

    "Uses" is followed through the objects: a pod's mounts/env/pull secrets/
    claims/ServiceAccount; a Service selecting its pods; an Ingress or Route
    to that Service; a RoleBinding for its ServiceAccount, and the Role it
    binds; an HPA/PDB on it; an ExternalSecret writing a Secret it reads, and
    that store's own SA/CA. Returns report lines."""
    owners = [(stem, docs) for stem, docs in kept] + [(None, docs) for docs in skipped]

    def pod_specs(docs):
        for d in docs:
            spec = d.get("spec") or {}
            if d.get("kind") == "CronJob":
                spec = (spec.get("jobTemplate") or {}).get("spec") or {}
            if d.get("kind") in WORKLOAD_KINDS | {"Job", "CronJob"}:
                tpl = spec.get("template") or {}
                yield d, (tpl.get("metadata") or {}).get("labels") or {}, tpl.get("spec") or {}

    # Who uses each (kind, name): a set of owner indexes (kept or skipped).
    users: dict[tuple[str, str], set[int]] = defaultdict(set)
    sa_users: dict[str, set[int]] = defaultdict(set)
    for i, (_, docs) in enumerate(owners):
        for d in docs:
            # A microservice uses what it declares itself, too — so a shared
            # Ingress to its own Service finds it.
            users[(d.get("kind"), (d.get("metadata") or {}).get("name"))].add(i)
            for ref in _object_refs(d):
                users[ref].add(i)
        for d, _, pod in pod_specs(docs):
            sa = pod.get("serviceAccountName") or pod.get("serviceAccount") or "default"
            sa_users[sa].add(i)
            users[("ServiceAccount", sa)].add(i)
            users[(d.get("kind"), (d.get("metadata") or {}).get("name"))].add(i)

    def reverse_users(doc) -> set[int]:
        """Users found from the object's side: what it selects or targets."""
        kind, spec = doc.get("kind"), doc.get("spec") or {}
        out: set[int] = set()
        if kind == "Service" and spec.get("selector"):
            sel = spec["selector"]
            out |= {i for i, (_, docs) in enumerate(owners)
                    for _, labels, _ in pod_specs(docs) if all(labels.get(k) == v for k, v in sel.items())}
        if kind == "PodDisruptionBudget":
            sel = (spec.get("selector") or {}).get("matchLabels") or {}
            if sel:
                out |= {i for i, (_, docs) in enumerate(owners)
                        for _, labels, _ in pod_specs(docs) if all(labels.get(k) == v for k, v in sel.items())}
        if kind == "HorizontalPodAutoscaler":
            ref = spec.get("scaleTargetRef") or {}
            out |= users.get((ref.get("kind"), ref.get("name")), set())
        if kind in ("RoleBinding", "ClusterRoleBinding"):
            for subj in doc.get("subjects") or []:
                if subj.get("kind") == "ServiceAccount":
                    out |= sa_users.get(subj.get("name"), set()) | users.get(("ServiceAccount", subj.get("name")), set())
        if kind == "ExternalSecret":
            target = (spec.get("target") or {}).get("name") or (doc.get("metadata") or {}).get("name")
            out |= users.get(("Secret", target), set())
        if kind in ROUTING_KINDS:  # belongs to whoever owns the Service it routes to
            for ref in _object_refs(doc):
                if ref[0] == "Service":
                    out |= users.get(ref, set())
        return out

    key = lambda d: (d.get("kind"), (d.get("metadata") or {}).get("name"))
    used: dict[tuple, set[int]] = {key(d): set(users.get(key(d), set())) for d in shared_docs}
    # To a fixed point: what a shared object points at is used by its users
    # (a Role by its binding's, a store by its ExternalSecret's) — except a
    # route, which points the other way: it is used BY its Service's users.
    changed = True
    while changed:
        changed = False
        for d in shared_docs:
            k = key(d)
            new = used[k] | reverse_users(d)
            for other in shared_docs:
                if other.get("kind") not in ROUTING_KINDS and k in _object_refs(other):
                    new |= used[key(other)]
            if new != used[k]:
                used[k] = new
                users[k] |= new
                changed = True

    n_kept = len(kept)
    stay, notes = [], []
    for d in shared_docs:
        k = key(d)
        live = sorted(i for i in used[k] if i < n_kept)
        label = f"{k[0]}/{k[1]}"
        if len(live) >= 2:
            stay.append(d)
        elif len(live) == 1:
            stem, docs = kept[live[0]]
            if k not in {key(x) for x in docs}:
                docs.append(d)
            notes.append(f"shared {label}: only '{stem}' still uses it — moved into that microservice")
        elif used[k]:
            notes.append(f"shared {label}: used only by microservices left out of this run — dropped")
        elif (INCLUDE_PATTERNS and matches_patterns(d, INCLUDE_PATTERNS)) or \
                (not INCLUDE_PATTERNS and not is_excluded(d)) or matches_patterns(d, KEEP_PATTERNS):
            stay.append(d)
        else:
            notes.append(f"shared {label}: used by no microservice and not named by --include — dropped")
    return stay, notes


def unify_shared_placement(loaded: dict) -> None:
    """One object, one placement, in every namespace it appears in.

    `shared` is decided per namespace — the importer files an object there when
    two of that namespace's workloads use it, and promote_shared_candidates
    lifts what two microservice files declare identically. So the same
    ConfigMap is in `shared` in the namespace where a second workload happens
    to mount it, and in its microservice's file in the next. The base/ merge
    then sees two unrelated objects: nothing about it is common, and it is
    written out per namespace.

    The rule: an object (kind + name, env words aside — _env_stem) stays in
    `shared` only where it is shared in EVERY namespace that has it. Mixed, it
    goes to the microservice that owns it elsewhere (the most common owner) in
    each namespace where it was shared:

      * from shared.yaml, it moves into that microservice's file;
      * as a would-be promotion (several files declaring it identically), the
        owner keeps its copy and the others drop theirs and reference it by
        name — what they would have done with the shared copy.

    A namespace with no such microservice, or whose duplicates differ, keeps
    the old placement and says why. Kinds are promotion's own (raw-named,
    no Service/Route: a Service carries its release's selector). Mutates
    `loaded` (namespace -> (shared_docs, ms_docs, warnings)) in place."""
    kinds = set(NS_RAW_NAME_KINDS) - _NEVER_PROMOTE_KINDS
    ident = lambda d: (d.get("kind"), _env_stem(str((d.get("metadata") or {}).get("name") or "")))
    eligible = lambda d: (d.get("kind") in kinds and (d.get("metadata") or {}).get("name")
                          and not (d.get("kind") == "Secret" and is_auto_sa_secret(d)))

    # Where each object sits, per namespace: "shared", or the one microservice.
    placement: dict[tuple, dict[str, str]] = defaultdict(dict)
    for ns, (shared_docs, ms_docs, _) in loaded.items():
        declarers: dict[tuple, set[str]] = defaultdict(set)
        for ms, docs in ms_docs:
            for d in docs:
                if eligible(d):
                    declarers[ident(d)].add(ms)
        for k, who in declarers.items():
            placement[k][ns] = "shared" if len(who) > 1 else next(iter(who))
        for d in shared_docs or []:
            if eligible(d):
                placement[ident(d)][ns] = "shared"

    for k, by_ns in sorted(placement.items()):
        owners = Counter(v for v in by_ns.values() if v != "shared")
        if not owners or "shared" not in by_ns.values():
            continue  # one placement everywhere already
        owner = sorted(owners.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        label = f"{k[0]}/{k[1]}"
        for ns, where in sorted(by_ns.items()):
            if where != "shared":
                continue
            shared_docs, ms_docs, warns = loaded[ns]
            target = next((docs for ms, docs in ms_docs if ms == owner), None)
            if target is None:
                warns.append(f"{label}: in '{owner}' in other namespaces, but there is no '{owner}' here — "
                             f"left in the shared release")
                continue
            moving = [d for d in shared_docs or [] if eligible(d) and ident(d) == k]
            if moving:
                loaded[ns] = ([d for d in shared_docs if not any(d is m for m in moving)] or None, ms_docs, warns)
                target.extend(m for m in moving if ident(m) not in {ident(x) for x in target if eligible(x)})
                warns.append(f"{label}: in '{owner}' in other namespaces — moved out of the shared release "
                             f"into '{owner}' here too, so base/ sees one object")
                continue
            copies = [(ms, d) for ms, docs in ms_docs for d in docs if eligible(d) and ident(d) == k]
            if owner not in {ms for ms, _ in copies}:
                warns.append(f"{label}: in '{owner}' in other namespaces, but '{owner}' does not declare it "
                             f"here — left to the shared release")
                continue
            if len({content_hash(d) for _, d in copies}) != 1:
                continue  # differing copies are two objects: the registry's rename path
            for ms, docs in ms_docs:
                if ms != owner:
                    docs[:] = [d for d in docs if not (eligible(d) and ident(d) == k)]
            warns.append(f"{label}: in '{owner}' in other namespaces — kept in '{owner}' here too "
                         f"(not promoted to shared); {', '.join(sorted(ms for ms, _ in copies if ms != owner))} "
                         f"reference it by name")


# (source file stem, kind, name) per Kubernetes namespace: raw objects the run
# was told to leave out (--include / --exclude), so the fidelity check does not
# count them as lost. Filled by main()'s Pass 0.
LEFT_OUT: dict[str, set[tuple[str, str, str]]] = {}


def _kn(doc: dict) -> tuple[str, str]:
    return doc.get("kind"), (doc.get("metadata") or {}).get("name")


def is_included(docs: list[dict]) -> bool:
    """--include: whether a microservice FILE is selected. Without the flag,
    every file is. shared.yaml is never filtered by it — what several
    microservices share is kept whichever of them are picked."""
    return not INCLUDE_PATTERNS or any(matches_patterns(d, INCLUDE_PATTERNS) for d in docs)


def record_eso_api_version(doc: dict, ctx: MicroserviceContext) -> None:
    """Carry the SOURCE manifest's external-secrets.io apiVersion through.

    The chart renders every SecretStore/ClusterSecretStore/ExternalSecret from
    one `externalSecretsApiVersion` value, whose default is
    external-secrets.io/v1. Never setting it meant a v1beta1 source manifest
    silently converted to v1 — which applies fine against a recent External
    Secrets Operator and fails outright against an older one that only serves
    v1beta1 ("no matches for kind ExternalSecret in version
    external-secrets.io/v1"). Reading it from the source keeps the converted
    output deployable on the same cluster the manifests came from.

    It is a single value for the whole release, so a release mixing versions
    can only get one; that is the chart's shape, and mixing them in one
    microservice is itself a mistake worth surfacing rather than hiding.
    """
    api_version = doc.get("apiVersion")
    if not api_version or not api_version.startswith("external-secrets.io/"):
        return
    if ESO_API_VERSION_OVERRIDE:
        ctx.values["externalSecretsApiVersion"] = ESO_API_VERSION_OVERRIDE
        return
    existing = ctx.values.get("externalSecretsApiVersion")
    if existing and existing != api_version:
        ctx.warnings.append(
            f"{doc.get('kind')}/{(doc.get('metadata') or {}).get('name')}: apiVersion "
            f"'{api_version}' differs from '{existing}' already used by another External "
            f"Secrets resource in this microservice — the chart renders them all from one "
            f"externalSecretsApiVersion value, so '{existing}' wins. Split them into "
            f"separate microservices, or align the source manifests."
        )
        return
    ctx.values["externalSecretsApiVersion"] = api_version


def _eso_metadata(doc: dict) -> dict:
    """metadata that every External Secrets object needs carried, and that none
    of the ESO builders used to read.

    `labels` matters because these objects carry the same custom governance
    metadata as everything else — team / environment / critical — and none of the
    ESO builders read them.

    NOTE the one label that is deliberately NOT among them.
    `argocd.argoproj.io/instance` is in CHART_OWNED_LABELS: ArgoCD stamps it at
    sync time, naming the Application that owns the object, and a chart cannot
    know which Application that will be. This docstring used to argue the
    opposite — "lose it and ArgoCD stops managing the ExternalSecret" — which
    describes stripping the label off a LIVE object, not a chart declining to
    invent one. Rendering a guess is what would actually break tracking.

    `ownerReferences` is deliberately NOT here, even though the report asks for
    it. sanitize_object strips it from every object before any handler runs
    (VOLATILE_METADATA_KEYS), and that is correct: an ownerReference is a
    `uid`, and a uid from the SOURCE cluster does not exist in the target. Carry
    it over and the API server's garbage collector sees an owner that is gone
    and deletes the object, usually within seconds. Dropping it does not break
    garbage collection — keeping it breaks the object. The chart templates still
    render an `ownerReferences` key if a values file sets one by hand, which is
    the only way it can be meaningful.
    """
    out: dict = {}
    meta = doc.get("metadata") or {}
    if meta.get("labels"):
        out["labels"] = raw_labels(doc)
    ann = clean_annotations(meta.get("annotations"))
    if ann:
        out["annotations"] = ann
    return out


def _eso_spec(spec: dict) -> dict:
    """Every spec key, not a whitelist.

    These builders used to copy a fixed list — provider/retrySettings/
    refreshInterval/conditions for a store, secretStoreRef/refreshInterval/
    target/data/dataFrom for an ExternalSecret — and silently drop the rest.
    `deliverySpec` and `managementPolicy` were the two that showed up in the
    report, but the whitelist is the defect: every field the External Secrets
    Operator adds from here on would vanish the same way, with no warning and
    nothing in the fidelity report to say which one.

    Carrying everything through is safe in a way the whitelist is not: the
    source of these values is a raw manifest the cluster already accepted, so
    anything present is a field the CRD already validates.
    """
    return {k: v for k, v in (spec or {}).items() if v is not None}


def build_secretstore_entry(doc: dict) -> dict:
    entry = _eso_spec(doc.get("spec", {}) or {})
    entry.update(_eso_metadata(doc))
    return entry


def handle_secretstore(doc: dict, ctx: MicroserviceContext, name: str) -> None:
    record_eso_api_version(doc, ctx)
    ctx.values.setdefault("secretStores", {})[name] = build_secretstore_entry(doc)


def build_externalsecret_entry(doc: dict, ctx: MicroserviceContext) -> dict:
    spec = doc.get("spec", {}) or {}
    entry = _eso_spec(spec)
    ssr = spec.get("secretStoreRef") or {}
    if ssr.get("name"):
        ref_kind = ssr.get("kind", "SecretStore")
        ref_name = ssr["name"]
        if ref_kind == "SecretStore":
            # Remap through the same name-claiming pass as ConfigMap/Secret/
            # Service, in case the target SecretStore got renamed by
            # collision resolution. ClusterSecretStore names are deduped
            # globally without renaming, so no remap needed for that kind.
            ref_name = ctx.name_map["SecretStore"].get(ref_name, ref_name)
        entry["secretStoreRef"] = {"name": ref_name, "kind": ref_kind}
    # `target` used to be filtered to four keys here, which dropped anything
    # else ESO puts under it. _eso_spec already carried it through whole, so
    # there is nothing left to do but leave it alone.
    entry.update(_eso_metadata(doc))
    return entry


def handle_externalsecret(doc: dict, ctx: MicroserviceContext, name: str) -> None:
    record_eso_api_version(doc, ctx)
    ctx.values.setdefault("externalSecrets", {})[name] = build_externalsecret_entry(doc, ctx)


def build_clustersecretstore_entry(doc: dict) -> dict:
    # Same whitelist, same fix as build_secretstore_entry — no folder in
    # ERRORS_ANALYSIS names ClusterSecretStore, but leaving the identical
    # dropped-field bug in the function next door would only mean rediscovering
    # it later. Metadata is deliberately NOT carried: a ClusterSecretStore is
    # cluster-scoped and deduped globally across namespaces, so one namespace's
    # labels and ownerReferences are not the shared object's to set.
    return _eso_spec(doc.get("spec", {}) or {})


def handle_pvc(doc: dict, ctx: MicroserviceContext, name: str,
               known_pv_names: set[str] | None = None) -> None:
    spec = doc.get("spec", {}) or {}
    entry = {"accessModes": spec.get("accessModes", ["ReadWriteOnce"])}
    size = ((spec.get("resources") or {}).get("requests") or {}).get("storage")
    if size:
        entry["size"] = size
    for k in ("storageClassName", "volumeMode", "selector", "dataSource"):
        if spec.get(k):
            entry[k] = spec[k]
    # spec.volumeName is a STATIC binding to one hand-made PersistentVolume.
    # Carried to a cluster where that PV does not exist, the claim is "bound to
    # non-existent persistentvolume" forever and every pod mounting it never
    # schedules (the redisinsight case). Kept only when the dump contains that
    # PV; None means the caller has no PV inventory, so it is kept as before.
    vol = spec.get("volumeName")
    if vol:
        if known_pv_names is None or vol in known_pv_names:
            entry["volumeName"] = vol
        else:
            ctx.warnings.append(
                f"[P0] PersistentVolumeClaim/{name}: spec.volumeName '{vol}' names a "
                f"PersistentVolume that is not in the dump — dropped so the claim can bind "
                f"normally instead of staying Pending forever")
    ann = clean_annotations((doc.get("metadata") or {}).get("annotations"))
    if ann:
        entry["annotations"] = ann
    if raw_labels(doc):
        entry["labels"] = raw_labels(doc)
    ctx.values.setdefault("pvc", {})[name] = entry


def handle_role(doc: dict, ctx: MicroserviceContext, name: str) -> None:
    entry = {"rules": doc.get("rules", [])}
    if raw_labels(doc):
        entry["labels"] = raw_labels(doc)
    ann = clean_annotations((doc.get("metadata") or {}).get("annotations"))
    if ann:
        entry["annotations"] = ann
    ctx.values.setdefault("rbac", {}).setdefault("roles", {})[name] = entry


def handle_rolebinding(doc: dict, ctx: MicroserviceContext, name: str) -> None:
    role_ref_name = (doc.get("roleRef") or {}).get("name", "")
    final_role = ctx.name_map["Role"].get(role_ref_name, role_ref_name)
    subjects = []
    for s in doc.get("subjects", []) or []:
        s = dict(s)
        # A subject in the binding's own namespace is the default, and dropping
        # it keeps the release portable across namespaces. One naming ANOTHER
        # namespace (argo-workflows binds `default`'s workflow SA) is kept:
        # dropped, the binding silently granted this namespace's SA instead.
        if s.get("namespace") in (None, k8s_ns(ctx.namespace)):
            s.pop("namespace", None)
        if s.get("kind") == "ServiceAccount":
            s["name"] = ctx.name_map["ServiceAccount"].get(s["name"], s["name"])
        subjects.append(s)
    entry = {"roleRef": final_role, "subjects": subjects}
    if raw_labels(doc):
        entry["labels"] = raw_labels(doc)
    ann = clean_annotations((doc.get("metadata") or {}).get("annotations"))
    if ann:
        entry["annotations"] = ann
    ctx.values.setdefault("rbac", {}).setdefault("roleBindings", {})[name] = entry


def _carry_automount(ctx: MicroserviceContext, sa: dict) -> dict:
    """Preserve a pod-spec-level automountServiceAccountToken across this
    wholesale replacement of ctx.values['serviceAccount'].

    handle_workload records the pod's own setting into that same key (the
    chart renders both the ServiceAccount's and the pod's from it), but the
    docs are processed in file order — so a ServiceAccount appearing after
    the workload replaced the whole dict and silently dropped the pod's
    value, flipping an explicit `false` back to the chart's default `true`.
    The ServiceAccount's own setting, if it has one, still wins: it was
    applied by the caller before this runs.
    """
    prior = (ctx.values.get("serviceAccount") or {}).get("automountServiceAccountToken")
    if prior is not None and "automountServiceAccountToken" not in sa:
        sa["automountServiceAccountToken"] = prior
    return sa


def handle_serviceaccount(doc: dict, ctx: MicroserviceContext, name: str) -> None:
    action = ctx.claim_actions["ServiceAccount"].get(name, "own")
    final = ctx.name_map["ServiceAccount"].get(name, name)
    # A ServiceAccount no pod in this file runs as — typically the Vault auth SA
    # a SecretStore names, which the importer files next to its only user —
    # must not become the chart's `serviceAccount`: the chart's
    # serviceAccountName helper would then move every pod here onto it. It is
    # carried verbatim instead, and pods that ran as `default` stay there (the
    # chart's default, create: false) rather than handed a chart-made SA.
    unused_by_pods = ctx.pod_sa_names is not None and name not in ctx.pod_sa_names

    if action == "reference_shared":
        if not ctx.primary_sa_set and not unused_by_pods:
            ctx.primary_sa_set = True
            ctx.values["serviceAccount"] = _carry_automount(ctx, {"name": final})
        return

    if not ctx.primary_sa_set and not unused_by_pods:
        ctx.primary_sa_set = True
        sa = {"create": True, "name": final}
        if raw_labels(doc):
            sa["labels"] = raw_labels(doc)
        # Registry credentials, but ONLY the ones somebody added on purpose.
        # OpenShift's integrated registry controller stamps every SA with
        # `<sa>-dockercfg-XXXXX`, naming a Secret it also creates; on a vanilla
        # cluster that Secret never exists, so carrying the reference would
        # point the SA at an object that never appears. A hand-added private
        # registry credential is the opposite case and must survive — without
        # it every pod using this SA fails ImagePullBackOff.
        custom_pull = [
            s for s in (doc.get("imagePullSecrets") or [])
            if s.get("name") and not AUTO_SA_SECRET_RE.match(s["name"])
        ]
        if custom_pull:
            sa["imagePullSecrets"] = custom_pull
        # `secrets:` is never carried. Those are ServiceAccount token
        # references that Kubernetes creates and rotates itself; declaring them
        # from a chart would fight the controller that owns them, and they are
        # regenerated on the target cluster regardless.
        ann = clean_annotations((doc.get("metadata") or {}).get("annotations"))
        if ann:
            sa["annotations"] = ann
        if "automountServiceAccountToken" in doc:
            sa["automountServiceAccountToken"] = doc["automountServiceAccountToken"]
            ctx.sa_automount_from_doc = True
        ctx.values["serviceAccount"] = _carry_automount(ctx, sa)
    else:
        extra = copy.deepcopy(doc)
        extra["metadata"]["name"] = final
        ctx.extra_deploy.append(as_extra_deploy(extra))
        why = ("no pod in this release runs as it" if unused_by_pods else
               "chart supports one managed ServiceAccount per release")
        ctx.warnings.append(
            f"ServiceAccount/{name}: {why}; "
            f"extra ServiceAccount emitted via extraDeploy as '{final}'"
        )


def handle_hpa(doc: dict, ctx: MicroserviceContext) -> None:
    spec = doc.get("spec", {}) or {}
    hpa = {"enabled": True}
    if spec.get("minReplicas") is not None:
        hpa["minReplicas"] = spec["minReplicas"]
    if spec.get("maxReplicas") is not None:
        hpa["maxReplicas"] = spec["maxReplicas"]
    if spec.get("metrics"):
        hpa["metrics"] = spec["metrics"]
    if spec.get("behavior"):
        hpa["behavior"] = spec["behavior"]
    ann = clean_annotations((doc.get("metadata") or {}).get("annotations"))
    if ann:
        hpa["annotations"] = ann
    ctx.values["hpa"] = hpa


def handle_pdb(doc: dict, ctx: MicroserviceContext) -> None:
    """Verbatim via extraDeploy, not the chart's single `pdb` slot.

    That slot renders ONE PDB named after the release with the chart's own
    selector: the raw metadata.name and spec.selector were dropped, a second PDB
    in the same file overwrote the first, and on a workload.type none release
    the chart's validation rejected it outright."""
    name = (doc.get("metadata") or {}).get("name")
    ctx.extra_deploy.append(as_extra_deploy(doc))
    ctx.warnings.append(
        f"PodDisruptionBudget/{name}: emitted verbatim via extraDeploy (keeps its own name "
        f"and selector, and allows more than one per release)")


def handle_ingress(doc: dict, ctx: MicroserviceContext) -> None:
    spec = doc.get("spec", {}) or {}
    ing = {"enabled": True}
    if spec.get("ingressClassName"):
        ing["className"] = spec["ingressClassName"]
    hosts = []
    backend_svc = None
    for rule in spec.get("rules", []) or []:
        h = {"host": rule.get("host", "")}
        paths = []
        for p in (rule.get("http", {}) or {}).get("paths", []) or []:
            svc = ((p.get("backend") or {}).get("service") or {})
            port = svc.get("port") or {}
            port_name = port.get("name")
            if svc.get("name") and backend_svc is None:
                backend_svc = svc["name"]
            entry = {"path": p.get("path", "/"), "pathType": p.get("pathType", "Prefix"), "portName": port_name}
            # A port addressed by NUMBER has no name to carry; `portName: null`
            # made the chart fall back to the primary Service's first port, or
            # fail outright when this release has none (Harbor's shared Ingress).
            if port_name is None and port.get("number") is not None:
                del entry["portName"]
                entry["portNumber"] = port["number"]
            # A path routed to a different Service than the Ingress's first
            # backend keeps its own — Harbor sends / to the portal and /api to core.
            if svc.get("name") and svc["name"] != backend_svc:
                entry["serviceName"] = ctx.name_map["Service"].get(svc["name"], svc["name"])
            paths.append(entry)
        h["paths"] = paths
        hosts.append(h)
    # Which Service the Ingress routes to by default: the first backend (a path
    # naming another one carries its own serviceName above) — and it must be carried:
    # once the primary Service keeps its raw '<app>-service' name, an Ingress
    # still pointing at the release fullname routes to nothing. Remapped
    # through name_map in case collision resolution renamed the Service.
    if backend_svc:
        ing["serviceName"] = ctx.name_map["Service"].get(backend_svc, backend_svc)
    if hosts:
        ing["hosts"] = hosts
    if spec.get("tls"):
        ing["tls"] = spec["tls"]
    ann = clean_annotations((doc.get("metadata") or {}).get("annotations"))
    if ann:
        ing["annotations"] = ann
    ctx.values["ingress"] = ing


def handle_nad(doc: dict, ctx: MicroserviceContext) -> None:
    """NetworkAttachmentDefinition (Multus). The chart has no template for it, so
    it is carried verbatim via extraDeploy — on purpose, not through the
    'Unrecognized kind' fallback. Its name is recorded for cross_reference_checks."""
    ctx.extra_deploy.append(as_extra_deploy(doc))
    ctx.nad_names.add((doc.get("metadata") or {}).get("name"))


def handle_vpa(doc: dict, ctx: MicroserviceContext) -> None:
    spec = doc.get("spec", {}) or {}
    vpa = {"enabled": True}
    up = spec.get("updatePolicy", {}) or {}
    if up.get("updateMode"):
        vpa["updateMode"] = up["updateMode"]
    if spec.get("resourcePolicy"):
        vpa["resourcePolicy"] = spec["resourcePolicy"]
    ann = clean_annotations((doc.get("metadata") or {}).get("annotations"))
    if ann:
        vpa["annotations"] = ann
    ctx.values["vpa"] = vpa


def short_subresource_name(raw_name: str, ctx: MicroserviceContext) -> str:
    """CronJob/Job source manifests conventionally name themselves
    "<microservice>-<suffix>" (e.g. "analytics-svc-batch-1-backup"). The
    chart's CronJob/Job templates already prepend the microservice's own
    fullname to this map key, so keeping the full raw name here doubles it
    ("analytics-svc-batch-1-analytics-svc-batch-1-backup") and can blow past
    Kubernetes' 52-char CronJob name limit. Strip the redundant prefix so the
    map is keyed by just the meaningful suffix ("backup")."""
    # STRICT NAMING: returns the raw name verbatim.
    #
    # This used to strip a leading "<microservice>-" so the chart could re-add
    # its own fullname prefix and arrive back at the original. That round-tripped
    # for the common case and broke in two others: a name that did NOT carry the
    # prefix came out with one bolted on, and two names differing only by the
    # prefix collapsed onto one map key (the whole of folders 13 and 14).
    #
    # Carrying the full name through and rendering it verbatim is both what the
    # directive requires and strictly safer: `metadata.name` is unique per kind
    # per namespace by API rule, so a full-name key CANNOT collide — the entire
    # class of collision that claim_subresource_key exists to survive is gone.
    return raw_name


def rendered_subresource_name(key: str, ctx: MicroserviceContext) -> str:
    """The Kubernetes object name the chart will produce for a cronjobs/jobs
    map KEY — a mirror of `universal-chart.subresourceName` in _helpers.tpl:
    the release fullname when the key already IS the fullname, otherwise
    "<fullname>-<key>". Kept next to short_subresource_name because the two are
    inverses, and store_subresource below needs to know which raw names this
    chart can actually reproduce."""
    return ctx.name if key == ctx.name else f"{ctx.name}-{key}"


def claim_subresource_key(ctx: MicroserviceContext, top_key: str, raw_name: str) -> str:
    """Reserve a UNIQUE key in ctx.values[top_key] for one raw CronJob/Job and
    return it. The caller fills the entry in at that key.

    The key used to be short_subresource_name() alone, assigned with a bare
    `ctx.values.setdefault(top_key, {})[sub_name] = entry`. Since that helper
    strips a leading
    "<microservice>-" — two objects in one microservice file that differ only by
    that prefix (`nightly-report-cleanup` and `cleanup`) reduce to the same key
    and the second silently REPLACED the first. Both the object and every field
    on it were lost, and the survivor was then rendered under the lost object's
    Kubernetes name, because the chart re-adds the prefix. A CronJob therefore
    fired on the wrong day carrying another CronJob's schedule, command and env,
    with nothing anywhere saying so.

    Which entry gets the short key is decided by whether the chart can reproduce
    the raw name from it (rendered_subresource_name), NOT by document order — so
    reordering the manifests in a file cannot move a schedule from one object to
    another. The loser is kept under a disambiguated key and warned about: this
    chart names a subresource from its key alone, so two raw objects that reduce
    to one key cannot BOTH keep their names. A visibly renamed CronJob that still
    runs its own schedule is recoverable; a silently deleted one is not.
    """
    bucket = ctx.values.setdefault(top_key, {})
    short = short_subresource_name(raw_name, ctx)
    entitled = rendered_subresource_name(short, ctx) == raw_name

    if short not in bucket:
        bucket[short] = {}
        ctx.sub_key_owner[(top_key, short)] = (raw_name, entitled)
        return short

    def free_key(base: str) -> str:
        if base not in bucket:
            return base
        n = 2
        while f"{base}-{n}" in bucket:
            n += 1
        return f"{base}-{n}"

    held_by, held_entitled = ctx.sub_key_owner.get((top_key, short), (short, False))
    kind = "CronJob" if top_key == "cronjobs" else "Job"

    if entitled and not held_entitled:
        # The incoming object is the one whose name the chart can reproduce, so
        # it takes the short key and the squatter moves aside.
        moved = free_key(held_by)
        bucket[moved] = bucket.pop(short)
        # The image/env/raw-image trackers are keyed by (top_key, sub_name, ...)
        # and are read back by build_minimal_values long after this point, so a
        # moved entry has to take them with it — otherwise the evicted CronJob's
        # tag and literal env would be applied to whichever object now holds the
        # short key, which is the very swap this function exists to prevent.
        for tracker in (ctx.sub_full_image, ctx.sub_full_env, ctx.sub_raw_image):
            for path in [p for p in tracker if p[:2] == (top_key, short)]:
                tracker[(top_key, moved) + path[2:]] = tracker.pop(path)
        ctx.sub_key_owner[(top_key, moved)] = (held_by, held_entitled)
        ctx.sub_key_owner[(top_key, short)] = (raw_name, entitled)
        bucket[short] = {}
        loser, loser_key, mine = held_by, moved, short
    else:
        loser_key = free_key(raw_name)
        bucket[loser_key] = {}
        ctx.sub_key_owner[(top_key, loser_key)] = (raw_name, entitled)
        loser, mine = raw_name, loser_key

    ctx.warnings.append(
        # held_by was read first, raw_name second - so "the second" below is
        # unambiguous, and stays so if the eviction branch reversed which of
        # them ended up holding the short key.
        f"{kind}/{held_by} and {kind}/{raw_name} both reduce to the '{top_key}' key "
        f"'{short}' - name collision. The second used to silently overwrite the "
        f"first; both are kept now, but '{loser}' renders as "
        f"'{rendered_subresource_name(loser_key, ctx)}' rather than its own name."
    )
    return mine


JOB_POD_FIELDS = ("automountServiceAccountToken", "enableServiceLinks", "terminationGracePeriodSeconds",
                  "priorityClassName", "dnsPolicy", "hostAliases", "nodeSelector", "tolerations", "affinity")
JOB_CONTAINER_FIELDS = ("workingDir", "ports", "livenessProbe", "readinessProbe", "startupProbe", "lifecycle")


def carry_job_pod_fields(pod_spec: dict, main: dict, dest: dict) -> None:
    """The pod- and container-level fields a Job's own manifest sets that the
    chart's jobPodSpec renders per job — kept in ONE place so handle_job and
    handle_cronjob cannot drift. Admission-webhook and CRD-upgrade hooks pin
    themselves to Linux nodes, opt out of service links or a token, and a
    descheduler CronJob runs system-cluster-critical with a liveness probe;
    each of those used to be dropped. `is not None`, so false and 0 survive.
    """
    for k in JOB_POD_FIELDS:
        val = pod_spec.get(k)
        if isinstance(val, dict):
            val = {sk: sv for sk, sv in val.items() if sv is not None}
        if val is not None and val != {} and val != []:
            dest[k] = val
    for k in JOB_CONTAINER_FIELDS:
        if main.get(k) not in (None, {}, []):
            dest[k] = main[k]


def handle_cronjob(doc: dict, ctx: MicroserviceContext, name: str) -> None:
    spec = doc.get("spec", {}) or {}
    jt = (spec.get("jobTemplate") or {}).get("spec") or {}
    pod_tpl = jt.get("template") or {}
    pod_tpl_spec = pod_tpl.get("spec") or {}
    pod_tpl_meta = pod_tpl.get("metadata") or {}
    containers = pod_tpl_spec.get("containers", []) or []
    main = containers[0] if containers else {}

    entry = {"schedule": spec.get("schedule", "0 0 * * *")}
    for k in ("concurrencyPolicy", "successfulJobsHistoryLimit", "failedJobsHistoryLimit",
              "suspend", "startingDeadlineSeconds"):
        if spec.get(k) is not None:
            entry[k] = spec[k]

    sub_name = claim_subresource_key(ctx, "cronjobs", name)

    job_template = {}
    # Same reasoning as handle_main_container: without this the chart names the
    # container after the cronjobs map KEY, so a raw `report` under a cronjob
    # keyed `nightly-report` renders as `nightly-report`.
    if main.get("name"):
        job_template["containerName"] = main["name"]
    # A CronJob commonly runs as a ServiceAccount that belongs to something
    # else — a shared backup/maintenance identity — which is NOT the release's
    # own SA. Uncaptured, jobPodSpec falls back to the release-level
    # ServiceAccount helper and the pod ends up naming an object that does not
    # exist: it never starts, and whatever RBAC was bound to the real SA never
    # applies. Remapped through name_map for the same reason handle_workload
    # does it — if the collision resolver renamed the SA object, this reference
    # has to follow it.
    job_sa = pod_tpl_spec.get("serviceAccountName") or pod_tpl_spec.get("serviceAccount")
    if job_sa:
        job_template["serviceAccountName"] = ctx.name_map["ServiceAccount"].get(job_sa, job_sa)
    # Pod-level and container-level securityContext, per job. Uncaptured, the
    # chart falls back to the RELEASE-level .Values.podSecurityContext, so a job
    # asking for runAsUser/fsGroup 1000 inherits the workload's value and can no
    # longer read files on a volume its own PVC owns as 1000. The container-level
    # one was not rendered by jobPodSpec at all — a total loss of the job's
    # hardening (allowPrivilegeEscalation, readOnlyRootFilesystem, capabilities),
    # not merely an override.
    if pod_tpl_spec.get("securityContext"):
        job_template["podSecurityContext"] = pod_tpl_spec["securityContext"]
    # An explicit {} only under --exact-copy — see handle_main_container.
    if main.get("securityContext") or (EXACT_COPY and isinstance(main.get("securityContext"), dict)):
        job_template["securityContext"] = main["securityContext"]
    carry_job_pod_fields(pod_tpl_spec, main, job_template)
    # `parallelism` and `completions` join the list rather than being captured
    # separately: the report rates parallelism P8 (its lowest) because the value
    # it saw was 1, the Kubernetes default. That reasoning holds for the value
    # observed and not for the field — a batch job declaring `parallelism: 4`
    # that silently converts to 1 quarters its throughput, and nothing in the
    # conversion says so. `completions` is the same field's other half and has
    # the same failure mode, so leaving it out would recreate this folder for
    # the next reader.
    #
    # `is not None` rather than truthiness, so an explicit 0 — a legal way to
    # suspend a Job — survives.
    for k in ("backoffLimit", "activeDeadlineSeconds", "ttlSecondsAfterFinished",
              "parallelism", "completions"):
        if jt.get(k) is not None:
            job_template[k] = jt[k]
    if pod_tpl_spec.get("restartPolicy"):
        job_template["restartPolicy"] = pod_tpl_spec["restartPolicy"]
    # Container-level, distinct from the pod-level line above — see the matching
    # comment in handle_main_container.
    if main.get("restartPolicy"):
        job_template["containerRestartPolicy"] = main["restartPolicy"]
    if main.get("image"):
        repo, tag, digest = split_image(main["image"])
        # Repository, tag and digest deliberately excluded here — same reasoning
        # as handle_main_container: they're the minimal override's job (see
        # sub_full_image below / build_minimal_values), not base/'s.
        # pullPolicy IS structural, so unlike them it belongs in base.
        # Without it jobPodSpec falls back to the RELEASE-level
        # .Values.image.pullPolicy, so a CronJob asking for `Always` silently
        # inherits the main workload's `IfNotPresent` and never re-pulls — on a
        # mutable tag that means it runs whatever the node last cached.
        if main.get("imagePullPolicy"):
            job_template["image"] = {"pullPolicy": main["imagePullPolicy"]}
        ctx.sub_full_image[("cronjobs", sub_name)] = {
            k: v for k, v in (("repository", repo), ("tag", tag), ("digest", digest)) if v
        }
    for k in ("command", "args"):
        if main.get(k):
            job_template[k] = main[k]
    if main.get("env"):
        if ENV_IN_VALUES:
            ctx.sub_full_env[("cronjobs", sub_name)] = env_list(main["env"], ctx)
        else:
            job_template["env"] = env_list(main["env"], ctx)
    if main.get("envFrom"):
        job_template["envFrom"] = remap_envfrom(envfrom_list_to_map(main["envFrom"]), ctx)
        set_order(job_template, "envFrom", "envFromOrder")
    if main.get("resources"):
        job_template["resources"] = main["resources"]
    if main.get("volumeMounts"):
        job_template["volumeMounts"] = copy.deepcopy(main["volumeMounts"])
    for extra in containers[1:]:
        remap_container_refs(extra, ctx)
        sc_entry = {k: val for k, val in extra.items() if k != "name"}
        if sc_entry.get("image"):
            ctx.sub_raw_image[("cronjobs", sub_name, "jobTemplate", "sidecars", extra["name"], "image")] = sc_entry.pop("image")
        job_template.setdefault("sidecars", {})[extra["name"]] = sc_entry
    set_order(job_template, "sidecars", "sidecarOrder")
    init_containers = pod_tpl_spec.get("initContainers", []) or []
    if init_containers:
        for ic in init_containers:
            remap_container_refs(ic, ctx)
        job_template["initContainers"], order = named_list_to_map_split_image(
            init_containers, ctx, ("cronjobs", sub_name, "jobTemplate", "initContainers"))
        if len(order) > 1:
            job_template["initContainerOrder"] = order
    if pod_tpl_spec.get("volumes"):
        job_template["volumes"] = remap_volumes(named_list_to_map(pod_tpl_spec["volumes"]), ctx)
        set_order(job_template, "volumes", "volumeOrder")
    # spec.jobTemplate.spec.template.metadata — the CronJob's POD labels, which
    # are a different thing from the CronJob object's own labels captured below.
    # handle_workload has read these since it was written; this handler never
    # did, and the template rendered only the two selector labels, so the five
    # governance labels on every CronJob pod vanished.
    if pod_template_labels(pod_tpl_meta):
        job_template["podLabels"] = pod_template_labels(pod_tpl_meta)
    if pod_template_annotations(pod_tpl_meta):
        job_template["podAnnotations"] = pod_template_annotations(pod_tpl_meta)
    entry["jobTemplate"] = job_template

    ann = clean_annotations((doc.get("metadata") or {}).get("annotations"))
    if ann:
        entry["annotations"] = ann
    if raw_labels(doc):
        entry["labels"] = raw_labels(doc)
    ctx.values["cronjobs"][sub_name] = entry


def handle_job(doc: dict, ctx: MicroserviceContext, name: str) -> None:
    spec = doc.get("spec", {}) or {}
    pod_tpl = spec.get("template") or {}
    pod_spec = pod_tpl.get("spec") or {}
    pod_meta = pod_tpl.get("metadata") or {}
    containers = pod_spec.get("containers", []) or []
    main = containers[0] if containers else {}

    sub_name = claim_subresource_key(ctx, "jobs", name)

    entry = {}
    # Same reasoning as handle_main_container / handle_cronjob.
    if main.get("name"):
        entry["containerName"] = main["name"]
    # See the matching comment in handle_cronjob.
    job_sa = pod_spec.get("serviceAccountName") or pod_spec.get("serviceAccount")
    if job_sa:
        entry["serviceAccountName"] = ctx.name_map["ServiceAccount"].get(job_sa, job_sa)
    # See the matching comment in handle_cronjob.
    if pod_spec.get("securityContext"):
        entry["podSecurityContext"] = pod_spec["securityContext"]
    # An explicit {} only under --exact-copy — see handle_main_container.
    if main.get("securityContext") or (EXACT_COPY and isinstance(main.get("securityContext"), dict)):
        entry["securityContext"] = main["securityContext"]
    carry_job_pod_fields(pod_spec, main, entry)
    # Same list as handle_cronjob, and kept in step with it deliberately: a Job
    # and a CronJob's jobTemplate are the same spec, and every time these two
    # have drifted apart it has produced a defect in one of them (see folder 5,
    # where pod-template labels were captured for workloads and neither of
    # these).
    for k in ("backoffLimit", "activeDeadlineSeconds", "ttlSecondsAfterFinished",
              "parallelism", "completions"):
        if spec.get(k) is not None:
            entry[k] = spec[k]
    if pod_spec.get("restartPolicy"):
        entry["restartPolicy"] = pod_spec["restartPolicy"]
    # See the matching comment in handle_cronjob.
    if main.get("restartPolicy"):
        entry["containerRestartPolicy"] = main["restartPolicy"]
    if main.get("image"):
        repo, tag, digest = split_image(main["image"])
        # Repository, tag and digest excluded — see the matching comment in handle_cronjob.
        if main.get("imagePullPolicy"):
            entry["image"] = {"pullPolicy": main["imagePullPolicy"]}
        ctx.sub_full_image[("jobs", sub_name)] = {
            k: v for k, v in (("repository", repo), ("tag", tag), ("digest", digest)) if v
        }
    for k in ("command", "args"):
        if main.get(k):
            entry[k] = main[k]
    if main.get("env"):
        if ENV_IN_VALUES:
            ctx.sub_full_env[("jobs", sub_name)] = env_list(main["env"], ctx)
        else:
            entry["env"] = env_list(main["env"], ctx)
    if main.get("envFrom"):
        entry["envFrom"] = remap_envfrom(envfrom_list_to_map(main["envFrom"]), ctx)
        set_order(entry, "envFrom", "envFromOrder")
    if main.get("resources"):
        entry["resources"] = main["resources"]
    if main.get("volumeMounts"):
        entry["volumeMounts"] = copy.deepcopy(main["volumeMounts"])
    for extra in containers[1:]:
        remap_container_refs(extra, ctx)
        sc_entry = {k: val for k, val in extra.items() if k != "name"}
        if sc_entry.get("image"):
            ctx.sub_raw_image[("jobs", sub_name, "sidecars", extra["name"], "image")] = sc_entry.pop("image")
        entry.setdefault("sidecars", {})[extra["name"]] = sc_entry
    set_order(entry, "sidecars", "sidecarOrder")
    init_containers = pod_spec.get("initContainers", []) or []
    if init_containers:
        for ic in init_containers:
            remap_container_refs(ic, ctx)
        entry["initContainers"], order = named_list_to_map_split_image(
            init_containers, ctx, ("jobs", sub_name, "initContainers"))
        if len(order) > 1:
            entry["initContainerOrder"] = order
    if pod_spec.get("volumes"):
        entry["volumes"] = remap_volumes(named_list_to_map(pod_spec["volumes"]), ctx)
        set_order(entry, "volumes", "volumeOrder")
    # Pod-template labels/annotations — see the matching comment in
    # handle_cronjob. A Job has the same gap for the same reason, and error.txt
    # says so: "handle_job() has the same gap."
    if pod_template_labels(pod_meta):
        entry["podLabels"] = pod_template_labels(pod_meta)
    if pod_template_annotations(pod_meta):
        entry["podAnnotations"] = pod_template_annotations(pod_meta)

    ann = clean_annotations((doc.get("metadata") or {}).get("annotations"))
    if ann:
        entry["annotations"] = ann
    if raw_labels(doc):
        entry["labels"] = raw_labels(doc)
    ctx.values["jobs"][sub_name] = entry


# --- cluster-scoped builders -------------------------------------------------

def _with_metadata(entry: dict, doc: dict) -> dict:
    """Labels and annotations, which rbac.yaml renders for these kinds too. Not
    cosmetic on a ClusterRole: `rbac.authorization.k8s.io/aggregate-to-*` labels
    are how its rules reach admin/edit/view, and a Helm hook annotation is what
    makes a CRD-upgrade Job's ClusterRole exist before that Job runs."""
    if raw_labels(doc):
        entry["labels"] = raw_labels(doc)
    ann = clean_annotations((doc.get("metadata") or {}).get("annotations"))
    if ann:
        entry["annotations"] = ann
    return entry


def build_clusterrole_entry(doc: dict) -> dict:
    entry = {"rules": doc.get("rules", [])}
    if doc.get("aggregationRule"):
        entry["aggregationRule"] = doc["aggregationRule"]
    return _with_metadata(entry, doc)


def build_clusterrolebinding_entry(doc: dict) -> dict:
    subjects = [dict(s) for s in doc.get("subjects", []) or []]
    return _with_metadata({"roleRef": (doc.get("roleRef") or {}).get("name", ""), "subjects": subjects}, doc)


def build_storageclass_entry(doc: dict) -> dict:
    entry = {"provisioner": doc.get("provisioner", "")}
    for k in ("reclaimPolicy", "volumeBindingMode", "allowVolumeExpansion",
              "parameters", "mountOptions", "allowedTopologies"):
        if doc.get(k) is not None:
            entry[k] = doc[k]
    return entry


def build_pv_entry(doc: dict) -> dict:
    spec = doc.get("spec", {}) or {}
    entry = {}
    cap = (spec.get("capacity") or {}).get("storage")
    if cap:
        entry["capacity"] = cap
    if spec.get("accessModes"):
        entry["accessModes"] = spec["accessModes"]
    if spec.get("persistentVolumeReclaimPolicy"):
        entry["reclaimPolicy"] = spec["persistentVolumeReclaimPolicy"]
    for k in ("storageClassName", "volumeMode", "mountOptions", "nodeAffinity", "hostPath", "nfs",
              "csi", "local", "iscsi", "fc", "rbd", "gcePersistentDisk", "awsElasticBlockStore",
              "azureDisk", "azureFile"):
        if spec.get(k) is not None:
            entry[k] = spec[k]
    return entry


def build_scc_entry(doc: dict) -> dict:
    entry = {}
    for k in ("priority", "allowPrivilegedContainer", "allowPrivilegeEscalation",
              "defaultAddCapabilities", "allowedCapabilities", "requiredDropCapabilities",
              "runAsUser", "seLinuxContext", "fsGroup", "supplementalGroups", "volumes",
              "allowHostNetwork", "allowHostPorts", "allowHostPID", "allowHostIPC",
              "readOnlyRootFilesystem", "users", "groups", "seccompProfiles",
              "allowedUnsafeSysctls", "forbiddenSysctls"):
        if doc.get(k) is not None:
            entry[k] = doc[k]
    return entry


def handle_cluster_scoped(doc: dict, kind: str, cluster_registry: ClusterRegistry, namespace: str,
                          ms_name: str, cluster_values: dict, warnings: list,
                          ctx: Optional[MicroserviceContext] = None,
                          local_cluster_roles: Optional[set] = None) -> None:
    name = (doc.get("metadata") or {}).get("name")
    if not name:
        return

    # Namespace-scoped mode (the default): emit a copy inside the declaring namespace's own
    # release instead of deduplicating every namespace's copy into the single
    # global cluster-shared release. For a tenant with no cluster-wide rights,
    # a separate cluster-shared Application is something they cannot deploy at
    # all, and it would sit permanently OutOfSync.
    #
    # STRICT NAMING: the name is carried through VERBATIM. This used to be
    # namespace-prefixed, because these kinds are cluster-scoped and two
    # namespaces both emitting 'log-agent-reader' are two releases claiming ONE
    # cluster object. That is a real conflict — but prefixing solved it by
    # renaming, and a rename is exactly what the strict-naming directive
    # forbids: every RoleRef, every audit policy and every external reference to
    # the original name breaks silently.
    #
    # The conflict does not go away, it becomes VISIBLE: verify_no_conflicts
    # reports the duplicate ownership, and the warning below names both
    # claimants. When the two copies are byte-identical the shared object was
    # almost certainly shared in the source cluster too, and `--cluster-scoped`
    # models that properly by emitting it once. When they DIFFER, two namespaces
    # genuinely disagree about one cluster object and no converter can fix that.
    if NAMESPACE_SCOPED_ONLY and ctx is not None:
        scoped_name = name
        if kind == "ClusterRole":
            ctx.values.setdefault("rbac", {}).setdefault("clusterRoles", {})[scoped_name] = \
                build_clusterrole_entry(doc)
        elif kind == "ClusterRoleBinding":
            entry = build_clusterrolebinding_entry(doc)
            # Only re-point roleRef if this microservice declares that
            # ClusterRole itself. A binding onto a BUILT-IN ClusterRole (view,
            # edit, cluster-admin) must keep the original name — prefixing it
            # would point at a role that does not exist.
            # STRICT NAMING: the ClusterRole keeps its own name now, so the
            # roleRef pointing at it needs no rewrite either — and rewriting it
            # would point at a role that no longer exists under that name.
            pass
            ctx.values.setdefault("rbac", {}).setdefault("clusterRoleBindings", {})[scoped_name] = entry
        else:
            warnings.append(
                f"[cluster-scoped] {kind}/{name}: namespace-scoped mode only relocates "
                f"ClusterRole/ClusterRoleBinding into a namespace release; {kind} was DROPPED. "
                f"It has no namespaced equivalent — hand it to whoever holds cluster rights."
            )
            return
        warnings.append(
            f"[cluster-scoped] {kind}/{name}: namespace-scoped (default) — emitted into "
            f"'{namespace}/{ms_name}' as '{scoped_name}' instead of the shared cluster-shared "
            f"release. NOTE it is STILL a cluster-scoped object and still needs cluster-level "
            f"rights to apply; only the deduplication changed, not the kind."
        )
        return

    h = content_hash(doc)
    should_emit, note = cluster_registry.claim(kind, name, h, namespace, ms_name)
    if note:
        warnings.append(f"[cluster-scoped] {note}")
    if not should_emit:
        return
    if kind == "ClusterRole":
        cluster_values["rbac"]["clusterRoles"][name] = build_clusterrole_entry(doc)
    elif kind == "ClusterRoleBinding":
        cluster_values["rbac"]["clusterRoleBindings"][name] = build_clusterrolebinding_entry(doc)
    elif kind == "StorageClass":
        cluster_values["storageClasses"][name] = build_storageclass_entry(doc)
    elif kind == "PersistentVolume":
        cluster_values["persistentVolumes"][name] = build_pv_entry(doc)
    elif kind == "SecurityContextConstraints":
        cluster_values["scc"][name] = build_scc_entry(doc)
    elif kind == "ClusterSecretStore":
        cluster_values["clusterSecretStores"][name] = build_clustersecretstore_entry(doc)
        # Same rule as record_eso_api_version, for the cluster-shared release:
        # without it a v1beta1 source rendered as the chart's default v1.
        version = ESO_API_VERSION_OVERRIDE or doc.get("apiVersion")
        existing = cluster_values.get("externalSecretsApiVersion")
        if existing and existing != version:
            warnings.append(f"[cluster-scoped] ClusterSecretStore/{name}: apiVersion '{version}' differs "
                            f"from '{existing}' already used in cluster-shared — '{existing}' wins")
        elif version:
            cluster_values["externalSecretsApiVersion"] = version


# ---------------------------------------------------------------------------
# Microservice orchestration
# ---------------------------------------------------------------------------

def is_stateful(docs: list[dict]) -> str | None:
    """Why a release is stateful, or None: a StatefulSet, or a workload whose
    pods mount a PersistentVolumeClaim. Those are the ones a cutover restart
    hurts — a database's pods come back one by one, an RWO volume has to
    detach and re-attach — so by default they are converted with
    --exact-copy and the first sync restarts nothing."""
    for d in docs:
        kind = d.get("kind")
        if kind == "StatefulSet":
            return f"StatefulSet/{(d.get('metadata') or {}).get('name')}"
        if kind in WORKLOAD_KINDS:
            pod = (((d.get("spec") or {}).get("template") or {}).get("spec") or {})
            if any(isinstance(v, dict) and "persistentVolumeClaim" in v for v in pod.get("volumes") or []):
                return f"{kind}/{(d.get('metadata') or {}).get('name')} mounts a PersistentVolumeClaim"
    return None


def build_microservice(namespace: str, ms_name: str, docs: list[dict], ns_registry: NamespaceRegistry,
                       cluster_registry: ClusterRegistry, cluster_values: dict, warnings: list,
                       owner_label: str, known_pv_names: set[str] | None = None) -> MicroserviceContext:
    """_build_microservice with EXACT_COPY set for this release: as the flag
    says, or — no flag — on for a stateful one (is_stateful)."""
    global EXACT_COPY
    reason = is_stateful(docs)
    EXACT_COPY = EXACT_COPY_FLAG if EXACT_COPY_FLAG is not None else reason is not None
    if EXACT_COPY_FLAG is None and reason:
        warnings.append(f"{ms_name}: stateful ({reason}) — converted as --exact-copy, so the "
                        f"cutover sync restarts none of its pods (--exact-copy false to turn off)")
    try:
        return _build_microservice(namespace, ms_name, docs, ns_registry, cluster_registry, cluster_values,
                                   warnings, owner_label, known_pv_names)
    finally:
        EXACT_COPY = bool(EXACT_COPY_FLAG)


def _build_microservice(namespace: str, ms_name: str, docs: list[dict], ns_registry: NamespaceRegistry,
                        cluster_registry: ClusterRegistry, cluster_values: dict, warnings: list,
                        owner_label: str, known_pv_names: set[str] | None = None) -> MicroserviceContext:
    ctx = MicroserviceContext(namespace=namespace, name=ms_name)
    # No nameOverride: the Argo CD Application that deploys the release already
    # names it, so the chart's fullname needs nothing from the values file.
    #
    # That release name derives every release-named resource — the workload,
    # its primary Service, its ServiceAccount — from the FILE name. When the
    # raw manifest's workload is actually called something else (a redis.yaml
    # whose StatefulSet is 'redis-cache'), that silently renames it, and with
    # it: the Service's DNS name, the pod names, and — for a StatefulSet — the
    # PVCs, which Kubernetes names '<template>-<statefulset>-<ordinal>'. A
    # rename there orphans the existing volume and provisions a fresh empty
    # one, which is silent data loss on anything that isn't a cache.
    #
    # Worse, two microservices whose names cross over (a redis.yaml declaring
    # 'redis-cache' next to a cache.yaml declaring 'redis') silently TRADE
    # names: each renders under the other's, so traffic to 'redis' reaches the
    # cache. No existing check catches it, because no name is duplicated —
    # they are merely misassigned, so the run still reports conflict-free.
    #
    # fullnameOverride takes precedence over the release name in the chart's
    # fullname helper (universal-chart/templates/_helpers.tpl), so pinning it
    # to the raw name preserves the real identity while leaving the filename as
    # the release identity that output paths, base/ sharing and the
    # ApplicationSet globs all key off — nothing structural changes.
    workload_names = [n for n in ((d.get("metadata") or {}).get("name") for d in docs
                                   if d.get("kind") in WORKLOAD_KINDS) if n]
    # A release with no workload at all (a config/RBAC-only one, workload.type
    # none) still renders its PRIMARY Service under the same fullname, so the
    # raw Service name is lost the same way. Which Service that is is not a
    # guess: handle_service takes the FIRST one in document order as primary
    # and files the rest under `services` by their own names, and this scans
    # the same list in the same order — so the first Service here is exactly
    # the one whose name the fullname will drive.
    fullname_source = workload_names
    if not fullname_source:
        fullname_source = [n for n in ((d.get("metadata") or {}).get("name") for d in docs
                                        if d.get("kind") == "Service") if n]
    # Pinned even when the raw name IS the file name: Argo CD renders the
    # release under its Application's name (postgres-dev for postgres.yaml in
    # shop-dev — argo_release_name), so without the pin the fullname falls back
    # to that and the cutover creates a NEW StatefulSet postgres-dev, with new,
    # empty PVCs, next to the live one.
    if fullname_source:
        raw_name = fullname_source[0]
        ctx.values["fullnameOverride"] = raw_name
        # 'ms1-black' in ms1.yaml is the --env-group stripping the token on
        # purpose, not a misnamed file — the pin is expected, nothing to warn.
        if raw_name != ms_name and not (NAME_TOKENS and strip_name_tokens(raw_name, NAME_TOKENS) == ms_name):
            ctx.warnings.append(
                f"its main resource is named '{raw_name}' in the raw manifest but the file is "
                f"'{ms_name}.yaml' — pinned fullnameOverride to '{raw_name}' so the rendered "
                f"workload/Service/ServiceAccount keep the raw name (Service DNS, pod names and "
                f"StatefulSet PVC names all derive from it). Rename the file to match if you "
                f"actually intended the resource to be renamed."
            )
    if len(set(workload_names)) > 1:
        ctx.warnings.append(
            f"file declares {len(set(workload_names))} differently-named workloads "
            f"({sorted(set(workload_names))}) but the chart renders one per release — "
            f"'{workload_names[0]}' was used for fullnameOverride; split them into separate files"
        )

    # Which ClusterRoles this microservice declares ITSELF — used under
    # namespace-scoped mode to tell "my own ClusterRole, safe to rename"
    # apart from a built-in one (view/edit/cluster-admin) that must not be.
    local_cluster_roles = {(d.get("metadata") or {}).get("name") for d in docs
                            if d.get("kind") == "ClusterRole"}

    # Pass 1 — claim raw-name resources first so every reference (env,
    # envFrom, volumes, rbac) can be rewritten consistently in pass 2,
    # regardless of the order objects appear in the source multi-doc file.
    for kind in NS_RAW_NAME_KINDS:
        for doc in docs:
            if doc.get("kind") != kind:
                continue
            if kind == "Secret" and is_auto_sa_secret(doc):
                continue
            name = (doc.get("metadata") or {}).get("name")
            if not name:
                continue
            h = content_hash(doc)
            final, action, note = ns_registry.claim(kind, name, h, owner_label)
            if note:
                ctx.warnings.append(note)
            ctx.name_map[kind][name] = final
            ctx.claim_actions[kind][name] = action

    # Which ServiceAccounts the pods here actually run as — decided before pass 2
    # because the SA doc can come before the workload doc in the file.
    for doc in docs:
        spec = doc.get("spec") or {}
        if doc.get("kind") == "CronJob":
            spec = (spec.get("jobTemplate") or {}).get("spec") or {}
        if doc.get("kind") in WORKLOAD_KINDS | {"CronJob", "Job"}:
            pod = (spec.get("template") or {}).get("spec") or {}
            if ctx.pod_sa_names is None:
                ctx.pod_sa_names = set()
            ctx.pod_sa_names.add(pod.get("serviceAccountName") or pod.get("serviceAccount") or "default")

    # Pass 2 — build the actual values.
    for doc in docs:
        kind = doc.get("kind")
        name = (doc.get("metadata") or {}).get("name")

        if kind in WORKLOAD_KINDS:
            handle_workload(doc, ctx, kind)
        elif kind == "Route":
            if ctx.claim_actions["Route"].get(name) != "reference_shared":
                handle_route(doc, ctx, ctx.name_map["Route"][name])
        elif kind == "Service":
            if ctx.claim_actions["Service"].get(name) != "reference_shared":
                handle_service(doc, ctx, ctx.name_map["Service"][name])
        elif kind == "ConfigMap":
            if ctx.claim_actions["ConfigMap"].get(name) != "reference_shared":
                handle_configmap(doc, ctx, ctx.name_map["ConfigMap"][name])
        elif kind == "Secret":
            if is_auto_sa_secret(doc):
                continue
            if ctx.claim_actions["Secret"].get(name) != "reference_shared":
                handle_secret(doc, ctx, ctx.name_map["Secret"][name])
        elif kind == "SecretStore":
            if ctx.claim_actions["SecretStore"].get(name) != "reference_shared":
                handle_secretstore(doc, ctx, ctx.name_map["SecretStore"][name])
        elif kind == "ExternalSecret":
            if ctx.claim_actions["ExternalSecret"].get(name) != "reference_shared":
                handle_externalsecret(doc, ctx, ctx.name_map["ExternalSecret"][name])
        elif kind == "PersistentVolumeClaim":
            if ctx.claim_actions["PersistentVolumeClaim"].get(name) != "reference_shared":
                handle_pvc(doc, ctx, ctx.name_map["PersistentVolumeClaim"][name], known_pv_names)
        elif kind == "Role":
            if ctx.claim_actions["Role"].get(name) != "reference_shared":
                handle_role(doc, ctx, ctx.name_map["Role"][name])
        elif kind == "RoleBinding":
            if ctx.claim_actions["RoleBinding"].get(name) != "reference_shared":
                handle_rolebinding(doc, ctx, ctx.name_map["RoleBinding"][name])
        elif kind == "ServiceAccount":
            handle_serviceaccount(doc, ctx, name)
        elif kind in CLUSTER_SCOPED_KINDS:
            handle_cluster_scoped(doc, kind, cluster_registry, namespace, ms_name, cluster_values,
                                   warnings, ctx, local_cluster_roles)
        elif kind == "HorizontalPodAutoscaler":
            handle_hpa(doc, ctx)
        elif kind == "PodDisruptionBudget":
            handle_pdb(doc, ctx)
        elif kind == "Ingress":
            handle_ingress(doc, ctx)
        elif kind == "VerticalPodAutoscaler":
            handle_vpa(doc, ctx)
        elif kind == "NetworkAttachmentDefinition":
            handle_nad(doc, ctx)
        elif kind == "CronJob":
            handle_cronjob(doc, ctx, name)
        elif kind == "Job":
            handle_job(doc, ctx, name)
        else:
            ctx.extra_deploy.append(as_extra_deploy(doc))
            ctx.warnings.append(f"Unrecognized kind '{kind}' ({name}) passed through via extraDeploy")

    if not ctx.saw_workload:
        ctx.values.setdefault("workload", {})["type"] = "none"
        # The chart hard-fails validation on these with workload.type none.
        for k in ("hpa", "vpa", "pdb"):
            if ctx.values.pop(k, None) is not None:
                ctx.warnings.append(
                    f"{k} dropped: this release has no workload (workload.type: none), and the "
                    f"chart rejects {k} with no pods to target")

    # The pod's token mount where it differs from the ServiceAccount's (see
    # handle_workload). Only then, so a release whose two agree is unchanged.
    sa_auto = (ctx.values.get("serviceAccount") or {}).get("automountServiceAccountToken")
    if ctx.pod_automount is not None and sa_auto is not None and sa_auto != ctx.pod_automount:
        ctx.values["automountServiceAccountToken"] = ctx.pod_automount


    if ctx.extra_deploy:
        ctx.values["extraDeploy"] = ctx.extra_deploy

    return ctx


def build_minimal_values(ctx: MicroserviceContext) -> dict:
    minimal: dict = {}
    if ctx.replica_count is not None:
        minimal["replicaCount"] = ctx.replica_count
    img = {k: v for k, v in ctx.full_image.items() if k in ("repository", "tag", "digest")}
    if img:
        minimal["image"] = img
    if ctx.full_env:
        minimal["env"] = ctx.full_env

    # CronJob/Job sub-resources: same image-tag/env split as above,
    # just at their own nested path (cronjobs.<name>.jobTemplate.image /
    # jobs.<name>.image) instead of the microservice's top level — see
    # sub_full_image/sub_full_env on MicroserviceContext.
    for (top_key, sub_name), img_val in ctx.sub_full_image.items():
        path = minimal.setdefault(top_key, {}).setdefault(sub_name, {})
        if top_key == "cronjobs":
            path = path.setdefault("jobTemplate", {})
        path["image"] = {k: v for k, v in img_val.items() if k in ("repository", "tag", "digest")}
    for (top_key, sub_name), env_val in ctx.sub_full_env.items():
        if not env_val:
            continue
        path = minimal.setdefault(top_key, {}).setdefault(sub_name, {})
        if top_key == "cronjobs":
            path = path.setdefault("jobTemplate", {})
        path["env"] = env_val

    # Init container / sidecar images (see sub_raw_image's field comment) —
    # each path already ends in "image", so this places the raw string back
    # at wherever it was pulled from, regardless of how deeply nested.
    for path, raw_image in ctx.sub_raw_image.items():
        set_nested(minimal, path, raw_image)
    return minimal


# ---------------------------------------------------------------------------
# defaults.yaml generation — see the module docstring and the comment above
# DEFAULTS_FILE_NAME for the full rationale.
# ---------------------------------------------------------------------------

def common_subtree(dicts: list[dict]) -> dict:
    """
    Recursively finds the key/value pairs that are PRESENT, with the exact
    same value, in EVERY dict in `dicts`. Partial agreement (some but not
    all dicts share it) is deliberately excluded — Helm merges maps, so
    promoting a key to a defaults file that only SOME
    releases actually had would silently inject it into the releases that
    never declared it, changing their behavior. Nested dicts are compared
    key-by-key (recursively) rather than as opaque blobs, so e.g.
    `resources.requests.cpu` can be promoted even if `resources.limits`
    differs per release. Lists and scalars must match exactly (by value) —
    lists are never partially merged.
    """
    if len(dicts) < 2:
        return {}
    common: dict = {}
    first = dicts[0]
    rest = dicts[1:]
    for key, val in first.items():
        if not all(key in d for d in rest):
            continue
        others = [d[key] for d in rest]
        if isinstance(val, dict) and all(isinstance(o, dict) for o in others):
            sub = common_subtree([val] + others)
            if sub:
                common[key] = sub
        elif all(o == val for o in others):
            common[key] = val
    return common


def subtract_defaults(values: dict, defaults: dict) -> dict:
    """
    The inverse of common_subtree: removes anything from `values` that is
    identical to `defaults` at the same path, recursively, leaving exactly
    the per-release delta. Safe by construction — since defaults.yaml is
    always layered underneath this file, whatever's removed here is still
    supplied from there.
    """
    if not defaults:
        return values
    out: dict = {}
    for key, val in values.items():
        if key not in defaults:
            out[key] = val
            continue
        dval = defaults[key]
        if isinstance(val, dict) and isinstance(dval, dict):
            sub = subtract_defaults(val, dval)
            if sub:
                out[key] = sub
        elif val != dval:
            out[key] = val
    return out


def deep_merge(base: dict, override: dict) -> dict:
    """Recursive dict merge — `override` wins on conflicting scalars/lists,
    nested dicts are merged key-by-key rather than one replacing the other
    wholesale. Used to combine a same-named microservice's per-namespace
    structural remainder (see base-registry grouping in main()) with its
    existing image/env minimal override into one values/<slug>.yaml."""
    out = dict(base)
    for key, val in override.items():
        if key in out and isinstance(out[key], dict) and isinstance(val, dict):
            out[key] = deep_merge(out[key], val)
        else:
            out[key] = val
    return out


def _env_stem(name: str) -> str:
    """A name without its environment words, so 'shop-dev-config' in one
    namespace and 'shop-prd-config' in the next compare equal."""
    tokens = KNOWN_ENV_SUFFIXES | NAME_TOKENS
    return strip_name_tokens(name, tokens) if tokens else name


def group_shared_releases(shared: dict[str, "MicroserviceContext"],
                          objects: dict[str, set[tuple[str, str]]],
                          releases: dict[str, set[str]] | None = None) -> list[list[str]]:
    """Which namespaces' `shared` releases are one release, in first-seen order.

    A namespace's shared.yaml holds what several of its microservices use, so
    the dev, tst and prd namespaces of one app hold the same ConfigMaps and
    Secrets, differing only by environment words in their values. Those are
    one release — one base/shared.yaml, the differences in each
    values/shared.yaml — the way a microservice running in every namespace is.

    A namespace joins the first group it has an object in common with (env
    words aside), whose content is identical, or whose namespace is the same
    app's in another environment — unless that group already has a namespace
    ending in the same '-<token>', since the two would get one Application
    name (shared-dev) for two deployments. Otherwise (another app's namespaces
    in the same run) it starts a group of its own.

    The same app's namespaces need not name one shared object alike
    (goldenbell-ui's ui-settings, goldenbell-next's flags-next). They are
    known by name instead: equal once env words are stripped (goldenbell-ui /
    goldenbell-next with 'ui' and 'next' env words), or equal but for the last
    '-<word>' AND running a release of the same name (`releases`: web and
    worker in both). platform-data and platform-ops run nothing alike and
    stay two apps."""
    releases = releases or {}
    sibling_keys: dict[str, set] = defaultdict(set)
    for a in shared:
        for b in shared:
            pa, pb = k8s_ns(a).rpartition("-")[0], k8s_ns(b).rpartition("-")[0]
            if a != b and pa and pa == pb and releases.get(a, set()) & releases.get(b, set()):
                sibling_keys[a].add(("#namespace", pa))
    groups: list[tuple[list[str], set, set[str]]] = []  # (members, object keys, app suffixes)
    for ns, ctx in shared.items():
        keys = {(kind, _env_stem(str(name))) for kind, name in objects.get(ns, set())}
        keys.add(("#content", content_hash(ctx.values)))
        ns_stem = _env_stem(k8s_ns(ns))
        if ns_stem != k8s_ns(ns):
            keys.add(("#namespace", ns_stem))
        keys |= sibling_keys.get(ns, set())
        suffix = app_name_suffix(ns)
        for members, group_keys, suffixes in groups:
            if keys & group_keys and suffix not in suffixes:
                members.append(ns)
                group_keys |= keys
                suffixes.add(suffix)
                break
        else:
            groups.append(([ns], keys, {suffix}))
    return [members for members, _, _ in groups]


def shared_group_slug(members: list[str], taken: set[str]) -> str:
    """The release name of a `shared` group other than the first: its
    namespaces' name without the env word (pay-dev + pay-prd -> pay-shared),
    else the first namespace's (pay-dev-shared) — never a name in `taken`."""
    stems = {_env_stem(k8s_ns(ns)) for ns in members}
    first = members[0].replace("/", "-")
    candidates = [f"{stems.pop()}-{SHARED_DIR_NAME}"] if len(members) > 1 and len(stems) == 1 else []
    candidates += [f"{first}-{SHARED_DIR_NAME}"]
    candidates += [f"{first}-{SHARED_DIR_NAME}-{n}" for n in range(2, len(taken) + 3)]
    return next(c for c in candidates if c not in taken)


def write_base_file(base_dir: Path, slug: str, values: dict) -> None:
    """
    The comprehensive, environment-agnostic file for one microservice —
    written ONCE per release-slug (see the base registry in main()) and
    reused by every namespace whose release_slugs map points at it. Never
    contains an image tag; that, and any value that is not the same in every
    namespace, lives in each namespace's own <folder>/values/<slug>.yaml.
    """
    base_dir.mkdir(parents=True, exist_ok=True)
    # A file someone moved into a base/ sub-folder stays there.
    target = next(iter(sorted(base_dir.rglob(f"{slug}.yaml"))), base_dir / f"{slug}.yaml")
    header = (
        f"# Auto-generated by convert_to_universal_chart.py — DO NOT EDIT BY HAND.\n"
        f"# Shared, environment-agnostic base for '{slug}' — reused by every namespace\n"
        f"# that runs this microservice. What differs per folder (the image tag, any\n"
        f"# value that is not the same everywhere) lives in <folder>/values/{slug}.yaml.\n"
        f"# Re-run the converter to regenerate after the source manifests change.\n\n"
    )
    target.write_text(header + dump_yaml(helm_safe_values(values), spaced=True), encoding="utf-8")


def write_microservice_output(namespace_dir: Path, ms_name: str, release_slug: str, namespace: str,
                              ctx: MicroserviceContext, minimal_extra: dict | None = None) -> None:
    """
    Writes this microservice's per-NAMESPACE artifacts only — the shared
    comprehensive file lives in base/ and is written once by write_base_file,
    not here. values/<slug>.yaml (image tag + what differs + minor per-
    namespace tweaks) is a FLAT file — no per-microservice subdirectory —
    under the namespace's own output directory.

    That file IS the release: the ms-applicationSet chart's git "files"
    generator globs <namespace>/values/*.yaml, so writing one here is what
    makes ArgoCD deploy it, and its filename is what both base/<file> and
    values/<file> resolve against. Hence release_slug, not ms_name — see the
    release_slugs comment in main().

    minimal_extra: this instance's structural remainder versus its shared
    base/<slug>.yaml (see the Pass 2.5 grouping in main()) — e.g. etcd's own
    advertise URL. Deep-merged on top of the usual image/env minimal values,
    so a genuinely-differing field doesn't force the whole base to fork.
    """
    values_dir = namespace_dir / VALUES_DIR_NAME
    values_dir.mkdir(parents=True, exist_ok=True)
    # A file someone moved into a values/ sub-folder stays there.
    target = next(iter(sorted(values_dir.rglob(f"{release_slug}.yaml"))), values_dir / f"{release_slug}.yaml")

    minimal = build_minimal_values(ctx)
    if minimal_extra:
        minimal = deep_merge(minimal_extra, minimal)
    header_min = (
        f"# Auto-generated MINIMAL override for '{ms_name}' — image, replicas,\n"
        f"# plus (if present) this namespace's own structural difference from the\n"
        f"# shared base/{release_slug}.yaml (see conflicts_and_warnings.txt).\n"
        f"# This is the client's single source of truth for day-2 tuning; safe to hand-edit\n"
        f"# and applied LAST (highest precedence) over base/{release_slug}.yaml.\n\n"
    )
    target.write_text(header_min + dump_yaml(helm_safe_values(minimal), spaced=True), encoding="utf-8")


# ---------------------------------------------------------------------------
# ArgoCD ApplicationSet generation
# ---------------------------------------------------------------------------

def common_ignore_differences() -> list[dict]:
    """
    OpenShift/Kubernetes admission mutates several fields on live objects
    that Helm's desired manifest never sets (SCC-assigned runAsUser/fsGroup/
    seLinuxOptions when a pod doesn't pin its own, controller-populated
    ServiceAccount secrets/imagePullSecrets). Without this, ArgoCD's
    self-heal fights the cluster's own admission controllers and the
    Application flips OutOfSync -> Synced every reconcile. StatefulSet
    volumeClaimTemplates are immutable post-creation, so a spec diff there
    can never actually be applied — ignore it too instead of looping forever.
    """
    security_context_paths = [
        ".spec.template.spec.securityContext.runAsUser",
        ".spec.template.spec.securityContext.runAsGroup",
        ".spec.template.spec.securityContext.fsGroup",
        ".spec.template.spec.securityContext.seLinuxOptions",
        ".spec.template.spec.containers[]?.securityContext.runAsUser",
        ".spec.template.spec.containers[]?.securityContext.capabilities",
    ]
    return [
        {"group": "apps", "kind": "Deployment", "jqPathExpressions": security_context_paths},
        {"group": "apps", "kind": "StatefulSet",
         "jqPathExpressions": security_context_paths + [".spec.volumeClaimTemplates"]},
        {"group": "apps", "kind": "DaemonSet", "jqPathExpressions": security_context_paths},
        {"group": "", "kind": "ServiceAccount", "jsonPointers": ["/secrets", "/imagePullSecrets"]},
    ]


def _values_ref(values_path: str, rest: str) -> str:
    """Build a '$values/...' Application valueFiles reference.

    Every path this converter writes into an Application/ApplicationSet is
    relative to the ROOT of the values repo, which matches the default
    layout (a dedicated repo whose root IS the --output directory). --values-
    path exists for the other case — the --output directory folded into a
    subpath of a larger repo (e.g. this repo's own homelab checkout, at
    universal/gitops-factory/output-showcase) — so the same references still
    resolve. Empty (the default) reproduces the original unprefixed paths
    exactly.
    """
    prefix = f"{values_path}/" if values_path else ""
    return f"$values/{prefix}{rest}"


def _values_repo_path(values_path: str, rest: str) -> str:
    """Same prefixing as _values_ref, for a path into the values repo that
    ISN'T '$values/'-prefixed — namely the ApplicationSet git files
    generator's glob, which addresses the repo directly rather than through
    a multi-source Application's named source alias."""
    return f"{values_path}/{rest}" if values_path else rest


def build_root_application(name: str, values_repo_url: str, values_revision: str,
                            values_path: str = "", argocd_namespace: str = "argocd",
                            project: str = "default") -> dict:
    """The single entry point: one Application, applied by hand exactly once,
    that deploys the root ApplicationSet ("app-of-apps").

    Everything else follows from that one object: the root ApplicationSet
    discovers each namespace directory in this tree and deploys the
    ms-applicationSet chart for it, which in turn fans out over that
    namespace's values/*.yaml. Adding a namespace or a microservice after
    bootstrap is a git commit, never another kubectl apply.

    Its source is a DIRECTORY, not Helm — the file it deploys is already a
    finished Kubernetes object, not a chart to render.

    The include list names EXACT filenames and recursion stays off, which is
    what makes this safe. ArgoCD matches directory globs against paths
    relative to source.path and a single `*` spans directory levels
    (argoproj/argo-cd#7319), so a pattern like '*application.yaml' would also
    match a microservice legitimately named 'web-application' down in
    <ns>/values/ and ArgoCD would try to apply a VALUES file as a manifest.
    Naming the two files outright means nothing under base/, <ns>/values/ or
    report/ — nor the tree-root defaults.yaml — can ever be picked up, no
    matter what a namespace or a microservice is called.

    It deliberately does NOT include itself: this is the one object a human
    applies, and leaving it out keeps "what ArgoCD manages" and "what you
    bootstrap with" from being the same file.

    prune is deliberately FALSE. The blast radius differs by an order of
    magnitude from the objects it deploys: pruning the root ApplicationSet
    deletes every per-namespace ApplicationSet it generated, which deletes
    every Application those generated, which deletes every workload. Losing a
    cluster's workloads should not be the side effect of a file disappearing
    from a directory listing. selfHeal still keeps the root ApplicationSet
    itself reconciled.
    """
    return {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "Application",
        "metadata": {
            "name": name,
            "namespace": argocd_namespace,
            "labels": {"app": name},
            "annotations": {"generated-by": "convert_to_universal_chart.py"},
        },
        "spec": {
            "project": project,
            "destination": {
                "server": "https://kubernetes.default.svc",
                # ApplicationSets and Applications are themselves argocd-namespace
                # objects; the workloads they deploy land in their own namespaces.
                "namespace": argocd_namespace,
            },
            "source": {
                "repoURL": values_repo_url,
                "targetRevision": values_revision,
                "path": values_path or ".",
                "directory": {
                    "recurse": False,
                    "include": "{" + ",".join((ROOT_APPSET_FILE_NAME, CLUSTER_SHARED_APP_FILE_NAME)) + "}",
                },
            },
            "syncPolicy": {
                "automated": {"selfHeal": True, "prune": False},
                "syncOptions": ["ServerSideApply=true"],
            },
        },
    }


# ms-applicationSet's own defaults (ms-applicationSet/values.yaml): a helm
# parameter equal to one of these is left out of the root ApplicationSet.
APPSET_CHART_DEFAULTS = {
    "chartRepoURL": "https://github.com/devops-ezrahi/universal-chart.git",
    "chartRevision": "main",
    "chartPath": ".",
}


def _yaml_scalar(v: str) -> str:
    """One scalar as it would appear in a block mapping, quoted only if needed."""
    return yaml.safe_dump(v, default_style=None, width=1000).removesuffix("\n...\n").strip()


def build_root_applicationset(name: str, chart_repo_url: str, appset_chart_path: str, chart_revision: str,
                              chart_path: str, values_repo_url: str, values_revision: str,
                              values_path: str = "", argocd_namespace: str = "argocd",
                              project: str = "default",
                              destination_server: str = "https://kubernetes.default.svc",
                              destination_name: str = "", auto_sync: bool = False) -> str:
    """One Application per namespace folder in this tree, as YAML text.

    Every namespace folder — plain or variant (prd/yellow, prd/yellow/eu) — has
    a defaults.yaml, so one git files generator on '**/defaults.yaml' finds
    them all at any depth, under both ArgoCD globbing modes. Each Application
    deploys the ms-applicationSet chart, parameterised with its folder, which
    fans out over <folder>/values/*.yaml and layers base/<file> ->
    <folder>/defaults.yaml -> <folder>/values/<file>. Keeping that second level
    in the chart keeps the layering rules in ONE place, versioned with the
    chart they apply to.

    Written as text rather than dumped: the anchors (&repoURL, &branch,
    &project) say each value once, so retargeting the tree is one edit, and
    the commented exclude shows how to switch a namespace off without deleting
    its folder.
    """
    q = _yaml_scalar
    segments = [p for p in values_path.split("/") if p]
    rel = ".path.path" + (f' | trimPrefix "{"/".join(segments)}/"' if segments else "")
    params = [
        ("namespace", q("{{ index .path.segments %d }}" % len(segments))),
        ("folder", q("{{ %s }}" % rel)),
        ("originRepoURL", "*repoURL"),
        ("originBranch", "*branch"),
        *([("originPath", q(values_path))] if values_path else []),
        ("project", "*project"),
        ("destinationServer", q(destination_server)),
        *([("destinationName", q(destination_name))] if destination_name else []),
        *[(k, q(v)) for k, v in (("chartRepoURL", chart_repo_url), ("chartRevision", chart_revision),
                                  ("chartPath", chart_path)) if v != APPSET_CHART_DEFAULTS[k]],
        # The microservice Applications the chart renders: manual sync unless
        # --auto-sync true. Always written — the chart's own default is true.
        ("autoSync", q("true" if auto_sync else "false")),
    ]
    lines = [
        "apiVersion: argoproj.io/v1alpha1",
        "kind: ApplicationSet",
        "metadata:",
        f"  name: &name {q(name)}",
        "  labels:",
        "    app: *name",
        "spec:",
        "  generators:",
        "  - git:",
        "      files:",
        f"      - path: {q(_values_repo_path(values_path, '**/' + DEFAULTS_FILE_NAME))}",
        "      # Switch a namespace off without deleting its folder:",
        f"      # - path: {q(_values_repo_path(values_path, '<namespace>/**'))}",
        "      #   exclude: true",
        f"      repoURL: &repoURL {q(values_repo_url)}",
        f"      revision: &branch {q(values_revision)}",
        "  goTemplate: true",
        "  goTemplateOptions:",
        "  - missingkey=error",
        "  template:",
        "    metadata:",
        "      labels:",
        "        app: *name",
        "        managed-by: universal-chart-converter",
        "      name: " + q('{{ %s | replace "/" "-" }}' % rel),
        "    spec:",
        f"      project: &project {q(project)}",
        "      sources:",
        "      - helm:",
        "          parameters:",
        *[line for k, v in params for line in (f"          - name: {k}", f"            value: {v}")],
        f"        path: {q(appset_chart_path)}",
        f"        repoURL: {q(chart_repo_url)}",
        f"        targetRevision: {q(chart_revision)}",
        "      destination:",
        f"        namespace: {q(argocd_namespace)}",
        (f"        name: {q(destination_name)}" if destination_name
         else "        server: https://kubernetes.default.svc"),
        "      syncPolicy:",
        "        automated:",
        "          selfHeal: true",
        "        syncOptions:",
        "        - ServerSideApply=true",
    ]
    return "\n".join(lines) + "\n"


def _destination(server: str, name: str, namespace: str) -> dict:
    """ArgoCD destination: a registered cluster NAME (--argocd-cluster) wins over the URL."""
    return {"name": name, "namespace": namespace} if name else {"server": server, "namespace": namespace}


def build_cluster_shared_application(namespace: str, chart_repo_url: str, chart_path: str, chart_revision: str,
                                     values_repo_url: str, values_revision: str, values_path: str = "",
                                     argocd_namespace: str = "argocd", project: str = "default",
                                     destination_server: str = "https://kubernetes.default.svc",
                                     destination_name: str = "", auto_sync: bool = False) -> dict:
    """The one release that is NOT part of the per-namespace fan-out.

    It gets its own Application, sitting at the tree root next to the root
    ApplicationSet (which excludes cluster-shared/ from its directory
    generator), for the same reason it always did: it is deduplicated across
    the ENTIRE run and belongs to no single namespace. It also cannot take
    the fan-out's fixed valueFiles chain — its values are a pool of
    cluster-scoped objects, a completely different shape from a
    microservice's, so a defaults.yaml layer computed from microservices
    could switch on a Route or an HPA it never declared and has no workload
    to back.
    """
    return {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "Application",
        "metadata": {"name": "cluster-shared-resources", "namespace": argocd_namespace},
        "spec": {
            "project": project,
            "sources": [
                {
                    "repoURL": chart_repo_url,
                    "targetRevision": chart_revision,
                    "path": chart_path,
                    "helm": {"valueFiles": [
                        _values_ref(values_path, f"{CLUSTER_SHARED_DIR_NAME}/{VALUES_DIR_NAME}/cluster-shared-values.yaml")
                    ]},
                },
                {"repoURL": values_repo_url, "targetRevision": values_revision, "ref": "values"},
            ],
            "destination": _destination(destination_server, destination_name, namespace),
            "syncPolicy": {
                **({"automated": {"prune": True, "selfHeal": True}} if auto_sync else {}),
                "syncOptions": ["CreateNamespace=true", "ServerSideApply=true"],
            },
        },
    }


# ---------------------------------------------------------------------------
# Render-time verification — the real proof the output is conflict-free
# ---------------------------------------------------------------------------
# Rendering each release in isolation only proves its *values* parse. The
# real proof that nothing will fight over ownership once every release is
# `helm install`ed is rendering them ALL and checking, on the actual
# manifests `helm template` produces, that no two releases claim the same
# (kind, namespace, name) — and, for cluster-scoped kinds, no two releases in
# the WHOLE run claim the same (kind, name) regardless of namespace.

def argo_release_name(namespace: str, release: str) -> str:
    """The Helm release name Argo CD renders a release under: its Application's
    name (ms-applicationSet names it <file>-<namespace's last word>[-<folder>]
    and sets no helm.releaseName). Verification renders under THIS name, not the
    file's: anything the chart derives from .Release.Name — the fullname of a
    workload whose fullnameOverride is missing — then comes out as Argo CD would
    apply it, a renamed StatefulSet included."""
    return f"{release}-{app_name_suffix(namespace)}"


def helm_template(chart: Path, release: str, namespace: str, value_files: list[Path]) -> list[dict]:
    cmd = ["helm", "template", release, str(chart), "--namespace", k8s_ns(namespace)]
    for vf in value_files:
        cmd += ["-f", str(vf)]
    # encoding="utf-8" is NOT optional, and its absence was a real data-corruption
    # bug rather than a cosmetic one. `text=True` alone decodes the child's stdout
    # with locale.getpreferredencoding() — on Windows that is the ANSI codepage
    # (cp1252 on a US install), while Helm always emits UTF-8. Decoding UTF-8
    # bytes with a single-byte codepage is textbook mojibake: `Ľ` (U+013D, bytes
    # C4 BD) arrives as `Ä¾`, an em-dash (U+2014, bytes E2 80 94) as `â€"`. Every
    # such character in a ConfigMap reaches the cluster corrupted — a mangled name
    # inside an XML config ships as mangled, and a mangled character inside a
    # shell script is executed.
    #
    # This is the ONE place the pipeline crosses a process boundary; every
    # read_text/write_text in this file already passes encoding="utf-8".
    # `errors="replace"` matches the known-good pattern in convert_and_verify.py: a byte
    # sequence that is genuinely not UTF-8 should surface as U+FFFD in the diff,
    # not abort a 25-release verification run.
    result = subprocess.run(cmd, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(f"helm template failed for {release}: {result.stderr}")
    return [d for d in yaml.load_all(result.stdout, Loader=_HelmOutputLoader) if d]


def verify_no_conflicts(chart: Path, output_dir: Path, skip: frozenset = frozenset()) -> tuple[int, list[str]]:
    """Renders every generated release and returns (releases_rendered, conflicts).

    Each top-level entry under output_dir (other than NON_NAMESPACE_OUTPUT_DIRS,
    base/, and cluster-shared) is a namespace directory containing the FLAT
    values/ tree written by write_microservice_output — releases are
    discovered by globbing values/*.yaml exactly as the ms-applicationSet
    chart's git "files" generator does, and layered through the same chain it
    builds: base/<file> -> <ns>/defaults.yaml ->
    <ns>/values/<file>. Every release goes through that one chain, 'shared'
    included — which is why <ns>/defaults.yaml is computed across the shared
    release too (see main()), so nothing can be promoted into it that shared
    did not already declare.

    Both defaults files and base/<file> MUST exist for every values file:
    ArgoCD fails the whole sync on a valueFiles entry that is not there, so a
    missing one is reported here as a conflict rather than silently skipped.

    `skip`: stale files from an earlier run (see find_stale_output). They are
    reported separately; judging them here would fail this run for objects it
    did not produce.
    """
    ns_claims: dict[str, dict[tuple[str, str], str]] = defaultdict(dict)
    cluster_claims: dict[tuple[str, str], str] = {}
    conflicts: list[str] = []
    rendered = 0

    base_dir = output_dir / BASE_DIR_NAME
    for namespace, ns_dir in namespace_dirs_of(output_dir):
        values_dir = ns_dir / VALUES_DIR_NAME
        if not values_dir.is_dir():
            continue
        ns_defaults_vf = ns_dir / DEFAULTS_FILE_NAME
        # values/ sub-folders are only for grouping: the release is the file
        # name, so one name in two folders is two Applications with one name.
        for stem, n in Counter(vf.stem for vf in values_files(values_dir)).items():
            if n > 1:
                conflicts.append(f"DUPLICATE RELEASE in '{namespace}': {n} files named '{stem}.yaml' "
                                 f"under {VALUES_DIR_NAME}/ — sub-folders do not namespace a release, "
                                 f"rename or remove all but one.")
        for vf in values_files(values_dir):
            if vf in skip:
                continue
            release = vf.stem
            base_vf = base_file_for(base_dir, values_dir, vf)
            chain = [base_vf, ns_defaults_vf, vf]
            missing = [f for f in chain if not f.exists()]
            if missing:
                conflicts.append(
                    f"BROKEN CHAIN in '{namespace}': release '{release}' is missing "
                    + ", ".join(str(f.relative_to(output_dir)) for f in missing)
                    + ". ArgoCD fails the entire sync on a valueFiles entry that does "
                      "not exist — every release needs all three layers on disk.")
                continue
            docs = helm_template(chart, argo_release_name(namespace, release), namespace, chain)
            rendered += 1
            for doc in docs:
                kind = doc.get("kind")
                name = (doc.get("metadata") or {}).get("name")
                if not kind or not name:
                    continue
                if kind in CLUSTER_SCOPED_KINDS:
                    key = (kind, name)
                    # Claimed by NAMESPACE + release, not release alone. Two
                    # namespaces each running a release called 'log-agent' are
                    # two independent ArgoCD Applications, and if both render
                    # ClusterRole/log-agent-reader they genuinely both claim one
                    # cluster object — comparing bare release names called that
                    # "the same owner" and stayed silent. It went unnoticed
                    # while handle_cluster_scoped namespace-prefixed these
                    # names, because then they never actually collided; strict
                    # naming removes the prefix and makes the real conflict
                    # reachable, so the detector has to be able to see it.
                    owner = f"{namespace}/{release}"
                    if key in cluster_claims and cluster_claims[key] != owner:
                        conflicts.append(f"CLUSTER CONFLICT: {kind}/{name} claimed by both "
                                         f"'{cluster_claims[key]}' and '{owner}'. Cluster-scoped "
                                         f"objects are global: these two releases will overwrite "
                                         f"each other. Deploy it once via --cluster-scoped, or give "
                                         f"the copies distinct names in the raw manifests.")
                    else:
                        cluster_claims[key] = owner
                else:
                    key = (kind, name)
                    # --variants: prd/yellow and prd/black share namespace
                    # 'prd', so their releases are checked against each other.
                    claims, owner = ns_claims[k8s_ns(namespace)], release
                    if "/" in namespace:
                        owner = f"{namespace.split('/', 1)[1]}/{release}"
                    if key in claims and claims[key] != owner:
                        conflicts.append(f"NAMESPACE CONFLICT in '{k8s_ns(namespace)}': {kind}/{name} claimed by both "
                                         f"'{claims[key]}' and '{owner}'")
                    else:
                        claims[key] = owner

    cluster_shared_dir = output_dir / CLUSTER_SHARED_DIR_NAME / VALUES_DIR_NAME
    if cluster_shared_dir.exists() and cluster_shared_dir / "cluster-shared-values.yaml" not in skip:
        vf = cluster_shared_dir / "cluster-shared-values.yaml"
        docs = helm_template(chart, "cluster-shared-resources", "platform-shared", [vf])
        rendered += 1
        for doc in docs:
            kind, name = doc.get("kind"), (doc.get("metadata") or {}).get("name")
            if kind in CLUSTER_SCOPED_KINDS and name:
                key = (kind, name)
                if key in cluster_claims and cluster_claims[key] not in (
                        CLUSTER_SHARED_DIR_NAME, f"{CLUSTER_SHARED_DIR_NAME}/{CLUSTER_SHARED_DIR_NAME}"):
                    conflicts.append(f"CLUSTER CONFLICT: {kind}/{name} claimed by both "
                                     f"'{cluster_claims[key]}' and '{CLUSTER_SHARED_DIR_NAME}'")
                else:
                    cluster_claims[key] = CLUSTER_SHARED_DIR_NAME

    print(f"Rendered {rendered} releases.")
    print(f"Namespaced resource claims checked: {sum(len(v) for v in ns_claims.values())}")
    print(f"Cluster-scoped resource claims checked: {len(cluster_claims)}")
    return rendered, conflicts


# ---------------------------------------------------------------------------
# Stale-output detection (always reported; deleted only with --prune)
# ---------------------------------------------------------------------------
# The converter only ever WRITES files. Delete a microservice from the input
# and its values/<slug>.yaml and releases/<name>.yaml stay behind in the
# output forever — and since the ApplicationSet's git "files" generator
# enumerates releases/*.yaml, ArgoCD keeps deploying a microservice that no
# longer exists in the source of truth. Re-running the converter does NOT
# clean it up, which makes this easy to believe is already handled.
#
# Detection is unconditional and cheap, because silence is what made this
# dangerous. Deletion is opt-in (--prune): removing a release pointer tells
# ArgoCD to prune the workload it deploys, which is real cluster impact and
# not something a conversion run should do to someone by surprise.

def find_stale_output(output_dir: Path, expected_values: dict[str, set], expected_base: set) -> list[Path]:
    """Generated files with no counterpart in this run's input, newest run wins."""
    stale: list[Path] = []

    base_dir = output_dir / BASE_DIR_NAME
    if base_dir.is_dir():
        stale += [f for f in sorted(base_dir.rglob("*.yaml")) if f.stem not in expected_base]

    # A tree-root defaults.yaml from an older run: no chain references it any
    # more (the chart layers <ns>/defaults.yaml first), so it is a file that
    # reads like a layer and is applied by nothing.
    if (output_dir / DEFAULTS_FILE_NAME).exists():
        stale.append(output_dir / DEFAULTS_FILE_NAME)

    # Switching an existing tree to namespace-scoped output leaves the
    # cluster-wide artefacts behind, and they are not inert: the root
    # Application still deploys cluster-shared-application.yaml by name, so a
    # surviving one would STILL be applied — precisely the object a
    # namespace-limited tenant cannot apply. The root files under their old
    # names (LEGACY_ROOT_FILE_NAMES) are flagged too.
    leftovers = [output_dir / n for n in LEGACY_ROOT_FILE_NAMES]
    if NAMESPACE_SCOPED_ONLY:
        leftovers += [output_dir / CLUSTER_SHARED_APP_FILE_NAME, output_dir / CLUSTER_SHARED_DIR_NAME]
    elif not ARGOCD_MANIFESTS:
        leftovers.append(output_dir / CLUSTER_SHARED_APP_FILE_NAME)
    # A run without --argocd-manifests writes no wiring, so an earlier run's
    # root files would keep deploying a tree that no longer matches.
    if not ARGOCD_MANIFESTS:
        leftovers += [output_dir / ROOT_APP_FILE_NAME, output_dir / ROOT_APPSET_FILE_NAME]
    for leftover in leftovers:
        if leftover.is_file():
            stale.append(leftover)
        elif leftover.is_dir():
            stale += sorted(f for f in leftover.rglob("*") if f.is_file())

    for namespace, ns_dir in namespace_dirs_of(output_dir):
        # A namespace directory whose input namespace is gone entirely: every
        # generated file under it is stale, not just the per-release ones.
        # Only when it carries a generation signature, though — with --output
        # pointed at a repo root, an unrelated tooling folder is not a vanished
        # namespace, and --prune must not delete its .yaml files.
        if namespace not in expected_values:
            if (ns_dir / VALUES_DIR_NAME).is_dir() or (ns_dir / "releases").is_dir() \
                    or (ns_dir / DEFAULTS_FILE_NAME).is_file():
                stale += [f for f in sorted(ns_dir.rglob("*.yaml"))]
            continue
        values_dir = ns_dir / VALUES_DIR_NAME
        if values_dir.is_dir():
            stale += [f for f in values_files(values_dir)
                      if f.stem not in expected_values[namespace]]
        # Everything the PREVIOUS layout wrote per namespace: the releases/
        # pointer tree the old git "files" generator enumerated, and the
        # per-namespace ApplicationSet/Application manifests. The root
        # Application no longer deploys these (it names two files outright),
        # so they are inert rather than dangerous — but they still read as the
        # source of truth to anyone opening the tree, and a stale
        # releases/*.yaml is exactly the kind of file someone re-applies by
        # hand. Reported so --prune can clear an old tree in one run.
        old_releases = ns_dir / "releases"
        if old_releases.is_dir():
            stale += sorted(f for f in old_releases.rglob("*") if f.is_file())
        stale += sorted(ns_dir.glob("*-applicationset.yaml"))
        stale += sorted(ns_dir.glob("*-shared-application.yaml"))
    return stale


# ---------------------------------------------------------------------------
# Round-trip fidelity verification (--verify-fidelity) — OPT-IN, ADDITIVE
# ---------------------------------------------------------------------------
# verify_no_conflicts above proves the output is INTERNALLY consistent: every
# release renders, and no two releases claim the same object. Neither check
# ever looks at the raw input, so a converter bug that silently dropped every
# ConfigMap in the run would pass both of them cleanly.
#
# This is the missing check — render the converted output and compare it back
# against the raw manifests it came from. Two independent steps, in order:
#
#   1. MATCH — where did each raw object end up? Several kinds of move are
#      EXPECTED, not loss, and all of them are resolved here before a single
#      field is compared:
#        * anything declared in <ns>/shared.yaml belongs to that namespace's
#          dedicated 'shared' release, not to a microservice;
#        * cluster-scoped kinds are deduped globally into the single
#          cluster-shared release (so the same ClusterRole present in four
#          namespaces renders ONCE — the other three must not read as lost);
#        * a microservice's duplicate declaration of something the shared
#          release already owns is dropped in favour of referencing it
#          (NamespaceRegistry.claim -> "reference_shared");
#        * a name collision renames the loser to '<owner>-<name>'.
#      Only "present in the raw input, rendered by NO release at all" is an
#      actual error.
#
#   2. COMPARE — for matched pairs only, deep-diff raw against rendered,
#      skipping what the chart derives itself rather than from values (see
#      _fidelity_ignored_path) and classifying a field the render ADDED as
#      informational rather than an error: a Helm chart filling in a
#      Kubernetes default the raw dump omitted is normal and harmless. A
#      field the raw had and the render LOST, or one whose value CHANGED, is
#      the real signal.
#
# Deliberately kept OUT of verify_no_conflicts and behind its own flag: the
# two checks stay decoupled so neither can break the other, and a conversion
# run that does not pass --verify-fidelity behaves byte-identically to before
# this was added.

# Labels every Helm chart stamps onto everything it renders. Their absence
# from a raw cluster dump is not data loss.
FIDELITY_HELM_LABELS = {
    # The Helm identity labels are no longer here: the converter carries them
    # (the chart's default chartLabels: false), so a changed one is a real diff ArgoCD will show.
    # Except helm.sh/chart: the converter drops it on purpose (CHART_OWNED_LABELS).
    "helm.sh/chart",
    # Not a Helm label but the same argument: ArgoCD writes it at sync time, so
    # a chart that does not render it has lost nothing. Deliberately the ONLY
    # label added here for the Route defect — the report authorises exactly this
    # row ("That single row IS suppressible. The other ~520 are not"), and a
    # broader rule would bury the 613-705 real losses this suppression sits next
    # to. Kept in step with CHART_OWNED_LABELS, which stops the converter
    # capturing it in the first place.
    "argocd.argoproj.io/instance",
}


# Kubernetes rejects most object names over 63 characters; CronJob is capped
# at 52 because it appends a timestamp suffix when it creates Jobs.
K8S_NAME_MAX = 63
CRONJOB_NAME_MAX = 52

# Values maps whose KEY becomes the Kubernetes object name verbatim — the
# chart renders `name: {{ $name }}` for each of these with no truncation
# (templates/configmap.yaml, secret.yaml, pvc.yaml, rbac.yaml,
# networkpolicy.yaml, services.yaml), so an over-length key is an object the
# API server refuses. That is the dangerous case: nothing catches it until
# apply time, on the cluster.
RAW_NAME_VALUE_MAPS = [
    ("configMaps",), ("secrets",), ("pvc",), ("networkPolicies",),
    ("routes",), ("secretStores",), ("externalSecrets",), ("services",),
    ("rbac", "roles"), ("rbac", "roleBindings"),
    ("rbac", "clusterRoles"), ("rbac", "clusterRoleBindings"),
]


def validate_resource_names(namespace: str, ms_name: str, values: dict) -> tuple[list[str], list[str]]:
    """Catch names Kubernetes will reject, at conversion time.

    Returns (errors, warnings). Errors are names that simply cannot be
    created; warnings are names the chart will silently TRUNCATE, which is
    milder but still means the object is not called what the manifest said.

    This matters more since cluster-scoped resources started being prefixed
    with their namespace in namespace-scoped mode: the showcase already
    produces a 58-character name, five short of the limit, and a realistic
    on-prem namespace would push it over.
    """
    errors: list[str] = []
    warnings: list[str] = []
    where = f"[{namespace}/{ms_name}]"

    fullname = values.get("fullnameOverride") or ms_name
    if len(fullname) > K8S_NAME_MAX:
        warnings.append(
            f"{where} release name '{fullname}' is {len(fullname)} chars; the chart truncates it "
            f"to {K8S_NAME_MAX}, so the workload/Service/ServiceAccount will not be named what "
            f"the manifest says. Shorten the file name (or its fullnameOverride)."
        )

    for path in RAW_NAME_VALUE_MAPS:
        node = values
        for part in path:
            node = (node or {}).get(part) or {}
        for key in node:
            if len(key) > K8S_NAME_MAX:
                errors.append(
                    f"{where} {'.'.join(path)}/'{key}' is {len(key)} chars — Kubernetes rejects "
                    f"names over {K8S_NAME_MAX}, and this one is rendered verbatim with no "
                    f"truncation, so it would fail on apply."
                )

    # CronJob/Job names render as the values-map key VERBATIM
    # (universal-chart.subresourceName) and ARE truncated by the chart, so they
    # fail quietly rather than loudly. Counting '<release>-<key>' here
    # double-counted the release name and raised false "too long" warnings.
    for key in (values.get("cronjobs") or {}):
        rendered = len(key)
        if rendered > CRONJOB_NAME_MAX:
            warnings.append(
                f"{where} CronJob '{key}' is {rendered} chars; the chart truncates "
                f"CronJob names to {CRONJOB_NAME_MAX} (Kubernetes appends a timestamp when it "
                f"spawns Jobs), so it will be renamed silently."
            )
    for key in (values.get("jobs") or {}):
        rendered = len(key)
        if rendered > K8S_NAME_MAX:
            warnings.append(
                f"{where} Job '{key}' is {rendered} chars and will be truncated to "
                f"{K8S_NAME_MAX} by the chart."
            )
    return errors, warnings


def _dot(path: tuple) -> str:
    return ".".join(str(p) for p in path).replace(".[", "[") or "<root>"


def _fidelity_ignored_path(kind: str, path: tuple) -> bool:
    """Paths the Universal Chart derives ITSELF rather than from values —
    diffing them compares the chart's labelling convention against the source
    cluster's, which is noise rather than a converter defect.

    Every entry below is here for a specific, verified reason, NOT as a
    blanket "ignore all labels" rule:

    * <workload>.spec.selector.matchLabels, Service.spec.selector, and
      PodDisruptionBudget/ServiceMonitor .spec.selector.matchLabels all render
      from the `universal-chart.selectorLabels` helper
      (universal-chart/templates/_helpers.tpl), which is computed from
      Release.Name plus the chart name and never reads .Values at all. The
      converter cannot influence them — handle_service() drops the raw
      selector for the primary Service for exactly this reason.
    * spec.template.metadata.labels comes from that same helper plus
      .Values.podLabels, into which handle_workload deliberately does not copy
      the raw `app` / `app.kubernetes.io/name` / `app.kubernetes.io/instance` /
      `pod-template-hash` labels, since those are the chart's to set.

    Note this is scoped to the SELECTOR paths only. A resource's own
    metadata.labels are still compared, so a label the raw manifest carried
    and the render lost is still reported.
    """
    # Workload selector / pod template labels and Service.spec.selector are
    # compared now: the converter pins them (selectorLabels, service.selector).
    if kind in ("PodDisruptionBudget", "ServiceMonitor") and path == ("spec", "selector", "matchLabels"):
        return True
    # NOTE: containers[0].name used to be suppressed here, because the chart
    # named its managed container after the release and every workload
    # therefore reported a renamed container. That suppression is gone: the
    # converter now carries the raw name (handle_main_container /
    # handle_cronjob / handle_job -> .Values.containerName, rendered by
    # templates/_pod-spec.tpl), so the names match and there is nothing to
    # hide. Keeping it would have left the checker permanently unable to see a
    # container[0] that genuinely disappeared, which is the one thing it most
    # needs to catch.
    return False


def _is_benign_default_loss(kind: str, path: tuple, value: Any) -> bool:
    """A field the converter deliberately does not carry, whose absence changes
    nothing on the target.

    Keyed on kind + exact path + THE VALUE, not on the path alone. That is the
    whole discipline of this rule, and the reason the report's own bulk
    suppression list was dangerous: it grouped fields by where they sit in the
    pod spec rather than by whether losing them does anything, and swept up
    three real losses that way (minReadySeconds, volumeClaimTemplate labels,
    allocateLoadBalancerNodePorts) — each of which is FIXED in this batch rather
    than suppressed, so none of them can reach here.

    Every entry below states the condition that makes it benign, and the
    condition is checked rather than assumed:

    * Service.spec.sessionAffinity == "None" — literally the Kubernetes default.
      Scoped to the VALUE, because `ClientIP` is a real setting that
      build_service_entry carries; a path-only rule would hide a lost ClientIP,
      which the previous round rated MEDIUM ("Load balancer session affinity
      broken").
    * ServiceAccount.secrets / .imagePullSecrets — decided in the previous round
      (ServiceAccount_Secrets_And_ImagePullSecrets_Lost) and independently
      re-reached by this report: Kubernetes creates and rotates SA token
      Secrets itself, and OpenShift's `<sa>-dockercfg-XXXXX` names an object
      that does not exist on a vanilla target. handle_serviceaccount carries a
      hand-added pull credential and drops only these, so the two rules agree.

    Deliberately NOT here, though the report's bulk list includes them:
    enableServiceLinks, shareProcessNamespace, pod-template name/namespace, and
    `immutable: false`. Each has its own folder in this batch with its own
    stated condition to verify, and pre-empting those verdicts from inside a
    bulk rule is precisely the mistake this function exists to avoid.
    """
    if kind == "Service" and path == ("spec", "sessionAffinity") and value == "None":
        return True
    if kind == "ServiceAccount" and path in (("secrets",), ("imagePullSecrets",)):
        return True
    # shareProcessNamespace == False — the Kubernetes default, so the rendered
    # pod behaves identically. Scoped to False: `true` is the value that means
    # something (containers see each other's processes, which debugging sidecars
    # and some service meshes depend on), and it is rendered by the chart, so it
    # can never reach here.
    #
    # Suppressed rather than rendered, which is the opposite of the neighbouring
    # enableServiceLinks. The reason is not the field's semantics but the
    # chart's: values.yaml declares `shareProcessNamespace: false` as a default,
    # so a hasKey guard is always true and rendering it would stamp the field
    # onto every pod the chart produces — inventing it on workloads whose source
    # never mentioned it. enableServiceLinks has no declared default, so the same
    # guard there means what it says. See folder 21's STATUS.md.
    if kind in WORKLOAD_KINDS and \
            path == ("spec", "template", "spec", "shareProcessNamespace") and value is False:
        return True
    # A RoleBinding subject in the binding's own namespace: the namespace is
    # the default there, and the converter drops it so the release stays
    # portable (handle_rolebinding). A subject naming another one is kept.
    # A claim template whose only labels are the identity pair the chart owns:
    # the StatefulSet controller stamps its selector labels onto every PVC it
    # creates, so carrying the source chart's copies would change nothing.
    if kind == "StatefulSet" and len(path) == 5 and path[:2] == ("spec", "volumeClaimTemplates") \
            and path[3:] == ("metadata", "labels") and isinstance(value, dict) \
            and set(value) <= (FIDELITY_HELM_LABELS | {"app"}):
        return True
    if kind == "RoleBinding" and len(path) == 3 and path[0] == "subjects" and path[2] == "namespace" \
            and value == _FIDELITY_NAMESPACE[0]:
        return True
    return False


# The namespace verify_raw_fidelity is comparing, for _is_benign_default_loss.
_FIDELITY_NAMESPACE: list = [None]


def _strip_helm_labels(doc: dict) -> dict:
    doc = copy.deepcopy(doc)
    meta = doc.get("metadata")
    if isinstance(meta, dict) and isinstance(meta.get("labels"), dict):
        for key in FIDELITY_HELM_LABELS:
            meta["labels"].pop(key, None)
        # An emptied labels map is deliberately KEPT rather than removed: drop
        # the key and the raw side's whole `labels` block reads as one lost
        # field, hiding which individual labels actually differ. Left in place,
        # the diff recurses key-by-key and the chart-owned identity labels are
        # skipped there (see _fidelity_diff), so only a genuinely lost label
        # is reported.
    return doc


def _item_name(item: Any) -> Optional[str]:
    """The name a list entry is addressed by: `name` for containers/volumes/
    env/ports, or `metadata.name` for embedded objects like a StatefulSet's
    volumeClaimTemplates."""
    if not isinstance(item, dict):
        return None
    if isinstance(item.get("name"), str):
        return item["name"]
    meta = item.get("metadata")
    if isinstance(meta, dict) and isinstance(meta.get("name"), str):
        return meta["name"]
    # A container port is not required to have a name, and one unnamed port used
    # to drop the WHOLE list back to positional comparison — so the chart's
    # sorted-key reordering was reported as a change to every port at once,
    # instead of naming the one field that actually differed. The port number is
    # the identity Kubernetes uses when there is no name (a container may not
    # declare the same containerPort/protocol pair twice), so it works as a key.
    if isinstance(item.get("containerPort"), int):
        return f"#port:{item['containerPort']}/{item.get('protocol', 'TCP')}"
    return None


def _by_name(items: list):
    """Index a list of named dicts by name, or None if that doesn't apply.

    Compared positionally, a pure reordering would read as a difference — and
    the chart legitimately reorders: the converter stores containers, volumes,
    volumeMounts, env and ports as MAPS keyed by name, and the chart's
    mapToList helper renders them back in sorted-key order. Kubernetes itself
    addresses every one of these by name and never by position, so keying by
    name compares what actually matters. Returns None for lists where this
    doesn't hold (e.g. `args`, where order IS the meaning), which then fall
    back to exact ordered comparison.
    """
    if not items:
        return None
    names = [_item_name(i) for i in items]
    if any(n is None for n in names):
        return None  # unnamed — positional is safer
    if len(set(names)) == len(names):
        return dict(zip(names, items))

    # Duplicate names. This used to fall straight back to positional comparison,
    # which made a pure REORDERING read as a wholesale CHANGED of the entire
    # list — the report's "identical volumeMounts lists but different dictionary
    # key ordering" on midasson-blue-fac1 and midasson-yellow-tst.
    #
    # Duplicate names are not a malformed manifest here: mounting one volume
    # several times under different subPaths is the standard way to project
    # individual ConfigMap keys onto individual files, and every such mount
    # carries the same `name`. named_list_to_map already treats
    # '<name>:<subPath or mountPath>' as the real identity for exactly this
    # reason — so the checker was using a coarser identity than the converter,
    # the same mismatch that lost a container port in folder 6, and with the
    # same resolution: the checker adopts the identity that actually
    # distinguishes the objects.
    #
    # Falls back to positional only if the composite is ALSO ambiguous, so a
    # list this cannot identify is still compared conservatively rather than
    # paired by guesswork.
    composite = [
        f"{n}:{i.get('subPath') or i.get('mountPath')}"
        if isinstance(i, dict) and (i.get("subPath") or i.get("mountPath")) else None
        for n, i in zip(names, items)
    ]
    if any(c is None for c in composite) or len(set(composite)) != len(composite):
        return None
    return dict(zip(composite, items))


_NO_DEFAULT = object()
# What the API server writes for an empty value of these fields.
KUBERNETES_FILLED_DEFAULTS = {"podManagementPolicy": "OrderedReady"}
# Fields whose dropped value is exactly what the API server fills in anyway.
KUBERNETES_DEFAULTS = {"hostNetwork": False, "hostIPC": False, "hostPID": False,
                       "priorityClassName": "", "minReadySeconds": 0}


def _fidelity_diff(kind: str, raw: Any, rendered: Any, path: tuple = ()) -> list[tuple[str, str, str]]:
    """Deep-compare one raw object against its rendered counterpart.

    Returns (severity, dotted-path, detail) triples, where severity is:
      LOST    — the raw manifest had it, the render does not   (real risk)
      CHANGED — both have it, with different values            (real risk)
      ADDED   — the render has it, the raw did not             (informational)
    """
    if _fidelity_ignored_path(kind, path):
        return []

    if isinstance(raw, dict) and isinstance(rendered, dict):
        out: list[tuple[str, str, str]] = []
        # An object's own metadata.labels are compared — a label the raw
        # manifest carried and the render dropped is real — EXCEPT for the
        # identity labels the chart owns and sets itself from Release.Name
        # (see FIDELITY_HELM_LABELS and handle_workload's podLabels filter).
        # Those four are the chart's to define, so the raw dump's versions
        # disappearing is the intended substitution, not loss.
        skip = FIDELITY_HELM_LABELS if path[-1:] == ("labels",) else set()
        # `serviceAccount` is Kubernetes' DEPRECATED alias for
        # `serviceAccountName`, and the API server populates both with the same
        # value. The chart renders only the modern key, which is correct — but
        # comparing key-by-key made every workload report a permanent
        # `LOST spec.template.spec.serviceAccount`, noise that trains people to
        # skim this report. Skimming it is how the real ServiceAccount defect
        # (pods naming an SA that does not exist) went unnoticed.
        #
        # Suppressed only when the modern key is present AND holds the same
        # value — matched on the values rather than the path, so it can only
        # ever fire inside a pod spec. A pod spec where the two DISAGREE, or one
        # carrying the legacy key alone, is still reported: those are cases
        # where something really is wrong.
        if isinstance(raw.get("serviceAccount"), str) and \
                raw.get("serviceAccount") in (raw.get("serviceAccountName"), rendered.get("serviceAccountName")):
            skip = skip | {"serviceAccount"}
        # `checksum/*` pod annotations hold a hash of the referenced
        # ConfigMap/Secret content, and exist so that changing the content
        # changes the pod template and Kubernetes performs a rolling restart.
        # The raw values are hashes of the SOURCE cluster's content, so they are
        # correctly not carried — but only if something regenerates them.
        #
        # error.txt makes that the whole condition: "the benign verdict depends
        # entirely on who regenerates them [...] If the universal-chart itself is
        # expected to compute them, and it does not, then config changes stop
        # triggering restarts."
        #
        # So the condition is CHECKED here rather than assumed: raw checksum
        # annotations are ignored only when the RENDER carries at least one of
        # its own. A render with none means nothing regenerates them, the
        # restart-on-config-change behaviour is genuinely gone, and every raw
        # checksum key reports as lost. The key NAMES need not match — the chart
        # emits checksum/configmaps and checksum/secrets — because what matters
        # is that the pod template moves when the content does, not what the
        # annotation is called.
        #
        # Applied to BOTH sides. The raw keys are ignored, and so are the
        # chart's regenerated ones — otherwise the render's checksum/configmaps
        # comes back as an ADDED field on every workload that had checksums,
        # which is the asymmetric suppression this function already warns about
        # a few lines down ("worse than none: it makes the rule look
        # unreliable").
        # A restart stamp (RESTART_STAMP_ANNOTATIONS) is left out on purpose:
        # Argo CD syncs with ServerSideApply, so the live stamp — owned by
        # `kubectl rollout restart`'s field manager, not by Argo — stays on the
        # pod template and nothing restarts. Not a loss; an annotations map that
        # held nothing else is not one either.
        if path[-1:] == ("annotations",):
            skip = skip | (set(raw) & RESTART_STAMP_ANNOTATIONS)
        if path[-1:] == ("metadata",) and "annotations" not in rendered and \
                isinstance(raw.get("annotations"), dict) and set(raw["annotations"]) <= RESTART_STAMP_ANNOTATIONS:
            skip = skip | {"annotations"}
        if path[-1:] == ("annotations",) and isinstance(rendered, dict) and \
                any(k.startswith("checksum/") for k in rendered):
            skip = skip | {k for k in raw if k.startswith("checksum/")} \
                        | {k for k in rendered if k.startswith("checksum/")}
        for key, val in raw.items():
            if key in skip:
                continue
            if key not in rendered:
                # An EMPTY map and an absent key are the same thing to
                # Kubernetes: `resources: {}` and no `resources:` produce
                # byte-identical objects once the API server is done. Reporting
                # them was ~600 of the report's ~1,960 LOST lines — by far its
                # largest single source of noise, and noise in this report is
                # not free: it is what made the real ServiceAccount and
                # StatefulSet defects easy to skim past.
                #
                # Scoped to {} and [] exactly. A raw `false`, `0` or `""` is a
                # deliberate value that the render dropping IS a loss, and stays
                # reported — that distinction is the whole of folder 15.
                if val == {} or val == []:
                    continue
                # A null is the same absence as {} — `envFrom:`, `nodePort:` and
                # `annotations:` rendered empty by a source chart were ~120 of a
                # 57-chart run's ~200 errors. And a value that IS the Kubernetes
                # default for that field changes nothing when dropped; only those
                # few, by exact value (0 and false stay reported everywhere else:
                # ttlSecondsAfterFinished 0 and enableServiceLinks false matter).
                if val is None or KUBERNETES_DEFAULTS.get(key, _NO_DEFAULT) == val:
                    continue
                if _is_benign_default_loss(kind, path + (key,), val):
                    continue
                out.append(("LOST", _dot(path + (key,)), f"raw had {val!r}"))
            else:
                out += _fidelity_diff(kind, val, rendered[key], path + (key,))
        for key, val in rendered.items():
            # _fidelity_ignored_path is consulted here as well as on the way
            # down. It used to gate only the recursive descent, which meant a
            # path was suppressed when the raw HAD the field and reported when
            # the raw did not — so a Service whose raw manifest declared no
            # selector still produced `ADDED spec.selector` even though that
            # exact path is listed as chart-derived. Asymmetric suppression is
            # worse than none: it makes the rule look unreliable.
            if key not in raw and key not in skip \
                    and not _fidelity_ignored_path(kind, path + (key,)):
                out.append(("ADDED", _dot(path + (key,)), f"render added {val!r}"))
        return out

    if isinstance(raw, list) and isinstance(rendered, list):
        # A single-element list on both sides can't be ambiguous: pair them
        # positionally even if the names differ, so the chart renaming the one
        # container it manages (raw `nginx` -> the release's own name) reports
        # as one CHANGED name rather than a LOST+ADDED pair that hides whether
        # the container's actual contents survived.
        if len(raw) == 1 and len(rendered) == 1:
            return _fidelity_diff(kind, raw[0], rendered[0], path + ("[0]",))
        raw_map, rendered_map = _by_name(raw), _by_name(rendered)
        if raw_map is not None and rendered_map is not None:
            out = []
            # Init containers run one after another, so their ORDER is part of
            # the spec — matching by name alone hid a builder that sorted them
            # (a migration running before the wait-for-database it depends on).
            # env and volumeMounts are written as lists in the deployment's
            # own order, so any reorder there is a converter bug too: it rolls
            # the pods, and `$(VAR)` expands only vars defined earlier.
            if path[-1:] in (("initContainers",), ("env",), ("volumeMounts",)):
                raw_order = [n for n in raw_map if n in rendered_map]
                rendered_order = [n for n in rendered_map if n in raw_map]
                if raw_order != rendered_order:
                    out.append(("CHANGED", _dot(path), f"order raw={raw_order} render={rendered_order}"))
            for name, item in raw_map.items():
                if name not in rendered_map:
                    out.append(("LOST", _dot(path + (f"[{name}]",)), "present in raw, absent from render"))
                else:
                    out += _fidelity_diff(kind, item, rendered_map[name], path + (f"[{name}]",))
            for name in rendered_map:
                if name not in raw_map:
                    out.append(("ADDED", _dot(path + (f"[{name}]",)), "present in render, absent from raw"))
            return out
        if raw != rendered:
            return [("CHANGED", _dot(path), f"raw={raw!r} render={rendered!r}")]
        return []

    # An empty raw value the render spells as the API default it stands for.
    if raw in (None, "") and path and KUBERNETES_FILLED_DEFAULTS.get(path[-1]) == rendered:
        return []
    if raw != rendered:
        return [("CHANGED", _dot(path), f"raw={raw!r} render={rendered!r}")]
    return []


def load_raw_index(ns_dir: Path) -> dict[tuple[str, str], list[tuple[str, dict]]]:
    """Index one namespace's RAW input by (kind, name) -> [(source file stem, doc)].

    Applies the same sanitize_object + filter_transient the converter itself
    applies on the way in, so the baseline this test compares against is
    "everything the converter was ever asked to convert" — without it, every
    Pod/ReplicaSet/Endpoints object in a `oc get all -o yaml` dump would be
    reported as dropped, when dropping them is correct behaviour.

    A LIST per key, not a single entry: two files in one namespace may both
    declare e.g. ConfigMap/app-config, which is a real collision the converter
    resolves by renaming the second to '<owner>-<name>'. Keyed one-to-one this
    silently kept only the last one read and then compared it against the
    other one's rendered object — reporting a phantom content mismatch for a
    conversion that was entirely correct.
    """
    index: dict[tuple[str, str], list[tuple[str, dict]]] = defaultdict(list)
    for path in sorted(ns_dir.glob("*.yaml")):
        for doc in filter_transient([sanitize_object(d) for d in load_multidoc(path)]):
            kind = doc.get("kind")
            name = (doc.get("metadata") or {}).get("name")
            if kind and name:
                index[(kind, name)].append((path.stem, doc))
    return index


# The path is matched non-greedily up to a colon FOLLOWED BY WHITESPACE, not to
# the first colon: `_dot()` renders an unnamed container port as the synthetic
# `ports[#port:9125/TCP]`, so a field path can legitimately contain a colon and
# stopping at the first one truncates it to `ports[#port` — which then buckets
# every unnamed port under its own category of one.
# Field paths whose value is an object NAME rather than data. A CHANGED here is
# a reference following (or failing to follow) a rename, not a content change.
_REFERENCE_PATH_TAILS = (
    "configMap.name", "configMapRef.name", "configMapKeyRef.name",
    "secret.secretName", "secretRef.name", "secretKeyRef.name",
    "serviceAccountName", "to.name",
)

_CHANGED_DETAIL = re.compile(r"^raw=(?P<raw>.+?) render=(?P<render>.+)$")


def _is_followed_rename(field_path: str, detail: str, rendered_index: dict) -> bool:
    """True when a CHANGED reference points at a name this namespace renders.

    Deliberately conservative on both ends. It fires only on a known reference
    path — so a CHANGED to a ConfigMap's DATA can never be mistaken for one —
    and only when the new value names an object that actually exists in the
    render. A reference rewritten to something nothing produces stays an ERROR,
    and the reference-integrity pass reports it separately with a message that
    says what is wrong rather than just that two strings differ.
    """
    if not field_path.endswith(_REFERENCE_PATH_TAILS):
        return False
    match = _CHANGED_DETAIL.match(detail)
    if not match:
        return False
    try:
        rendered_name = ast.literal_eval(match.group("render"))
    except (ValueError, SyntaxError):
        return False
    return isinstance(rendered_name, str) and \
        any(n == rendered_name for (_k, n) in rendered_index)


def _identity_free_hash(doc: dict) -> str:
    """content_hash, but blind to the object's NAME as well as its labels.

    Used to recognise the SAME object under a different name. content_hash keeps
    metadata.name — correctly, since it answers "are these two objects
    interchangeable" — but the question here is "did this object survive the
    conversion under a name I cannot predict", and for that the name is the one
    field that must not count.

    Also drops the chart's own metadata so a rendered object can match its raw
    counterpart: _strip_helm_labels handles labels, and content_hash already
    ignores annotations.
    """
    d = copy.deepcopy(doc)
    meta = d.get("metadata")
    if isinstance(meta, dict):
        meta.pop("name", None)
    # The chart always writes a ServiceAccount's automountServiceAccountToken;
    # `true` is what an unset one means, so it does not make it another object.
    if d.get("kind") == "ServiceAccount" and d.get("automountServiceAccountToken") is True:
        d.pop("automountServiceAccountToken")
    return content_hash(d)


def collect_object_references(doc: dict) -> list[tuple[str, str, str]]:
    """Every by-NAME reference a rendered object makes, as (kind, name, where).

    A rename is only safe if every reference follows it. The converter does
    rewrite them — env_list / remap_envfrom / remap_volumes /
    remap_container_refs all consult ctx.name_map — but "we rewrote the ones we
    know about" is a different claim from "no reference dangles", and the report
    could not decide between "real issue" and "internally consistent / benign"
    precisely because nobody had checked.

    Walked generically rather than by following the values schema: the point is
    to catch a reference site the remapping code does NOT know about, and a
    checker that looked only where the remapper looks could never find one.
    """
    found: list[tuple[str, str, str]] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key, kind, sub in (
                ("configMap", "ConfigMap", "name"),
                ("configMapRef", "ConfigMap", "name"),
                ("configMapKeyRef", "ConfigMap", "name"),
                ("secret", "Secret", "secretName"),
                ("secretRef", "Secret", "name"),
                ("secretKeyRef", "Secret", "name"),
            ):
                ref = node.get(key)
                if isinstance(ref, dict) and isinstance(ref.get(sub), str):
                    found.append((kind, ref[sub], f"{path}.{key}"))
            if isinstance(node.get("serviceAccountName"), str):
                found.append(("ServiceAccount", node["serviceAccountName"],
                              f"{path}.serviceAccountName"))
            for k, v in node.items():
                walk(v, f"{path}.{k}")
        elif isinstance(node, list):
            for i, item in enumerate(node):
                walk(item, f"{path}[{i}]")

    walk(doc.get("spec"), "spec")
    # A Route's backing Service, which is a reference by another name.
    to = ((doc.get("spec") or {}).get("to") or {}) if doc.get("kind") == "Route" else {}
    if isinstance(to.get("name"), str):
        found.append(("Service", to["name"], "spec.to.name"))
    return found


_FIDELITY_FIELD_ERROR = re.compile(
    r"^\s*ERROR\s+(?P<kind>[A-Za-z0-9.]+)/\S+\s+(?P<verb>LOST|CHANGED)\s+at\s+(?P<path>.+?):\s")


def fidelity_error_breakdown(report: list[str]) -> list[str]:
    """Count THIS run's ERROR lines by category, for the report's own header.

    The point is not the counts — the summary block above already has totals.
    It is that a per-category breakdown now EXISTS as generated output, so
    nobody has to keep one by hand. The defect this closes is a hand-maintained
    summary and a generated report being used interchangeably to set priorities
    while describing different runs: eight error types the summary rated
    significant appeared zero times in the report, and the two documents had no
    way to say so.

    Categories match sanitize_fidelity_report.categorise() — field errors split
    by the LEAF of the field path, because "76 objects lost metadata.labels.team"
    and "1 object lost spec.schedule" are different problems that happen to
    share a verb. Lumping them is exactly the summarisation that hides things.
    """
    counts: Counter[str] = Counter()
    for line in report:
        stripped = line.strip()
        if not stripped.startswith("ERROR") and not stripped.startswith("RENDER FAILURE"):
            continue
        if stripped.startswith("RENDER FAILURE"):
            counts["render_failure"] += 1
            continue
        match = _FIDELITY_FIELD_ERROR.match(line)
        if match:
            leaf = re.sub(r"\[[^\]]*\]", "", match.group("path").rstrip(".").split(".")[-1])
            counts[f"field_{match.group('verb').lower()}_{leaf or 'field'}"] += 1
        elif "rendered by NO release" in stripped:
            counts["missing_object"] += 1
        elif "present in NO raw manifest" in stripped:
            counts["extra_object"] += 1
        else:
            # Never silently absorbed into a neighbouring bucket: an ERROR shape
            # this function does not recognise is the one most likely to be a
            # genuinely new problem, so it is counted under its own name and
            # stays visible until someone teaches the parser about it.
            counts["other_error"] += 1
    if not counts:
        return ["", "ERROR BREAKDOWN BY CATEGORY (generated from the lines below): none", ""]
    width = max(len(c) for c in counts)
    return ["", "ERROR BREAKDOWN BY CATEGORY (generated from the lines below):"] + [
        f"  {cat:<{width}}  {n}" for cat, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ] + [""]


def verify_raw_fidelity(chart: Path, input_dir: Path, output_dir: Path,
                        skip: frozenset = frozenset()) -> tuple[list[str], dict]:
    """Round-trip check: does the rendered output still say what the raw input said?

    Returns (report_lines, stats). Renders independently of verify_no_conflicts
    (same layering rules, re-derived) so the two checks cannot break each other.
    """
    report: list[str] = []
    stats = {"matched": 0, "relocated": 0, "lost": 0, "changed": 0, "added": 0,
             "missing_objects": 0, "extra_objects": 0, "render_failures": 0,
             "dangling_refs": 0}

    base_dir = output_dir / BASE_DIR_NAME

    # The single global cluster-shared release owns every cluster-scoped kind
    # in the whole run, so it is rendered once and matched against by every
    # namespace (see the dedupe note in this section's header comment).
    cluster_index: dict[tuple[str, str], dict] = {}
    cluster_shared_vf = output_dir / CLUSTER_SHARED_DIR_NAME / VALUES_DIR_NAME / "cluster-shared-values.yaml"
    if cluster_shared_vf.exists() and cluster_shared_vf not in skip:
        try:
            for doc in helm_template(chart, "cluster-shared-resources", "platform-shared", [cluster_shared_vf]):
                kind, name = doc.get("kind"), (doc.get("metadata") or {}).get("name")
                if kind and name:
                    cluster_index[(kind, name)] = doc
        except RuntimeError as exc:
            stats["render_failures"] += 1
            report.append(f"RENDER FAILURE cluster-shared: {exc}")

    # Compared per KUBERNETES namespace: variant folders (dev/yellow, dev/black)
    # are all namespace 'dev', and so is the raw input they came from.
    out_folders: dict[str, list[Path]] = defaultdict(list)
    for key, d in namespace_dirs_of(output_dir):
        out_folders[k8s_ns(key)].append(d)
    raw_folders: dict[str, list[Path]] = defaultdict(list)
    for key, d in namespace_dirs_of(input_dir, is_input_ns):
        raw_folders[k8s_ns(key)].append(d)
    for namespace, ns_dirs in sorted(out_folders.items()):
        if namespace not in raw_folders:
            continue

        # Render every release in this namespace, in the ApplicationSet's own
        # value-file order, and index what came out by (kind, name).
        rendered_index: dict[tuple[str, str], tuple[str, dict]] = {}
        for ns_dir in ns_dirs:
            values_dir = ns_dir / VALUES_DIR_NAME
            ns_defaults_vf = ns_dir / DEFAULTS_FILE_NAME
            for vf in values_files(values_dir):
                if vf in skip:
                    continue
                release = vf.stem
                base_vf = base_file_for(base_dir, values_dir, vf)
                value_files = [f for f in (base_vf, ns_defaults_vf, vf) if f.exists()]
                try:
                    docs = helm_template(chart, argo_release_name(namespace, release), namespace, value_files)
                except RuntimeError as exc:
                    stats["render_failures"] += 1
                    report.append(f"RENDER FAILURE {namespace}/{release}: {exc}")
                    continue
                for doc in docs:
                    kind, name = doc.get("kind"), (doc.get("metadata") or {}).get("name")
                    if kind and name:
                        rendered_index[(kind, name)] = (release, doc)

        raw_index: dict[tuple[str, str], list[tuple[str, dict]]] = defaultdict(list)
        for raw_dir in raw_folders[namespace]:
            for k, v in load_raw_index(raw_dir).items():
                raw_index[k] += v
        # What --include / --exclude left out was never asked to be converted.
        left_out = LEFT_OUT.get(namespace, set())
        n_left_out = 0
        for k in list(raw_index):
            kept = [(stem, d) for stem, d in raw_index[k] if (stem, *k) not in left_out]
            n_left_out += len(raw_index[k]) - len(kept)
            if kept:
                raw_index[k] = kept
            else:
                del raw_index[k]
        raw_total = sum(len(v) for v in raw_index.values())
        # The raw file behind this namespace's shared release, env suffix and
        # all (shared-prd.yaml) — resolved the same way main() resolves it.
        shared_raw_stem = next((stem for entries in raw_index.values() for stem, _ in entries
                                if env_normalized_ms_name(namespace, stem) == SHARED_DIR_NAME), None)
        report.append(f"\n=== {namespace} — {raw_total} raw objects vs {len(rendered_index)} rendered ===")
        if n_left_out:
            report.append(f"  INFO   {n_left_out} raw object(s) left out by --include / --exclude — not checked")
        _FIDELITY_NAMESPACE[0] = namespace

        # Releases whose rendered output includes an HPA. The chart omits
        # spec.replicas for those on purpose (templates/deployment.yaml guards
        # it with `if not .Values.hpa.enabled`, since setting a replica count
        # AND an autoscaler is a Helm anti-pattern) — so a missing replicas
        # there is correct, while a missing one WITHOUT an autoscaler is still
        # a real loss and must keep reporting.
        hpa_releases = {rel for (k, _n), (rel, _d) in rendered_index.items()
                        if k == "HorizontalPodAutoscaler"}

        matched_rendered: set[tuple[str, str]] = set()
        for (kind, name), raw_entries in sorted(raw_index.items()):
          for source_stem, raw_doc in raw_entries:
            # Which release SHOULD own this, if nothing moved? The source file
            # stem, env-normalized so 'storefront-prd.yaml' in namespace
            # 'shop-web-prd' is understood as the same 'storefront' release its
            # unsuffixed siblings use (reusing the converter's own function
            # rather than re-deriving that rule).
            expected = SHARED_DIR_NAME if source_stem == SHARED_DIR_NAME \
                else env_normalized_ms_name(namespace, source_stem)

            if kind in CLUSTER_SCOPED_KINDS and NAMESPACE_SCOPED_ONLY:
                # No cluster-shared release in this mode: the object was copied
                # into its declaring namespace's own release under its OWN name
                # (handle_cluster_scoped), so it is in THIS namespace's rendered
                # index, not the global one. Matched on the raw name verbatim —
                # under strict naming there is no prefix to reconstruct.
                hit = rendered_index.get((kind, name))
                if hit is None:
                    stats["missing_objects"] += 1
                    report.append(f"  ERROR  {kind}/{name} (from {source_stem}.yaml): "
                                  f"cluster-scoped, but rendered by NO release")
                    continue
                release, rendered_doc = hit
                matched_rendered.add((kind, name))
                stats["relocated"] += 1
                report.append(f"  INFO   {kind}/{name} (from {source_stem}.yaml): cluster-scoped, "
                              f"copied into release '{release}' under its own name "
                              f"(namespace-scoped mode; pass --cluster-scoped to dedupe globally). "
                              f"If another namespace declares this name too, both releases claim "
                              f"ONE cluster object — see conflicts_and_warnings.txt")
            elif kind in CLUSTER_SCOPED_KINDS:
                found = cluster_index.get((kind, name))
                if found is None:
                    stats["missing_objects"] += 1
                    report.append(f"  ERROR  {kind}/{name} (from {source_stem}.yaml): "
                                  f"cluster-scoped, but rendered by NO release")
                    continue
                stats["relocated"] += 1
                report.append(f"  INFO   {kind}/{name} (from {source_stem}.yaml): "
                              f"relocated to the global '{CLUSTER_SHARED_DIR_NAME}' release (expected)")
                rendered_doc = found
                release = CLUSTER_SHARED_DIR_NAME
            else:
                # A name collision renames the LOSING microservice's copy to
                # '<owner>-<name>' (NamespaceRegistry.claim), so look for this
                # declarer's own renamed object FIRST — otherwise the second
                # file declaring e.g. ConfigMap/app-config matches the first
                # file's rendered object and reports a phantom content
                # mismatch. Applying the converter's own rename rule rather
                # than scanning for any name ending in '-<name>', which could
                # match an unrelated object.
                renamed_key = (kind, f"{expected}-{name}")
                hit = rendered_index.get(renamed_key) or rendered_index.get((kind, name))
                # When SEVERAL raw files declare this (kind, name), the plain
                # name lookup is ambiguous by construction: it returns whichever
                # rendered object kept the name, which for every declarer but one
                # is somebody ELSE'S object. That produced a phantom content
                # mismatch plus a phantom extra object — the comment above
                # anticipated it and mirrored the converter's `<owner>-<name>`
                # rename rule to avoid it, but a checker that mirrors a rule has
                # to be updated whenever the rule changes, and the collision
                # resolver can now also emit `<name>-2`.
                #
                # Matching on CONTENT instead needs no mirror. It is applied only
                # where the ambiguity actually exists, so the ordinary
                # one-declarer path keeps its cheap name lookup.
                if len(raw_entries) > 1:
                    raw_h = _identity_free_hash(raw_doc)
                    twin = next(((rname, rrel, rdoc) for (rkind, rname), (rrel, rdoc)
                                 in sorted(rendered_index.items())
                                 if rkind == kind and (kind, rname) not in matched_rendered
                                 and _identity_free_hash(_strip_helm_labels(rdoc)) == raw_h), None)
                    if twin is not None:
                        hit = (twin[1], twin[2])
                if hit is None:
                    # Before calling it lost, run the test error.txt specifies:
                    # "for each object rendered by nothing, search the rendered
                    # output for an object of the same KIND with matching content
                    # under a different name. If found, it is a rename and
                    # benign. If not found, it is genuinely lost and is a P0."
                    #
                    # The name-based lookups above only try the ONE rename shape
                    # the converter's own rule produces, `<release>-<name>`. That
                    # is no longer the only shape — the collision resolver can now
                    # emit `<name>-2` when a prefix would be redundant — and it
                    # never covered fullnameOverride or the chart's per-kind
                    # naming either. Matching on CONTENT finds a rename whatever
                    # the new name looks like, which turns "rendered by NO
                    # release" from a claim into a conclusion.
                    #
                    # Content hash ignores labels/annotations, so the chart's own
                    # metadata does not defeat the match.
                    raw_h = _identity_free_hash(raw_doc)
                    twin = next(((rname, rrel) for (rkind, rname), (rrel, rdoc)
                                 in sorted(rendered_index.items())
                                 if rkind == kind and (kind, rname) not in matched_rendered
                                 and _identity_free_hash(_strip_helm_labels(rdoc)) == raw_h), None)
                    if twin is not None:
                        twin_name, twin_release = twin
                        matched_rendered.add((kind, twin_name))
                        stats["relocated"] += 1
                        report.append(
                            f"  INFO   {kind}/{name} (from {source_stem}.yaml): rendered as "
                            f"'{twin_name}' by release '{twin_release}' — matched by CONTENT, "
                            f"not by name. The object survived the conversion under a name "
                            f"this checker's rename rules do not predict; verify nothing "
                            f"outside these releases referred to it as '{name}'")
                        continue
                    stats["missing_objects"] += 1
                    report.append(f"  ERROR  {kind}/{name} (from {source_stem}.yaml): "
                                  f"rendered by NO release, and no object of this kind in the "
                                  f"namespace has matching content under any other name — "
                                  f"genuinely lost, not renamed")
                    continue
                release, rendered_doc = hit
                rendered_name = (rendered_doc.get("metadata") or {}).get("name")
                matched_rendered.add((kind, rendered_name))
                if rendered_name != name:
                    # Two different mechanisms produce the same '<prefix>-<name>'
                    # shape and are not distinguishable from here: a
                    # NamespaceRegistry collision rename, and the chart's own
                    # release-prefixed naming for kinds like CronJob/Job. Say
                    # what is observable rather than guessing which one it was.
                    stats["relocated"] += 1
                    report.append(f"  INFO   {kind}/{name} (from {source_stem}.yaml): "
                                  f"rendered as '{rendered_name}' by release '{release}' "
                                  f"(expected — collision rename or the chart's naming for this kind)")
                elif release != expected:
                    stats["relocated"] += 1
                    report.append(f"  INFO   {kind}/{name} (from {source_stem}.yaml): "
                                  f"relocated to release '{release}' (expected)")

            stats["matched"] += 1
            # If the object was matched under a different name, that rename is
            # already reported above as its own INFO line — re-reporting it as
            # a CHANGED field would double-count the same fact.
            raw_for_diff = raw_doc
            if (rendered_doc.get("metadata") or {}).get("name") != name:
                raw_for_diff = copy.deepcopy(raw_doc)
                raw_for_diff.setdefault("metadata", {})["name"] = \
                    (rendered_doc.get("metadata") or {}).get("name")
            diffs = _fidelity_diff(kind, raw_for_diff, _strip_helm_labels(rendered_doc))
            # Drop `replicas` ONLY where the chart deliberately omits it, i.e.
            # where this release actually rendered an autoscaler. Without the
            # HPA check this would hide a genuinely lost replica count.
            if release in hpa_releases:
                diffs = [d for d in diffs if not (d[0] == "LOST" and d[1] == "spec.replicas")]
            # STRICT NAMING: a ClusterRole is now copied in under its OWN name,
            # so its binding's roleRef needs no rewrite and this suppression is
            # gone. Any roleRef difference is now a real one and reports — which
            # is the point: under the old prefixing scheme a genuinely broken
            # roleRef and the expected rewrite looked identical here.
            for severity, field_path, detail in diffs:
                if severity == "ADDED":
                    stats["added"] += 1
                    continue  # chart filling in a Kubernetes default — not loss
                # A reference that CHANGED because the object it names was
                # renamed on collision is the SAME FACT as the rename, which is
                # already reported as its own INFO line above. _fidelity_diff
                # applies exactly this reasoning to a renamed object's own
                # metadata.name and says so; the downstream references were
                # simply never covered by it, which is why the report saw 12
                # CHANGEDs and could not decide whether they were bugs.
                #
                # Downgraded ONLY when the new name is one this namespace
                # actually renders. If the reference points at nothing, the
                # reference-integrity pass below reports it as an ERROR — so a
                # genuinely broken rename is still caught, by a check that looks
                # for exactly that rather than inferring it from a string diff.
                if severity == "CHANGED" and _is_followed_rename(field_path, detail,
                                                                 rendered_index):
                    stats["relocated"] += 1
                    report.append(f"  INFO   {kind}/{name} reference at {field_path} follows a "
                                  f"collision rename ({detail}) — target is rendered by this "
                                  f"namespace, verified by the reference-integrity check")
                    continue
                stats["lost" if severity == "LOST" else "changed"] += 1
                report.append(f"  ERROR  {kind}/{name} {severity} at {field_path}: {detail}")

        for (kind, name), (release, _doc) in sorted(rendered_index.items()):
            if (kind, name) in matched_rendered:
                continue
            # The chart creates a ServiceAccount per release whenever
            # serviceAccount.create is true, so one
            # with no raw counterpart is the chart doing its job, not the
            # converter inventing a resource. Matched on kind alone rather than
            # on the name: the SA follows the release's fullname, which
            # fullnameOverride legitimately changes. A raw-declared SA would
            # already have matched by name above, and an SA the conversion LOST
            # is reported from the raw side, so this can't mask either.
            if kind == "ServiceAccount":
                stats["relocated"] += 1
                report.append(f"  INFO   {kind}/{name}: created by the chart for release "
                              f"'{release}' (serviceAccount.create) — no raw source expected")
                continue
            stats["extra_objects"] += 1
            report.append(f"  ERROR  {kind}/{name}: rendered by '{release}' "
                          f"but present in NO raw manifest")

        # --- reference integrity ------------------------------------------
        # The converter renames ConfigMaps/Secrets/ServiceAccounts on collision
        # and rewrites the references it knows about. Whether that is "a real
        # issue" or "internally consistent / benign" was the open question the
        # report could not settle, and it is decidable: every reference must
        # resolve to an object that this namespace's render actually produces.
        #
        # Scoped to names the RAW input also declared. A reference to something
        # this conversion never owned — an ESO-materialised Secret, a
        # cluster-wide CA bundle, an OpenShift-injected SA token — is legitimate
        # and must not be reported. What cannot be legitimate is a reference to
        # an object that existed in the source, still exists in the render under
        # a NEW name, and is still being asked for by its OLD one: that is a
        # rename whose references did not follow, and the pod does not start.
        for (kind, name), (release, doc) in sorted(rendered_index.items()):
            for ref_kind, ref_name, where in collect_object_references(doc):
                target = rendered_index.get((ref_kind, ref_name))

                if target is None:
                    # Points at nothing this namespace renders. Only an error if
                    # the raw input declared it — a reference to something the
                    # conversion never owned (an ESO-materialised Secret, a
                    # cluster-wide CA bundle, an OpenShift-injected SA token) is
                    # legitimate and common.
                    if (ref_kind, ref_name) not in raw_index:
                        continue
                    stats["dangling_refs"] += 1
                    report.append(
                        f"  ERROR  {kind}/{name} (release '{release}') references "
                        f"{ref_kind}/{ref_name} at {where}, but no release in this "
                        f"namespace renders it — the raw input declared it, so a "
                        f"rename lost this reference")
                    continue

                # The subtler and more dangerous case, and the one a
                # name-resolves-to-something check misses entirely: the
                # reference DOES resolve, but to another microservice's object.
                # After a collision rename that is precisely what a
                # not-followed reference looks like — release B kept pointing at
                # `job-defaults` while its own copy was renamed to
                # `B-job-defaults`, so B now silently consumes A's config. The
                # pod starts. Nothing reports it. The values are simply wrong.
                #
                # Detected by asking the RAW input whether the referrer's own
                # source file declared an object of that kind and name. If it
                # did, the reference belonged to its own copy and should have
                # been remapped; resolving to a different release means it was
                # not. If it did not, this is an ordinary cross-release
                # reference (typically the namespace-shared release) and fine.
                target_release = target[0]
                if target_release == release:
                    continue
                own_declaration = any(stem == release
                                      for stem, _ in raw_index.get((ref_kind, ref_name), []))
                if not own_declaration:
                    continue
                # Two shapes are the converter's intent, not a rename that lost
                # its reference:
                #   1. the referrer's own raw copy is byte-identical to the
                #      rendered target — a duplicate promoted into shared;
                #   2. the namespace's raw shared file declared the same object,
                #      and the converter deduped the private copy to it.
                declarations = raw_index.get((ref_kind, ref_name), [])
                own_raw = next(d for stem, d in declarations if stem == release)
                if _identity_free_hash(own_raw) == _identity_free_hash(_strip_helm_labels(target[1])) \
                        or any(stem == shared_raw_stem for stem, _ in declarations):
                    stats["relocated"] += 1
                    report.append(
                        f"  INFO   {kind}/{name} (release '{release}') references "
                        f"{ref_kind}/{ref_name} at {where}, rendered by release "
                        f"'{target_release}' — intended: its own raw copy is the same "
                        f"object the shared release now owns")
                    continue
                stats["dangling_refs"] += 1
                report.append(
                    f"  ERROR  {kind}/{name} (release '{release}') references "
                    f"{ref_kind}/{ref_name} at {where}, which is rendered by a "
                    f"DIFFERENT release ('{target_release}') — this release declared "
                    f"its own {ref_kind}/{ref_name} in the raw input and it was "
                    f"renamed on collision, so this reference should have followed "
                    f"the rename and now resolves to another microservice's object")

        # --- Service selectors ------------------------------------------------
        # spec.selector is in _fidelity_ignored_path because the chart derives
        # it, so a Service whose selector matches NO pod after conversion was
        # invisible here — a StatefulSet's headless Service, or Kafka's client
        # Service, kept in `shared` with that release's labels. Checked by its
        # effect instead: a raw Service that selected some pod must still.
        def _pod_labels(doc):
            spec = doc.get("spec") or {}
            return (((spec.get("template") or {}).get("metadata") or {}).get("labels") or {})
        raw_pods = [d for entries in raw_index.values() for _, d in entries if d.get("kind") in WORKLOAD_KINDS]
        rendered_pods = [d for (k, _n), (_r, d) in rendered_index.items() if k in WORKLOAD_KINDS]
        for (kind, name), raw_entries in sorted(raw_index.items()):
            if kind != "Service" or (kind, name) not in rendered_index:
                continue
            raw_sel = ((raw_entries[0][1].get("spec") or {}).get("selector") or {})
            release, rdoc = rendered_index[(kind, name)]
            sel = (rdoc.get("spec") or {}).get("selector") or {}
            if not raw_sel or not sel:
                continue
            matches = lambda selector, pods: [p for p in pods
                                              if all(_pod_labels(p).get(k) == v for k, v in selector.items())]
            if matches(raw_sel, raw_pods) and not matches(sel, rendered_pods):
                stats["changed"] += 1
                report.append(
                    f"  ERROR  Service/{name} (release '{release}') CHANGED at spec.selector: "
                    f"raw selected {[(p.get('kind'), (p.get('metadata') or {}).get('name')) for p in matches(raw_sel, raw_pods)]}, "
                    f"the rendered selector {sel} selects no rendered pod")

    return report, stats


# ---------------------------------------------------------------------------
# Cross-reference checks — always on, advisory WARNINGs only
# ---------------------------------------------------------------------------
# Complementary to --verify-fidelity (opt-in, flags renames as ERROR): this
# looks at the RAW namespace as a whole and asks whether what a pod needs is
# actually in the dump. Nothing here changes output or fails the run.

# Present in every namespace without anyone declaring them.
_BUILTIN_SECRETS = frozenset({"kube-root-ca.crt", "openshift-service-ca.crt"})
_BUILTIN_SERVICE_ACCOUNTS = frozenset({"default", "builder", "deployer"})
_NAD_ANNOTATION = "k8s.v1.cni.cncf.io/networks"
# pod-spec key -> (referenced kind, field holding the name)
_REF_KEYS = {
    "configMap": ("ConfigMap", "name"), "configMapKeyRef": ("ConfigMap", "name"),
    "configMapRef": ("ConfigMap", "name"), "secret": ("Secret", "secretName"),
    "secretKeyRef": ("Secret", "name"), "secretRef": ("Secret", "name"),
}


def _join_path(path: str, key) -> str:
    if isinstance(key, int):
        return f"{path}[{key}]"
    return f"{path}.{key}" if path else str(key)


def _collect_ref_targets(node, path: str = "") -> list[tuple[str, str, str]]:
    """(kind, name, path) for every non-optional ConfigMap/Secret a pod spec names."""
    out = []
    if isinstance(node, dict):
        for key, val in node.items():
            here = _join_path(path, key)
            if key in _REF_KEYS and isinstance(val, dict):
                kind, field_name = _REF_KEYS[key]
                if val.get(field_name) and not val.get("optional"):
                    out.append((kind, val[field_name], here))
            elif key == "imagePullSecrets" and isinstance(val, list):
                out += [("Secret", s["name"], _join_path(here, i))
                        for i, s in enumerate(val) if isinstance(s, dict) and s.get("name")]
            out += _collect_ref_targets(val, here)
    elif isinstance(node, list):
        for i, item in enumerate(node):
            out += _collect_ref_targets(item, _join_path(path, i))
    return out


def _parse_nad_names(value, namespace: str) -> list[str]:
    """NAD names a networks annotation requests in THIS namespace. Accepts both
    the JSON list form and the 'ns/name@iface, name' shorthand; attachments in
    another namespace cannot be checked against this dump and are skipped."""
    value = (value or "").strip()
    if not value:
        return []
    if value.startswith("["):
        try:
            refs = [(i.get("namespace"), i.get("name")) for i in json.loads(value) if isinstance(i, dict)]
        except ValueError:
            return []
    else:
        refs = []
        for part in value.split(","):
            ns, _, name = part.strip().split("@", 1)[0].rpartition("/")
            refs.append((ns, name))
    return [name for ns, name in refs if name and ns in (None, "", namespace)]


def cross_reference_checks(namespace: str, docs: list[dict], nad_names: set) -> list[str]:
    """Three reference-integrity warnings over one namespace's raw docs:
      1. a pod references a ConfigMap/Secret/ServiceAccount no release here creates;
      2. a pod runs as a ServiceAccount no RoleBinding grants any rule to;
      3. a pod's networks annotation requests a NAD that is not in the dump."""
    created: dict[str, set] = defaultdict(set)
    bound_sas: set = set()
    for d in docs:
        kind, name = d.get("kind"), (d.get("metadata") or {}).get("name")
        if kind in ("ConfigMap", "Secret", "ServiceAccount"):
            created[kind].add(name)
        elif kind == "ExternalSecret":  # ESO materialises the target Secret
            created["Secret"].add(((d.get("spec") or {}).get("target") or {}).get("name") or name)
        elif kind in ("RoleBinding", "ClusterRoleBinding"):
            bound_sas |= {s.get("name") for s in d.get("subjects") or []
                          if s.get("kind") == "ServiceAccount" and s.get("namespace") in (None, namespace)}

    warnings = []
    for d in docs:
        kind, name = d.get("kind"), (d.get("metadata") or {}).get("name")
        spec = d.get("spec") or {}
        if kind in WORKLOAD_KINDS or kind == "Job":
            tpl = spec.get("template") or {}
        elif kind == "CronJob":
            tpl = ((spec.get("jobTemplate") or {}).get("spec") or {}).get("template") or {}
        else:
            continue
        pod = tpl.get("spec") or {}
        where = f"{kind}/{name}"

        for ref_kind, ref_name, path in _collect_ref_targets(pod):
            if ref_name in created[ref_kind] or ref_name in _BUILTIN_SECRETS \
                    or (ref_kind == "Secret" and AUTO_SA_SECRET_RE.match(ref_name)):
                continue
            warnings.append(f"cross-reference: {where} references {ref_kind}/{ref_name} at "
                            f"{path}, but no release in this namespace creates it")

        sa = pod.get("serviceAccountName") or pod.get("serviceAccount")
        if sa and sa not in _BUILTIN_SERVICE_ACCOUNTS:
            if sa not in created["ServiceAccount"]:
                warnings.append(f"cross-reference: {where} runs as ServiceAccount/{sa}, but no "
                                f"release in this namespace creates it")
            elif sa not in bound_sas:
                warnings.append(f"cross-reference: {where} runs as ServiceAccount/{sa}, but no "
                                f"RoleBinding in this namespace grants it any rule")

        annotations = (tpl.get("metadata") or {}).get("annotations") or {}
        for nad in _parse_nad_names(annotations.get(_NAD_ANNOTATION), namespace):
            if nad not in nad_names:
                warnings.append(f"cross-reference: {where} requests NetworkAttachmentDefinition "
                                f"'{nad}' ({_NAD_ANNOTATION}), which is not in the dump")
    return warnings


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _bool_arg(v: str) -> bool:
    if str(v).lower() in ("true", "yes", "1", "on"):
        return True
    if str(v).lower() in ("false", "no", "0", "off"):
        return False
    raise argparse.ArgumentTypeError(f"expected true or false, got {v!r}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", default="namespaces", help="root folder containing <namespace>/<microservice>.yaml")
    ap.add_argument("--output", default="output",
                     help="output folder for values/ApplicationSets/Applications (per-namespace)/report")
    ap.add_argument("--chart-repo-url", default="https://github.com/devops-ezrahi/universal-chart.git")
    ap.add_argument("--chart-path", default=".",
                     help="path to the chart inside --chart-repo-url; the universal-chart repo is the chart, so '.'")
    ap.add_argument("--chart-revision", default="main")
    ap.add_argument("--appset-chart-path", default="ms-applicationSet",
                     help="path inside --chart-repo-url to the ms-applicationSet chart — the "
                          "per-namespace ApplicationSet the generated root ApplicationSet deploys "
                          "once per namespace directory. It lives in the universal-chart repo "
                          "alongside the chart it deploys, so the layering rules are versioned "
                          "with the chart that applies them instead of being copied into every "
                          "values repo this converter writes.")
    ap.add_argument("--values-repo-url", default=VALUES_REPO_PLACEHOLDER,
                     help="the git repo this output is pushed to. Unset, the ArgoCD wiring carries "
                          f"the placeholder {VALUES_REPO_PLACEHOLDER} to replace by hand.")
    ap.add_argument("--values-revision", default="main")
    ap.add_argument("--values-path", default="",
                     help="subpath within --values-repo-url where this run's --output directory's "
                          "content actually lives, e.g. 'universal/gitops-factory/output-showcase' when "
                          "the output is folded into a larger repo instead of a dedicated values repo "
                          "(whose root would otherwise just BE the --output directory). Every "
                          "$values/... reference and the ApplicationSet git generator's file glob are "
                          "prefixed with this. Leave empty (the default) for a dedicated values repo.")
    ap.add_argument("--cluster-shared-namespace", default="platform-shared")
    ap.add_argument("--argocd-project", default="default",
                     help="ArgoCD AppProject every generated Application/ApplicationSet belongs to.")
    ap.add_argument("--argocd-namespace", "--argocd-destination-namespace", default="argocd",
                     help="namespace ArgoCD itself runs in — where the root Application, the root "
                          "ApplicationSet and the per-namespace ApplicationSets are created. The "
                          "workloads they deploy still land in their own namespaces.")
    ap.add_argument("--argocd-cluster", default="",
                     help="deploy to the ArgoCD cluster registered under this NAME instead of "
                          "--destination-server's URL (destination.name rather than .server).")
    ap.add_argument("--destination-server", default="https://kubernetes.default.svc",
                     help="cluster API URL every generated Application deploys to. The default is "
                          "the in-cluster address, correct when ArgoCD manages its own cluster; "
                          "point it at a registered remote cluster's URL otherwise.")
    # Variant sub-folders in the input are detected on their own now.
    ap.add_argument("--variants", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--cluster-scoped", action="store_true",
                     help="opt back in to cluster-wide output. By DEFAULT the converter stays "
                          "inside the namespace boundary: no cluster-shared release — a ClusterRole/ClusterRoleBinding is "
                          "copied into the namespace that declared it under a namespace-prefixed "
                          "name, since two namespaces emitting the same cluster-scoped name would "
                          "be two releases fighting over one object. Pass this only when you "
                          "actually hold cluster-wide rights and want the deduplicated "
                          "cluster-shared release. Either way "
                          "those objects stay cluster-scoped and need cluster rights to APPLY; "
                          "relocations and drops are listed in conflicts_and_warnings.txt.")
    ap.add_argument("--root-app-name", default="platform-root",
                     help="name of the generated root app-of-apps Application "
                          "(<output>/rootApplication.yaml.txt) — the single object you apply by hand "
                          "to bootstrap every generated ApplicationSet. Give two output trees "
                          "deployed to the same cluster different names so they don't collide.")
    ap.add_argument("--root-set-name", default="",
                     help="name of the root ApplicationSet (default: '<--root-app-name>-set').")
    # Kept so existing command lines still parse: the wiring it used to opt into
    # (rootApplication.yaml.txt, rootApplicationSet.yaml and, under
    # --cluster-scoped, cluster-shared/ + cluster-shared-application.yaml) is
    # now written on every run.
    ap.add_argument("--argocd-manifests", nargs="?", const=True, default=False, type=_bool_arg,
                     metavar="true|false",
                     help=f"write the ArgoCD wiring: {ROOT_APPSET_FILE_NAME}, {ROOT_APP_FILE_NAME} "
                          f"(the root Application you apply by hand) and, under --cluster-scoped, "
                          f"{CLUSTER_SHARED_APP_FILE_NAME}. Off by default: only the values tree "
                          f"(base/, <folder>/defaults.yaml, <folder>/values/) is written, and wiring "
                          f"left by an earlier run is reported stale. A bare --argocd-manifests "
                          f"means true.")
    ap.add_argument("--auto-sync", nargs="?", const=True, default=False, type=_bool_arg,
                     metavar="true|false",
                     help="whether the Applications that deploy releases sync themselves "
                          "(syncPolicy.automated, prune + selfHeal). Off by default: each "
                          "microservice's Application (through the ms-applicationSet chart's "
                          "autoSync parameter) and the cluster-shared one wait for a manual Sync. "
                          "The root Application and root ApplicationSet always reconcile, so a "
                          "new namespace folder still appears in Argo CD on its own.")
    ap.add_argument("--exclude", action="append", default=[], metavar="PATTERN",
                     help="skip whole stacks: a microservice file with any object whose name or "
                          "label/annotation VALUE contains PATTERN (case-insensitive) is skipped "
                          "entirely, and matching objects are removed from shared.yaml. Data "
                          "payloads are never matched. Repeatable, or comma-separated.")
    ap.add_argument("--include", action="append", default=[], metavar="PATTERN",
                     help="convert ONLY the microservice files with an object whose name or "
                          "label/annotation VALUE contains PATTERN (case-insensitive); everything "
                          "else is skipped. shared.yaml is never filtered by it, and --exclude "
                          "still wins. `=NAME` matches an object name exactly (also in --exclude "
                          "/ --keep). Repeatable, or comma-separated.")
    ap.add_argument("--keep", action="append", default=[], metavar="PATTERN",
                     help="exception to --exclude: an object matching a --keep PATTERN is never "
                          "excluded. Repeatable, or comma-separated.")
    ap.add_argument("--env-suffixes", default="", metavar="TOK,TOK",
                     help="strip these environment words from ANYWHERE in a microservice's "
                          "file name ('pay-fac1-api' -> 'pay-api' with 'fac1'), so the copies in "
                          "different namespaces share one base/ file. Replaces the default "
                          "rule, which strips only a trailing suffix equal to the namespace's. "
                          "Values differing by these words become {{ .Values.environment }}, "
                          "set per namespace in its defaults.yaml; they never make folders.")
    ap.add_argument("--auto-env-suffixes", nargs="?", const=True, default=True, type=_bool_arg,
                     metavar="true|false",
                     help="without --env-suffixes, take a namespace's last word as an environment "
                          "word when the same app is named after it in another namespace too "
                          "(goldenbell-ui/goldenbell-ui.yaml + goldenbell-next/goldenbell-next.yaml "
                          "-> one base/goldenbell.yaml, 'ui'/'next' templated as "
                          "{{ .Values.environment }}). Default true.")
    ap.add_argument("--env-group", action="append", default=[], metavar="NAME=TOK[:VALUE],...",
                     help="a release whose file name carries one of these words moves into a "
                          "folder of that name inside its namespace (dev/ms1-yellow.yaml -> "
                          "dev/yellow/values/ms1.yaml, release 'ms1'), and values that differ "
                          "only by them merge into one templated base value: "
                          "'a.black.org'/'a.yellow.org' -> 'a.{{ .Values.color }}.org', with "
                          "'color: yellow' in dev/yellow/defaults.yaml. A word also given to "
                          "--env-suffixes is the namespace's, not a folder. The namespace name "
                          "becomes {{ .Release.Namespace }}; '--env-group namespace' enables only "
                          "that. Repeatable; a second group nests (dev/yellow/eu/).")
    ap.add_argument("--env-in-values", nargs="?", const=True, default=False, type=_bool_arg,
                     help="write each namespace's env list into its own values/ file. Default "
                          "false: the env list is treated like any other value — in base/ when it "
                          "is the same in every namespace, in values/ only where it differs.")
    ap.add_argument("--exact-copy", nargs="?", const=True, default=None, type=_bool_arg,
                     metavar="true|false",
                     help="render migrated pod templates byte-for-byte as they run now, so the "
                          "cutover sync restarts no pods: writes portOrder/volumeOrder/sidecarOrder "
                          "and keeps an empty securityContext: {}. Default: "
                          "on for stateful releases only (a StatefulSet, or a workload mounting a "
                          "PersistentVolumeClaim), the rest get cleaner values and roll once on the "
                          "first sync. true: every release. false: none. Orders that change "
                          "behaviour (init containers, envFrom) are kept either way, and env / "
                          "volumeMounts are always lists in the deployment's own order.")
    ap.add_argument("--interactive", action="store_true",
                     help="with --env-group, ask before each combination (needs a terminal).")
    ap.add_argument("--no-interactive", action="store_false", dest="interactive",
                     help=argparse.SUPPRESS)
    ap.add_argument("--chart", default=str(Path(__file__).resolve().parent.parent),
                     help="path to the Universal Chart, used for the render-verification step "
                          "(default: this repo's root, one level up)")
    ap.add_argument("--skip-verify", action="store_true",
                     help="skip rendering every release with `helm template` to prove no ownership conflicts")
    ap.add_argument("--external-secrets-api-version", default="",
                     help="pin every SecretStore/ClusterSecretStore/ExternalSecret to this "
                          "apiVersion (e.g. external-secrets.io/v1beta1) regardless of what the "
                          "source manifests say. Leave unset to carry each source manifest's own "
                          "version through, which is correct when the TARGET cluster runs the same "
                          "External Secrets Operator the manifests came from. Set it when it does "
                          "not - an older on-prem ESO serving only v1beta1 rejects v1 outright.")
    ap.add_argument("--prune", action="store_true",
                     help="delete generated files left over from a previous run whose input or "
                          "options have since changed (stale values/, releases/ and base/ entries, "
                          "and root-application.yaml / root-applicationSet.yaml from before the "
                          "root files were renamed). Stale files are ALWAYS reported; this deletes them. Off by "
                          "default because removing a values file makes ArgoCD prune the workload "
                          "it deploys.")
    ap.add_argument("--verify-fidelity", action="store_true",
                     help="additionally round-trip check the output: render it and compare back against "
                          "the raw input manifests, reporting anything the conversion lost or altered "
                          "(writes report/fidelity_report.txt). Opt-in and report-only — it never changes "
                          "the generated output and never fails the run.")
    ap.add_argument("--fidelity-strict", action="store_true",
                     help="with --verify-fidelity, exit non-zero if the round-trip check finds "
                          "anything the conversion lost, altered or invented. Intended for CI, so "
                          "a bad conversion is blocked before it reaches a cluster rather than "
                          "noticed afterwards. A clean conversion reports zero findings, so any "
                          "finding is real.")
    args = ap.parse_args()

    global ESO_API_VERSION_OVERRIDE, NAMESPACE_SCOPED_ONLY, EXCLUDE_PATTERNS, KEEP_PATTERNS, ARGOCD_MANIFESTS, \
        INCLUDE_PATTERNS
    global ENV_IN_VALUES, EXACT_COPY, EXACT_COPY_FLAG
    ESO_API_VERSION_OVERRIDE = args.external_secrets_api_version
    ARGOCD_MANIFESTS = args.argocd_manifests
    ENV_IN_VALUES = args.env_in_values
    EXACT_COPY_FLAG = args.exact_copy
    EXACT_COPY = bool(EXACT_COPY_FLAG)
    EXCLUDE_PATTERNS, KEEP_PATTERNS, INCLUDE_PATTERNS = (
        [p.strip() for arg in opt for p in arg.split(",") if p.strip()]
        for opt in (args.exclude, args.keep, args.include))
    chart_keys = set((yaml.safe_load((Path(args.chart) / "values.yaml").read_text(encoding="utf-8"))
                      if (Path(args.chart) / "values.yaml").is_file() else None) or {})
    env_groups = [parse_env_group(spec, chart_keys) for spec in args.env_group]
    suffixes = [t.strip() for t in args.env_suffixes.split(",") if t.strip()]
    if suffixes:
        KNOWN_ENV_SUFFIXES.clear()  # the list replaces the default trailing-suffix rule
        NAME_TOKENS.update(suffixes)
        # Env suffixes are an implicit group: values differing by them become
        # {{ .Values.environment }} ('env' is the chart's env-var list). A
        # suffix token listed in a --env-group too is the namespace's, not a
        # folder's, so it leaves that group.
        for g in env_groups:
            g.tokens = {t: v for t, v in g.tokens.items() if t not in suffixes}
        env_groups = [g for g in env_groups if g.tokens or g.name == NAMESPACE_GROUP]
        if not any(g.name == ENVIRONMENT_GROUP for g in env_groups):
            env_groups.append(EnvGroup(ENVIRONMENT_GROUP, {t: t for t in suffixes}, folder=False))
    for g in env_groups:
        if g.name in (ENVIRONMENT_GROUP, NAMESPACE_GROUP):
            g.folder = False
    folder_groups = [g for g in env_groups if g.folder]
    NAME_TOKENS.update(t for g in env_groups for t in g.tokens)
    # Namespace-scoped is the DEFAULT: it is the assumption that holds for the
    # widest range of clusters, and it fails safe. Defaulting to cluster-wide
    # output produces a cluster-shared Application that a namespace-limited
    # tenant simply cannot deploy — it sits permanently OutOfSync — whereas
    # defaulting to namespace-scoped merely gives an operator who DOES hold
    # cluster rights a per-namespace copy instead of a deduplicated one.
    NAMESPACE_SCOPED_ONLY = not args.cluster_scoped

    input_dir = Path(args.input)
    output_dir = Path(args.output)
    report_dir = output_dir / "report"

    if not input_dir.is_dir():
        sys.exit(f"Input directory not found: {input_dir}")

    cluster_registry = ClusterRegistry()
    cluster_values = {"rbac": {"clusterRoles": {}, "clusterRoleBindings": {}},
                       "storageClasses": {}, "persistentVolumes": {}, "scc": {},
                       "clusterSecretStores": {}}
    all_warnings: list[str] = []
    summary = {"namespaces": {}, "totals": {"microservices": 0, "warnings": 0}}

    namespace_dirs = namespace_dirs_of(input_dir, is_input_ns)

    # goldenbell-ui/goldenbell-ui.yaml + goldenbell-next/goldenbell-next.yaml
    # are one app: 'ui' and 'next' become environment words, as if given to
    # --env-suffixes (the default trailing rule stays). An explicit
    # --env-suffixes list is the whole list, so it turns this off.
    if args.auto_env_suffixes and not suffixes:
        auto = sibling_env_suffixes(namespace_dirs) - {t for g in env_groups for t in g.tokens}
        if auto:
            NAME_TOKENS.update(auto)
            env_group = next((g for g in env_groups if g.name == ENVIRONMENT_GROUP), None)
            if env_group is None:
                env_groups.append(EnvGroup(ENVIRONMENT_GROUP, {t: t for t in sorted(auto)}, folder=False))
            else:
                env_group.tokens.update({t: t for t in sorted(auto)})
            all_warnings.append(
                f"environment words taken from the namespace names: {', '.join(sorted(auto))} — the "
                f"same app is named after each of these namespaces, so its copies share one base/ "
                f"file (pass --env-suffixes to set the list yourself, --auto-env-suffixes false "
                f"to keep them apart)")
            print(f"Environment words from the namespace names: {', '.join(sorted(auto))}")

    # Pass 1 — build every microservice's values (in memory, nothing written
    # yet). Needed up front because defaults.yaml is computed FROM the full
    # set of already-built values, both globally and per namespace.
    per_namespace_ms: dict[str, list[tuple[str, MicroserviceContext]]] = {}
    out_ms: dict[str, list[tuple[str, MicroserviceContext]]] = {}  # per OUTPUT folder
    per_namespace_shared: dict[str, MicroserviceContext] = {}
    per_namespace_shared_objects: dict[str, set[tuple[str, str]]] = {}  # (kind, name) it declares
    per_namespace_warnings: dict[str, list[str]] = {}
    # Names Kubernetes would reject. Collected across the WHOLE run and
    # reported together at the end of Pass 1 — before a single file is
    # written — so you fix every one of them in one pass rather than
    # discovering them one at a time on the cluster.
    name_errors: list[str] = []
    all_renames: list[dict] = []
    all_promotions: list[dict] = []

    # Pass 0 — load every namespace's docs up front, honouring --exclude/--keep.
    # Promotion looks across all of a namespace's files before anything is
    # built, and a PVC's static volumeName is checked against PVs that may live
    # in a different file or a different namespace.
    loaded: dict[str, tuple[Optional[list[dict]], list[tuple[str, list[dict]]], list[str]]] = {}
    ms_meta: dict[str, list[tuple[str, str]]] = {}  # namespace -> [(file stem, variant folder)]

    # Before any name is normalized: which file names a token must not touch,
    # because no other file would share the name it leaves (token_exempt_stems).
    TOKEN_EXEMPT_STEMS.clear()
    LEFT_OUT.clear()
    TOKEN_EXEMPT_STEMS.update(token_exempt_stems(
        {f.stem for _, d in namespace_dirs for f in d.glob("*.yaml")}, NAME_TOKENS))
    for stem in sorted(TOKEN_EXEMPT_STEMS):
        all_warnings.append(
            f"{stem}.yaml: kept as '{stem}' — it holds an env token, but no other file shares "
            f"'{strip_name_tokens(stem, NAME_TOKENS)}', so it is one microservice named this "
            f"in every namespace, not an environment's copy")
    for namespace, ns_dir in namespace_dirs:
        ns_warnings: list[str] = []
        left_out = LEFT_OUT.setdefault(k8s_ns(namespace), set())

        # The shared file may carry the namespace's own environment suffix
        # (shared-prd.yaml inside a -prd namespace), exactly like every other
        # microservice file there — so it is resolved the same way, not by
        # literal filename. Matched literally, a shared-prd.yaml fell through
        # to the regular-microservice branch below and became an ordinary
        # microservice: it stopped owning the namespace's shared resources,
        # was left out of the base-registry fork logic that keeps two
        # namespaces' unrelated 'shared' content apart, and joined the pool
        # that computes this namespace's defaults instead of constraining it.
        shared_path = next(
            (f for f in sorted(ns_dir.glob("*.yaml"))
             if env_normalized_ms_name(namespace, f.stem) == SHARED_DIR_NAME),
            None,
        )
        shared_docs = None
        if shared_path is not None:
            raw_shared = filter_transient(
                [sanitize_object(d, ns_warnings) for d in load_multidoc(shared_path)], ns_warnings)
            shared_docs = [d for d in raw_shared if not is_excluded(d)]
            left_out.update((shared_path.stem, *_kn(d)) for d in raw_shared if is_excluded(d))
            if len(shared_docs) != len(raw_shared):
                ns_warnings.append(f"{shared_path.name}: {len(raw_shared) - len(shared_docs)} "
                                   f"object(s) removed by --exclude")

        ms_docs: list[tuple[str, list[dict]]] = []
        skipped_docs: list[list[dict]] = []
        for ms_file in sorted(ns_dir.glob("*.yaml")):
            if ms_file == shared_path:  # loaded above as the shared release
                continue
            docs = filter_transient([sanitize_object(d, ns_warnings) for d in load_multidoc(ms_file)],
                                    ns_warnings)
            if not is_included(docs):
                ns_warnings.append(f"{ms_file.name}: not selected by --include — no base/, values/ "
                                   f"or release written")
                skipped_docs.append(docs)
                left_out.update((ms_file.stem, *_kn(d)) for d in docs)
                continue
            hit = next((d for d in docs if is_excluded(d)), None)
            if hit is not None:
                ns_warnings.append(
                    f"{ms_file.name}: excluded by --exclude (matched {hit.get('kind')}/"
                    f"{(hit.get('metadata') or {}).get('name')}) — no base/, values/ or release written")
                skipped_docs.append(docs)
                left_out.update((ms_file.stem, *_kn(d)) for d in docs)
                continue
            ms_docs.append((ms_file.stem, docs))
        # A selection decides what `shared` is for: what two or more of the
        # microservices being converted use. One user takes it home; none drops it.
        if (INCLUDE_PATTERNS or EXCLUDE_PATTERNS) and shared_docs:
            before = shared_docs
            shared_docs, notes = place_shared_by_use(shared_docs, ms_docs, skipped_docs)
            ns_warnings.extend(notes)
            # Dropped, not moved into a microservice: left out like a skipped file.
            kept_keys = {_kn(d) for d in shared_docs} | {_kn(d) for _, docs in ms_docs for d in docs}
            left_out.update((shared_path.stem, *_kn(d)) for d in before if _kn(d) not in kept_keys)
            shared_docs = shared_docs or None
        # The variant folder each file lands in: one level per folder group
        # whose token its name carries (ms1-yellow -> yellow/).
        stems = [stem for stem, _ in ms_docs]
        folders = ["" if stem in TOKEN_EXEMPT_STEMS
                   else "/".join(t for t in (g.token_in(stem) for g in folder_groups) if t) for stem in stems]
        # Two files normalizing to one name in the SAME folder would be two
        # releases writing one values/ file — those keep their own file names.
        normalized = [env_normalized_ms_name(namespace, stem) for stem in stems]
        clashes = {k for k, c in Counter(zip(folders, normalized)).items() if c > 1}
        for i, (stem, docs) in enumerate(ms_docs):
            if normalized[i] != stem and ((folders[i], normalized[i]) in clashes
                                          or normalized[i] == SHARED_DIR_NAME):
                ns_warnings.append(f"{stem}.yaml: would normalize to '{normalized[i]}', which another "
                                   f"release in this namespace also claims — kept as '{stem}'")
            else:
                ms_docs[i] = (normalized[i], docs)
        ms_meta[namespace] = list(zip(stems, folders))
        loaded[namespace] = (shared_docs, ms_docs, ns_warnings)

    if INCLUDE_PATTERNS and not any(ms_docs or shared_docs for shared_docs, ms_docs, _ in loaded.values()):
        sys.exit(f"--include {','.join(INCLUDE_PATTERNS)}: no object in {input_dir} matches — nothing "
                 f"to convert, nothing was written. Patterns match object names and label/annotation "
                 f"values (not namespace names); `=NAME` is an exact name, `key=value` one label.")

    # Same object, same place in every namespace: shared only where it is
    # shared everywhere, otherwise in its microservice everywhere.
    unify_shared_placement(loaded)

    known_pv_names = {(d.get("metadata") or {}).get("name")
                      for shared_docs, ms_docs, _ in loaded.values()
                      for docs in [shared_docs or []] + [docs for _, docs in ms_docs]
                      for d in docs if d.get("kind") == "PersistentVolume"}

    for namespace, ns_dir in namespace_dirs:
        shared_docs, ms_docs, ns_warnings = loaded[namespace]
        ns_registry = NamespaceRegistry(namespace)

        promoted = promote_shared_candidates(namespace, ms_docs, shared_docs or [], ns_registry)
        if ns_registry.promotions:
            # Re-scrub: a promoted copy is held to --exclude like anything in shared.yaml.
            shared_docs = [d for d in promoted if not is_excluded(d)]
            for p in ns_registry.promotions:
                ns_warnings.append(
                    f"{p['kind']}/{p['from']}: byte-identical in {p['promoted_from']} — promoted to "
                    f"the namespace-shared release under its original name; each declarer now "
                    f"references the shared copy")

        if shared_docs is not None:
            shared_ctx = build_microservice(
                namespace, SHARED_DIR_NAME, shared_docs, ns_registry, cluster_registry,
                cluster_values, ns_warnings, owner_label="_shared", known_pv_names=known_pv_names,
            )
            per_namespace_shared[namespace] = shared_ctx
            per_namespace_shared_objects[namespace] = {
                (d.get("kind"), (d.get("metadata") or {}).get("name")) for d in shared_docs}
            errs, warns = validate_resource_names(namespace, SHARED_DIR_NAME, shared_ctx.values)
            name_errors += errs
            all_warnings += warns
            # Regular microservices get this same extend right after their own
            # build_microservice call, a few lines below — shared was missing
            # it, so e.g. "chart supports one managed ServiceAccount per
            # release" for a second ServiceAccount in shared.yaml silently
            # never reached conflicts_and_warnings.txt.
            all_warnings.extend(f"[{namespace}/{SHARED_DIR_NAME}] {w}" for w in shared_ctx.warnings)

        ms_contexts: list[tuple[str, MicroserviceContext]] = []
        for (ms_name, docs), (stem, folder) in zip(ms_docs, ms_meta[namespace]):
            # ms1-yellow and ms1-black are both 'ms1' but two owners.
            ctx = build_microservice(namespace, ms_name, docs, ns_registry, cluster_registry, cluster_values,
                                     ns_warnings, owner_label=stem if folder else ms_name,
                                     known_pv_names=known_pv_names)
            ms_contexts.append((ms_name, ctx))
            out_ms.setdefault(f"{namespace}/{folder}" if folder else namespace, []).append((ms_name, ctx))
            all_warnings.extend(f"[{namespace}/{ms_name}] {w}" for w in ctx.warnings)
            errs, warns = validate_resource_names(namespace, ms_name, ctx.values)
            name_errors += errs
            all_warnings += warns

        releases = [(c, docs) for (_, c), (_, docs) in zip(ms_contexts, ms_docs)]
        if namespace in per_namespace_shared:
            releases.append((per_namespace_shared[namespace], shared_docs or []))
        ns_warnings.extend(rewire_service_selectors(releases))

        per_namespace_ms[namespace] = ms_contexts
        per_namespace_warnings[namespace] = ns_warnings
        all_renames.extend(ns_registry.renames)
        all_promotions.extend(ns_registry.promotions)

        contexts = [c for _, c in ms_contexts] + [per_namespace_shared[namespace]] \
            if namespace in per_namespace_shared else [c for _, c in ms_contexts]
        nad_names = set().union(*(c.nad_names for c in contexts))
        ns_warnings.extend(cross_reference_checks(
            k8s_ns(namespace), (shared_docs or []) + [d for _, docs in ms_docs for d in docs], nad_names))

    if name_errors:
        sys.exit("\n".join(
            [f"\n{len(name_errors)} resource name(s) Kubernetes will not accept — "
             f"nothing was written:"] + [f"  - {e}" for e in name_errors] +
            ["\nShorten them in the source manifests and re-run."]
        ))

    # Pass 2 — there is no GLOBAL defaults layer.
    #
    # The ms-applicationSet chart layers exactly three files per release:
    # base/<file> -> <ns>/defaults.yaml -> <ns>/values/<file>. A tree-root
    # defaults.yaml has nowhere to be applied from, and adding it back would
    # be actively unsafe rather than merely useless: 'shared' now goes through
    # that same fixed chain (it is a values/shared.yaml like any other
    # release), so every defaults layer it reads must be computed with the
    # shared release in the pool — and shared's values are a config/RBAC pool,
    # a completely different shape from a microservice's. Intersecting an
    # ENTIRE RUN's microservices with it leaves essentially nothing, so a
    # global layer computed safely is an empty file, and one computed
    # unsafely silently switches on a Route or an HPA that shared never
    # declared and has no Service or workload to back.
    #
    # Whatever is genuinely common across namespaces is therefore repeated in
    # each <ns>/defaults.yaml — N small copies instead of one, which is what
    # the values-repo layout this converter targets does by hand.
    #
    # (Historical note, kept because the failure was silent and expensive: an
    # earlier version subtracted a global layer from every microservice while
    # writing the file only in cluster-scoped mode. namespace_defaults is
    # computed in Pass 3 from ctx.values AFTER that subtraction, so anything
    # taken could never reappear anywhere — a total, silent loss of
    # workload.type, containerName, ports, service.ports and image.repository
    # whenever the input was homogeneous enough for them to be common. Nothing
    # is subtracted here any more, in either mode.)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Pass 2.5 — base registry: decide, per (namespace, microservice-or-
    # shared-release), which release-slug it publishes under, and write
    # base/<slug>.yaml ONCE per distinct slug.
    #
    # Regular microservices: every (namespace, name) pair sharing the same
    # normalized `name` is grouped, and common_subtree() — the SAME function
    # and the SAME 100%-agreement safety guarantee already used for
    # defaults.yaml (see its docstring) — computes what's identical across
    # EVERY instance in the group. That becomes the shared base/<name>.yaml.
    # Whatever's left over per instance (subtract_defaults, the inverse of
    # common_subtree) is a real, structural difference — e.g. etcd's own
    # advertise URL, or cache-edge's differing ConfigMap value — and is
    # merged into that instance's own values/<slug>.yaml alongside its
    # image/env minimal override (deep_merge), rather than forcing the
    # entire base to fork into a namespace-qualified copy. A name used by
    # only one instance needs no splitting — its whole values IS the base,
    # same as before.
    #
    # 'shared' releases are not grouped by their name: two apps' namespaces in
    # one run each have a 'shared' that only coincidentally shares the generic
    # name, and common_subtree across those would make base/shared.yaml the
    # accidental intersection of unrelated resource pools. They are grouped by
    # the objects they declare instead (group_shared_releases): the dev/tst/prd
    # copies of one app's shared ConfigMaps and Secrets are one release, split
    # into base/ and values/ exactly like a microservice. Before that, a shared
    # release differing in a single env word forked into one
    # base/<namespace>-shared.yaml per namespace. A group other than the first
    # still gets a namespace-qualified name (shared_group_slug).
    groups: dict[str, list[tuple[str, MicroserviceContext]]] = {}
    for namespace, ms_list in out_ms.items():
        for name, ctx in ms_list:
            groups.setdefault(name, []).append((namespace, ctx))

    base_dir = output_dir / BASE_DIR_NAME
    release_slugs: dict[tuple[str, str], str] = {}
    minimal_extras: dict[tuple[str, str], dict] = {}
    # What THIS run produced — anything else already in the output tree is
    # left over from a previous run whose input has since changed (see
    # find_stale_output).
    expected_values: dict[str, set] = {}
    env_group_applied: list[dict] = []

    def publish(slug: str, release: str, entries: list[tuple[str, MicroserviceContext]]) -> None:
        """Write base/<slug>.yaml once for every (folder, ctx) in entries — what
        all of them agree on, group words templated in — and leave the rest
        (minimal_extras) for each one's own values/<slug>.yaml."""
        for namespace, _ in entries:
            release_slugs[(namespace, release)] = slug
        if len(entries) == 1:
            write_base_file(base_dir, slug, entries[0][1].values)
            return
        key_injections: dict = {}
        if env_groups:
            unify_group_keys(entries, env_groups, key_injections)
        shared_base = common_subtree([ctx.values for _, ctx in entries])
        injections = {}
        if env_groups:
            injections, applied = combine_env_groups(slug, entries, env_groups, shared_base,
                                                     args.interactive)
            for ns, inj in key_injections.items():
                injections[ns] = {**inj, **injections.get(ns, {})}
            env_group_applied.extend(applied)
            all_warnings.extend(f"[{slug}] env-group: {a['path']} -> {a['template']!r}"
                                + (f", injected {a['injection']}" if a["injection"] else "")
                                for a in applied)
        write_base_file(base_dir, slug, shared_base)
        for namespace, ctx in entries:
            remainder = subtract_defaults(ctx.values, shared_base)
            # The group values this release's templates read come from its
            # folder's defaults.yaml (folder_values); only a value that
            # disagrees with the folder rides in the release's own values/.
            own = {g: v for g, v in injections.get(namespace, {}).items()
                   if folder_values(namespace, env_groups).get(g) != v}
            extra = deep_merge(own, remainder)
            if extra:
                minimal_extras[(namespace, release)] = extra
            if remainder:
                all_warnings.append(
                    f"[{namespace}/{release}] shares base/{slug}.yaml with its other namespaces' "
                    f"copies; {_leaf_paths(remainder)} differ and were pushed into this "
                    f"namespace's own values/{slug}.yaml instead"
                )

    for name, entries in groups.items():
        publish(name, name, entries)

    # 'shared' releases: the namespaces whose shared objects are the same ones
    # (each environment's copy) are ONE release, published exactly like a
    # microservice above. Another app's namespaces in the same run form a
    # group of their own, under a name of their own (shared_group_slug).
    shared_groups = group_shared_releases(per_namespace_shared, per_namespace_shared_objects,
                                          {ns: {name for name, _ in ms} for ns, ms in out_ms.items()})
    taken = set(groups)
    for i, members in enumerate(shared_groups):
        slug = SHARED_DIR_NAME if i == 0 else shared_group_slug(members, taken)
        taken.add(slug)
        if i:
            all_warnings.append(
                f"[{', '.join(members)}] '{SHARED_DIR_NAME}' is not another environment's copy of "
                f"the one in {', '.join(shared_groups[0])} (no object in common, or a namespace "
                f"ending in the same '-<token>') — writing base/{slug}.yaml instead of sharing "
                f"base/{SHARED_DIR_NAME}.yaml"
            )
        publish(slug, SHARED_DIR_NAME, [(ns, per_namespace_shared[ns]) for ns in members])

    # Pass 3 — per namespace: this namespace's own defaults.yaml layer plus
    # each release's values/<slug>.yaml. base/ was already written in pass 2.5.
    # No per-namespace ArgoCD manifest is written at all any more: the two
    # files at the tree root are the entire wiring, and a namespace directory
    # is discovered by the root ApplicationSet simply by existing.
    out_keys = set(out_ms) | set(per_namespace_shared) | {k for k, d in namespace_dirs if is_input_ns(d)}
    for namespace in sorted(out_keys):
        out_ns_dir = output_dir / namespace
        ms_contexts = out_ms.get(namespace, [])
        ns_warnings = per_namespace_warnings.get(namespace, [])
        shared_ctx = per_namespace_shared.get(namespace)

        # 'shared' is in the pool, not excluded from it. Every release in this
        # namespace — shared included — reads this exact file as the middle
        # layer of the chart's fixed three-file chain, and common_subtree only
        # ever promotes a key that is present with an IDENTICAL value in every
        # member of the pool. Including shared is therefore what makes the
        # uniform chain safe for it: a key two unrelated microservices happen
        # to agree on (say route.enabled with identical TLS settings) can no
        # longer be promoted here and silently switch on a Route that the
        # shared release never declared and has no Service to back. The chart's
        # own validation would catch that at render time, but it should never
        # come up. The cost is a smaller defaults layer whenever a namespace
        # has a shared.yaml, which is the right trade: a defaults file is an
        # optimisation, rendering objects nobody asked for is a defect.
        defaults_pool = [ctx.values for _, ctx in ms_contexts]
        if shared_ctx is not None:
            defaults_pool.append(shared_ctx.values)
        # Only labels and the folder's group values. Everything else common_subtree
        # finds is already in base/ or in every values/ file (neither is ever
        # subtracted against this layer), so copying it here only made a second
        # source of truth for containerName, service ports, the image — and a
        # defaults file editors read as "set once for the namespace" when it wasn't.
        common = common_subtree(defaults_pool)
        namespace_defaults = {k: common[k] for k in DEFAULTS_KEYS if k in common}
        labelled = [ctx.values for _, ctx in ms_contexts
                    if (ctx.values.get("workload") or {}).get("type") in LABELLED_WORKLOAD_TYPES]
        if labelled:
            same = common_subtree([{k: v[k] for k in WORKLOAD_LABEL_KEYS if isinstance(v.get(k), dict)}
                                   for v in labelled])
            namespace_defaults.update({k: same[k] for k in WORKLOAD_LABEL_KEYS if same.get(k)})
            # Said once, here, rather than again in every values/ file: what a
            # namespace file restates of these is dropped (defaults sit right
            # under it, so the rendered labels are the same). base/ is left
            # alone — it is shared with namespaces whose defaults may not say so.
            for key, extra in minimal_extras.items():
                if key[0] != namespace:
                    continue
                for k in WORKLOAD_LABEL_KEYS:
                    own, dflt = extra.get(k), namespace_defaults.get(k)
                    if isinstance(own, dict) and isinstance(dflt, dict):
                        rest = {lk: lv for lk, lv in own.items() if dflt.get(lk) != lv}
                        if rest:
                            extra[k] = rest
                        else:
                            extra.pop(k)
        namespace_defaults.update(folder_values(namespace, env_groups))

        out_ns_dir.mkdir(parents=True, exist_ok=True)
        header_ns_defaults = (
            "# Auto-generated by convert_to_universal_chart.py — DO NOT EDIT BY HAND.\n"
            f"# Namespace defaults for '{namespace}': the labels every microservice in\n"
            "# THIS folder carries (on its workload and its pods), plus the group\n"
            "# values (--env-group / --env-suffixes) the folder stands for. Applied\n"
            "# over base/<release>.yaml, beneath this folder's values/<release>.yaml.\n"
            "# Re-run the converter to regenerate after the source manifests change.\n\n"
        )
        (out_ns_dir / DEFAULTS_FILE_NAME).write_text(
            header_ns_defaults + dump_yaml(helm_safe_values(namespace_defaults), spaced=True), encoding="utf-8"
        )

        expected_values.setdefault(namespace, set())
        for ms_name, ctx in ms_contexts:
            slug = release_slugs[(namespace, ms_name)]
            write_microservice_output(out_ns_dir, ms_name, slug, namespace, ctx,
                                       minimal_extra=minimal_extras.get((namespace, ms_name)))
            expected_values[namespace].add(slug)

        if shared_ctx is not None:
            slug = release_slugs[(namespace, SHARED_DIR_NAME)]
            write_microservice_output(out_ns_dir, SHARED_DIR_NAME, slug, namespace, shared_ctx,
                                       minimal_extra=minimal_extras.get((namespace, SHARED_DIR_NAME)))
            expected_values[namespace].add(slug)

        ms_count = len(ms_contexts)
        all_warnings.extend(f"[{namespace}] {w}" for w in ns_warnings)
        summary["namespaces"][namespace] = {"microservices": ms_count, "warnings": len(ns_warnings)}
        summary["totals"]["microservices"] += ms_count

    # The ms-applicationSet chart names each generated Application
    # '<release>-<last "-" token of the namespace>' — 'jenkins' in
    # 'devops-team-dev' becomes Application 'jenkins-dev'. Two namespaces
    # ending in the same token that run a release of the same name therefore
    # produce ONE Application name for two different deployments, and ArgoCD
    # keeps a single object per name in its own namespace: they would
    # overwrite each other's spec on every reconcile. Nothing here can rename
    # them safely (the name is what a human greps for), so it is reported the
    # same way every other unresolvable collision in this converter is.
    app_names: dict[tuple[str, str], str] = {}
    for (namespace, ms_name), slug in sorted(release_slugs.items()):
        # ms-applicationSet uses the file name as the Helm release name, and
        # Helm refuses one over 53 characters (or not a DNS subdomain): the
        # Application can never render. A 63-char workload name is valid
        # Kubernetes, so this is only caught here — before a sync fails.
        if len(slug) > 53 or not HELM_RELEASE_NAME_RE.match(slug):
            all_warnings.append(
                f"[P0] [{namespace}/{ms_name}] release name '{slug}' is not a valid Helm release name "
                f"({len(slug)} chars; at most 53, lowercase DNS subdomain) — its ArgoCD Application "
                f"cannot render. Rename the file (and set fullnameOverride to keep the object names)."
            )
        key = (slug, app_name_suffix(namespace))
        if key in app_names and app_names[key] != namespace:
            all_warnings.append(
                f"[{namespace}/{ms_name}] ArgoCD Application name '{key[0]}-{key[1]}' collides with "
                f"namespace '{app_names[key]}' — both namespaces end in '-{key[1]}' and both run "
                f"'{slug}'. The two Applications would overwrite each other; rename one namespace "
                f"or one microservice."
            )
        else:
            app_names[key] = namespace

    # In namespace-scoped mode nothing ever reaches cluster_values —
    # handle_cluster_scoped routes it into the declaring namespace instead —
    # so this stays empty and no cluster-shared release is written.
    has_cluster_values = not NAMESPACE_SCOPED_ONLY and (
       any(cluster_values[k] for k in ("storageClasses", "persistentVolumes", "scc", "clusterSecretStores")) or
       cluster_values["rbac"]["clusterRoles"] or cluster_values["rbac"]["clusterRoleBindings"])
    if has_cluster_values:
        cluster_dir = output_dir / CLUSTER_SHARED_DIR_NAME / VALUES_DIR_NAME
        cluster_dir.mkdir(parents=True, exist_ok=True)
        header = (
            "# Auto-generated by convert_to_universal_chart.py\n"
            "# Cluster-scoped resources deduplicated across the ENTIRE run — deploy exactly ONCE.\n"
            "workload:\n  type: none\nservice:\n  enabled: false\n\n"
        )
        (cluster_dir / "cluster-shared-values.yaml").write_text(header + dump_yaml(helm_safe_values(cluster_values), spaced=True), encoding="utf-8")
    if has_cluster_values and ARGOCD_MANIFESTS:
        app = build_cluster_shared_application(
            args.cluster_shared_namespace, args.chart_repo_url, args.chart_path, args.chart_revision,
            args.values_repo_url, args.values_revision, args.values_path,
            argocd_namespace=args.argocd_namespace, project=args.argocd_project,
            destination_server=args.destination_server, destination_name=args.argocd_cluster,
            auto_sync=args.auto_sync)
        # At the tree ROOT, next to the root ApplicationSet: that is where the
        # root Application looks for it by name, and cluster-shared/ itself is
        # excluded from the root ApplicationSet's directory generator because
        # it is not a namespace.
        (output_dir / CLUSTER_SHARED_APP_FILE_NAME).write_text(dump_yaml(app), encoding="utf-8")

    summary["totals"]["warnings"] = len(all_warnings)
    if env_groups:
        summary["env_group_combinations"] = env_group_applied
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "conversion_report.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    # Read by name_fidelity_check.py, which otherwise reports every deliberate
    # collision rename as an INVENTED name and fails the run. Written every time,
    # including empty, so the checker can tell "no renames happened" from "this
    # output predates the record and cannot be judged". Promotions keep their
    # name (from == to) and are recorded alongside for the same audit trail.
    (report_dir / "renames.json").write_text(json.dumps(all_renames + all_promotions, indent=2),
                                             encoding="utf-8")
    (report_dir / "conflicts_and_warnings.txt").write_text(
        ("\n".join(all_warnings) + "\n") if all_warnings else "No conflicts detected.\n", encoding="utf-8"
    )

    print(f"Converted {summary['totals']['microservices']} microservices across {len(namespace_dirs)} namespaces.")
    print(f"Auto-resolved conflicts / warnings: {summary['totals']['warnings']} "
          f"(see {report_dir / 'conflicts_and_warnings.txt'})")
    print(f"Base (shared) values written under: {output_dir}/{BASE_DIR_NAME}/")
    print(f"Minimal values written under:       {output_dir}/<namespace>/{VALUES_DIR_NAME}/")
    if ARGOCD_MANIFESTS:
        print(f"ArgoCD wiring:                      {output_dir}/{ROOT_APP_FILE_NAME} "
              f"+ {output_dir}/{ROOT_APPSET_FILE_NAME}")
        print(f"Bootstrap with:                     kubectl apply -f {output_dir}/{ROOT_APP_FILE_NAME}")
        if args.values_repo_url == VALUES_REPO_PLACEHOLDER:
            print(f"NOTE: no --values-repo-url given; replace {VALUES_REPO_PLACEHOLDER} in "
                  f"{ROOT_APPSET_FILE_NAME} and {ROOT_APP_FILE_NAME} before applying.")
    else:
        print("ArgoCD wiring:                      not written (pass --argocd-manifests true)")

    # The whole ArgoCD wiring, written once everything it deploys exists on
    # disk: one ApplicationSet that discovers the namespace directories, and one
    # Application that deploys it. Only with --argocd-manifests.
    if ARGOCD_MANIFESTS:
        root_appset = build_root_applicationset(
            args.root_set_name or f"{args.root_app_name}-set", args.chart_repo_url, args.appset_chart_path, args.chart_revision,
            args.chart_path, args.values_repo_url, args.values_revision, args.values_path,
            argocd_namespace=args.argocd_namespace, project=args.argocd_project,
            destination_server=args.destination_server, destination_name=args.argocd_cluster,
            auto_sync=args.auto_sync)
        appset_header = (
            "# Auto-generated by convert_to_universal_chart.py — DO NOT EDIT BY HAND.\n"
            "# One Application per NAMESPACE DIRECTORY in this tree, each deploying the\n"
            f"# '{args.appset_chart_path}' chart, which in turn fans out over that\n"
            "# namespace's values/*.yaml. A new namespace is a new directory here and\n"
            "# nothing else — no manifest to add, no apply to run.\n"
            f"# Deployed by {ROOT_APP_FILE_NAME}; do not apply it by hand.\n\n"
        )
        (output_dir / ROOT_APPSET_FILE_NAME).write_text(appset_header + root_appset, encoding="utf-8")

        root_app = build_root_application(args.root_app_name, args.values_repo_url, args.values_revision,
                                           args.values_path, argocd_namespace=args.argocd_namespace,
                                           project=args.argocd_project)
        root_header = (
            "# Auto-generated by convert_to_universal_chart.py — DO NOT EDIT BY HAND.\n"
            "# ROOT app-of-apps: the ONE object you apply by hand. It is .txt so that\n"
            "# nothing applying this directory (ArgoCD included) ever picks it up; kubectl\n"
            "# reads it fine when the file is named explicitly. Bootstrapping is:\n"
            "#\n"
            f"#     kubectl apply -f {output_dir.name}/{ROOT_APP_FILE_NAME}\n"
            "#\n"
            f"# and nothing else. It deploys {ROOT_APPSET_FILE_NAME} (and, under\n"
            f"# --cluster-scoped, {CLUSTER_SHARED_APP_FILE_NAME}) by name; everything\n"
            "# after that reconciles from git. Adding a namespace or a microservice\n"
            "# later is a git commit, not another apply.\n"
            "#\n"
            "# prune is false on purpose: pruning the root ApplicationSet would cascade\n"
            "# into deleting every Application it generated and every workload those\n"
            "# deploy. See build_root_application's docstring.\n\n"
        )
        (output_dir / ROOT_APP_FILE_NAME).write_text(root_header + dump_yaml(root_app), encoding="utf-8")

    # Stale output — always reported, because a leftover <ns>/values/<name>.yaml
    # keeps ArgoCD deploying a microservice that no longer exists in the input.
    expected_base = {f.stem for f in base_dir.rglob("*.yaml")} if base_dir.is_dir() else set()
    expected_base &= {slug for slug in release_slugs.values()}
    stale = find_stale_output(output_dir, expected_values, expected_base)
    if stale:
        stale_report = report_dir / "stale_output.txt"
        stale_releases = [f for f in stale if f.parent.name == VALUES_DIR_NAME]
        stale_report.write_text(
            "Generated files with no counterpart in this run's input — left over from a\n"
            "previous run whose input or options have since changed (including the root\n"
            "files under their old names root-application.yaml / root-applicationSet.yaml). The\n"
            "converter only ever writes, so re-running does NOT remove these; pass --prune\n"
            "to delete them.\n\n"
            "The <namespace>/values/*.yaml ones are the dangerous kind: the\n"
            "ms-applicationSet chart's git 'files' generator enumerates exactly that\n"
            "directory, so each one still deploys a microservice that no longer exists\n"
            "in the source of truth.\n\n"
            + "\n".join(str(f.relative_to(output_dir)) for f in stale) + "\n",
            encoding="utf-8",
        )
        if args.prune:
            for f in stale:
                f.unlink()
            # Drop directories the prune just emptied (e.g. cluster-shared/ and
            # its values/ when switching a tree to namespace-scoped output), so
            # the tree doesn't keep hollow folders suggesting content that is
            # gone. Deepest-first, and only ever when genuinely empty.
            for d in sorted({f.parent for f in stale}, key=lambda p: -len(p.parts)):
                while d != output_dir and d.is_dir() and not any(d.iterdir()):
                    d.rmdir()
                    d = d.parent
            print(f"\nPruned {len(stale)} stale generated file(s) "
                  f"({len(stale_releases)} live release(s)) — see {stale_report}")
        else:
            print(f"\n{len(stale)} STALE generated file(s) found, "
                  f"{len(stale_releases)} of them values files that still deploy "
                  f"a microservice no longer in the input (see {stale_report}).")
            print("   Re-running does not remove them — pass --prune to delete them.")

    if args.skip_verify:
        print("Skipping render-verification (--skip-verify).")
        return

    chart = Path(args.chart)
    if not chart.is_dir():
        sys.exit(f"\n--chart path not found: {chart} "
                  f"(pass --chart to point at the Universal Chart, or --skip-verify to skip this step)")

    print(f"\nVerifying against {chart} — rendering every release with `helm template`...")
    try:
        rendered, conflicts = verify_no_conflicts(chart, output_dir, frozenset(stale))
    except RuntimeError as e:
        sys.exit(f"\n{e}")

    render_report = report_dir / "render_conflicts.txt"
    render_report.write_text(
        ("\n".join(conflicts) + "\n") if conflicts else "No duplicate resource ownership detected.\n",
        encoding="utf-8",
    )
    if conflicts:
        print(f"\n{len(conflicts)} REAL RENDER-TIME CONFLICT(S) FOUND (see {render_report}):")
        for c in conflicts:
            print(f"  - {c}")
        sys.exit(1)
    print(f"\nNo duplicate resource ownership detected across any of the {rendered} rendered releases. Conflict-free.")

    # Opt-in round-trip fidelity check. Runs LAST and only when asked for, so
    # everything above behaves exactly as it did before this existed. Note it
    # rides along with the verification step above rather than being separately
    # skippable: --skip-verify means "don't shell out to helm", and this needs
    # helm for the same reason.
    if not args.verify_fidelity:
        return

    print(f"\nRound-trip fidelity check — comparing the rendered output back against {input_dir}...")
    try:
        fidelity_report, fidelity_stats = verify_raw_fidelity(chart, input_dir, output_dir, frozenset(stale))
    except RuntimeError as e:
        sys.exit(f"\n{e}")

    fidelity_path = report_dir / "fidelity_report.txt"
    converter_h, input_h, run_id = run_fingerprint(input_dir)
    summary_lines = [
        "Round-trip fidelity report — raw input vs `helm template` of the converted output.",
        "",
        "RUN IDENTITY — counts below describe THIS run and no other.",
        f"  run id:         {run_id}",
        f"  converter:      {converter_h}  ({Path(__file__).name})",
        f"  input:          {input_h}  ({input_dir})",
        f"  output:         {output_dir}",
        "  Compare two fidelity reports ONLY when their run ids match. A differing",
        "  run id means the converter, the input, or both changed — so a count that",
        "  moved between them may be more data rather than a regression, and the two",
        "  numbers are not evidence about each other. Do not maintain a summary of",
        "  this report by hand; the breakdown below is generated from these same",
        "  lines, in this same run, and is the only summary that cannot drift.",
        "",
        "Legend:",
        "  INFO   an object that legitimately moved (shared release, cluster-shared,",
        "         renamed on collision) — expected, not a loss.",
        "  ERROR  an object rendered by no release at all, an object with no raw",
        "         source, or a field the conversion LOST or CHANGED.",
        "",
        "Fields the chart derives itself (selector labels, pod-template labels, Helm's",
        "own metadata labels) are excluded by design — see _fidelity_ignored_path.",
        "Fields the render ADDED are counted but not listed: a Helm chart filling in a",
        "Kubernetes default the raw dump omitted is normal and harmless.",
        "",
        f"objects matched:            {fidelity_stats['matched']}",
        f"  of those, relocated:      {fidelity_stats['relocated']}",
        f"fields LOST (raw -> render): {fidelity_stats['lost']}",
        f"fields CHANGED:             {fidelity_stats['changed']}",
        f"fields ADDED (chart default, informational): {fidelity_stats['added']}",
        f"objects in raw but rendered by nothing:      {fidelity_stats['missing_objects']}",
        f"objects rendered with no raw source:         {fidelity_stats['extra_objects']}",
        f"render failures:            {fidelity_stats['render_failures']}",
        f"dangling references (rename lost a reference): {fidelity_stats['dangling_refs']}",
    ]
    summary_lines += fidelity_error_breakdown(fidelity_report)
    fidelity_path.write_text("\n".join(summary_lines + fidelity_report) + "\n", encoding="utf-8")

    problems = (fidelity_stats["lost"] + fidelity_stats["changed"]
                + fidelity_stats["missing_objects"] + fidelity_stats["extra_objects"]
                + fidelity_stats["render_failures"] + fidelity_stats["dangling_refs"])
    print(f"  {fidelity_stats['matched']} objects matched "
          f"({fidelity_stats['relocated']} legitimately relocated).")
    if problems:
        print(f"  {problems} potential fidelity issue(s) written to {fidelity_path}")
        if args.fidelity_strict:
            sys.exit(f"  --fidelity-strict: failing the run. The conversion lost, altered or "
                     f"invented something — see {fidelity_path}")
        print("  (report-only — this does NOT fail the run; review before treating any as a bug)")
    else:
        print(f"  No fidelity issues found. Report: {fidelity_path}")


if __name__ == "__main__":
    main()
#