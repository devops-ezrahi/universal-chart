"""--upgrade TREE: bring a values tree an earlier run wrote — by any version
of this converter — up to what the converter writes today.

The old tree is only read for WHAT to convert: which namespaces and variant
folders it deploys, what its releases are called and where their files sit,
and the settings its rootApplicationSet.yaml and defaults.yaml files record.
Its values themselves are never interpreted with today's chart, because the
values format has changed across releases (env as a list, then NAME: value,
now NAME: {value|valueFrom}) and nothing in a tree says which one it uses.

  live (default)   the objects come from the cluster: those namespaces are
                   pulled exactly like -n. What runs is what the old tree
                   deployed, hand edits included, whatever produced it.
  --upgrade-offline  the old tree is rendered with the chart AS IT WAS when
                   the tree was written (the tree tracks the chart's main
                   branch, so that is what Argo CD rendered it with): a temp
                   copy of this repo at the last commit on or before the
                   tree's last commit date in its own git repo.

Either way the result is the same dump every other source produces, so the
conversion, the render check and the fidelity check are the ordinary ones.
"""
from __future__ import annotations

import argparse
import io
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO)) if str(REPO) not in sys.path else None
from converter import engine  # noqa: E402

# Only what Argo CD / a helm install stamps on an object it applies. Labels the
# app itself carries (app.kubernetes.io/instance, managed-by, ...) are NOT
# touched: the engine carries labels as live on purpose — dropping one from a
# pod template restarts the pods — and it already drops the chart's own link
# annotations and checksum stamps when it re-imports a chart-deployed object.
STAMP_LABELS = ("argocd.argoproj.io/instance",)
STAMP_ANNOTATION_PREFIXES = ("argocd.argoproj.io/", "meta.helm.sh/")
DEFAULTS_SETTING_KEYS = {"environment", engine.CLUSTER_KEY, "labels", "podLabels", "commonLabels", "namespace"}


# ---------------------------------------------------------------------------
# Reading the old tree
# ---------------------------------------------------------------------------

