"""
Regression tests for ERRORS_NEW_ANALYSIS/CronJob_Parallelism_Lost.

The lowest-priority item in the report — P8, the bottom of its priority table —
and the reasoning for that rating is worth reading carefully:

  "Technically this is the K8s default, so fixing it is optional. [...] Low
   priority - 1 is the default. Including it explicitly is a
   documentation/clarity improvement."

True of the VALUE observed. Not true of the FIELD. error.txt spots this itself
and asks for the check:

  "VERIFY BEFORE CLOSING, same caveat as every other default-valued field in
   this report: confirm no CronJob anywhere sets parallelism to something other
   than 1. A parallelism of 2+ that silently drops to 1 halves throughput on a
   batch job and would be a real regression. The report only ever cites 1."

Nobody can confirm that for manifests this repo does not have. So the field is
captured rather than the value judged — which costs one line per handler and
removes the question.

`completions` is captured alongside it. It is the same field's other half, has
the identical failure mode, and was in neither whitelist; leaving it out would
recreate this exact folder for the next reader. That is the RC-4 pattern from
README_REGRESSION_ANALYSIS.md — an allow-list means "we fixed field X" never
generalises — and the cheapest place to break it is at the moment you are
already editing the list.
"""
import importlib.util
import pathlib
import subprocess
import sys

import pytest
import yaml

TEST_DIR = pathlib.Path(__file__).resolve().parent
GITOPS_DIR = TEST_DIR.parents[3]
CHART_DIR = GITOPS_DIR.parent
CONVERTER_PY = GITOPS_DIR / "convert_to_universal_chart.py"
SHOWCASE = GITOPS_DIR / "namespaces-showcase"


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


conv = _load(CONVERTER_PY, "convert_to_universal_chart")


def _ctx():
    return conv.MicroserviceContext(namespace="background-jobs", name="jaeger-cleanup")


def _cronjob(**jt):
    return {"apiVersion": "batch/v1", "kind": "CronJob",
            "metadata": {"name": "jaeger-cleanup"},
            "spec": {"schedule": "0 3 * * *", "jobTemplate": {"spec": {
                **jt,
                "template": {"metadata": {}, "spec": {
                    "restartPolicy": "Never",
                    "containers": [{"name": "cleanup", "image": "jaegertracing/jaeger:1.5"}]}}}}}}


def _job(**spec):
    return {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": "db-migrate"},
            "spec": {**spec, "template": {"metadata": {}, "spec": {
                "restartPolicy": "Never",
                "containers": [{"name": "migrate", "image": "busybox:1.36"}]}}}}


# ---------------------------------------------------------------------------
# 1. Converter
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", [1, 2, 4, 16])
def test_cronjob_parallelism_is_captured_at_any_value(value):
    ctx = _ctx()
    conv.handle_cronjob(_cronjob(parallelism=value), ctx, "jaeger-cleanup")
    assert ctx.values["cronjobs"]["jaeger-cleanup"]["jobTemplate"]["parallelism"] == value


@pytest.mark.parametrize("value", [1, 4])
def test_job_parallelism_is_captured(value):
    ctx = _ctx()
    conv.handle_job(_job(parallelism=value), ctx, "db-migrate")
    assert ctx.values["jobs"]["db-migrate"]["parallelism"] == value


def test_completions_is_captured_too():
    """The same field's other half, and it was in neither whitelist."""
    ctx = _ctx()
    conv.handle_job(_job(parallelism=4, completions=4), ctx, "db-migrate")
    assert ctx.values["jobs"]["db-migrate"]["completions"] == 4


def test_an_explicit_zero_survives():
    """`parallelism: 0` is a legal way to suspend a Job. A truthiness check
    would drop it — the same trap as folder 7's minReadySeconds and the previous
    round's Subtract_Defaults_Removes_Explicit_Zero."""
    ctx = _ctx()
    conv.handle_job(_job(parallelism=0), ctx, "db-migrate")
    assert ctx.values["jobs"]["db-migrate"]["parallelism"] == 0


def test_absent_stays_absent():
    ctx = _ctx()
    conv.handle_job(_job(), ctx, "db-migrate")
    assert "parallelism" not in ctx.values["jobs"]["db-migrate"]


