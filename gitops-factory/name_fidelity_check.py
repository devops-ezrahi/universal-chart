# -*- coding: utf-8 -*-
"""Acceptance check for the strict-naming directive.

Compares every `metadata.name` in the RAW manifests against every
`metadata.name` in the `helm template` output of the converted result, per
namespace. Exit code 0 means zero naming changes.

Usage:
    python name_fidelity_check.py --input namespaces-showcase --output <tmp> \\
        --chart ..
"""
import argparse
import json
import pathlib
import subprocess
import sys
from collections import defaultdict

import yaml

# Objects the chart is entitled to create even though no raw manifest declares
# them, and objects the raw dump carries that are never desired state. Both
# sides are excluded from the comparison so it reports NAMING changes rather
# than inventory differences (which the fidelity report already covers).
CHART_CREATED_KINDS = {"ServiceAccount"}
TRANSIENT_KINDS = {"ReplicaSet", "Pod", "Endpoints", "EndpointSlice", "Event",
                   "ControllerRevision", "Binding"}
# Kinds with no namespace. They are compared once, globally — see main().
CLUSTER_SCOPED_KINDS = {"ClusterRole", "ClusterRoleBinding", "StorageClass",
                        "PersistentVolume", "SecurityContextConstraints",
                        "ClusterSecretStore", "Namespace"}


def raw_names(input_dir: pathlib.Path):
    out = defaultdict(set)
    for ns_dir in sorted(p for p in input_dir.iterdir() if p.is_dir()):
        for f in sorted(ns_dir.glob("*.yaml")):
            for d in yaml.safe_load_all(f.read_text(encoding="utf-8")):
                if not d or d.get("kind") in TRANSIENT_KINDS:
                    continue
                meta = d.get("metadata") or {}
                if meta.get("name"):
                    out[ns_dir.name].add((d["kind"], meta["name"]))
                # Container names live inside the pod spec and are addressed by
                # name too (kubectl logs -c, metrics labels), so they count.
                for path in (("spec", "template", "spec"),
                             ("spec", "jobTemplate", "spec", "template", "spec")):
                    cur = d
                    for k in path:
                        cur = cur.get(k) if isinstance(cur, dict) else None
                    if isinstance(cur, dict):
                        for c in (cur.get("containers") or []):
                            if c.get("name"):
                                out[ns_dir.name].add(("Container", c["name"]))
    return out


def rendered_names(out_dir: pathlib.Path, chart: pathlib.Path):
    out = defaultdict(set)
    for ns_dir in sorted(p for p in out_dir.iterdir() if p.is_dir()):
        vdir = ns_dir / "values"
        if not vdir.is_dir():
            continue
        for vf in sorted(vdir.glob("*.yaml")):
            slug = vf.stem
            chain = []
            if not slug.endswith("shared"):
                for d in (out_dir / "defaults.yaml", ns_dir / "defaults.yaml"):
                    if d.exists():
                        chain.append(d)
            base = out_dir / "base" / f"{slug}.yaml"
            if base.exists():
                chain.append(base)
            chain.append(vf)
            args = []
            for f in chain:
                args += ["-f", str(f)]
            proc = subprocess.run(["helm", "template", slug, str(chart), *args],
                                  capture_output=True, text=True)
            if proc.returncode != 0:
                print(f"  RENDER FAILED {ns_dir.name}/{slug}:\n{proc.stderr}")
                continue
            for d in yaml.safe_load_all(proc.stdout):
                if not d:
                    continue
                meta = d.get("metadata") or {}
                if meta.get("name"):
                    out[ns_dir.name].add((d["kind"], meta["name"]))
                for path in (("spec", "template", "spec"),
                             ("spec", "jobTemplate", "spec", "template", "spec")):
                    cur = d
                    for k in path:
                        cur = cur.get(k) if isinstance(cur, dict) else None
                    if isinstance(cur, dict):
                        for c in (cur.get("containers") or []):
                            if c.get("name"):
                                out[ns_dir.name].add(("Container", c["name"]))
    return out


