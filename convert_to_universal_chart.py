#!/usr/bin/env python3
"""Kubernetes / OpenShift namespaces in, universal-chart values tree out.

    python convert_to_universal_chart.py --input namespaces --output out

The conversion engine lives in converter/engine.py; run this file with --help
for every option. See docs/converter.md.
"""
import sys
from pathlib import Path

# The `converter` package sits next to this file.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from converter.engine import main  # noqa: E402

if __name__ == "__main__":
    main()