class OldTree:
    """What an old tree says: folders, releases, and the settings it recorded."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        if not self.root.is_dir():
            sys.exit(f"--upgrade: {root} is not a folder")
        # A folder is a directory holding defaults.yaml next to values/.
        self.folders: dict[str, Path] = {}
        for d in sorted(self.root.rglob(engine.DEFAULTS_FILE_NAME)):
            rel = d.parent.relative_to(self.root)
            if rel.parts and rel.parts[0] not in engine.NON_NAMESPACE_OUTPUT_DIRS \
                    and rel.parts[0] != engine.BASE_DIR_NAME and (d.parent / engine.VALUES_DIR_NAME).is_dir():
                self.folders[rel.as_posix()] = d.parent
        if not self.folders:
            sys.exit(f"--upgrade: {root} holds no <folder>/defaults.yaml + values/ — not a converter tree")
        # {folder: {release stem: path relative to that folder's values/}}
        self.releases: dict[str, dict[str, Path]] = {
            f: {vf.stem: vf.relative_to(d / engine.VALUES_DIR_NAME)
                for vf in sorted((d / engine.VALUES_DIR_NAME).rglob("*.yaml"))}
            for f, d in self.folders.items()}
        self.defaults = {f: (yaml.safe_load((d / engine.DEFAULTS_FILE_NAME).read_text(encoding="utf-8")) or {})
                         for f, d in self.folders.items()}
        self.cluster_shared = (self.root / engine.CLUSTER_SHARED_DIR_NAME).is_dir()

    def namespaces(self) -> list[str]:
        return list(dict.fromkeys(engine.k8s_ns(f) for f in self.folders))

    def root_appset(self) -> dict:
        for name in (engine.ROOT_APPSET_FILE_NAME, "root-applicationSet.yaml"):
            p = self.root / name
            if p.is_file():
                docs = [d for d in yaml.safe_load_all(p.read_text(encoding="utf-8")) if isinstance(d, dict)]
                if docs:
                    return docs[0]
        return {}

    def has_wiring(self) -> bool:
        return any((self.root / n).is_file() for n in
                   (engine.ROOT_APPSET_FILE_NAME, engine.ROOT_APP_FILE_NAME, *engine.LEGACY_ROOT_FILE_NAMES))


def _find(node, pred):
    """First dict anywhere in node for which pred(dict) is true."""
    if isinstance(node, dict):
        if pred(node):
            return node
        for v in node.values():
            hit = _find(v, pred)
            if hit is not None:
                return hit
    elif isinstance(node, list):
        for v in node:
            hit = _find(v, pred)
            if hit is not None:
                return hit
    return None


def recorded_settings(tree: OldTree) -> dict[str, object]:
    """{argparse dest: value} the old tree records. Tolerant of every layout
    the root ApplicationSet has had: it searches rather than indexes."""
    s: dict[str, object] = {}
    appset = tree.root_appset()
    if appset:
        name = (appset.get("metadata") or {}).get("name")
        if name:
            s["root_set_name"] = name
        git = _find(appset.get("spec") or {}, lambda d: "repoURL" in d and ("files" in d or "directories" in d))
        if git:
            s["values_repo_url"] = git["repoURL"]
            if git.get("revision"):
                s["values_revision"] = git["revision"]
            paths = [f.get("path", "") for f in git.get("files") or git.get("directories") or [] if isinstance(f, dict)]
            prefix = next((p.split("**")[0].rstrip("/") for p in paths if "**" in p), "")
            if prefix:
                s["values_path"] = prefix
        tmpl = (appset.get("spec") or {}).get("template") or {}
        project = (tmpl.get("spec") or {}).get("project")
        if isinstance(project, str) and "{{" not in project:
            s["argocd_project"] = project
        src = _find(tmpl.get("spec") or {}, lambda d: "path" in d and "repoURL" in d and "helm" in d)
        if src:
            s["chart_repo_url"] = src["repoURL"]
            if src.get("targetRevision"):
                s["chart_revision"] = src["targetRevision"]
            s["appset_chart_path"] = src["path"]
            params = {p.get("name"): p.get("value") for p in (src.get("helm") or {}).get("parameters") or []
                      if isinstance(p, dict)}
            for param, dest in (("destinationServer", "destination_server"), ("links.argocdUrl", "argocd_url"),
                                ("links.consoleUrl", "console_url"), ("links.repoWebUrl", "repo_web_url"),
                                ("project", "argocd_project")):
                v = params.get(param)
                if isinstance(v, str) and v and "{{" not in v:
                    s[dest] = v
            if str(params.get("autoSync", "")).lower() in ("true", "false"):
                s["auto_sync"] = str(params["autoSync"]).lower() == "true"
    if tree.has_wiring():
        s["argocd_manifests"] = True
    if tree.cluster_shared:
        s["cluster_scoped"] = True

    # Group values each variant folder stands for (--env-group), environment
    # words (--env-suffixes), clusters (--namespace-cluster), from defaults.yaml.
    groups: dict[str, dict[str, str]] = {}
    envs: list[str] = []
    clusters: dict[str, str] = {}
    for folder, d in tree.defaults.items():
        parts = folder.split("/")
        group_keys = [k for k, v in d.items() if k not in DEFAULTS_SETTING_KEYS and isinstance(v, str)]
        for depth, tok in enumerate(parts[1:]):
            # the key whose value matches this folder level, else the depth-th group key
            key = next((k for k in group_keys if d[k] == tok), group_keys[depth] if depth < len(group_keys) else None)
            if key:
                groups.setdefault(key, {})[tok] = d[key]
        if isinstance(d.get("environment"), str):
            envs.append(d["environment"])
        c = d.get(engine.CLUSTER_KEY)
        if isinstance(c, dict) and (c.get("server") or c.get("name")):
            clusters[engine.k8s_ns(folder)] = c.get("server") or c.get("name")
    if groups:
        s["env_group"] = [f"{g}=" + ",".join(t if v == t else f"{t}:{v}" for t, v in toks.items())
                          for g, toks in groups.items()]
    if envs:
        s["env_suffixes"] = ",".join(dict.fromkeys(envs))
    if clusters:
        s["namespace_cluster"] = [f"{n}={c}" for n, c in clusters.items()]
    instances: dict[str, list[str]] = {}
    for folder, rels in tree.releases.items():
        for stem, rel in rels.items():
            data = yaml.safe_load((tree.folders[folder] / engine.VALUES_DIR_NAME / rel).read_text(encoding="utf-8")) or {}
            if isinstance(data, dict) and data.get(engine.BASE_FILE_KEY):
                base = str(data[engine.BASE_FILE_KEY]).removesuffix(".yaml")
                instances.setdefault(base, []).append(stem)
    if instances:
        s["instances"] = [f"{b}={','.join(dict.fromkeys(names))}" for b, names in instances.items()]
    return s


def apply_settings(args: argparse.Namespace, ap: argparse.ArgumentParser, recorded: dict) -> list[str]:
    """Fill every option the user left at its default from what the old tree
    recorded. Returns one line per option taken over."""
    taken = []
    for dest, value in recorded.items():
        if not hasattr(args, dest):
            continue
        if getattr(args, dest) != ap.get_default(dest):
            continue  # given on the command line: that wins
        setattr(args, dest, value)
        taken.append(f"{dest.replace('_', '-')} = {value}")
    return taken


# ---------------------------------------------------------------------------
# Stripping what deployment added
# ---------------------------------------------------------------------------

def strip_stamps(doc: dict) -> dict:
    meta = doc.get("metadata") or {}
    labels = meta.get("labels") or {}
    for k in STAMP_LABELS:
        labels.pop(k, None)
    if not labels:
        meta.pop("labels", None)
    ann = meta.get("annotations") or {}
    for k in list(ann):
        if k.startswith(STAMP_ANNOTATION_PREFIXES):
            ann.pop(k)
    if not ann:
        meta.pop("annotations", None)
    return doc


# ---------------------------------------------------------------------------
# Offline: render the old tree with the chart of its time
# ---------------------------------------------------------------------------

def tree_date(tree: Path) -> str | None:
    r = subprocess.run(["git", "-C", str(tree), "log", "-1", "--format=%cI", "--", "."],
                       capture_output=True, text=True)
    return r.stdout.strip() or None


def chart_at(rev_or_date: str, *, is_date: bool, dest: Path) -> str:
    """Extract this repo at a revision (or the last main commit on/before a
    date) into dest; returns the commit."""
    if is_date:
        r = subprocess.run(["git", "-C", str(REPO), "rev-list", "-1", "--first-parent", f"--before={rev_or_date}", "HEAD"],
                           capture_output=True, text=True)
        sha = r.stdout.strip()
        if not sha:
            sys.exit(f"--upgrade-offline: no commit of {REPO} on or before {rev_or_date}")
    else:
        r = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--verify", f"{rev_or_date}^{{commit}}"],
                           capture_output=True, text=True)
        sha = r.stdout.strip()
        if r.returncode or not sha:
            sys.exit(f"--upgrade-chart-rev: {rev_or_date} is not a revision of {REPO}")
    data = subprocess.run(["git", "-C", str(REPO), "archive", sha], capture_output=True, check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(data)) as t:
        t.extractall(dest, filter="data")
    return sha


def layer_order(chart: Path) -> list[str]:
    """The order that chart's ApplicationSet layered the files in: September
    2026 charts put the folder's defaults.yaml under base/, later ones over it."""
    tpl = chart / "ms-applicationSet" / "templates" / "applicationSet.yaml"
    text = tpl.read_text(encoding="utf-8") if tpl.is_file() else ""
    block = text[text.find("valueFiles:"):] if "valueFiles:" in text else ""
    d, b = block.find("defaults.yaml"), block.find("base")
    return ["defaults", "base", "values"] if 0 <= d < b else ["base", "defaults", "values"]


