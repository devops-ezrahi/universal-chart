#!/usr/bin/env python3
"""Kubernetes / OpenShift namespaces in, universal-chart values tree out.

    python gitops-factory/convert_to_universal_chart.py -n shop-dev -n shop-prd -o out      # live namespaces
    python gitops-factory/convert_to_universal_chart.py --link <console URL> -o out          # console links
    python gitops-factory/convert_to_universal_chart.py --from-yaml manifests/ -o out        # files, no cluster
    python gitops-factory/convert_to_universal_chart.py --input dump/ -o out                 # an already-pulled dump

Every source becomes the same dump (<namespace>/<microservice>.yaml), which is
then converted, rendered with helm and verified. Run with --help for every
option; see gitops-factory/README.md.
"""
import sys
from pathlib import Path

# The `converter` package sits at the repo root, one level up.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from converter import engine, sources  # noqa: E402


def main() -> int:
    ap = engine.build_parser()
    ap.description = __doc__
    sources.add_arguments(ap)
    args = ap.parse_args()
    return sources.run(args, ap)


if __name__ == "__main__":
    sys.exit(main())
