"""
Regression tests for ERRORS_ANALYSIS/ImagePullPolicy_Always_Downgraded_To_IfNotPresent.

Defect: `imagePullPolicy` is captured in exactly one place —
handle_main_container, line 693 — so the Deployment path works. Neither
handle_cronjob nor handle_job captures it, and jobPodSpec renders the
RELEASE-level `.Values.image.pullPolicy` for the Job/CronJob container. A CronJob
that declares `Always` therefore inherits whatever the main workload declared,
downgrades to `IfNotPresent`, and never re-pulls: on a mutable tag it runs
whatever the node happens to have cached.

This is root-cause row C from folder 6's error.txt — "Chart uses release-level
defaults instead of per-resource values | Affects: CronJob/Job SA,
securityContext, imagePullPolicy". The SA third was fixed in folder 6; this is
the pullPolicy third.

error.txt blames `subtract_defaults` removing `Always` as "same as default". That
is NOT the mechanism here — see STATUS.md §1.
"""
import importlib.util
import pathlib
import shutil
import subprocess
import sys
import tempfile

import pytest
import yaml

TEST_DIR = pathlib.Path(__file__).resolve().parent
GITOPS_DIR = TEST_DIR.parents[3]
CHART_DIR = GITOPS_DIR.parent
CONVERTER_PY = GITOPS_DIR / "convert_to_universal_chart.py"
FIXTURE_DIR = TEST_DIR / "fixture"
FIXTURE = FIXTURE_DIR / "background-jobs" / "elastic-storage.yaml"

RELEASE = "elastic-storage"


