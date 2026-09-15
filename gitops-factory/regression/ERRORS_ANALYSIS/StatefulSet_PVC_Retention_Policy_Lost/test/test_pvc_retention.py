"""
Regression tests for ERRORS_ANALYSIS/StatefulSet_PVC_Retention_Policy_Lost.

Defect (two-sided):
  1. convert_to_universal_chart.handle_workload() never extracts
     spec.persistentVolumeClaimRetentionPolicy from a StatefulSet.
  2. universal-chart/templates/statefulset.yaml never renders it.

Either side alone leaves the rendered StatefulSet without the field, so
Kubernetes applies its default of whenDeleted/whenScaled: Delete and the PVCs
(and the data on them) are destroyed on delete / scale-down / ArgoCD recreate.

The converter tests assert on the values dict directly; the render tests shell
out to `helm template` (client-side, no cluster) and assert on the rendered
StatefulSet. Both layers are required: a converter-only fix still renders
nothing.
"""
import importlib.util
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile

import pytest
import yaml

TEST_DIR = pathlib.Path(__file__).resolve().parent
GITOPS_DIR = TEST_DIR.parents[3]                    # universal/gitops-factory
UNIVERSAL_DIR = GITOPS_DIR.parent                   # universal
CHART_DIR = UNIVERSAL_DIR  # gitops-factory lives inside the chart repo
CONVERTER_PY = GITOPS_DIR / "convert_to_universal_chart.py"
FIXTURE_DIR = TEST_DIR / "fixture"
FIXTURE_STS = FIXTURE_DIR / "platform-data" / "postgres.yaml"

RETENTION = {"whenDeleted": "Retain", "whenScaled": "Retain"}