def test_the_two_handlers_capture_the_same_field_set():
    """handle_cronjob and handle_job describe the same spec and have drifted
    apart before — folder 5 found pod-template labels captured for workloads and
    for neither of these. Asserting the lists match is cheaper than rediscovering
    that."""
    src = CONVERTER_PY.read_text(encoding="utf-8")
    field_lists = [line for line in src.splitlines() if '"ttlSecondsAfterFinished",' in line]
    assert len(field_lists) == 2, f"expected both handlers to share the list, found {field_lists}"
    following = [src.splitlines()[src.splitlines().index(l) + 1] for l in field_lists]
    assert all('"parallelism", "completions"' in f for f in following), following


# ---------------------------------------------------------------------------
# 2. Template
# ---------------------------------------------------------------------------

def _render(values, tmp_path):
    vf = tmp_path / "values.yaml"
    vf.write_text(yaml.safe_dump(values), encoding="utf-8")
    out = subprocess.run(
        ["helm", "template", "jobs", str(CHART_DIR), "--namespace", "background-jobs",
         "-f", str(vf)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert out.returncode == 0, out.stderr
    return [d for d in yaml.safe_load_all(out.stdout) if d]


BASE = {"workload": {"type": "none"}, "service": {"enabled": False},
        "image": {"repository": "busybox", "tag": "1.36"}}


@pytest.mark.parametrize("value", [0, 1, 4])
def test_job_template_renders_parallelism(value, tmp_path):
    docs = _render({**BASE, "jobs": {"db-migrate": {
        "image": {"repository": "busybox"}, "parallelism": value}}}, tmp_path)
    job = next(d for d in docs if d["kind"] == "Job")
    assert job["spec"]["parallelism"] == value


@pytest.mark.parametrize("value", [0, 2])
def test_cronjob_template_renders_parallelism(value, tmp_path):
    docs = _render({**BASE, "cronjobs": {"cleanup": {
        "schedule": "0 3 * * *",
        "jobTemplate": {"image": {"repository": "busybox"}, "parallelism": value}}}}, tmp_path)
    cj = next(d for d in docs if d["kind"] == "CronJob")
    assert cj["spec"]["jobTemplate"]["spec"]["parallelism"] == value


def test_templates_omit_the_fields_when_unset(tmp_path):
    docs = _render({**BASE, "jobs": {"db-migrate": {"image": {"repository": "busybox"}}}},
                   tmp_path)
    job = next(d for d in docs if d["kind"] == "Job")
    assert "parallelism" not in job["spec"] and "completions" not in job["spec"]


def test_completions_renders(tmp_path):
    docs = _render({**BASE, "jobs": {"db-migrate": {
        "image": {"repository": "busybox"}, "parallelism": 4, "completions": 4}}}, tmp_path)
    job = next(d for d in docs if d["kind"] == "Job")
    assert job["spec"]["completions"] == 4


# ---------------------------------------------------------------------------
# 3. Corpus coverage
# ---------------------------------------------------------------------------

def test_the_corpus_uses_a_NON_default_parallelism():
    """error.txt's own reasoning turned into a test. A corpus entry of 1 would
    prove nothing — losing the default is undetectable, which is precisely why
    the report rated this P8 and why that rating cannot be checked from the
    report alone."""
    values = []
    for f in SHOWCASE.rglob("*.yaml"):
        for d in yaml.safe_load_all(f.read_text(encoding="utf-8")):
            if not d:
                continue
            if d.get("kind") == "Job":
                values.append(d["spec"].get("parallelism"))
            elif d.get("kind") == "CronJob":
                values.append(d["spec"]["jobTemplate"]["spec"].get("parallelism"))
    present = [v for v in values if v is not None]
    assert present, "no Job or CronJob in the corpus declares parallelism"
    assert any(v != 1 for v in present), \
        f"only default values present ({present}) — their loss would be undetectable"


def test_both_a_job_and_a_cronjob_carry_it():
    """Two separate handlers, two separate template paths. Covering one would
    leave the other free to regress."""
    kinds = set()
    for f in SHOWCASE.rglob("*.yaml"):
        for d in yaml.safe_load_all(f.read_text(encoding="utf-8")):
            if not d:
                continue
            if d.get("kind") == "Job" and d["spec"].get("parallelism") is not None:
                kinds.add("Job")
            elif (d.get("kind") == "CronJob"
                  and d["spec"]["jobTemplate"]["spec"].get("parallelism") is not None):
                kinds.add("CronJob")
    assert kinds == {"Job", "CronJob"}, kinds