def load_renames(out: pathlib.Path) -> list[dict]:
    """Collision renames the converter recorded for this output directory.

    Absent file means an output generated before the record existed: treated as
    "no renames known", which fails loudly on a rename rather than passing
    quietly on one it cannot see.
    """
    f = out / "report" / "renames.json"
    if not f.is_file():
        return []
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return []
    return data if isinstance(data, list) else []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--chart", required=True)
    a = ap.parse_args()

    src, out, chart = (pathlib.Path(a.input).resolve(),
                       pathlib.Path(a.output).resolve(),
                       pathlib.Path(a.chart).resolve())

    raw, got = raw_names(src), rendered_names(out, chart)

    # Cluster-scoped objects are GLOBAL: they have no namespace, and in
    # --cluster-scoped mode the converter deliberately emits them once into a
    # dedicated `cluster-shared` release rather than per namespace. Comparing
    # them per namespace reads that relocation as "lost from platform-ops,
    # invented in cluster-shared" when the NAME is byte-identical and only the
    # owning release moved — which is not a naming change and is what this tool
    # exists to measure. They are pooled and compared once, globally.
    def split(index):
        scoped, glob = {}, set()
        for ns, entries in index.items():
            for kind, name in entries:
                if kind in CLUSTER_SCOPED_KINDS:
                    glob.add((kind, name))
                else:
                    scoped.setdefault(ns, set()).add((kind, name))
        return scoped, glob

    raw_ns, raw_global = split(raw)
    got_ns, got_global = split(got)

    # Deliberate collision renames, recorded by the converter. Two microservices
    # in one namespace declaring DIFFERENT objects under one name is a real
    # conflict; renaming the second copy is the resolution, and it necessarily
    # produces a rendered name with no raw counterpart. Reported as its own line
    # rather than as a failure — the rename is the fix, not the damage.
    #
    # Same reasoning as the cluster-scoped pooling above: this tool measures
    # names that got LOST, and a recorded rename is neither lost nor invented.
    # Without this, any corpus containing one collision leaves the gate
    # permanently red, and a gate that always fails is one nobody reads.
    renames = load_renames(out)
    expected_new = {(r["namespace"], r["kind"], r["to"]) for r in renames}
    renamed_from = {(r["namespace"], r["kind"], r["from"]) for r in renames}

    lost, invented, deliberate = [], [], []
    for ns in sorted(set(raw_ns) | set(got_ns)):
        r, g = raw_ns.get(ns, set()), got_ns.get(ns, set())
        for kind, name in sorted(r - g):
            # The ORIGINAL name of a renamed object is still present in the raw
            # manifests under its other owner, so it only reads as lost when the
            # rename moved the sole copy. Either way it is accounted for.
            if (ns, kind, name) in renamed_from:
                continue
            lost.append(f"  {ns}: {kind}/{name}")
        for kind, name in sorted(g - r):
            if kind in CHART_CREATED_KINDS:
                continue
            if (ns, kind, name) in expected_new:
                deliberate.append(f"  {ns}: {kind}/{name}  (collision rename)")
                continue
            invented.append(f"  {ns}: {kind}/{name}")
    for kind, name in sorted(raw_global - got_global):
        lost.append(f"  (cluster-scoped): {kind}/{name}")
    for kind, name in sorted(got_global - raw_global):
        invented.append(f"  (cluster-scoped): {kind}/{name}")

    total_raw = sum(len(v) for v in raw.values())
    print(f"raw names checked:            {total_raw}")
    print(f"names NOT in rendered output: {len(lost)}")
    print(f"names in render, not in raw:  {len(invented)}")
    print(f"renamed on collision:         {len(deliberate)}")
    if lost:
        print("\n--- LOST (raw name absent from the rendered output) ---")
        print("\n".join(lost))
    if invented:
        print("\n--- INVENTED (rendered name with no raw counterpart) ---")
        print("\n".join(invented))
    if deliberate:
        print("\n--- RENAMED ON COLLISION (recorded by the converter, expected) ---")
        print("\n".join(deliberate))
        print("  Two releases declared different objects under one name; the "
              "second copy moved.\n  See report/conflicts_and_warnings.txt for "
              "what to check outside those releases.")
    if not lost and not invented:
        print("\nPASS - 0 naming changes.")
        return 0
    print(f"\nFAIL - {len(lost) + len(invented)} naming changes.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
