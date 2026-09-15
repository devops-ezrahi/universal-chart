"""
Regression tests for ERRORS_ANALYSIS/ServiceAccountName_Overridden_By_Release_Name.

Defect: handle_cronjob() and handle_job() never read the pod spec's
`serviceAccountName`, and jobPodSpec renders the RELEASE-level
`universal-chart.serviceAccountName` helper. So a CronJob that runs as the
pre-existing `nexus` ServiceAccount renders as `nexus-backup` (the release), and
the pod references a ServiceAccount that does not exist: it never starts, and
the RBAC bound to the real SA never applies.

error.txt says the converter "captures the serviceAccountName for CronJob/Job at
line 1079 but ONLY stores it at the release level". In this codebase it is worse
and simpler: it does not capture it at all — `serviceAccountName` appears
exactly once in the converter, in handle_workload. See STATUS.md §1.

The main workload path (Deployment/StatefulSet/DaemonSet) already works; the
tests for it are guards, so a fix here cannot regress it.
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
FIXTURE = FIXTURE_DIR / "background-jobs" / "nexus-backup.yaml"

RAW_SA = "nexus"
RELEASE = "nexus-backup"


def _load_converter():
    spec = importlib.util.spec_from_file_location("convert_to_universal_chart", CONVERTER_PY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["convert_to_universal_chart"] = mod
    spec.loader.exec_module(mod)
    return mod


conv = _load_converter()


def _doc(kind, name=None):
    for d in yaml.safe_load_all(FIXTURE.read_text(encoding="utf-8")):
        if d and d.get("kind") == kind and (name is None or d["metadata"]["name"] == name):
            return d
    raise AssertionError(f"no {kind}/{name} in fixture")


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


def _sa_of(docs, kind, name_contains=None):
    for d in docs:
        if d.get("kind") != kind:
            continue
        if name_contains and name_contains not in d["metadata"]["name"]:
            continue
        if kind == "CronJob":
            return d["spec"]["jobTemplate"]["spec"]["template"]["spec"].get("serviceAccountName")
        return d["spec"]["template"]["spec"].get("serviceAccountName")
    raise AssertionError(f"chart rendered no {kind} matching {name_contains}")


# --------------------------------------------------------------------------
# Layer 1 — converter must capture the per-job ServiceAccount
# --------------------------------------------------------------------------

def test_converter_captures_job_service_account_name():
    job = _doc("Job", "nexus-backup")
    assert job["spec"]["template"]["spec"]["serviceAccountName"] == RAW_SA

    ctx = conv.MicroserviceContext(namespace="background-jobs", name=RELEASE)
    conv.handle_job(job, ctx, "nexus-backup")

    entry = ctx.values["jobs"]["nexus-backup"]
    assert entry.get("serviceAccountName") == RAW_SA, (
        "handle_job dropped the pod's serviceAccountName; the chart will fall back "
        f"to the release-level SA and the pod will reference a nonexistent "
        f"ServiceAccount. entry keys = {sorted(entry)}"
    )


def test_converter_captures_cronjob_service_account_name():
    cj = _doc("CronJob")
    assert cj["spec"]["jobTemplate"]["spec"]["template"]["spec"]["serviceAccountName"] == RAW_SA

    ctx = conv.MicroserviceContext(namespace="background-jobs", name=RELEASE)
    conv.handle_cronjob(cj, ctx, "nexus-prune")

    entry = ctx.values["cronjobs"]["nexus-prune"]["jobTemplate"]
    assert entry.get("serviceAccountName") == RAW_SA, (
        f"handle_cronjob dropped serviceAccountName; entry keys = {sorted(entry)}"
    )


def test_converter_omits_service_account_when_the_job_does_not_set_one():
    """Must not invent one — the release-level SA has to stay the fallback."""
    job = _doc("Job", "nexus-verify")
    ctx = conv.MicroserviceContext(namespace="background-jobs", name=RELEASE)
    conv.handle_job(job, ctx, "nexus-verify")
    assert "serviceAccountName" not in ctx.values["jobs"]["nexus-verify"]


def test_converter_remaps_a_renamed_service_account():
    """If the collision resolver renamed the SA object, the per-job reference
    must follow it, exactly as the main workload path does."""
    job = _doc("Job", "nexus-backup")
    ctx = conv.MicroserviceContext(namespace="background-jobs", name=RELEASE)
    ctx.name_map["ServiceAccount"][RAW_SA] = "nexus-backup-nexus"
    conv.handle_job(job, ctx, "nexus-backup")
    assert ctx.values["jobs"]["nexus-backup"]["serviceAccountName"] == "nexus-backup-nexus"


# --------------------------------------------------------------------------
# Layer 2 — jobPodSpec must render it, and still fall back
# --------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_job_and_cronjob_render_the_per_job_service_account():
    values = {
        "workload": {"type": "none"},
        "image": {"repository": "busybox", "tag": "1.36"},
        "serviceAccount": {"create": True, "name": RELEASE},
        "jobs": {"nexus-backup": {"serviceAccountName": RAW_SA,
                                  "image": {"repository": "busybox", "tag": "1.36"}}},
        "cronjobs": {"nexus-prune": {"schedule": "0 4 * * *",
                                     "jobTemplate": {"serviceAccountName": RAW_SA,
                                                     "image": {"repository": "busybox",
                                                               "tag": "1.36"}}}},
    }
    docs = _helm_template(values)
    assert _sa_of(docs, "Job") == RAW_SA, (
        f"jobPodSpec ignored the per-job serviceAccountName; rendered "
        f"{_sa_of(docs, 'Job')!r}, expected {RAW_SA!r}"
    )
    assert _sa_of(docs, "CronJob") == RAW_SA


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_job_falls_back_to_the_release_service_account_when_unset():
    """Pre-existing behaviour must survive."""
    values = {
        "workload": {"type": "none"},
        "image": {"repository": "busybox", "tag": "1.36"},
        "serviceAccount": {"create": True, "name": RELEASE},
        "jobs": {"nexus-verify": {"image": {"repository": "busybox", "tag": "1.36"}}},
    }
    assert _sa_of(_helm_template(values), "Job") == RELEASE


# --------------------------------------------------------------------------
# Layer 3 — guards: the main workload path already works, keep it working
# --------------------------------------------------------------------------

def test_main_workload_still_captures_its_service_account():
    dep = {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": "storefront"},
        "spec": {"template": {"spec": {
            "serviceAccountName": "storefront",
            "containers": [{"name": "nginx", "image": "nginx:1.27-alpine"}],
        }}},
    }
    ctx = conv.MicroserviceContext(namespace="shop-web", name="storefront")
    conv.handle_workload(dep, ctx, "Deployment")
    assert ctx.values["serviceAccount"]["name"] == "storefront"


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_main_workload_renders_the_raw_service_account_not_the_release():
    values = {
        "workload": {"type": "deployment"},
        "image": {"repository": "prometheus"},
        "serviceAccount": {"create": False, "name": "prometheus"},
    }
    assert _sa_of(_helm_template(values, "prometheus-prometheus"), "Deployment") == "prometheus"


# --------------------------------------------------------------------------
# Layer 4 — end to end over the real CLI
# --------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_round_trip_service_account_names_match_the_raw_manifests(tmp_path):
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

    # nexus-backup Job and nexus-prune CronJob both declared 'nexus'.
    for d in docs:
        if d.get("kind") == "Job" and "verify" not in d["metadata"]["name"]:
            got = d["spec"]["template"]["spec"].get("serviceAccountName")
            assert got == RAW_SA, f"Job {d['metadata']['name']}: SA {got!r}, expected {RAW_SA!r}"
        if d.get("kind") == "CronJob":
            got = d["spec"]["jobTemplate"]["spec"]["template"]["spec"].get("serviceAccountName")
            assert got == RAW_SA, f"CronJob {d['metadata']['name']}: SA {got!r}, expected {RAW_SA!r}"


# --------------------------------------------------------------------------
# Layer 5 — the fidelity checker must SEE a wrong per-job SA
# --------------------------------------------------------------------------

def test_fidelity_checker_reports_a_changed_job_service_account():
    raw = _doc("Job", "nexus-backup")
    rendered = yaml.safe_load(yaml.safe_dump(raw))
    rendered["spec"]["template"]["spec"]["serviceAccountName"] = RELEASE

    diffs = conv._fidelity_diff("Job", raw, rendered)
    assert any(d[0] == "CHANGED" and "serviceAccountName" in d[1] for d in diffs), (
        f"checker did not report the changed serviceAccountName; diffs={diffs}"
    )
