"""
Regression tests for ERRORS_ANALYSIS/ContainerRestartPolicy_Not_Captured.

Defect: the CONTAINER-level `restartPolicy` is never captured by any handler and
never rendered by any template. Only the POD-level field round-trips.

    spec.template.spec.restartPolicy                 <- pod level, already worked
    spec.template.spec.containers[*].restartPolicy   <- container level, the gap

`restartPolicy: Always` on an initContainer is how Kubernetes 1.28+ declares a
native SIDECAR — it starts first and keeps running alongside the main container.
Lose it and the sidecar degrades into an ordinary init container the Job waits to
COMPLETE, so a long-running log shipper hangs the Job forever. On a regular
container (1.33+) it controls whether that container restarts in place rather
than failing the pod.

error.txt claims `_pod-spec.tpl` line 244 reads
`containerRestartPolicy: {{ .containerRestartPolicy | default "OnFailure" }}`.
There is no such line in this chart — `_pod-spec.tpl` never mentions
`containerRestartPolicy`, and its line 234 `{{ .job.restartPolicy | default
"OnFailure" }}` is the POD-level field, which works. So the "wrong template
default" half of the report does not apply here: the field was simply absent
from both sides.

The values key is `containerRestartPolicy` (error.txt's own name) rather than
`restartPolicy`, because `.Values.restartPolicy` is already taken by the
pod-level field in `_pod-spec.tpl`.
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
JOB_SRC = FIXTURE_DIR / "background-jobs" / "db-migrate.yaml"
DEP_SRC = FIXTURE_DIR / "background-jobs" / "api-gateway.yaml"

NS = "background-jobs"


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
    raise AssertionError(f"no {kind} in {path.name}")


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

def test_workload_main_container_restart_policy_is_captured():
    ctx = conv.MicroserviceContext(namespace=NS, name="api-gateway")
    conv.handle_workload(_doc(DEP_SRC, "Deployment"), ctx, "Deployment")
    assert ctx.values.get("containerRestartPolicy") == "Always", (
        f"the main container's restartPolicy was dropped; values keys = {sorted(ctx.values)}")


def test_pod_level_restart_policy_is_still_separate():
    """Guard: the two fields must not collapse into one another."""
    ctx = conv.MicroserviceContext(namespace=NS, name="db-migrate")
    conv.handle_job(_doc(JOB_SRC, "Job"), ctx, "db-migrate")
    entry = ctx.values["jobs"]["db-migrate"]
    assert entry["restartPolicy"] == "Never", "pod-level restartPolicy changed"
    assert entry.get("containerRestartPolicy") == "Always", (
        f"the Job container's restartPolicy was dropped; entry keys = {sorted(entry)}")


def test_cronjob_container_restart_policy_is_captured():
    ctx = conv.MicroserviceContext(namespace=NS, name="db-migrate")
    conv.handle_cronjob(_doc(JOB_SRC, "CronJob"), ctx, "db-migrate-verify")
    jt = ctx.values["cronjobs"]["db-migrate-verify"]["jobTemplate"]
    assert jt.get("containerRestartPolicy") == "Always", (
        f"the CronJob container's restartPolicy was dropped; keys = {sorted(jt)}")


def test_init_container_restart_policy_passes_through_untouched():
    """Init containers are carried verbatim, so the native-sidecar marker should
    already survive. Pinned because it is the most common use of the field and
    a future 'clean up init container fields' change would silently break it."""
    ctx = conv.MicroserviceContext(namespace=NS, name="db-migrate")
    conv.handle_job(_doc(JOB_SRC, "Job"), ctx, "db-migrate")
    ic = ctx.values["jobs"]["db-migrate"]["initContainers"]["log-shipper"]
    assert ic.get("restartPolicy") == "Always", f"native-sidecar marker lost; {ic}"


def test_nothing_is_invented_when_no_container_declares_one():
    dep = _doc(DEP_SRC, "Deployment")
    dep["spec"]["template"]["spec"]["containers"][0].pop("restartPolicy")
    ctx = conv.MicroserviceContext(namespace=NS, name="api-gateway")
    conv.handle_workload(dep, ctx, "Deployment")
    assert "containerRestartPolicy" not in ctx.values


# --------------------------------------------------------------------------
# Layer 2 — templates
# --------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_templates_render_the_container_restart_policy():
    values = {
        "workload": {"type": "deployment"},
        "image": {"repository": "nginx", "tag": "1.27"},
        "containerRestartPolicy": "Always",
        "jobs": {"seed": {"containerRestartPolicy": "Always",
                          "image": {"repository": "busybox", "tag": "1.36"}}},
        "cronjobs": {"verify": {"schedule": "0 4 * * *",
                                "jobTemplate": {"containerRestartPolicy": "Always",
                                                "image": {"repository": "busybox",
                                                          "tag": "1.36"}}}},
    }
    docs = _helm_template(values, "api-gateway")
    for kind in ("Deployment", "Job", "CronJob"):
        got = _pod_spec(docs, kind)["containers"][0].get("restartPolicy")
        assert got == "Always", f"{kind} container restartPolicy not rendered; got {got}"


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_the_pod_level_policy_is_unaffected():
    values = {
        "workload": {"type": "none"},
        "image": {"repository": "busybox", "tag": "1.36"},
        "jobs": {"seed": {"restartPolicy": "Never", "containerRestartPolicy": "Always"}},
    }
    ps = _pod_spec(_helm_template(values, "db-migrate"), "Job")
    assert ps["restartPolicy"] == "Never"
    assert ps["containers"][0]["restartPolicy"] == "Always"


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_no_restart_policy_is_invented_on_a_container():
    values = {
        "workload": {"type": "deployment"},
        "image": {"repository": "nginx", "tag": "1.27"},
        "jobs": {"seed": {}},
    }
    docs = _helm_template(values, "r")
    assert "restartPolicy" not in _pod_spec(docs, "Deployment")["containers"][0]
    assert "restartPolicy" not in _pod_spec(docs, "Job")["containers"][0]


# --------------------------------------------------------------------------
# Layer 3 — end to end
# --------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_round_trip_container_restart_policies_match_the_raw_manifests(tmp_path):
    out = tmp_path / "out"
    proc = subprocess.run(
        [sys.executable, str(CONVERTER_PY),
         "--input", str(FIXTURE_DIR), "--output", str(out),
         "--chart", str(CHART_DIR), "--skip-verify"],
        capture_output=True, text=True, cwd=str(GITOPS_DIR),
    )
    assert proc.returncode == 0, f"converter failed:\n{proc.stdout}\n{proc.stderr}"

    def layered(slug):
        merged: dict = {}
        for vf in (out / NS / "defaults.yaml", out / "base" / f"{slug}.yaml",
                   out / NS / "values" / f"{slug}.yaml"):
            if vf.exists():
                merged = conv.deep_merge(
                    merged, yaml.safe_load(vf.read_text(encoding="utf-8")) or {})
        return merged

    docs = _helm_template(layered("db-migrate"), "db-migrate")
    job_ps = _pod_spec(docs, "Job")
    raw_job = _doc(JOB_SRC, "Job")["spec"]["template"]["spec"]
    assert job_ps["containers"][0].get("restartPolicy") == \
        raw_job["containers"][0]["restartPolicy"] == "Always"
    assert job_ps["initContainers"][0].get("restartPolicy") == "Always", (
        "the native-sidecar initContainer lost its restartPolicy end to end")
    assert job_ps["restartPolicy"] == raw_job["restartPolicy"] == "Never"

    cj_ps = _pod_spec(docs, "CronJob")
    raw_cj = _doc(JOB_SRC, "CronJob")["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    assert cj_ps["containers"][0].get("restartPolicy") == \
        raw_cj["containers"][0]["restartPolicy"] == "Always"

    dep_ps = _pod_spec(_helm_template(layered("api-gateway"), "api-gateway"), "Deployment")
    raw_dep = _doc(DEP_SRC, "Deployment")["spec"]["template"]["spec"]
    assert dep_ps["containers"][0].get("restartPolicy") == \
        raw_dep["containers"][0]["restartPolicy"] == "Always"


def test_fidelity_checker_reports_a_lost_container_restart_policy():
    raw = _doc(JOB_SRC, "Job")
    rendered = yaml.safe_load(yaml.safe_dump(raw))
    rendered["spec"]["template"]["spec"]["containers"][0].pop("restartPolicy")
    diffs = conv._fidelity_diff("Job", raw, rendered)
    assert any(d[0] == "LOST" and "restartPolicy" in d[1] for d in diffs), f"diffs={diffs}"
