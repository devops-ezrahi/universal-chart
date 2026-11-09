"""Where the command reads from. Every source ends in the one layout the engine
converts — <dump>/<namespace>/<microservice>.yaml (+ shared.yaml) — so the
conversion itself never knows or cares where the manifests came from.

    -n NAMESPACE ...        live namespaces, through `oc` (or --oc kubectl)
    --link URL ...          OpenShift console links: a namespace, a topology app,
                            or one resource (pulled with the app it belongs to)
    --from-yaml PATH ...    manifest files/folders instead of a cluster; the
                            importer's reads are answered from them
    --input DIR             a dump already in that layout (the default source)
    --upgrade TREE          a values tree an earlier run wrote (see upgrade.py)

--pull-only stops after the pull and writes the dump to --output; a later run
converts it with --input. That is the devops-portal's review step.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import types
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO)) if str(REPO) not in sys.path else None
from converter import importer as ni  # noqa: E402
from converter.engine import CLUSTER_SCOPED_KINDS  # noqa: E402

# The title of the argument group holding the source options. The portal's
# options dialog leaves this group out: it chooses the source itself.
SOURCE_GROUP = "where to read from"

NS_RE = re.compile(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?")
OBJECT_RE = re.compile(r"[a-z0-9]([-a-z0-9.]*[a-z0-9])?")
TOPOLOGY_VIEWS = {"graph", "list"}


def add_arguments(ap: argparse.ArgumentParser) -> None:
    g = ap.add_argument_group(SOURCE_GROUP, "Pick one source; without any, --input is read.")
    g.add_argument("-n", "--namespace", action="append", default=[], metavar="NAMESPACE",
                   help="pull this namespace from the cluster; repeatable or comma-separated")
    g.add_argument("--link", action="append", default=[], metavar="URL",
                   help="OpenShift console link to pull: a namespace page, a topology app "
                        "(…/topology/ns/<ns>?selectId=group:<app>) or a resource page "
                        "(…/k8s/ns/<ns>/<kind>/<name>, pulled with its app); repeatable, mixes with -n")
    g.add_argument("--from-yaml", nargs="+", metavar="PATH",
                   help="read manifests from these files/folders instead of a cluster; "
                        "everything in them is converted")
    g.add_argument("--default-namespace", default="",
                   help="with --from-yaml: the namespace of documents that name none")
    g.add_argument("--kubeconfig", help="kubeconfig for every oc/kubectl call")
    g.add_argument("--oc", default="oc", help="cluster CLI to call (default: oc; kubectl works too)")
    g.add_argument("--pull-only", action="store_true",
                   help="stop after pulling and write the dump (<ns>/<microservice>.yaml) to --output; "
                        "convert it later with --input")
    g.add_argument("--warnings-file", metavar="FILE",
                   help="also write the pull's warnings to FILE as a JSON list")
    g.add_argument("--dump", metavar="DIR", help="keep the pulled dump in DIR (default: a temp dir, deleted)")
    g.add_argument("--report", metavar="DIR", help="write the report here instead of <output>/report")
    g.add_argument("--upgrade", metavar="TREE",
                   help="bring a tree an earlier run wrote (any converter version) up to date: its namespaces "
                        "are pulled again from the cluster and converted with today's converter; settings it "
                        "recorded (repos, groups, clusters, ...) fill every option not given. -o TREE upgrades in place")
    g.add_argument("--upgrade-offline", action="store_true",
                   help="with --upgrade: no cluster — render the old tree with the chart as it was when the tree "
                        "was last committed (its git date), and convert that")
    g.add_argument("--upgrade-chart-rev", metavar="REV",
                   help="with --upgrade-offline: render with this revision of this repo instead (a tag, a commit)")
    g.add_argument("--upgrade-chart", metavar="PATH",
                   help="with --upgrade-offline: render with the chart checked out at PATH")


def names(values: list[str]) -> list[str]:
    """`-n a -n b,c` -> [a, b, c], order kept, duplicates dropped."""
    out = [n.strip() for v in values for n in v.split(",") if n.strip()]
    return list(dict.fromkeys(out))


# ---------------------------------------------------------------------------
# Console links — the same reading as devops-portal's os4Links.ts
# ---------------------------------------------------------------------------

def _resource_of(segment: str) -> str | None:
    """A console kind segment as an `oc get` resource: `deployments` stays,
    `apps~v1~Deployment` becomes `deployment.apps` (core: `pod`)."""
    parts = segment.split("~")
    if len(parts) == 3:
        group, _, kind = parts
        name = kind.lower()
        return name if group in ("core", "") else f"{name}.{group.lower()}"
    return segment if re.fullmatch(r"[a-z0-9.]+", segment) else None


def parse_link(link: str) -> tuple[str, dict]:
    """(namespace, target): target is {"whole": True}, {"app": a} or
    {"resource": r, "name": n}. Raises ValueError for anything else."""
    link = link.strip()
    u = urlparse(link)
    if u.scheme not in ("http", "https") or not u.netloc:
        raise ValueError(f"'{link}' is not a web link")
    seg = [unquote(s) for s in u.path.split("/") if s]

    def at(i):
        return seg[i] if len(seg) > i else None

    def ns(n):
        if not n or not NS_RE.fullmatch(n):
            raise ValueError(f"'{link}': '{n or ''}' is not a namespace name")
        return n

    if seg[:2] == ["topology", "ns"]:
        select = (parse_qs(u.query).get("selectId") or [""])[0]
        app = select[len("group:"):] if select.startswith("group:") else \
            (at(3) if at(3) and at(3) not in TOPOLOGY_VIEWS else "")
        if not app:
            return ns(at(2)), {"whole": True}
        if not OBJECT_RE.fullmatch(app):
            raise ValueError(f"'{link}': '{app}' is not an app name")
        return ns(at(2)), {"app": app}
    if seg[:2] == ["k8s", "ns"]:
        if not at(4):
            return ns(at(2)), {"whole": True}
        resource = _resource_of(at(3))
        if not resource or not OBJECT_RE.fullmatch(at(4)):
            raise ValueError(f"'{link}' does not name a resource the importer can read")
        return ns(at(2)), {"resource": resource, "name": at(4)}
    if seg[:2] == ["k8s", "cluster"] and at(2) and re.fullmatch(
            r"projects|namespaces|project\.openshift\.io~v1~Project|core~v1~Namespace", at(2)):
        return ns(at(3)), {"whole": True}
    if at(0) in ("project-details", "dev-monitoring", "add") and at(1) == "ns":
        return ns(at(2)), {"whole": True}
    raise ValueError(f"'{link}' is not a namespace, topology or resource page of the OpenShift console")


def pull_plan(namespaces: list[str], links: list[str]) -> dict[str, dict]:
    """{namespace: {"whole": bool, "apps": [...], "resources": [(resource, name)]}}"""
    plan: dict[str, dict] = {}
    for n in namespaces:
        if not NS_RE.fullmatch(n):
            raise ValueError(f"'{n}' is not a Kubernetes namespace name")
        plan.setdefault(n, {"whole": False, "apps": [], "resources": []})["whole"] = True
    for link in links:
        n, target = parse_link(link)
        entry = plan.setdefault(n, {"whole": False, "apps": [], "resources": []})
        if target.get("whole"):
            entry["whole"] = True
        elif "app" in target:
            entry["apps"].append(target["app"])
        else:
            entry["resources"].append((target["resource"], target["name"]))
    return plan


# ---------------------------------------------------------------------------
# The importer's `oc`, live or answered from YAML
# ---------------------------------------------------------------------------

def live_oc(binary: str, kubeconfig: str | None):
    """The importer's run_oc, with the binary made choosable (kubectl works for
    plain Kubernetes; routes etc. are then simply reported as not found)."""
    def run_oc(args):
        cmd = [binary] + (["--kubeconfig", kubeconfig] if kubeconfig else []) + list(args)
        return subprocess.run(cmd, capture_output=True, text=True, check=True, encoding="utf-8", errors="replace")
    return run_oc


def offline_oc(docs: list[dict]):
    """Answers `oc get <type> [name] ...` from `docs`."""
    def run_oc(args):
        kind = ni.RESOURCE_PLURAL_TO_KIND.get(args[1].split(".")[0])
        name = args[2] if len(args) > 2 and not args[2].startswith("-") else None
        hits = [d for d in docs if d.get("kind") == kind
                and (name is None or (d.get("metadata") or {}).get("name") == name)]
        if name is None:
            return types.SimpleNamespace(stdout=yaml.safe_dump({"kind": "List", "items": hits}))
        if not hits:
            raise subprocess.CalledProcessError(1, args, stderr="referenced, but not in the given YAML")
        return types.SimpleNamespace(stdout=yaml.safe_dump(hits[0]))
    return run_oc


def load_manifests(paths: list[str], default_ns: str) -> dict[str, list[dict]]:
    """{namespace: docs}, each document in its own metadata.namespace. One that
    names none goes to --default-namespace; without one, a cluster-scoped
    object goes to the first namespace (it is deduplicated globally anyway)
    and anything else is an error."""
    files = []
    for p in map(Path, paths):
        if p.is_dir():
            files += sorted(p.rglob("*.y*ml"))
        elif p.is_file():
            files.append(p)
        else:
            sys.exit(f"--from-yaml: {p} not found")
    by_ns: dict[str, list[dict]] = {}
    homeless = []
    for f in files:
        for d in ni.parse_yaml_stream(f.read_text(encoding="utf-8")):
            ns = (d.get("metadata") or {}).get("namespace") or default_ns
            if ns:
                by_ns.setdefault(ns, []).append(d)
            elif d.get("kind") in CLUSTER_SCOPED_KINDS:
                homeless.append(d)
            else:
                sys.exit(f"{f}: {d.get('kind')}/{(d.get('metadata') or {}).get('name')} names no "
                         f"metadata.namespace — pass --default-namespace")
    if homeless:
        if not by_ns:
            sys.exit("--from-yaml: only cluster-scoped objects, no namespace to convert them in — "
                     "pass --default-namespace")
        by_ns[next(iter(by_ns))].extend(homeless)
    for ns in by_ns:
        if not NS_RE.fullmatch(ns):
            sys.exit(f"metadata.namespace '{ns}' is not a Kubernetes namespace name")
    return by_ns


def _written(ns_dir: Path) -> set:
    seen = set()
    for f in (ns_dir.rglob("*.y*ml") if ns_dir.is_dir() else []):
        try:
            for d in yaml.safe_load_all(f.read_text(encoding="utf-8")):
                if isinstance(d, dict):
                    seen.add((d.get("kind"), (d.get("metadata") or {}).get("name")))
        except yaml.YAMLError:
            pass
    return seen


# ---------------------------------------------------------------------------
# Pulls
# ---------------------------------------------------------------------------

def pull_yaml(by_ns: dict[str, list[dict]], dump: Path) -> list[str]:
    """Each namespace's documents through the importer's full-namespace pull,
    then every object it did not take filed into the dump too: given YAML is
    all app (ni.file_unclaimed). Anything still left is named, not lost."""
    notes: list[str] = []
    for ns, ns_docs in by_ns.items():
        print(f"==> reading namespace {ns}", file=sys.stderr, flush=True)
        ni.run_oc = offline_oc(ns_docs)
        try:
            ni.handle_full_namespace(ns, types.SimpleNamespace(output=str(dump)))
        except SystemExit:
            pass  # no workloads: file_unclaimed below still files the rest
        notes.extend(f"{n} (namespace {ns})" for n in ni.file_unclaimed(dump / ns, ns_docs))
        kept = _written(dump / ns)
        for d in ns_docs:
            key = (d.get("kind"), (d.get("metadata") or {}).get("name"))
            if key[0] and key not in kept and key[0] not in ni.UNCLAIMED_SKIP_KINDS \
                    and not any(n.startswith(f"{key[0]}/{key[1]}:") for n in notes):
                notes.append(f"{key[0]}/{key[1]} (namespace {ns}): in the given YAML, "
                             f"but not carried into the conversion")
    return notes


def pull_live(plan: dict[str, dict], dump: Path, oc: str, kubeconfig: str | None) -> list[str]:
    """Per namespace exactly what the importer's own modes do: the whole
    namespace (`-n`), or each app's resources (topology mode). A resource link
    joins as the app it belongs to, so what it references comes with it."""
    if shutil.which(oc) is None:
        sys.exit(f"'{oc}' is not on PATH — install it, pass --oc kubectl, or use --from-yaml")
    ni.run_oc = live_oc(oc, kubeconfig)
    notes: list[str] = []
    ni.prefetch_namespaces([ns for ns, want in plan.items() if want["whole"]])
    for ns, want in plan.items():
        print(f"==> pulling namespace {ns}", file=sys.stderr, flush=True)
        try:
            if want["whole"]:
                ni.handle_full_namespace(ns, types.SimpleNamespace(output=str(dump)))
                continue
            apps = list(want["apps"])
            for resource, name in want["resources"]:
                raw = ni.fetch_resource(ns, resource, name)
                doc = yaml.safe_load(raw) if raw else None
                app = ni.get_app_id(doc) if isinstance(doc, dict) else None
                if app:
                    apps.append(app)
                else:
                    notes.append(f"{resource}/{name} (namespace {ns}): could not be read — check the link, "
                                 f"and that the login may read it")
            for app in dict.fromkeys(apps):
                found = ni.fetch_all_app_resources(ns, app)
                if not found:
                    notes.append(f"App '{app}' (namespace {ns}): nothing found under that name")
                    continue
                ni.place_secret_store_dependencies(ns, found)
                ni.write_resources_to_namespace(found, dump / ns)
        except SystemExit as e:
            notes.append(f"Namespace '{ns}': nothing pulled — "
                         f"{e.code if isinstance(e.code, str) else 'no workloads found, or the cluster refused the read'}")
    return notes


def pull(args: argparse.Namespace, dump: Path) -> list[str]:
    """Run the chosen pull into `dump`; returns its warnings."""
    ni.FETCH_FAILURES.clear()
    wanted = names(args.namespace)
    if args.from_yaml:
        if args.link:
            sys.exit("--link pulls from a cluster; it cannot be combined with --from-yaml")
        by_ns = load_manifests(args.from_yaml, args.default_namespace)
        missing = [ns for ns in wanted if ns not in by_ns]
        if missing:
            sys.exit(f"no documents for namespace(s) {', '.join(missing)} in the given YAML")
        if wanted:
            by_ns = {ns: by_ns[ns] for ns in wanted}
        notes = pull_yaml(by_ns, dump)
    else:
        try:
            plan = pull_plan(wanted, args.link)
        except ValueError as e:
            sys.exit(str(e))
        notes = pull_live(plan, dump, args.oc, args.kubeconfig)
        for ns in plan:
            if not (dump / ns).is_dir() or not any((dump / ns).glob("*.yaml")):
                notes.append(f"Namespace '{ns}': nothing was pulled — it will not be in the tree")
    return list(ni.FETCH_FAILURES) + notes


def has_source(args: argparse.Namespace) -> bool:
    return bool(args.namespace or args.link or args.from_yaml or args.upgrade)


def run(args: argparse.Namespace, ap: argparse.ArgumentParser) -> int:
    """The whole command: pull (if a source was given), convert, report."""
    from converter import engine
    if not args.upgrade and (args.upgrade_offline or args.upgrade_chart_rev or args.upgrade_chart):
        ap.error("--upgrade-offline / --upgrade-chart-rev / --upgrade-chart need --upgrade TREE")
    if not has_source(args):
        for flag in ("pull_only", "dump", "warnings_file"):
            if getattr(args, flag):
                ap.error(f"--{flag.replace('_', '-')} needs a source: -n, --link or --from-yaml")
        engine.run(args)
        return 0
    if args.input != ap.get_default("input"):
        ap.error("--input is a source of its own; drop it, or drop -n/--link/--from-yaml/--upgrade")
    if args.upgrade and (args.namespace or args.link or args.from_yaml):
        ap.error("--upgrade reads its namespaces from the old tree; drop -n/--link/--from-yaml")
    if (args.upgrade_chart_rev or args.upgrade_chart) and not args.upgrade_offline:
        ap.error("--upgrade-chart-rev / --upgrade-chart choose the chart for --upgrade-offline")

    out = Path(args.output).resolve()
    tree = None
    if args.upgrade:
        from converter import upgrade
        tree = upgrade.prepare(args, ap, out)
    if args.pull_only:
        dump = out
        dump.mkdir(parents=True, exist_ok=True)
    elif args.dump:
        dump = Path(args.dump).resolve()
        if dump.exists() and any(dump.iterdir()):
            sys.exit(f"--dump {dump} is not empty — the converter would read whatever is already there")
        dump.mkdir(parents=True, exist_ok=True)
    else:
        dump = Path(tempfile.mkdtemp(prefix="ucc-dump-"))

    code = 0
    try:
        warnings = upgrade.pull(tree, args, dump) if tree else pull(args, dump)
        if args.warnings_file:
            Path(args.warnings_file).write_text(json.dumps(warnings, ensure_ascii=False), encoding="utf-8")
        if args.pull_only:
            _print_warnings(warnings)
            print(f"==> dump: {dump} — convert it with --input {dump}", file=sys.stderr)
            return 0
        print("==> converting", file=sys.stderr, flush=True)
        args.input = str(dump)
        try:
            engine.run(args)
        except SystemExit as e:
            if isinstance(e.code, str):
                print(e.code, file=sys.stderr)
                code = 1
            else:
                code = e.code or 0
        report = out / "report"
        if tree is not None and code == 0:
            moved = upgrade.place_like(tree, out)
            lines = upgrade.compare_releases(tree, out)
            report.mkdir(parents=True, exist_ok=True)
            text = f"Upgraded from {tree.root}\n\n"
            text += ("Release sets differ:\n" + "\n".join(lines) + "\n\n") if lines else "Same releases in every folder.\n\n"
            if moved:
                text += "Put back in their old sub-folders:\n" + "\n".join(moved) + "\n"
            (report / "upgrade.txt").write_text(text, encoding="utf-8")
            for line in lines:
                print(f"    upgrade: {line}", file=sys.stderr)
        if warnings:
            report.mkdir(parents=True, exist_ok=True)
            (report / "pull_warnings.txt").write_text(
                "What the pull could not bring into the conversion:\n\n" + "\n".join(warnings) + "\n",
                encoding="utf-8")
        if args.report and report.is_dir():
            dest = Path(args.report).resolve()
            shutil.rmtree(dest, ignore_errors=True)
            shutil.move(str(report), str(dest))
            print(f"==> report: {dest}", file=sys.stderr)
        _print_warnings(warnings)
        print(f"==> values tree: {out}" if code == 0 else f"==> conversion exited {code}", file=sys.stderr)
        return code
    finally:
        if not (args.pull_only or args.dump):
            shutil.rmtree(dump, ignore_errors=True)


def _print_warnings(warnings: list[str]) -> None:
    if warnings:
        print(f"==> {len(warnings)} pull warning(s):", file=sys.stderr)
        for w in warnings:
            print(f"    {w}", file=sys.stderr)
