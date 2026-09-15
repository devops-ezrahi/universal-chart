"""
Regression tests for ERRORS_ANALYSIS/StatefulSet_ServiceName_Hardcoded_To_Fullname.

Defect (two-sided):
  1. convert_to_universal_chart.handle_workload() never reads spec.serviceName.
  2. universal-chart/templates/statefulset.yaml hardcodes
     `serviceName: {{ include "universal-chart.fullname" . }}`.

So a StatefulSet governed by a dedicated headless Service ('<name>-headless',
the normal pattern for clustered data systems) renders pointing at the regular
ClusterIP Service instead. Pod DNS is <pod>.<serviceName>.<ns>.svc; against a
VIP Service there are no per-pod A records, so peer discovery and therefore
cluster formation break.

What is testable offline: that the rendered `spec.serviceName` equals the raw
headless Service name, and that the chart also renders a headless Service by
that name. That DNS then resolves is Kubernetes' job and needs a cluster.
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
UNIVERSAL_DIR = GITOPS_DIR.parent
CHART_DIR = UNIVERSAL_DIR  # gitops-factory lives inside the chart repo
CONVERTER_PY = GITOPS_DIR / "convert_to_universal_chart.py"
FIXTURE_DIR = TEST_DIR / "fixture"
FIXTURE_STS = FIXTURE_DIR / "platform-data" / "redis.yaml"

HEADLESS = "redis-cache-headless"
WORKLOAD = "redis-cache"


def _load_converter():
    spec = importlib.util.spec_from_file_location("convert_to_universal_chart", CONVERTER_PY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["convert_to_universal_chart"] = mod
    spec.loader.exec_module(mod)
    return mod


conv = _load_converter()


def _docs():
    return [d for d in yaml.safe_load_all(FIXTURE_STS.read_text(encoding="utf-8")) if d]


def _raw_statefulset():
    for d in _docs():
        if d.get("kind") == "StatefulSet":
            return d
    raise AssertionError("no StatefulSet in fixture")


def _convert(doc):
    ctx = conv.MicroserviceContext(namespace="platform-data", name="redis")
    conv.handle_workload(doc, ctx, "StatefulSet")
    return ctx.values


def _helm_template(values: dict, release: str = "redis") -> list:
    with tempfile.TemporaryDirectory() as tmp:
        vf = pathlib.Path(tmp) / "values.yaml"
        vf.write_text(yaml.safe_dump(values, sort_keys=False), encoding="utf-8")
        proc = subprocess.run(
            ["helm", "template", release, str(CHART_DIR), "-f", str(vf)],
            capture_output=True, text=True,
        )
    assert proc.returncode == 0, f"helm template failed:\n{proc.stderr}"
    return [d for d in yaml.safe_load_all(proc.stdout) if d]


def _rendered(values, kind="StatefulSet", release="redis"):
    for d in _helm_template(values, release):
        if d.get("kind") == kind:
            return d
    raise AssertionError(f"chart rendered no {kind}")


# --------------------------------------------------------------------------
# Layer 1 — converter must capture spec.serviceName
# --------------------------------------------------------------------------

def test_converter_captures_headless_service_name():
    raw = _raw_statefulset()
    assert raw["spec"]["serviceName"] == HEADLESS, "fixture lost the field under test"

    values = _convert(raw)

    assert values.get("serviceName") == HEADLESS, (
        "handle_workload dropped spec.serviceName; the template will fall back to "
        "the release fullname and the StatefulSet will govern the regular ClusterIP "
        "Service instead of the headless one -> no per-pod DNS -> no cluster"
    )


def test_converter_captures_service_name_even_when_it_equals_the_workload_name():
    """Captured UNCONDITIONALLY, not just when it differs from the workload name.

    Storing it only on mismatch assumes the chart's fullname always resolves back
    to the raw workload name. That holds today only because the converter pins
    fullnameOverride, and this whole defect exists because an implicit
    'the name will match' assumption went wrong. Pinning the real value removes
    the assumption instead of relocating it.
    """
    raw = _raw_statefulset()
    raw["spec"]["serviceName"] = WORKLOAD
    assert _convert(raw).get("serviceName") == WORKLOAD


def test_converter_omits_service_name_when_raw_has_none():
    raw = _raw_statefulset()
    raw["spec"].pop("serviceName")
    assert "serviceName" not in _convert(raw)


def test_converter_ignores_service_name_on_deployment():
    raw = _raw_statefulset()
    raw["kind"] = "Deployment"
    raw["spec"].pop("volumeClaimTemplates")
    ctx = conv.MicroserviceContext(namespace="platform-data", name="redis")
    conv.handle_workload(raw, ctx, "Deployment")
    assert "serviceName" not in ctx.values


# --------------------------------------------------------------------------
# Layer 2 — template must render it, and still default when unset
# --------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_template_renders_explicit_service_name():
    values = {
        "workload": {"type": "statefulset"},
        "image": {"repository": "redis", "tag": "7-alpine"},
        "serviceName": HEADLESS,
    }
    sts = _rendered(values)
    assert sts["spec"]["serviceName"] == HEADLESS, (
        "statefulset.yaml ignored .Values.serviceName and used the release fullname; "
        f"rendered serviceName = {sts['spec']['serviceName']!r}"
    )


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_template_falls_back_to_fullname_when_service_name_unset():
    """The pre-existing behaviour must survive the fix — a StatefulSet whose
    values carry no serviceName still gets a valid one."""
    values = {
        "workload": {"type": "statefulset"},
        "image": {"repository": "redis", "tag": "7-alpine"},
    }
    assert _rendered(values)["spec"]["serviceName"] == "redis"


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_template_service_name_respects_fullname_override_when_unset():
    values = {
        "workload": {"type": "statefulset"},
        "image": {"repository": "redis", "tag": "7-alpine"},
        "fullnameOverride": WORKLOAD,
    }
    assert _rendered(values)["spec"]["serviceName"] == WORKLOAD


# --------------------------------------------------------------------------
# Layer 3 — end to end over the real CLI
# --------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_round_trip_service_name_points_at_the_headless_service(tmp_path):
    out = tmp_path / "out"
    proc = subprocess.run(
        [sys.executable, str(CONVERTER_PY),
         "--input", str(FIXTURE_DIR), "--output", str(out),
         "--chart", str(CHART_DIR), "--skip-verify"],
        capture_output=True, text=True, cwd=str(GITOPS_DIR),
    )
    assert proc.returncode == 0, f"converter failed:\n{proc.stdout}\n{proc.stderr}"

    merged: dict = {}
    for vf in sorted(out.rglob("redis.yaml")):
        merged = conv.deep_merge(merged, yaml.safe_load(vf.read_text(encoding="utf-8")) or {})

    assert merged.get("serviceName") == HEADLESS, (
        f"generated values lost serviceName; got {merged.get('serviceName')!r}"
    )

    docs = _helm_template(merged)
    sts = next(d for d in docs if d["kind"] == "StatefulSet")
    raw = _raw_statefulset()

    assert sts["spec"]["serviceName"] == raw["spec"]["serviceName"], (
        "rendered serviceName differs from raw:\n"
        f"  raw      = {raw['spec']['serviceName']!r}\n"
        f"  rendered = {sts['spec']['serviceName']!r}"
    )

    # The governing Service must actually exist in the same release, and be
    # headless — a serviceName pointing at nothing is just as broken.
    services = {d["metadata"]["name"]: d for d in docs if d["kind"] == "Service"}
    assert HEADLESS in services, (
        f"no Service named {HEADLESS} rendered; serviceName points at a "
        f"non-existent object. Rendered Services: {sorted(services)}"
    )
    assert services[HEADLESS]["spec"].get("clusterIP") == "None", (
        f"{HEADLESS} rendered as a VIP Service, not headless — per-pod DNS "
        "records would not be created"
    )


# --------------------------------------------------------------------------
# Layer 4 — the fidelity checker must SEE a wrong serviceName
# --------------------------------------------------------------------------

def test_fidelity_checker_reports_a_changed_service_name():
    raw = _raw_statefulset()
    rendered = yaml.safe_load(yaml.safe_dump(raw))
    rendered["spec"]["serviceName"] = WORKLOAD

    diffs = conv._fidelity_diff("StatefulSet", raw, rendered)
    changed = [d for d in diffs if d[0] == "CHANGED" and "serviceName" in d[1]]
    assert changed, f"checker no longer reports a changed serviceName; diffs={diffs}"
