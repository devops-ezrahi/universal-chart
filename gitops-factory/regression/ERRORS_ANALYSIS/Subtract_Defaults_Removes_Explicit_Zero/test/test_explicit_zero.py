"""
Regression tests for ERRORS_ANALYSIS/Subtract_Defaults_Removes_Explicit_Zero.

Symptom: a CronJob/Job that explicitly declares `backoffLimit: 0` or
`successfulJobsHistoryLimit: 0` comes back with the chart's own fallback number
instead. `backoffLimit: 0` means "never retry a failed Job"; rendering 3 (or 6)
retries it. `successfulJobsHistoryLimit: 0` means "keep no completed pods";
rendering 3 leaks pods.

error.txt calls for a two-sided fix and blames `subtract_defaults` for the first
side. That half is wrong, and the tests below prove it: the converter captures 0
correctly (`if spec.get(k) is not None`, which is zero-safe), and although
`common_subtree` does promote a shared 0 into `defaults.yaml` and
`subtract_defaults` does then remove it from the microservice's own file, the
defaults layer is applied UNDERNEATH it at render time, so the 0 arrives intact.
Subtraction is only a loss when something is subtracted and never layered back.

The real and only cause is Go template's `default`, which treats 0 as empty:

    successfulJobsHistoryLimit: {{ $spec.successfulJobsHistoryLimit | default 3 }}
    failedJobsHistoryLimit:     {{ $spec.failedJobsHistoryLimit | default 1 }}
    backoffLimit:               {{ $spec.jobTemplate.backoffLimit | default 3 }}   (cronjob.yaml)
    backoffLimit:               {{ $spec.backoffLimit | default 3 }}               (job.yaml)

A values file holding a literal 0 renders the fallback anyway. One-sided fix, in
the chart.
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
CRONJOB_SRC = FIXTURE_DIR / "background-jobs" / "nightly-report.yaml"
JOB_SRC = FIXTURE_DIR / "background-jobs" / "db-migrate.yaml"

NS = "background-jobs"

# Kubernetes' own defaults for the three fields, for the "nothing is invented
# that Kubernetes would not have invented" assertions.
K8S_BACKOFF_LIMIT = 6
K8S_SUCCESS_HISTORY = 3
K8S_FAILED_HISTORY = 1


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


def _only(docs, kind):
    for d in docs:
        if d.get("kind") == kind:
            return d
    raise AssertionError(f"chart rendered no {kind}")


# --------------------------------------------------------------------------
# Layer 1 — the converter. error.txt blames it; it is not at fault.
# --------------------------------------------------------------------------

def test_converter_captures_an_explicit_zero_on_a_cronjob():
    ctx = conv.MicroserviceContext(namespace=NS, name="nightly-report")
    conv.handle_cronjob(_doc(CRONJOB_SRC, "CronJob"), ctx, "nightly-report")
    entry = ctx.values["cronjobs"]["nightly-report"]
    assert entry["successfulJobsHistoryLimit"] == 0, f"entry={entry}"
    assert entry["failedJobsHistoryLimit"] == 0, f"entry={entry}"
    assert entry["jobTemplate"]["backoffLimit"] == 0, f"entry={entry}"


def test_converter_captures_an_explicit_zero_on_a_job():
    ctx = conv.MicroserviceContext(namespace=NS, name="db-migrate")
    conv.handle_job(_doc(JOB_SRC, "Job"), ctx, "db-migrate")
    assert ctx.values["jobs"]["db-migrate"]["backoffLimit"] == 0


def test_subtract_defaults_removes_a_shared_zero_but_the_layer_supplies_it():
    """error.txt's stated cause, tested directly. Subtraction DOES remove the
    shared 0 — and that is not a loss, because defaults.yaml is layered
    underneath. A loss would need something subtracted and never written."""
    a = {"jobs": {"seed": {"backoffLimit": 0}}}
    b = {"jobs": {"seed": {"backoffLimit": 0}}}
    defaults = conv.common_subtree([a, b])
    assert defaults == {"jobs": {"seed": {"backoffLimit": 0}}}, defaults

    remainder = conv.subtract_defaults(a, defaults)
    assert remainder == {}, f"expected the 0 to move to defaults.yaml; got {remainder}"

    # What Helm actually renders is defaults UNDER the remainder.
    merged = conv.deep_merge(defaults, remainder)
    assert merged["jobs"]["seed"]["backoffLimit"] == 0, (
        "the layered result lost the explicit 0 — THIS would make error.txt right")


def test_subtract_defaults_does_not_confuse_zero_with_absent():
    """A 0 must only be removed when the default is also 0 — never because 0 is
    falsy."""
    assert conv.subtract_defaults({"a": 0}, {"a": 3}) == {"a": 0}
    assert conv.subtract_defaults({"a": 0}, {"a": 0}) == {}
    assert conv.subtract_defaults({"a": 0}, {}) == {"a": 0}
    assert conv.subtract_defaults({"a": 3}, {"a": 0}) == {"a": 3}


# --------------------------------------------------------------------------
# Layer 2 — the chart. This is the actual defect.
# --------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_cronjob_renders_an_explicit_zero_verbatim():
    values = {
        "workload": {"type": "none"},
        "image": {"repository": "busybox", "tag": "1.36"},
        "cronjobs": {"nightly-report": {
            "schedule": "0 2 * * *",
            "successfulJobsHistoryLimit": 0,
            "failedJobsHistoryLimit": 0,
            "jobTemplate": {"backoffLimit": 0},
        }},
    }
    spec = _only(_helm_template(values, "nightly-report"), "CronJob")["spec"]
    assert spec["successfulJobsHistoryLimit"] == 0, (
        f"values said 0, chart rendered {spec['successfulJobsHistoryLimit']}")
    assert spec["failedJobsHistoryLimit"] == 0, (
        f"values said 0, chart rendered {spec['failedJobsHistoryLimit']}")
    assert spec["jobTemplate"]["spec"]["backoffLimit"] == 0, (
        f"values said 0, chart rendered {spec['jobTemplate']['spec']['backoffLimit']}")


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_job_renders_an_explicit_zero_verbatim():
    values = {
        "workload": {"type": "none"},
        "image": {"repository": "busybox", "tag": "1.36"},
        "jobs": {"db-migrate": {"backoffLimit": 0}},
    }
    spec = _only(_helm_template(values, "db-migrate"), "Job")["spec"]
    assert spec["backoffLimit"] == 0, f"values said 0, chart rendered {spec['backoffLimit']}"


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_a_non_zero_value_still_renders_verbatim():
    """Control: the ordinary path must not change."""
    values = {
        "workload": {"type": "none"},
        "image": {"repository": "busybox", "tag": "1.36"},
        "cronjobs": {"c": {"schedule": "0 2 * * *",
                           "successfulJobsHistoryLimit": 5,
                           "failedJobsHistoryLimit": 4,
                           "jobTemplate": {"backoffLimit": 7}}},
        "jobs": {"j": {"backoffLimit": 9}},
    }
    docs = _helm_template(values, "r")
    cj = _only(docs, "CronJob")["spec"]
    assert cj["successfulJobsHistoryLimit"] == 5
    assert cj["failedJobsHistoryLimit"] == 4
    assert cj["jobTemplate"]["spec"]["backoffLimit"] == 7
    assert _only(docs, "Job")["spec"]["backoffLimit"] == 9


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_the_fallback_matches_kubernetes_when_nothing_is_declared():
    """The chart's own fallback for an unset field must be the value Kubernetes
    would have applied — otherwise the chart invents a number nobody chose, and
    the fidelity checker reports it as an ADDED field that differs from the raw
    dump. backoffLimit's fallback was 3; Kubernetes uses 6."""
    values = {
        "workload": {"type": "none"},
        "image": {"repository": "busybox", "tag": "1.36"},
        "cronjobs": {"c": {"schedule": "0 2 * * *", "jobTemplate": {}}},
        "jobs": {"j": {}},
    }
    docs = _helm_template(values, "r")
    cj = _only(docs, "CronJob")["spec"]
    assert cj["successfulJobsHistoryLimit"] == K8S_SUCCESS_HISTORY
    assert cj["failedJobsHistoryLimit"] == K8S_FAILED_HISTORY
    assert cj["jobTemplate"]["spec"]["backoffLimit"] == K8S_BACKOFF_LIMIT, (
        "the chart's backoffLimit fallback does not match Kubernetes' own "
        f"({K8S_BACKOFF_LIMIT}); got {cj['jobTemplate']['spec']['backoffLimit']}")
    assert _only(docs, "Job")["spec"]["backoffLimit"] == K8S_BACKOFF_LIMIT