def _load_converter():
    spec = importlib.util.spec_from_file_location("convert_to_universal_chart", CONVERTER_PY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["convert_to_universal_chart"] = mod
    spec.loader.exec_module(mod)
    return mod


conv = _load_converter()


def _raw_statefulset():
    """The StatefulSet document out of the fixture — same YAML the converter
    would read from namespaces/<ns>/<svc>.yaml."""
    docs = list(yaml.safe_load_all(FIXTURE_STS.read_text(encoding="utf-8")))
    for d in docs:
        if d and d.get("kind") == "StatefulSet":
            return d
    raise AssertionError(f"no StatefulSet in {FIXTURE_STS}")


def _convert(doc):
    ctx = conv.MicroserviceContext(namespace="platform-data", name="postgres")
    conv.handle_workload(doc, ctx, "StatefulSet")
    return ctx.values


def _helm_template(values: dict, release: str = "postgres") -> list:
    """Render the chart client-side and return the parsed documents."""
    with tempfile.TemporaryDirectory() as tmp:
        vf = pathlib.Path(tmp) / "values.yaml"
        vf.write_text(yaml.safe_dump(values, sort_keys=False), encoding="utf-8")
        proc = subprocess.run(
            ["helm", "template", release, str(CHART_DIR), "-f", str(vf)],
            capture_output=True, text=True,
        )
    assert proc.returncode == 0, f"helm template failed:\n{proc.stderr}"
    return [d for d in yaml.safe_load_all(proc.stdout) if d]


def _rendered_statefulset(values: dict) -> dict:
    for d in _helm_template(values):
        if d.get("kind") == "StatefulSet":
            return d
    raise AssertionError("chart rendered no StatefulSet")


# --------------------------------------------------------------------------
# Layer 1 — the converter must EXTRACT the field into values
# --------------------------------------------------------------------------

def test_converter_extracts_retention_policy():
    raw = _raw_statefulset()
    assert raw["spec"]["persistentVolumeClaimRetentionPolicy"] == RETENTION, \
        "fixture no longer carries the field under test"

    values = _convert(raw)

    assert "persistentVolumeClaimRetentionPolicy" in values, (
        "handle_workload dropped spec.persistentVolumeClaimRetentionPolicy; "
        "the rendered StatefulSet will fall back to the Kubernetes default "
        "whenDeleted/whenScaled: Delete and destroy the PVC on delete/scale"
    )
    assert values["persistentVolumeClaimRetentionPolicy"] == RETENTION


@pytest.mark.parametrize("policy", [
    {"whenDeleted": "Retain", "whenScaled": "Retain"},
    {"whenDeleted": "Retain", "whenScaled": "Delete"},
    {"whenDeleted": "Delete", "whenScaled": "Retain"},
    {"whenDeleted": "Retain"},
    {"whenScaled": "Retain"},
])
def test_converter_preserves_every_policy_combination(policy):
    """Retain must survive in either half independently — a partial policy is
    legal Kubernetes and the unspecified half defaults to Delete."""
    raw = _raw_statefulset()
    raw["spec"]["persistentVolumeClaimRetentionPolicy"] = policy
    assert _convert(raw)["persistentVolumeClaimRetentionPolicy"] == policy


def test_converter_does_not_invent_a_policy_when_raw_has_none():
    """Fidelity runs both ways: a raw StatefulSet with no policy must not gain
    one, or every such object reads as a CHANGED field in the report."""
    raw = _raw_statefulset()
    raw["spec"].pop("persistentVolumeClaimRetentionPolicy")
    assert "persistentVolumeClaimRetentionPolicy" not in _convert(raw)


def test_converter_ignores_the_field_on_non_statefulset_kinds():
    """Deployments and DaemonSets have no such field; extracting it there would
    render an invalid workload."""
    raw = _raw_statefulset()
    raw["kind"] = "Deployment"
    raw["spec"].pop("volumeClaimTemplates")
    ctx = conv.MicroserviceContext(namespace="platform-data", name="postgres")
    conv.handle_workload(raw, ctx, "Deployment")
    assert "persistentVolumeClaimRetentionPolicy" not in ctx.values


# --------------------------------------------------------------------------
# Layer 2 — the template must RENDER the field back out
# --------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_template_renders_retention_policy():
    values = {
        "workload": {"type": "statefulset"},
        "image": {"repository": "postgres", "tag": "16-alpine"},
        "persistentVolumeClaimRetentionPolicy": RETENTION,
        "volumeClaimTemplates": {"data": {"accessModes": ["ReadWriteOnce"], "size": "1Gi"}},
    }
    sts = _rendered_statefulset(values)
    assert sts["spec"].get("persistentVolumeClaimRetentionPolicy") == RETENTION, (
        "statefulset.yaml did not render persistentVolumeClaimRetentionPolicy; "
        f"rendered spec keys = {sorted(sts['spec'])}"
    )


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_template_omits_the_field_when_values_do_not_set_it():
    values = {
        "workload": {"type": "statefulset"},
        "image": {"repository": "postgres", "tag": "16-alpine"},
    }
    sts = _rendered_statefulset(values)
    assert "persistentVolumeClaimRetentionPolicy" not in sts["spec"], (
        "chart invented a retention policy the values never asked for"
    )


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_template_ignores_the_field_for_deployment_and_daemonset():
    for wtype, kind in (("deployment", "Deployment"), ("daemonset", "DaemonSet")):
        values = {
            "workload": {"type": wtype},
            "image": {"repository": "postgres", "tag": "16-alpine"},
            "persistentVolumeClaimRetentionPolicy": RETENTION,
        }
        docs = [d for d in _helm_template(values) if d.get("kind") == kind]
        assert docs, f"chart rendered no {kind}"
        assert "persistentVolumeClaimRetentionPolicy" not in docs[0]["spec"], \
            f"{kind} must never carry persistentVolumeClaimRetentionPolicy"


# --------------------------------------------------------------------------
# Layer 3 — end to end: raw fixture -> converter -> helm template -> raw value
# --------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_round_trip_raw_fixture_to_rendered_statefulset(tmp_path):
    """The layer that catches 'converter captured it but the template never
    renders it'. Runs the real CLI over the fixture, then helm template over
    whatever values it produced."""
    out = tmp_path / "out"
    proc = subprocess.run(
        [sys.executable, str(CONVERTER_PY),
         "--input", str(FIXTURE_DIR), "--output", str(out),
         "--chart", str(CHART_DIR), "--skip-verify"],
        capture_output=True, text=True, cwd=str(GITOPS_DIR),
    )
    assert proc.returncode == 0, f"converter failed:\n{proc.stdout}\n{proc.stderr}"

    values_files = sorted(out.rglob("postgres.yaml"))
    assert values_files, f"converter wrote no postgres values under {out}"

    merged: dict = {}
    for vf in values_files:
        loaded = yaml.safe_load(vf.read_text(encoding="utf-8")) or {}
        merged = conv.deep_merge(merged, loaded)

    assert merged.get("persistentVolumeClaimRetentionPolicy") == RETENTION, (
        "the generated values files lost the retention policy; "
        f"searched {[str(v) for v in values_files]}"
    )

    sts = _rendered_statefulset(merged)
    raw = _raw_statefulset()
    assert sts["spec"]["persistentVolumeClaimRetentionPolicy"] == \
        raw["spec"]["persistentVolumeClaimRetentionPolicy"], (
        "rendered retention policy differs from the raw manifest:\n"
        f"  raw      = {json.dumps(raw['spec']['persistentVolumeClaimRetentionPolicy'])}\n"
        f"  rendered = {json.dumps(sts['spec'].get('persistentVolumeClaimRetentionPolicy'))}"
    )


# --------------------------------------------------------------------------
# Layer 4 — the fidelity checker must SEE the loss (it is not on the ignore
# list). Guards against a future 'fix' that silences the report instead.
# --------------------------------------------------------------------------

def test_fidelity_checker_reports_a_lost_retention_policy():
    raw = _raw_statefulset()
    rendered = yaml.safe_load(yaml.safe_dump(raw))
    rendered["spec"].pop("persistentVolumeClaimRetentionPolicy")

    diffs = conv._fidelity_diff("StatefulSet", raw, rendered)
    lost = [d for d in diffs
            if d[0] == "LOST" and "persistentVolumeClaimRetentionPolicy" in d[1]]
    assert lost, (
        "the fidelity checker no longer reports a lost retention policy — "
        f"diffs were {diffs}"
    )