def render_tree(tree: OldTree, chart: Path, dump: Path) -> list[str]:
    """Each release rendered as Argo CD rendered it, written as dump/<folder>/<release>.yaml."""
    order = layer_order(chart)
    warnings: list[str] = []
    jobs = []
    base_dir = tree.root / engine.BASE_DIR_NAME
    for folder, d in tree.folders.items():
        vdir = d / engine.VALUES_DIR_NAME
        for stem, rel in tree.releases[folder].items():
            vf = vdir / rel
            layers = {"base": engine.base_file_for(base_dir, vdir, vf), "defaults": d / engine.DEFAULTS_FILE_NAME,
                      "values": vf}
            chain = [layers[k] for k in order if layers[k].exists()]
            jobs.append((folder, stem, (chart, engine.argo_release_name(folder, stem), folder, chain)))
    cs = tree.root / engine.CLUSTER_SHARED_DIR_NAME / engine.VALUES_DIR_NAME / "cluster-shared-values.yaml"
    engine.prerender([j[2] for j in jobs])
    first = next(iter(tree.folders))
    for folder, stem, job in jobs:
        try:
            docs = engine.helm_template(*job)
        except RuntimeError as e:
            warnings.append(f"{folder}/values/{stem}.yaml: does not render with the chart of its time — {str(e)[:300]}")
            continue
        out = dump / folder / f"{stem}.yaml"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(yaml.safe_dump_all([strip_stamps(x) for x in docs], sort_keys=False, allow_unicode=True),
                       encoding="utf-8")
    if cs.exists():
        try:
            docs = engine.helm_template(chart, "cluster-shared-resources", "platform-shared", [cs])
            target = dump / first / "shared.yaml"
            prev = target.read_text(encoding="utf-8") if target.exists() else ""
            target.write_text(prev + ("---\n" if prev else "")
                              + yaml.safe_dump_all([strip_stamps(x) for x in docs], sort_keys=False, allow_unicode=True),
                              encoding="utf-8")
        except RuntimeError as e:
            warnings.append(f"cluster-shared: does not render with the chart of its time — {str(e)[:300]}")
    return warnings


