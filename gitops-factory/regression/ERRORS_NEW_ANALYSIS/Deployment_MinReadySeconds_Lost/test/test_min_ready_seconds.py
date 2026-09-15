"""
Regression tests for ERRORS_NEW_ANALYSIS/Deployment_MinReadySeconds_Lost.

A one-occurrence defect that matters mainly because of HOW it was nearly lost.
The report contradicts itself, and error.txt says which side is right:

  line-by-line pass: "spec.minReadySeconds raw 10 - MEDIUM impact. [...] Losing
                      this means rolling updates will proceed immediately,
                      potentially causing brief downtime. Should be preserved."
  bulk suppression:  "Pod template defaults | ~12 | spec/template/spec/
                      enableServiceLinks, shareProcessNamespace, minReadySeconds"

  "Those cannot both be right, and the suppression is the sloppier judgement —
   it groups minReadySeconds with genuinely inert fields (enableServiceLinks:
   false, shareProcessNamespace: false) purely because they sit near each other
   in the pod spec."

DECISION: PRESERVE. The reasoning is not a matter of taste. `enableServiceLinks:
false` and `shareProcessNamespace: false` restate values that are already the
effective default for those workloads; `minReadySeconds`' Kubernetes default is
**0**, so an explicit 10 is a deliberate rollout-safety setting with no default
to fall back on. Dropping it changes behaviour.

It is also a textbook instance of the recurrence pattern this whole round is
about: minReadySeconds sits ONE LINE from progressDeadlineSeconds in
handle_workload, and the previous round fixed progressDeadlineSeconds and not
this. An allow-list architecture means "we fixed field X" never generalises to
field Y.
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


def _dep(**spec_extra):
    return {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": "gateway-tyk-gateway-dev"},
        "spec": {**spec_extra, "template": {"metadata": {}, "spec": {
            "containers": [{"name": "gateway", "image": "tykio/tyk-gateway:5.2"}]}}},
    }


def _values(doc, kind="Deployment"):
    ctx = conv.MicroserviceContext(namespace="shop-web", name="gateway")
    conv.handle_workload(doc, ctx, kind)
    return ctx.values


# ---------------------------------------------------------------------------
# 1. Converter
# ---------------------------------------------------------------------------

def test_minReadySeconds_is_captured():
    assert _values(_dep(minReadySeconds=10)).get("minReadySeconds") == 10


def test_an_explicit_zero_is_captured_not_treated_as_absent():
    """0 is the Kubernetes default, so a truthiness check would drop it — and
    dropping an explicit 0 is the whole of the previous round's
    Subtract_Defaults_Removes_Explicit_Zero. Captured with `in`, not `if`."""
    assert _values(_dep(minReadySeconds=0)).get("minReadySeconds") == 0


def test_absent_stays_absent():
    """Rendering a value the source never declared would be an invented field on
    every diff — the mirror-image failure of losing one."""
    assert "minReadySeconds" not in _values(_dep())


def test_it_did_not_get_swept_in_with_the_genuinely_inert_neighbours():
    """The specific mistake error.txt identifies. shareProcessNamespace is a
    pod-SPEC field and inert at its default; minReadySeconds is a Deployment-SPEC
    field and is not. They are not the same bucket and must not share a fate."""
    v = _values({
        "apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "g"},
        "spec": {"minReadySeconds": 10, "template": {"metadata": {}, "spec": {
            "shareProcessNamespace": False,
            "containers": [{"name": "g", "image": "x:1"}]}}},
    })
    assert v.get("minReadySeconds") == 10


# ---------------------------------------------------------------------------
# 2. Template
# ---------------------------------------------------------------------------

def _render(values, tmp_path):
    vf = tmp_path / "values.yaml"
    vf.write_text(yaml.safe_dump(values), encoding="utf-8")
    out = subprocess.run(
        ["helm", "template", "gateway", str(CHART_DIR), "--namespace", "shop-web", "-f", str(vf)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert out.returncode == 0, out.stderr
    return [d for d in yaml.safe_load_all(out.stdout) if d]


BASE = {"workload": {"type": "deployment"}, "image": {"repository": "tykio/tyk-gateway", "tag": "5.2"}}


def test_template_renders_minReadySeconds(tmp_path):
    docs = _render({**BASE, "minReadySeconds": 10}, tmp_path)
    dep = next(d for d in docs if d["kind"] == "Deployment")
    assert dep["spec"]["minReadySeconds"] == 10


def test_template_omits_it_when_unset(tmp_path):
    dep = next(d for d in _render(BASE, tmp_path) if d["kind"] == "Deployment")
    assert "minReadySeconds" not in dep["spec"]


def test_template_omits_it_for_the_empty_string_default(tmp_path):
    """values.yaml ships `minReadySeconds: ""`, matching progressDeadlineSeconds.
    `with` treats the empty string as falsey, so the field stays out."""
    dep = next(d for d in _render({**BASE, "minReadySeconds": ""}, tmp_path)
               if d["kind"] == "Deployment")
    assert "minReadySeconds" not in dep["spec"]


def test_the_schema_accepts_zero(tmp_path):
    """helm validates values against values.schema.json during template/lint.
    progressDeadlineSeconds uses `minimum: 1`, which would be wrong here — 0 is
    both legal and meaningful for this field."""
    out = subprocess.run(
        ["helm", "template", "gateway", str(CHART_DIR), "--namespace", "shop-web",
         "--set", "workload.type=deployment", "--set", "image.repository=tyk",
         "--set", "minReadySeconds=0"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert out.returncode == 0, out.stderr


def test_not_rendered_on_a_statefulset(tmp_path):
    """minReadySeconds on a StatefulSet is a different, beta-gated field. The
    converter reads it from Deployment spec only; the chart must not leak it
    into another kind's spec."""
    docs = _render({**BASE, "workload": {"type": "statefulset"}, "minReadySeconds": 10}, tmp_path)
    sts = next(d for d in docs if d["kind"] == "StatefulSet")
    assert "minReadySeconds" not in sts["spec"]


# ---------------------------------------------------------------------------
# 3. Corpus coverage
# ---------------------------------------------------------------------------

def test_a_corpus_deployment_declares_minReadySeconds():
    deps = [d for f in SHOWCASE.rglob("*.yaml")
            for d in yaml.safe_load_all(f.read_text(encoding="utf-8"))
            if d and d.get("kind") == "Deployment"]
    values = [d["spec"]["minReadySeconds"] for d in deps if "minReadySeconds" in d["spec"]]
    assert values, "no Deployment in the corpus declares minReadySeconds"
    assert any(v > 0 for v in values), \
        "only a zero value present — a lost 0 is invisible, so it proves nothing"
