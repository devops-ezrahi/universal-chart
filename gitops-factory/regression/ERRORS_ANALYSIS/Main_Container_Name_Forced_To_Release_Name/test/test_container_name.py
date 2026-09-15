"""
Regression tests for ERRORS_ANALYSIS/Main_Container_Name_Forced_To_Release_Name.

DECIDED: Option A — preserve the raw container name (user decision, 2026-08-16).

Defect: the chart hardcodes the managed container's name to the release name, so
a raw `nginx` renders as `storefront`. The container still runs, but anything
addressing it BY NAME breaks at the target: `kubectl logs -c nginx`,
`kubectl exec -c`, log-pipeline filters on container=..., and Prometheus series
carrying a `container` label.

Three call sites share the rename, not one:
  * Deployment/StatefulSet/DaemonSet — _pod-spec.tpl `mainContainer`, named
    `include "universal-chart.name"`.
  * Job and CronJob — _pod-spec.tpl `jobPodSpec`, named `.job.name`, which is
    the jobs/cronjobs MAP KEY, not the container name.
error.txt only discusses the first. Fixing it alone leaves Jobs and CronJobs
renamed, which would surface the moment the checker suppression is removed.

Part of this fix is REMOVING the `_fidelity_ignored_path` suppression of
`containers[0].name`, so a genuinely missing container becomes visible again.
test_fidelity_checker_no_longer_suppresses_container_name pins that.
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
FIXTURE_DEP = FIXTURE_DIR / "shop-web" / "storefront.yaml"
FIXTURE_JOBS = FIXTURE_DIR / "background-jobs" / "db-migrate.yaml"


def _load_converter():
    spec = importlib.util.spec_from_file_location("convert_to_universal_chart", CONVERTER_PY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["convert_to_universal_chart"] = mod
    spec.loader.exec_module(mod)
    return mod


conv = _load_converter()


def _doc(path, kind):
    for d in yaml.safe_load_all(path.read_text(encoding="utf-8")):
        if d and d.get("kind") == kind:
            return d
    raise AssertionError(f"no {kind} in {path}")


def _helm_template(values: dict, release: str):
    with tempfile.TemporaryDirectory() as tmp:
        vf = pathlib.Path(tmp) / "values.yaml"
        vf.write_text(yaml.safe_dump(values, sort_keys=False), encoding="utf-8")
        proc = subprocess.run(
            ["helm", "template", release, str(CHART_DIR), "-f", str(vf)],
            capture_output=True, text=True,
        )
    assert proc.returncode == 0, f"helm template failed:\n{proc.stderr}"
    return [d for d in yaml.safe_load_all(proc.stdout) if d]


def _containers(docs, kind):
    for d in docs:
        if d.get("kind") != kind:
            continue
        if kind == "Deployment":
            return d["spec"]["template"]["spec"]["containers"]
        if kind == "Job":
            return d["spec"]["template"]["spec"]["containers"]
        if kind == "CronJob":
            return d["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"]
    raise AssertionError(f"chart rendered no {kind}")


# --------------------------------------------------------------------------
# Layer 1 — converter must record the raw container name
# --------------------------------------------------------------------------

def test_converter_records_main_container_name():
    dep = _doc(FIXTURE_DEP, "Deployment")
    assert dep["spec"]["template"]["spec"]["containers"][0]["name"] == "nginx"

    ctx = conv.MicroserviceContext(namespace="shop-web", name="storefront")
    conv.handle_workload(dep, ctx, "Deployment")

    assert ctx.values.get("containerName") == "nginx", (
        "converter discarded the raw container name; the chart will name the "
        "container after the release and `kubectl logs -c nginx` will fail"
    )


def test_converter_records_job_container_name():
    job = _doc(FIXTURE_JOBS, "Job")
    assert job["spec"]["template"]["spec"]["containers"][0]["name"] == "migrate"

    ctx = conv.MicroserviceContext(namespace="background-jobs", name="db-migrate")
    conv.handle_job(job, ctx, "db-migrate")

    entry = ctx.values["jobs"]["db-migrate"]
    assert entry.get("containerName") == "migrate", (
        f"converter discarded the Job's container name; got keys {sorted(entry)}"
    )


def test_converter_records_cronjob_container_name():
    cj = _doc(FIXTURE_JOBS, "CronJob")
    assert cj["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["name"] == "report"

    ctx = conv.MicroserviceContext(namespace="background-jobs", name="nightly-report")
    conv.handle_cronjob(cj, ctx, "nightly-report")

    entry = ctx.values["cronjobs"]["nightly-report"]["jobTemplate"]
    assert entry.get("containerName") == "report", (
        f"converter discarded the CronJob's container name; got keys {sorted(entry)}"
    )


# --------------------------------------------------------------------------
# Layer 2 — templates must render it, and still default when unset
# --------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_deployment_renders_the_raw_container_name():
    values = {
        "workload": {"type": "deployment"},
        "image": {"repository": "nginx", "tag": "1.27-alpine"},
        "containerName": "nginx",
    }
    names = [c["name"] for c in _containers(_helm_template(values, "storefront"), "Deployment")]
    assert names == ["nginx"], (
        f"_pod-spec.tpl ignored .Values.containerName; rendered {names}, expected ['nginx']"
    )


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_deployment_falls_back_to_release_name_when_unset():
    """Pre-existing behaviour must survive the fix."""
    values = {"workload": {"type": "deployment"}, "image": {"repository": "nginx"}}
    names = [c["name"] for c in _containers(_helm_template(values, "storefront"), "Deployment")]
    assert names == ["storefront"]


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_job_and_cronjob_render_the_raw_container_name():
    values = {
        "workload": {"type": "none"},
        "image": {"repository": "busybox", "tag": "1.36"},
        "jobs": {"db-migrate": {"containerName": "migrate",
                                "image": {"repository": "busybox", "tag": "1.36"}}},
        "cronjobs": {"nightly-report": {"schedule": "17 3 * * *",
                                        "jobTemplate": {"containerName": "report",
                                                        "image": {"repository": "busybox",
                                                                  "tag": "1.36"}}}},
    }
    docs = _helm_template(values, "background-jobs")
    assert [c["name"] for c in _containers(docs, "Job")] == ["migrate"]
    assert [c["name"] for c in _containers(docs, "CronJob")] == ["report"]


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_job_falls_back_to_the_map_key_when_unset():
    values = {
        "workload": {"type": "none"},
        "image": {"repository": "busybox", "tag": "1.36"},
        "jobs": {"db-migrate": {"image": {"repository": "busybox", "tag": "1.36"}}},
    }
    assert [c["name"] for c in _containers(_helm_template(values, "bj"), "Job")] == ["db-migrate"]


# --------------------------------------------------------------------------
# Layer 3 — the checker must stop suppressing container[0].name
# --------------------------------------------------------------------------

def test_fidelity_checker_no_longer_suppresses_container_name():
    """The suppression existed only to hide this defect. With the name preserved
    it must go, or a container that genuinely vanished stays invisible."""
    assert not conv._fidelity_ignored_path(
        "Deployment", ("spec", "template", "spec", "containers", "[0]", "name")
    ), "_fidelity_ignored_path still suppresses containers[0].name"


def test_fidelity_checker_reports_a_renamed_container():
    raw = _doc(FIXTURE_DEP, "Deployment")
    rendered = yaml.safe_load(yaml.safe_dump(raw))
    rendered["spec"]["template"]["spec"]["containers"][0]["name"] = "storefront"

    diffs = conv._fidelity_diff("Deployment", raw, rendered)
    assert any(d[0] == "CHANGED" and d[1].endswith("name") for d in diffs), (
        f"checker did not report the container rename; diffs={diffs}"
    )


# --------------------------------------------------------------------------
# Layer 4 — end to end over the real CLI
# --------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_round_trip_container_names_match_the_raw_manifests(tmp_path):
    out = tmp_path / "out"
    proc = subprocess.run(
        [sys.executable, str(CONVERTER_PY),
         "--input", str(FIXTURE_DIR), "--output", str(out),
         "--chart", str(CHART_DIR), "--skip-verify"],
        capture_output=True, text=True, cwd=str(GITOPS_DIR),
    )
    assert proc.returncode == 0, f"converter failed:\n{proc.stdout}\n{proc.stderr}"

    def merged_for(release):
        m: dict = {}
        for vf in sorted(out.rglob(f"{release}.yaml")):
            if vf.parent.name == "releases":
                continue
            m = conv.deep_merge(m, yaml.safe_load(vf.read_text(encoding="utf-8")) or {})
        return m

    dep_docs = _helm_template(merged_for("storefront"), "storefront")
    assert [c["name"] for c in _containers(dep_docs, "Deployment")] == ["nginx"]

    job_docs = _helm_template(merged_for("db-migrate"), "db-migrate")
    assert [c["name"] for c in _containers(job_docs, "Job")] == ["migrate"]
    assert [c["name"] for c in _containers(job_docs, "CronJob")] == ["report"]