# ---------------------------------------------------------------------------
# Live: pull the namespaces the old tree deploys
# ---------------------------------------------------------------------------

def strip_dump(dump: Path) -> None:
    """strip_stamps over every document of a pulled dump."""
    from converter.yaml_style import BlockDumper
    for f in sorted(dump.rglob("*.yaml")):
        docs = [d for d in yaml.safe_load_all(f.read_text(encoding="utf-8")) if isinstance(d, dict)]
        f.write_text(yaml.dump_all([strip_stamps(d) for d in docs], Dumper=BlockDumper, sort_keys=False,
                                   allow_unicode=True), encoding="utf-8")


# ---------------------------------------------------------------------------
# After the conversion: files back where the old tree had them, and a report
# ---------------------------------------------------------------------------

def place_like(tree: OldTree, out: Path) -> list[str]:
    """A release file the old tree kept in a values/ sub-folder goes back
    there, and its base/ file to the same sub-folder of base/ (Argo CD reads
    base/<sub>/<file> for values/<sub>/<file>). In place, the engine already
    rewrote such files where they were; this is for a new output folder."""
    moved = []
    bases_done = set()
    for folder, rels in tree.releases.items():
        vdir = out / folder / engine.VALUES_DIR_NAME
        for stem, rel in rels.items():
            if rel.parent == Path("."):
                continue
            src, dst = vdir / f"{stem}.yaml", vdir / rel
            if src.exists() and not dst.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
                moved.append(f"{folder}/values/{rel.as_posix()}")
            old_base = tree.root / engine.BASE_DIR_NAME / rel
            bsrc, bdst = out / engine.BASE_DIR_NAME / f"{stem}.yaml", out / engine.BASE_DIR_NAME / rel
            if old_base.exists() and bsrc.exists() and not bdst.exists() and stem not in bases_done:
                bdst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(bsrc), str(bdst))
                bases_done.add(stem)
                moved.append(f"base/{rel.as_posix()}")
    return moved