def _load_converter():
    spec = importlib.util.spec_from_file_location("convert_to_universal_chart", CONVERTER_PY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["convert_to_universal_chart"] = mod
    spec.loader.exec_module(mod)
    return mod


conv = _load_converter()


def _doc(kind):
    for d in yaml.safe_load_all(FIXTURE.read_text(encoding="utf-8")):
        if d and d.get("kind") == kind:
            return d
    raise AssertionError(f"no {kind} in fixture")


def _helm_template(values: dict, release=RELEASE):
    with tempfile.TemporaryDirectory() as tmp:
        vf = pathlib.Path(tmp) / "values.yaml"
        vf.write_text(yaml.safe_dump(values, sort_keys=False), encoding="utf-8")
        proc = subprocess.run(
            ["helm", "template", release, str(CHART_DIR), "-f", str(vf)],
            capture_output=True, text=True,
        )
    assert proc.returncode == 0, f"helm template failed:\n{proc.stderr}"
    return [d for d in yaml.safe_load_all(proc.stdout) if d]


def _policy(docs, kind):
    for d in docs:
        if d.get("kind") != kind:
            continue
        if kind == "CronJob":
            cs = d["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"]
        else:
            cs = d["spec"]["template"]["spec"]["containers"]
        return cs[0].get("imagePullPolicy")
    raise AssertionError(f"chart rendered no {kind}")


# --------------------------------------------------------------------------
# Layer 1 — converter capture
# --------------------------------------------------------------------------

def test_converter_captures_main_container_pull_policy():
    """Guard: the Deployment path already worked and must keep working."""
    ctx = conv.MicroserviceContext(namespace="background-jobs", name=RELEASE)
    conv.handle_workload(_doc("Deployment"), ctx, "Deployment")
    assert ctx.full_image.get("pullPolicy") == "IfNotPresent"


def test_converter_captures_job_pull_policy():
    ctx = conv.MicroserviceContext(namespace="background-jobs", name=RELEASE)
    conv.handle_job(_doc("Job"), ctx, "elastic-storage-seed")
    entry = ctx.values["jobs"]["elastic-storage-seed"]
    assert entry.get("image", {}).get("pullPolicy") == "Always", (
        "handle_job dropped imagePullPolicy; the Job will inherit the release-level "
        f"policy and stop re-pulling. entry image = {entry.get('image')}"
    )


def test_converter_captures_cronjob_pull_policy():
    ctx = conv.MicroserviceContext(namespace="background-jobs", name=RELEASE)
    conv.handle_cronjob(_doc("CronJob"), ctx, "elastic-storage-retention")
    jt = ctx.values["cronjobs"]["elastic-storage-retention"]["jobTemplate"]
    assert jt.get("image", {}).get("pullPolicy") == "Always", (
        f"handle_cronjob dropped imagePullPolicy; jobTemplate image = {jt.get('image')}"
    )


def test_converter_omits_pull_policy_when_the_job_does_not_set_one():
    job = _doc("Job")
    job["spec"]["template"]["spec"]["containers"][0].pop("imagePullPolicy")
    ctx = conv.MicroserviceContext(namespace="background-jobs", name=RELEASE)
    conv.handle_job(job, ctx, "elastic-storage-seed")
    assert "pullPolicy" not in ctx.values["jobs"]["elastic-storage-seed"].get("image", {})


# --------------------------------------------------------------------------
# Layer 2 — templates must render the per-job policy, and still default
# --------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_job_and_cronjob_render_their_own_pull_policy():
    """The load-bearing test: the release says IfNotPresent, the jobs say Always.
    Each must render its own."""
    values = {
        "workload": {"type": "deployment"},
        "image": {"repository": "busybox", "tag": "1.36", "pullPolicy": "IfNotPresent"},
        "jobs": {"seed": {"image": {"repository": "busybox", "tag": "1.36",
                                    "pullPolicy": "Always"}}},
        "cronjobs": {"retention": {"schedule": "0 2 * * *",
                                   "jobTemplate": {"image": {"repository": "busybox",
                                                             "tag": "1.36",
                                                             "pullPolicy": "Always"}}}},
    }
    docs = _helm_template(values)
    assert _policy(docs, "Deployment") == "IfNotPresent"
    assert _policy(docs, "Job") == "Always", (
        f"Job inherited the release-level pull policy; got {_policy(docs, 'Job')!r}"
    )
    assert _policy(docs, "CronJob") == "Always", (
        f"CronJob inherited the release-level pull policy; got {_policy(docs, 'CronJob')!r}"
    )


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_job_falls_back_to_the_release_pull_policy_when_unset():
    """Pre-existing behaviour must survive."""
    values = {
        "workload": {"type": "deployment"},
        "image": {"repository": "busybox", "tag": "1.36", "pullPolicy": "Always"},
        "jobs": {"seed": {"image": {"repository": "busybox", "tag": "1.36"}}},
    }
    docs = _helm_template(values)
    assert _policy(docs, "Job") == "Always"


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_main_container_always_survives():
    values = {
        "workload": {"type": "deployment"},
        "image": {"repository": "busybox", "tag": "1.36", "pullPolicy": "Always"},
    }
    assert _policy(_helm_template(values), "Deployment") == "Always"


# --------------------------------------------------------------------------
# Layer 3 — end to end over the real CLI
# --------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_round_trip_every_pull_policy_matches_the_raw_manifest(tmp_path):
    out = tmp_path / "out"
    proc = subprocess.run(
        [sys.executable, str(CONVERTER_PY),
         "--input", str(FIXTURE_DIR), "--output", str(out),
         "--chart", str(CHART_DIR), "--skip-verify"],
        capture_output=True, text=True, cwd=str(GITOPS_DIR),
    )
    assert proc.returncode == 0, f"converter failed:\n{proc.stdout}\n{proc.stderr}"

    merged: dict = {}
    for vf in sorted(out.rglob(f"{RELEASE}.yaml")):
        if vf.parent.name == "releases":
            continue
        merged = conv.deep_merge(merged, yaml.safe_load(vf.read_text(encoding="utf-8")) or {})

    docs = _helm_template(merged)
    expected = {
        "Deployment": _doc("Deployment")["spec"]["template"]["spec"]["containers"][0]["imagePullPolicy"],
        "Job": _doc("Job")["spec"]["template"]["spec"]["containers"][0]["imagePullPolicy"],
        "CronJob": _doc("CronJob")["spec"]["jobTemplate"]["spec"]["template"]["spec"]
                   ["containers"][0]["imagePullPolicy"],
    }
    got = {k: _policy(docs, k) for k in expected}
    assert got == expected, f"pull policies differ.\n  raw      {expected}\n  rendered {got}"


# --------------------------------------------------------------------------
# Layer 4 — the value must not be lost by the base/minimal split
# --------------------------------------------------------------------------

def test_pull_policy_stays_in_the_base_values_not_stripped_as_a_default():
    """error.txt blames subtract_defaults for removing `Always` as 'same as
    default'. subtract_defaults compares against the generated base/defaults, not
    against the chart's values.yaml, so `Always` must survive a single-release
    conversion untouched."""
    ctx = conv.MicroserviceContext(namespace="background-jobs", name=RELEASE)
    dep = _doc("Deployment")
    dep["spec"]["template"]["spec"]["containers"][0]["imagePullPolicy"] = "Always"
    conv.handle_workload(dep, ctx, "Deployment")

    assert ctx.values["image"].get("pullPolicy") == "Always"
    assert conv.subtract_defaults(ctx.values, {})["image"]["pullPolicy"] == "Always"


def test_fidelity_checker_reports_a_downgraded_pull_policy():
    raw = _doc("CronJob")
    rendered = yaml.safe_load(yaml.safe_dump(raw))
    rendered["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["imagePullPolicy"] = "IfNotPresent"
    diffs = conv._fidelity_diff("CronJob", raw, rendered)
    assert any(d[0] == "CHANGED" and "imagePullPolicy" in d[1] for d in diffs), f"diffs={diffs}"
