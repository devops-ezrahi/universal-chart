"""
Regression tests for BOTH:
  ERRORS_ANALYSIS/Missing_Objects_Not_Rendered_By_Any_Release   (folder 8)
  ERRORS_ANALYSIS/Extra_Objects_Rendered_With_No_Raw_Source     (folder 9)

They are deliberately covered by ONE test module because they are one defect.
error.txt for folder 9 says so outright ("these collide with #5" ... "Fixing the
naming defect should eliminate BOTH counts simultaneously"), and the execution
brief asks specifically: *verify 8 and 9 cancel out; if only one count drops,
they were not the same objects.*

Root cause is the fullname-based renaming fixed in folder 7
(Route_And_Service_Names_Replaced_By_Fullname). Every renamed object is counted
twice by the fidelity checker: once as a raw object rendered by nothing, once as
a rendered object with no raw source.

These tests assert the END STATE (both counts zero, and the counts move
together), so they stand as regression cover for folder 7's fix from the other
direction: reintroduce the rename and both counts come back.
"""
import pathlib
import re
import shutil
import subprocess
import sys

import pytest

TEST_DIR = pathlib.Path(__file__).resolve().parent
GITOPS_DIR = TEST_DIR.parents[3]
CHART_DIR = GITOPS_DIR.parent
CONVERTER_PY = GITOPS_DIR / "convert_to_universal_chart.py"

# The folder-7 fixture is the canonical reproduction: Services and Routes whose
# raw names carry '-service' / '-route' suffixes. Reused rather than duplicated
# so the two folders cannot drift apart.
FIXTURE_DIR = (TEST_DIR.parents[1] / "Route_And_Service_Names_Replaced_By_Fullname"
               / "test" / "fixture")

SHOWCASE_INPUT = GITOPS_DIR / "namespaces-showcase"

COUNT_KEYS = {
    "missing": "objects in raw but rendered by nothing",
    "extra": "objects rendered with no raw source",
    "lost": "fields LOST (raw -> render)",
    "changed": "fields CHANGED",
}


def _parse_counts(report_text: str) -> dict:
    out = {}
    for name, key in COUNT_KEYS.items():
        m = re.search(rf"^{re.escape(key)}:\s*(\d+)\s*$", report_text, re.MULTILINE)
        if m:
            out[name] = int(m.group(1))
    return out


def _convert_with_fidelity(input_dir, out_dir, chart=None, extra=()):
    proc = subprocess.run(
        [sys.executable, str(CONVERTER_PY),
         "--input", str(input_dir), "--output", str(out_dir),
         "--chart", str(chart or CHART_DIR), "--verify-fidelity", *extra],
        capture_output=True, text=True, cwd=str(GITOPS_DIR),
    )
    assert proc.returncode == 0, f"converter failed:\n{proc.stdout}\n{proc.stderr}"
    report = (out_dir / "report" / "fidelity_report.txt").read_text(encoding="utf-8")
    return _parse_counts(report), report


# --------------------------------------------------------------------------
# The suffixed-name fixture: neither count may be non-zero
# --------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_suffixed_names_produce_no_missing_and_no_extra_objects(tmp_path):
    counts, report = _convert_with_fidelity(FIXTURE_DIR, tmp_path / "out")

    assert counts["missing"] == 0, (
        "raw objects rendered by NO release — the '-service'/'-route' names were "
        f"not preserved.\n\n{report}"
    )
    assert counts["extra"] == 0, (
        "objects rendered with NO raw source — the chart invented release-named "
        f"objects.\n\n{report}"
    )


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_the_two_counts_are_the_same_objects(tmp_path):
    """The brief's explicit question. If these two ever diverge, folders 8 and 9
    are NOT two views of one rename and must be re-analysed separately."""
    counts, report = _convert_with_fidelity(FIXTURE_DIR, tmp_path / "out")
    assert counts["missing"] == counts["extra"], (
        f"missing={counts['missing']} extra={counts['extra']} — the counts no longer "
        f"move together, so they are not the same objects counted twice.\n\n{report}"
    )