TRACKING_ANNOTATION = "argocd.argoproj.io/tracking-id"
TRACKING_LABEL = "app.kubernetes.io/instance"


def owner_of(doc: dict, apps: dict[str, tuple[str, str]]) -> tuple[str, str] | None:
    """(folder, release) of the old-tree Application that deployed `doc`, from
    Argo CD's own stamp: the tracking-id annotation (`<app>:<group>/<kind>:
    <ns>/<name>`, annotation tracking) or the instance label (label tracking)."""
    meta = doc.get("metadata") or {}
    tid = (meta.get("annotations") or {}).get(TRACKING_ANNOTATION)
    if isinstance(tid, str) and ":" in tid:
        hit = apps.get(tid.split(":", 1)[0])
        if hit:
            return hit
    return apps.get((meta.get("labels") or {}).get(TRACKING_LABEL))


def regroup_by_owner(tree: OldTree, dump: Path) -> list[str]:
    """Put every pulled object back into the release that deployed it.

    The importer groups a namespace by app label, which need not be how the
    old tree grouped it (a CronJob deployed inside its app's release comes
    back as a release of its own). Moving an object to another release moves
    it to another Argo CD Application: the old one prunes it and the new one
    creates it again — a Deployment would be deleted and recreated. So
    whatever Argo CD says it deployed for an old-tree Application is written
    to that release's dump file, <folder>/<release>.yaml, and only objects
    with no such stamp keep the importer's grouping. Returns report lines."""
    from converter.yaml_style import BlockDumper
    apps = {engine.argo_release_name(f, stem): (f, stem) for f, rels in tree.releases.items() for stem in rels}
    owned: dict[tuple[str, str], list[dict]] = {}
    kept: dict[Path, list[dict]] = {}
    for f in sorted(dump.rglob("*.yaml")):
        for doc in yaml.safe_load_all(f.read_text(encoding="utf-8")):
            if not isinstance(doc, dict):
                continue
            o = owner_of(doc, apps)
            (owned.setdefault(o, []) if o else kept.setdefault(f, [])).append(doc)
        f.unlink()
    lines = []
    if not owned:
        lines.append("no Argo CD tracking on the pulled objects: they keep the importer's grouping, which may "
                     "differ from the old tree's (see the release sets below) — --upgrade-offline keeps it exactly")
    for f, docs in kept.items():
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(yaml.dump_all(docs, Dumper=BlockDumper, sort_keys=False, allow_unicode=True), encoding="utf-8")
        if owned:
            lines.extend(f"{d.get('kind')}/{(d.get('metadata') or {}).get('name')}: no Argo CD tracking of an "
                         f"old-tree release — kept as pulled ({f.relative_to(dump).as_posix()})" for d in docs)
    for (folder, stem), docs in owned.items():
        target = dump / folder / f"{stem}.yaml"
        prev = list(yaml.safe_load_all(target.read_text(encoding="utf-8"))) if target.exists() else []
        # The importer can write one object twice (a ServiceAccount as a
        # workload's reference and again from the namespace scan): one copy.
        seen, unique = set(), []
        for d in [d for d in prev if d] + docs:
            m = d.get("metadata") or {}
            key = (d.get("kind"), m.get("namespace"), m.get("name"))
            if key not in seen:
                seen.add(key)
                unique.append(d)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(yaml.dump_all(unique, Dumper=BlockDumper, sort_keys=False,
                                        allow_unicode=True), encoding="utf-8")
    return lines


