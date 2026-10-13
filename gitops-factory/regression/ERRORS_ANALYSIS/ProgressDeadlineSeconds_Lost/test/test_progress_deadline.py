"""
Regression tests for ERRORS_ANALYSIS/ProgressDeadlineSeconds_Lost.

Reported: `spec.progressDeadlineSeconds` is neither captured nor rendered, 68 to
119 occurrences, classified LOW because the cited value (600) IS the Kubernetes
default so losing it changes nothing.

error.txt asks for one specific check before closing it out: *"If ANY Deployment
in the raw manifests sets a non-600 value ... that one IS a real behavioural
change. Grep the raw manifests and confirm every value is 600."*

Grep proof (STATUS.md §1): `progressDeadlineSeconds` appears **0 times** across
namespaces/, namespaces-showcase/ and namespaces-live-demo/. Not 600, not
anything — the field is absent entirely, so nothing in this repo is affected.

The code gap is nevertheless real: `handle_workload` never reads the field and
`deployment.yaml` never renders it, so a Deployment that DID set a non-default
value would silently get 600. This folder fixes that and tests it with 120 — a
short deadline for fast rollback detection, the case that actually matters —
rather than with the harmless 600 the report happened to cite.
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
FIXTURE = FIXTURE_DIR / "shop-web" / "storefront.yaml"

NS = "shop-web"
RELEASE = "storefront"
RAW_DIRS = ["namespaces", "namespaces-showcase", "namespaces-live-demo"]
K8S_DEFAULT = 600


def _load_converter():
    spec = importlib.util.spec_from_file_location("convert_to_universal_chart", CONVERTER_PY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["convert_to_universal_chart"] = mod
    spec.loader.exec_module(mod)
    return mod


conv = _load_converter()


def _doc():
    return next(d for d in yaml.safe_load_all(FIXTURE.read_text(encoding="utf-8"))
                if d and d.get("kind") == "Deployment")


def _helm_template(values: dict, release=RELEASE):
    with tempfile.TemporaryDirectory() as tmp:
        vf = pathlib.Path(tmp) / "values.yaml"
        vf.write_text(yaml.safe_dump(values, sort_keys=False), encoding="utf-8")
        proc = subprocess.run(["helm", "template", release, str(CHART_DIR), "-f", str(vf)],
                              capture_output=True, text=True)
    assert proc.returncode == 0, f"helm template failed:\n{proc.stderr}"
    return [d for d in yaml.safe_load_all(proc.stdout) if d]


def _deployment(docs):
    for d in docs:
        if d.get("kind") == "Deployment":
            return d
    raise AssertionError("chart rendered no Deployment")


def test_this_repo_sets_no_progress_deadline_at_all():
    """The severity check error.txt asks for, as a live test. Fails the moment a
    value appears, which is when somebody has to decide whether it is 600."""
    found = []
    for d in RAW_DIRS:
        root = GITOPS_DIR / d
        if not root.is_dir():
            continue
        for f in root.rglob("*.yaml"):
            for line in f.read_text(encoding="utf-8").splitlines():
                s = line.strip()
                if s.startswith("progressDeadlineSeconds:"):
                    found.append(f"{f.relative_to(GITOPS_DIR)}: {s}")
    non_default = [x for x in found if not x.endswith(f": progressDeadlineSeconds: {K8S_DEFAULT}")]
    assert not non_default, (
        "a non-default progressDeadlineSeconds now exists in the raw manifests, "
        "so this folder's LOW classification no longer holds:\n" + "\n".join(non_default))


def test_the_converter_captures_it():
    ctx = conv.MicroserviceContext(namespace=NS, name=RELEASE)
    conv.handle_workload(_doc(), ctx, "Deployment")
    assert ctx.values.get("progressDeadlineSeconds") == 120, (
        f"progressDeadlineSeconds dropped; values keys = {sorted(ctx.values)}")


def test_a_deployment_without_it_gets_nothing_invented():
    doc = _doc()
    doc["spec"].pop("progressDeadlineSeconds")
    ctx = conv.MicroserviceContext(namespace=NS, name=RELEASE)
    conv.handle_workload(doc, ctx, "Deployment")
    assert "progressDeadlineSeconds" not in ctx.values


def test_it_is_captured_even_when_it_equals_the_kubernetes_default():
    """600 is harmless to lose but still a value the manifest declared. Carrying
    it keeps raw and rendered identical, which is what the fidelity checker
    compares; inventing a rule about which values are 'worth' keeping is how
    fields go missing."""
    doc = _doc()
    doc["spec"]["progressDeadlineSeconds"] = K8S_DEFAULT
    ctx = conv.MicroserviceContext(namespace=NS, name=RELEASE)
    conv.handle_workload(doc, ctx, "Deployment")
    assert ctx.values.get("progressDeadlineSeconds") == K8S_DEFAULT


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_the_deployment_template_renders_it():
    values = {"workload": {"type": "deployment"},
              "image": {"repository": "nginx", "tag": "1.27"},
              "progressDeadlineSeconds": 120}
    assert _deployment(_helm_template(values))["spec"]["progressDeadlineSeconds"] == 120


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_the_openshift_default_is_rendered_when_nothing_is_set():
    values = {"workload": {"type": "deployment"},
              "image": {"repository": "nginx", "tag": "1.27"}}
    assert _deployment(_helm_template(values))["spec"]["progressDeadlineSeconds"] == 600


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_nothing_is_rendered_when_set_empty():
    values = {"workload": {"type": "deployment"}, "progressDeadlineSeconds": "",
              "image": {"repository": "nginx", "tag": "1.27"}}
    assert "progressDeadlineSeconds" not in _deployment(_helm_template(values))["spec"]


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_round_trip_matches_the_raw_manifest(tmp_path):
    out = tmp_path / "out"
    proc = subprocess.run(
        [sys.executable, str(CONVERTER_PY),
         "--input", str(FIXTURE_DIR), "--output", str(out),
         "--chart", str(CHART_DIR), "--skip-verify"],
        capture_output=True, text=True, cwd=str(GITOPS_DIR),
    )
    assert proc.returncode == 0, f"converter failed:\n{proc.stdout}\n{proc.stderr}"

    merged: dict = {}
    for vf in (out / NS / "defaults.yaml", out / "base" / f"{RELEASE}.yaml",
               out / NS / "values" / f"{RELEASE}.yaml"):
        if vf.exists():
            merged = conv.deep_merge(merged, yaml.safe_load(vf.read_text(encoding="utf-8")) or {})

    got = _deployment(_helm_template(merged))["spec"]
    assert got["progressDeadlineSeconds"] == _doc()["spec"]["progressDeadlineSeconds"] == 120
    # Control: the sibling field that already worked must be unaffected.
    assert got["revisionHistoryLimit"] == _doc()["spec"]["revisionHistoryLimit"] == 5


def test_fidelity_checker_reports_it_as_lost():
    raw = _doc()
    rendered = yaml.safe_load(yaml.safe_dump(raw))
    rendered["spec"].pop("progressDeadlineSeconds")
    diffs = conv._fidelity_diff("Deployment", raw, rendered)
    assert any(d[0] == "LOST" and "progressDeadlineSeconds" in d[1] for d in diffs), f"diffs={diffs}"