# --------------------------------------------------------------------------
# The real showcase: both counts stay at zero
# --------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
@pytest.mark.skipif(not (GITOPS_DIR / "namespaces-showcase").is_dir(),
                    reason="showcase input not present")
def test_showcase_has_no_orphaned_or_invented_objects(tmp_path):
    """Run in --cluster-scoped mode.

    Under strict naming (the zero-mutation directive) a cluster-scoped object
    keeps its own name, so `platform-ops` and `platform-ops-prd` both render
    ClusterRole/log-agent-reader — one global object claimed by two releases.
    The converter is right to stop on that, and `--cluster-scoped` is the mode
    that models it correctly: the object is emitted ONCE into a dedicated
    cluster-shared release, which is what the source cluster actually had.

    The namespace-scoped conflict is asserted directly by the test below, so
    switching modes here does not sweep it under the carpet."""
    counts, report = _convert_with_fidelity(
        SHOWCASE_INPUT, tmp_path / "out", extra=("--cluster-scoped",))
    problems = {k: v for k, v in counts.items() if v != 0}
    assert not problems, f"showcase fidelity regressions: {problems}\n\n{report}"


@pytest.mark.skipif(not (GITOPS_DIR / "namespaces-showcase").is_dir(),
                    reason="showcase input not present")
def test_namespace_scoped_mode_reports_the_cluster_scoped_conflict(tmp_path):
    """The consequence of strict naming, asserted rather than hidden.

    Two namespaces declaring the same ClusterRole name is a REAL conflict that
    the old namespace-prefixing hid by renaming. The directive forbids the
    rename, so the conflict must surface — loudly, and with the fix named."""
    proc = subprocess.run(
        [sys.executable, str(CONVERTER_PY),
         "--input", str(SHOWCASE_INPUT), "--output", str(tmp_path / "out"),
         "--chart", str(CHART_DIR), "--verify-fidelity"],
        capture_output=True, text=True, cwd=str(GITOPS_DIR),
    )
    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0, (
        "two releases claiming one cluster-scoped object was accepted silently:\n"
        + combined)
    assert "CLUSTER CONFLICT" in combined and "log-agent-reader" in combined, combined
    assert "--cluster-scoped" in combined, (
        "the conflict message must name the way out")


# --------------------------------------------------------------------------
# Over-length release names: error.txt's SECOND sub-cause for folder 8
# --------------------------------------------------------------------------

def test_over_length_names_are_reported_not_silently_skipped(tmp_path):
    """error.txt blames 1 Deployment + 1 PVC on a release whose name exceeded the
    limit and was 'SKIPPED entirely'. This converter does not silently skip: it
    validates names at conversion time and refuses to write anything. Assert the
    loud behaviour so a regression to silent-skip is caught."""
    long_name = "prometheus-thanos-" + ("x" * 60) + "-store"
    assert len(long_name) > 63

    ns = tmp_path / "input" / "obs"
    ns.mkdir(parents=True)
    (ns / f"{long_name}.yaml").write_text(
        "apiVersion: v1\n"
        "kind: ConfigMap\n"
        "metadata:\n"
        f"  name: {long_name}\n"
        "data:\n"
        "  a: b\n",
        encoding="utf-8",
    )

    out = tmp_path / "out"
    proc = subprocess.run(
        [sys.executable, str(CONVERTER_PY),
         "--input", str(tmp_path / "input"), "--output", str(out),
         "--chart", str(CHART_DIR), "--skip-verify"],
        capture_output=True, text=True, cwd=str(GITOPS_DIR),
    )
    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0, (
        "converter accepted a name Kubernetes will reject; error.txt's 'release "
        f"skipped entirely' failure mode is back.\n{combined}"
    )
    assert "nothing was written" in combined or "resource name" in combined, combined