def compare_releases(tree: OldTree, out: Path) -> list[str]:
    """What the new tree deploys that the old one did not, and the reverse."""
    lines = []
    new = OldTree(out) if any(out.rglob(engine.DEFAULTS_FILE_NAME)) else None
    new_rel = new.releases if new else {}
    for folder in sorted(set(tree.releases) | set(new_rel)):
        old_s, new_s = set(tree.releases.get(folder, {})), set(new_rel.get(folder, {}))
        if folder not in new_rel:
            lines.append(f"{folder}: in the old tree, not in the new one (nothing pulled or rendered for it)")
            continue
        if folder not in tree.releases:
            lines.append(f"{folder}: new — the old tree had no such folder")
            continue
        if new_s - old_s:
            lines.append(f"{folder}: deploys {sorted(new_s - old_s)} the old tree did not — new on the cluster, "
                         f"or the earlier run left them out (--include/--exclude): pass the same option again")
        if old_s - new_s:
            lines.append(f"{folder}: the old tree deployed {sorted(old_s - new_s)}, the new one does not")
    return lines


# ---------------------------------------------------------------------------
# The entry point the command calls
# ---------------------------------------------------------------------------

def prepare(args: argparse.Namespace, ap: argparse.ArgumentParser, out: Path) -> OldTree:
    """Read the old tree and take over the settings it recorded."""
    tree = OldTree(Path(args.upgrade))
    taken = apply_settings(args, ap, recorded_settings(tree))
    print(f"==> upgrading {tree.root}: {len(tree.folders)} folder(s), "
          f"{sum(len(r) for r in tree.releases.values())} release(s)", file=sys.stderr)
    for t in taken:
        print(f"    from the old tree: {t}", file=sys.stderr)
    if out == tree.root:
        args.prune = True  # in place: what the old layout had and the new one does not goes
    if not args.skip_verify:
        args.verify_fidelity = True  # the new tree must deploy what the old one did
    return tree


def pull(tree: OldTree, args: argparse.Namespace, dump: Path) -> list[str]:
    """The old tree's objects into `dump`: from the cluster, or offline by
    rendering it with the chart of its time. Returns warnings."""
    from converter import sources
    if not args.upgrade_offline:
        plan = {ns: {"whole": True, "apps": [], "resources": []} for ns in tree.namespaces()}
        # The importer drops Argo CD's tracking stamp while cleaning; keep it
        # through the pull — regroup_by_owner reads it, strip_dump then drops it.
        from converter import importer
        importer.ANNOTATIONS_TO_REMOVE.discard(TRACKING_ANNOTATION)
        try:
            warnings = sources.pull_live(plan, dump, args.oc, args.kubeconfig)
        finally:
            importer.ANNOTATIONS_TO_REMOVE.add(TRACKING_ANNOTATION)
        warnings += regroup_by_owner(tree, dump)  # reads Argo CD's stamps, so before they go
        strip_dump(dump)
        for ns in plan:
            if not (dump / ns).is_dir():
                warnings.append(f"Namespace '{ns}': in the old tree but nothing on the cluster")
        warnings.append("live upgrade: what is not yet synced from git is not on the cluster and not in the "
                        "new tree — sync first, or use --upgrade-offline")
        return warnings
    work = Path(tempfile.mkdtemp(prefix="ucc-chart-"))
    try:
        if args.upgrade_chart:
            chart, what = Path(args.upgrade_chart).resolve(), f"the chart at {args.upgrade_chart}"
        else:
            if args.upgrade_chart_rev:
                sha = chart_at(args.upgrade_chart_rev, is_date=False, dest=work)
            else:
                when = tree_date(tree.root)
                if not when:
                    sys.exit(f"--upgrade-offline: {tree.root} is not in a git repo, so the date it was written is "
                             f"unknown — pass --upgrade-chart-rev REV (the chart revision it was converted for)")
                sha = chart_at(when, is_date=True, dest=work)
            chart, what = work, f"chart commit {sha[:10]}"
        print(f"==> rendering the old tree with {what} ({' -> '.join(layer_order(chart))})", file=sys.stderr)
        return render_tree(tree, chart, dump)
    finally:
        shutil.rmtree(work, ignore_errors=True)
