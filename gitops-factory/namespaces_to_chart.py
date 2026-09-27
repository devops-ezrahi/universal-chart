#!/usr/bin/env python3
"""
Namespaces in, universal-chart values tree out — namespace_importer.py and
convert_to_universal_chart.py as one command.

    python namespaces_to_chart.py -n shop-dev -n shop-prd -o out \\
        --env-suffixes dev,prd --env-group color=black,yellow --argocd-manifests true

1. Every namespace is pulled with the importer's own full-namespace mode
   (`namespace_importer.py -n`): workloads grouped into microservices, what
   they reference, `shared.yaml`, runtime metadata stripped.
2. The converter runs over that dump. Every option this script does not know
   is handed to it unchanged, so `--env-suffixes`, `--env-group`,
   `--argocd-manifests`, `--values-repo-url`, `--skip-verify`… all work as
   documented in `convert_to_universal_chart.py --help`.
3. `-o` ends up holding the values tree and nothing else, and nothing else is
   left anywhere: the importer's dump and the converter's report are deleted.
   Keep them with `--dump DIR` / `--report DIR`.

No cluster at hand? `--from-yaml` takes files or folders of manifests
(`kubectl get -o yaml` output, a List, anything) and answers the importer's
`oc` calls from them instead — the same adapter the devops-portal's Convert
uses. Documents are grouped by their own `metadata.namespace`; with `-n`,
only those namespaces are kept.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import types
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import namespace_importer as ni  # noqa: E402
from convert_to_universal_chart import CLUSTER_SCOPED_KINDS  # noqa: E402

NS_RE = re.compile(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?")


def names(values: list[str]) -> list[str]:
    """`-n a -n b,c` -> [a, b, c], order kept, duplicates dropped."""
    out = [n.strip() for v in values for n in v.split(",") if n.strip()]
    return list(dict.fromkeys(out))


def live_oc(binary: str, kubeconfig: str | None):
    """The importer's run_oc, with the binary made choosable (kubectl works for
    plain Kubernetes; routes etc. are then simply reported as not found)."""
    def run_oc(args):
        cmd = [binary] + (["--kubeconfig", kubeconfig] if kubeconfig else []) + list(args)
        return subprocess.run(cmd, capture_output=True, text=True, check=True, encoding="utf-8", errors="replace")
    return run_oc


def load_manifests(paths: list[str], default_ns: str) -> dict[str, list[dict]]:
    """{namespace: docs}. Cluster-scoped docs are in every namespace's list, so
    a reference to one (a PV, a ClusterSecretStore) resolves from any of them."""
    by_ns: dict[str, list[dict]] = {}
    cluster: list[dict] = []
    files = [f for p in map(Path, paths)
             for f in (sorted(p.rglob("*.y*ml")) if p.is_dir() else [p])]
    for f in files:
        for d in ni.parse_yaml_stream(f.read_text(encoding="utf-8")):
            if d.get("kind") in CLUSTER_SCOPED_KINDS:
                cluster.append(d)
                continue
            ns = (d.get("metadata") or {}).get("namespace") or default_ns
            if not ns:
                sys.exit(f"{f}: {d.get('kind')}/{(d.get('metadata') or {}).get('name')} names no "
                         f"metadata.namespace — pass --default-namespace")
            by_ns.setdefault(ns, []).append(d)
    return {ns: docs + cluster for ns, docs in by_ns.items()}


def offline_oc(docs: list[dict]):
    """Answers `oc get <type> [name] ...` from `docs` (the portal's SPLIT_DUMP)."""
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


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Anything else is passed to convert_to_universal_chart.py.")
    ap.add_argument("-n", "--namespace", action="append", default=[],
                    help="namespace to pull; repeatable or comma-separated")
    ap.add_argument("-o", "--output", required=True, help="where the values tree is written")
    ap.add_argument("--kubeconfig", help="kubeconfig for every oc/kubectl call")
    ap.add_argument("--oc", default="oc", help="cluster CLI to call (default: oc; kubectl works too)")
    ap.add_argument("--from-yaml", nargs="+", metavar="PATH",
                    help="read manifests from these files/folders instead of a cluster")
    ap.add_argument("--default-namespace", default="",
                    help="with --from-yaml: namespace for documents that name none")
    ap.add_argument("--dump", help="keep the importer's per-namespace dump here instead of a temp dir")
    ap.add_argument("--report", help="keep the converter's report here (default: deleted)")
    args, converter_args = ap.parse_known_args()
    for flag in ("--input", "--output"):
        if flag in converter_args:
            ap.error(f"{flag} is set by this script")

    wanted = names(args.namespace)
    for ns in wanted:
        if not NS_RE.fullmatch(ns):
            ap.error(f"'{ns}' is not a Kubernetes namespace name")
    if not wanted and not args.from_yaml:
        ap.error("give at least one -n NAMESPACE (or --from-yaml)")

    out = Path(args.output).resolve()
    report = Path(args.report).resolve() if args.report else None
    dump = Path(args.dump).resolve() if args.dump else Path(tempfile.mkdtemp(prefix="ns2chart-"))
    if args.dump and dump.exists() and any(dump.iterdir()):
        sys.exit(f"--dump {dump} is not empty — the converter would read whatever is already there")

    try:
        # 1. Pull.
        if args.from_yaml:
            by_ns = load_manifests(args.from_yaml, args.default_namespace)
            missing = [ns for ns in wanted if ns not in by_ns]
            if missing:
                sys.exit(f"no documents for namespace(s) {', '.join(missing)} in the given YAML")
            if wanted:
                by_ns = {ns: by_ns[ns] for ns in wanted}
            for ns in by_ns:
                if not NS_RE.fullmatch(ns):
                    sys.exit(f"metadata.namespace '{ns}' is not a Kubernetes namespace name")
        else:
            if shutil.which(args.oc) is None:
                sys.exit(f"'{args.oc}' is not on PATH — install it, pass --oc kubectl, or use --from-yaml")
            ni.run_oc = live_oc(args.oc, args.kubeconfig)
            by_ns = {ns: None for ns in wanted}
        for ns, docs in by_ns.items():
            print(f"==> pulling namespace {ns}", flush=True)
            if docs is not None:
                ni.run_oc = offline_oc(docs)
            try:
                ni.handle_full_namespace(ns, types.SimpleNamespace(output=str(dump)))
            except SystemExit:  # the importer exits on a namespace with no workloads
                pass
            if not (dump / ns).is_dir() or not any((dump / ns).glob("*.yaml")):
                print(f"    warning: nothing was pulled from '{ns}' — it will not be in the tree", file=sys.stderr)

        # 2. Convert.
        print("==> converting", flush=True)
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        rc = subprocess.call([sys.executable, str(HERE / "convert_to_universal_chart.py"),
                              "--input", str(dump), "--output", str(out), *converter_args], env=env)

        # 3. Leave only the tree in the output.
        if (out / "report").is_dir():
            if report:
                shutil.rmtree(report, ignore_errors=True)
                shutil.move(str(out / "report"), str(report))
                print(f"==> report: {report}")
            else:
                shutil.rmtree(out / "report")
                print("==> report not kept (the path above is gone) — pass --report DIR to keep it")
        if ni.FETCH_FAILURES:
            print(f"==> {len(ni.FETCH_FAILURES)} referenced object(s) could not be fetched:", file=sys.stderr)
            for f in ni.FETCH_FAILURES:
                print(f"    {f}", file=sys.stderr)
        print(f"==> values tree: {out}" if rc == 0 else f"==> converter exited {rc}")
        return rc
    finally:
        if not args.dump:
            shutil.rmtree(dump, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
