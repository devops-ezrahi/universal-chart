"""A stand-in for `oc`/`kubectl get`, answering from YAML files, so the live
pull (-n, --link) is tested without a cluster.

    FAKE_OC_DIR=<dir with <namespace>/*.yaml>  python fake_oc.py get <type>[.<group>] [name] [-n ns] -o yaml

Each call is logged as its own file in the directory $FAKE_OC_LOG, when set
(the importer calls oc from several threads at once).
"""
import os
import sys
from pathlib import Path

import yaml

PLURAL_TO_KIND = None


def kinds():
    global PLURAL_TO_KIND
    if PLURAL_TO_KIND is None:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from converter.importer import RESOURCE_PLURAL_TO_KIND
        PLURAL_TO_KIND = dict(RESOURCE_PLURAL_TO_KIND)
    return PLURAL_TO_KIND


def main(argv):
    if os.environ.get("FAKE_OC_LOG"):
        import uuid
        log = Path(os.environ["FAKE_OC_LOG"])
        log.mkdir(parents=True, exist_ok=True)
        (log / f"{uuid.uuid4().hex}.txt").write_text(" ".join(argv), encoding="utf-8")
    args = list(argv)
    if args[:1] == ["--kubeconfig"]:
        args = args[2:]
    if not args or args[0] != "get":
        sys.stderr.write(f"fake oc: unsupported {argv}\n")
        return 2
    ns = None
    if "-n" in args:
        i = args.index("-n")
        ns = args[i + 1]
        del args[i:i + 2]
    rest = [a for a in args[1:] if not a.startswith("-") and a != "yaml"]
    resource = rest[0]
    name = rest[1] if len(rest) > 1 else None
    plural = resource.split(".")[0]
    kind = kinds().get(plural) or kinds().get(plural.rstrip("s") + "s")
    root = Path(os.environ["FAKE_OC_DIR"])
    docs = []
    for f in sorted((root / ns).glob("*.yaml") if ns else root.rglob("*.yaml")):
        docs += [d for d in yaml.safe_load_all(f.read_text(encoding="utf-8")) if isinstance(d, dict)]
    hits = [d for d in docs if kind and d.get("kind") == kind
            and (name is None or (d.get("metadata") or {}).get("name") == name)]
    if name is not None:
        if not hits:
            sys.stderr.write(f'Error from server (NotFound): {resource} "{name}" not found\n')
            return 1
        sys.stdout.write(yaml.safe_dump(hits[0]))
        return 0
    sys.stdout.write(yaml.safe_dump({"apiVersion": "v1", "kind": "List", "items": hits}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
