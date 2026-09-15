#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Do everything in one command.

    python run_all.py

That is the whole interface. It runs the four steps below in order, stops at the
first real problem, and prints plain-language results.

    STEP 1  CONVERT      raw manifests  ->  Helm values + ArgoCD Applications
    STEP 2  CHECK NAMES  every metadata.name must survive byte-for-byte
    STEP 3  CHECK FIELDS did any field get lost or changed?
    STEP 4  SANITIZE     turn the report into short, anonymized files you can
                         photograph and send outside the org

NOTHING IS EVER DELETED. Every step only ever writes NEW files. The raw
manifests are never touched, and the fidelity report is read, not modified.

Where things end up
-------------------
    <output>/                       the converted chart values + Applications
    <output>/report/                the full, UNSANITIZED reports (internal only)
    <output>/report/shareable/      the short, anonymized ones (safe to send)

Common uses
-----------
    python run_all.py                          # convert + check + sanitize
    python run_all.py --cluster-scoped         # if step 1 reports a cluster conflict
    python run_all.py --skip-convert           # re-sanitize an existing report
    python run_all.py --input other-manifests/ --output other-out/
"""
from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
PY = sys.executable


def say(msg: str = "") -> None:
    print(msg, flush=True)


def banner(step: int, title: str, what_it_does: str) -> None:
    say()
    say("=" * 70)
    say(f"STEP {step}  {title}")
    say(f"         {what_it_does}")
    say("=" * 70)


def run(cmd: list[str], cwd: pathlib.Path = HERE) -> tuple[int, str]:
    """Run a command, stream nothing, return (exit code, combined output)."""
    p = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def tail(text: str, n: int = 12) -> str:
    lines = [l for l in text.splitlines() if l.strip()]
    return "\n".join("    " + l for l in lines[-n:])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", default="namespaces-showcase",
                    help="folder of raw Kubernetes manifests (default: namespaces-showcase)")
    ap.add_argument("--output", default="output-showcase",
                    help="where converted values go (default: output-showcase)")
    ap.add_argument("--chart", default="..", help="the Helm chart (this repo's root)")
    ap.add_argument("--cluster-scoped", action="store_true",
                    help="deploy cluster-scoped objects once globally instead of per namespace")
    ap.add_argument("--skip-convert", action="store_true",
                    help="do not re-convert; just re-check and re-sanitize what is there")
    ap.add_argument("--seed", type=int, default=None,
                    help="fix the anonymizer seed so two runs produce the same fake names")
    a = ap.parse_args(argv)

    src, out, chart = (HERE / a.input), (HERE / a.output), (HERE / a.chart)
    report = out / "report" / "fidelity_report.txt"
    shareable = out / "report" / "shareable"
    problems: list[str] = []

    say(f"input manifests : {src}")
    say(f"output folder   : {out}")
    say(f"mode            : {'cluster-scoped' if a.cluster_scoped else 'namespace-scoped (default)'}")

    # ---------------------------------------------------------------- STEP 1
    if a.skip_convert:
        banner(1, "CONVERT", "skipped (--skip-convert)")
    else:
        banner(1, "CONVERT", "raw manifests -> Helm values, and check for conflicts")
        cmd = [PY, str(HERE / "convert_to_universal_chart.py"),
               "--input", str(src), "--output", str(out),
               "--chart", str(chart), "--verify-fidelity", "--argocd-manifests"]
        if a.cluster_scoped:
            cmd.append("--cluster-scoped")
        code, output = run(cmd)
        if code != 0:
            say(tail(output))
            say()
            if "CLUSTER CONFLICT" in output:
                say("  PROBLEM: two releases in different namespaces both create the same")
                say("           CLUSTER-SCOPED object (a ClusterRole or similar). Cluster-scoped")
                say("           objects are global, so they would overwrite each other.")
                say()
                say("  FIX:     re-run with --cluster-scoped, which creates it once:")
                say(f"           python run_all.py --cluster-scoped")
            else:
                say("  PROBLEM: the conversion stopped. The output above says why.")
            return 1
        say("  OK - conversion finished, no ownership conflicts.")

    if not report.is_file():
        say()
        say(f"  PROBLEM: no fidelity report at {report}")
        say("           Run without --skip-convert to produce one.")
        return 1

    # ---------------------------------------------------------------- STEP 2
    banner(2, "CHECK NAMES", "does every object keep the exact name it had?")
    code, output = run([PY, str(HERE / "name_fidelity_check.py"),
                        "--input", str(src), "--output", str(out), "--chart", str(chart)])
    say(tail(output, 20))
    if code != 0:
        problems.append("some object names changed during conversion (STEP 2)")
        say()
        say("  PROBLEM: the names above did not survive. Anything that referenced them")
        say("           by name - DNS, another release's config, a probe - will break.")
    else:
        say("  OK - every name survived byte-for-byte.")

    # ---------------------------------------------------------------- STEP 3
    banner(3, "CHECK FIELDS", "did any field get lost or changed?")
    text = report.read_text(encoding="utf-8", errors="replace")
    counts = {}
    for line in text.splitlines():
        for key in ("fields LOST", "fields CHANGED",
                    "objects in raw but rendered by nothing",
                    "objects rendered with no raw source", "render failures"):
            if line.strip().startswith(key):
                try:
                    counts[key] = int(line.rsplit(":", 1)[1].strip())
                except ValueError:
                    pass
    for key, n in counts.items():
        say(f"    {key:<44} {n}")
    bad = {k: v for k, v in counts.items() if v}
    if bad:
        problems.append(f"fidelity issues: {bad} (STEP 3)")
        say()
        say(f"  PROBLEM: {sum(bad.values())} issue(s). Full detail (INTERNAL ONLY):")
        say(f"           {report}")
    else:
        say("  OK - nothing lost, nothing changed.")

    # ---------------------------------------------------------------- STEP 4
    banner(4, "SANITIZE", "make short, anonymized files that are safe to share")
    cmd = [PY, str(HERE / "sanitize_fidelity_report.py"),
           "-i", str(report), "-o", str(shareable)]
    if a.seed is not None:
        cmd += ["--seed", str(a.seed)]
    code, output = run(cmd)
    say(tail(output, 15))
    if code != 0:
        problems.append("sanitization failed (STEP 4)")
        say()
        say("  PROBLEM: refused to write. This is deliberate - it means a real name")
        say("           would have leaked into a shareable file. Nothing was written.")
        return 1
    say()
    say(f"  OK - safe-to-share files are in:")
    say(f"       {shareable}")

    # ---------------------------------------------------------------- SUMMARY
    say()
    say("=" * 70)
    say("SUMMARY")
    say("=" * 70)
    say(f"  Full reports (KEEP INTERNAL): {out / 'report'}")
    say(f"  Safe to send outside:         {shareable}")
    say()
    if problems:
        say("  Needs attention:")
        for p in problems:
            say(f"    - {p}")
        return 1
    say("  Everything passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
