"""
Regression tests for ERRORS_ANALYSIS/Pod_SecurityContext_Overridden_By_Release_Defaults.

Defect: handle_cronjob/handle_job never capture the job's pod-level
`securityContext`, and jobPodSpec renders the RELEASE-level
`.Values.podSecurityContext`. A CronJob declaring runAsUser/fsGroup 1000
therefore inherits the workload's 2000 and cannot read files on a volume its own
PVC owns as 1000.

Second, related loss in the same template: jobPodSpec renders NO container-level
`securityContext` at all, so a job container's hardening
(allowPrivilegeEscalation:false, readOnlyRootFilesystem, capabilities.drop) is
dropped outright rather than overridden.

This is the last third of root-cause row C — "Chart uses release-level defaults
instead of per-resource values | Affects: CronJob/Job SA, securityContext,
imagePullPolicy". SA was folder 6, pullPolicy folder 11.
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
JOB_POD_SC = {"runAsUser": 1000, "runAsGroup": 1000, "fsGroup": 1000}
RELEASE_POD_SC = {"runAsUser": 2000, "runAsGroup": 2000, "fsGroup": 2000}


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


def _pod_spec(docs, kind):
    for d in docs:
        if d.get("kind") != kind:
            continue
        if kind == "CronJob":
            return d["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        return d["spec"]["template"]["spec"]
    raise AssertionError(f"chart rendered no {kind}")


# --------------------------------------------------------------------------
# Layer 1 — converter capture
# --------------------------------------------------------------------------

def test_converter_captures_workload_pod_security_context():
    """Guard: this path already worked (line 836) and must keep working."""
    ctx = conv.MicroserviceContext(namespace="background-jobs", name=RELEASE)
    conv.handle_workload(_doc("Deployment"), ctx, "Deployment")
    assert ctx.values["podSecurityContext"] == RELEASE_POD_SC


def test_converter_captures_job_pod_security_context():
    ctx = conv.MicroserviceContext(namespace="background-jobs", name=RELEASE)
    conv.handle_job(_doc("Job"), ctx, "elastic-storage-seed")
    entry = ctx.values["jobs"]["elastic-storage-seed"]
    assert entry.get("podSecurityContext") == JOB_POD_SC, (
        "handle_job dropped the pod-level securityContext; the Job will inherit "
        f"the release-level one. entry keys = {sorted(entry)}"
    )


def test_converter_captures_cronjob_pod_security_context():
    ctx = conv.MicroserviceContext(namespace="background-jobs", name=RELEASE)
    conv.handle_cronjob(_doc("CronJob"), ctx, "elastic-storage-retention")
    jt = ctx.values["cronjobs"]["elastic-storage-retention"]["jobTemplate"]
    assert jt.get("podSecurityContext") == JOB_POD_SC, (
        f"handle_cronjob dropped the pod-level securityContext; keys = {sorted(jt)}"
    )


def test_converter_captures_job_container_security_context():
    ctx = conv.MicroserviceContext(namespace="background-jobs", name=RELEASE)
    conv.handle_job(_doc("Job"), ctx, "elastic-storage-seed")
    sc = ctx.values["jobs"]["elastic-storage-seed"].get("securityContext")
    assert sc == {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
    }, f"container-level securityContext lost; got {sc}"


def test_converter_omits_security_context_when_the_job_has_none():
    job = _doc("Job")
    job["spec"]["template"]["spec"].pop("securityContext")
    job["spec"]["template"]["spec"]["containers"][0].pop("securityContext")
    ctx = conv.MicroserviceContext(namespace="background-jobs", name=RELEASE)
    conv.handle_job(job, ctx, "elastic-storage-seed")
    entry = ctx.values["jobs"]["elastic-storage-seed"]
    assert "podSecurityContext" not in entry
    assert "securityContext" not in entry


# --------------------------------------------------------------------------
# Layer 2 — templates must render per-job, and still fall back
# --------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_job_and_cronjob_render_their_own_pod_security_context():
    values = {
        "workload": {"type": "deployment"},
        "image": {"repository": "busybox", "tag": "1.36"},
        "podSecurityContext": RELEASE_POD_SC,
        "jobs": {"seed": {"podSecurityContext": JOB_POD_SC,
                          "image": {"repository": "busybox", "tag": "1.36"}}},
        "cronjobs": {"retention": {"schedule": "0 2 * * *",
                                   "jobTemplate": {"podSecurityContext": JOB_POD_SC,
                                                   "image": {"repository": "busybox",
                                                             "tag": "1.36"}}}},
    }
    docs = _helm_template(values)
    assert _pod_spec(docs, "Deployment")["securityContext"] == RELEASE_POD_SC
    assert _pod_spec(docs, "Job")["securityContext"] == JOB_POD_SC, (
        "Job inherited the release-level securityContext; got "
        f"{_pod_spec(docs, 'Job').get('securityContext')}"
    )
    assert _pod_spec(docs, "CronJob")["securityContext"] == JOB_POD_SC


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_job_renders_its_container_security_context():
    csc = {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True}
    values = {
        "workload": {"type": "deployment"},
        "image": {"repository": "busybox", "tag": "1.36"},
        "jobs": {"seed": {"securityContext": csc,
                          "image": {"repository": "busybox", "tag": "1.36"}}},
    }
    got = _pod_spec(_helm_template(values), "Job")["containers"][0].get("securityContext")
    assert got == csc, f"job container securityContext not rendered; got {got}"


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_job_falls_back_to_the_release_pod_security_context_when_unset():
    """Pre-existing behaviour must survive."""
    values = {
        "workload": {"type": "deployment"},
        "image": {"repository": "busybox", "tag": "1.36"},
        "podSecurityContext": RELEASE_POD_SC,
        "jobs": {"seed": {"image": {"repository": "busybox", "tag": "1.36"}}},
    }
    assert _pod_spec(_helm_template(values), "Job")["securityContext"] == RELEASE_POD_SC


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_no_security_context_is_invented_when_nothing_sets_one():
    values = {
        "workload": {"type": "deployment"},
        "image": {"repository": "busybox", "tag": "1.36"},
        "jobs": {"seed": {"image": {"repository": "busybox", "tag": "1.36"}}},
    }
    assert "securityContext" not in _pod_spec(_helm_template(values), "Job")


# --------------------------------------------------------------------------
# Layer 3 — end to end
# --------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_round_trip_security_contexts_match_the_raw_manifests(tmp_path):
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

    for kind, raw in (("Deployment", _doc("Deployment")),
                      ("Job", _doc("Job")),
                      ("CronJob", _doc("CronJob"))):
        raw_ps = (raw["spec"]["jobTemplate"]["spec"]["template"]["spec"]
                  if kind == "CronJob" else raw["spec"]["template"]["spec"])
        got = _pod_spec(docs, kind)
        assert got.get("securityContext") == raw_ps.get("securityContext"), (
            f"{kind} pod securityContext: raw={raw_ps.get('securityContext')} "
            f"rendered={got.get('securityContext')}"
        )
        assert got["containers"][0].get("securityContext") == \
            raw_ps["containers"][0].get("securityContext"), (
            f"{kind} container securityContext: "
            f"raw={raw_ps['containers'][0].get('securityContext')} "
            f"rendered={got['containers'][0].get('securityContext')}"
        )


def test_fidelity_checker_reports_an_overridden_security_context():
    raw = _doc("CronJob")
    rendered = yaml.safe_load(yaml.safe_dump(raw))
    rendered["spec"]["jobTemplate"]["spec"]["template"]["spec"]["securityContext"] = RELEASE_POD_SC
    diffs = conv._fidelity_diff("CronJob", raw, rendered)
    assert any(d[0] == "CHANGED" and "securityContext" in d[1] for d in diffs), f"diffs={diffs}"