# --------------------------------------------------------------------------
# Layer 3 — end to end, through the real defaults.yaml/base/values layering.
# --------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_round_trip_every_explicit_zero_survives_the_full_layering(tmp_path):
    out = tmp_path / "out"
    proc = subprocess.run(
        [sys.executable, str(CONVERTER_PY),
         "--input", str(FIXTURE_DIR), "--output", str(out),
         "--chart", str(CHART_DIR), "--skip-verify"],
        capture_output=True, text=True, cwd=str(GITOPS_DIR),
    )
    assert proc.returncode == 0, f"converter failed:\n{proc.stdout}\n{proc.stderr}"

    # The layer order the ApplicationSet applies: <ns>/defaults.yaml, then
    # base/<slug>.yaml, then <ns>/values/<slug>.yaml.
    def layered(slug):
        merged: dict = {}
        for vf in (out / NS / "defaults.yaml",
                   out / "base" / f"{slug}.yaml",
                   out / NS / "values" / f"{slug}.yaml"):
            if vf.exists():
                merged = conv.deep_merge(
                    merged, yaml.safe_load(vf.read_text(encoding="utf-8")) or {})
        return merged

    cj = _only(_helm_template(layered("nightly-report"), "nightly-report"), "CronJob")["spec"]
    raw_cj = _doc(CRONJOB_SRC, "CronJob")["spec"]
    assert cj["successfulJobsHistoryLimit"] == raw_cj["successfulJobsHistoryLimit"] == 0
    assert cj["failedJobsHistoryLimit"] == raw_cj["failedJobsHistoryLimit"] == 0
    assert cj["jobTemplate"]["spec"]["backoffLimit"] == \
        raw_cj["jobTemplate"]["spec"]["backoffLimit"] == 0

    job = _only(_helm_template(layered("db-migrate"), "db-migrate"), "Job")["spec"]
    assert job["backoffLimit"] == _doc(JOB_SRC, "Job")["spec"]["backoffLimit"] == 0


def test_fidelity_checker_reports_a_zero_replaced_by_a_default():
    raw = _doc(CRONJOB_SRC, "CronJob")
    rendered = yaml.safe_load(yaml.safe_dump(raw))
    rendered["spec"]["successfulJobsHistoryLimit"] = 3
    rendered["spec"]["jobTemplate"]["spec"]["backoffLimit"] = 6
    diffs = conv._fidelity_diff("CronJob", raw, rendered)
    changed = [d[1] for d in diffs if d[0] == "CHANGED"]
    assert any("successfulJobsHistoryLimit" in c for c in changed), f"diffs={diffs}"
    assert any("backoffLimit" in c for c in changed), f"diffs={diffs}"
