"""
Regression tests for ERRORS_NEW_ANALYSIS/StatefulSet_VolumeClaimTemplate_Labels_Lost.

One report row, two things inside it with OPPOSITE verdicts — error.txt is
emphatic that this is the trap:

  "THIS BUCKET CONTAINS TWO DIFFERENT THINGS AND THEY MUST BE SPLIT — the counts
   blur them together, which is why the same field path appears in both the
   'suppress' and 'fix' columns:
     1. RUNTIME BOILERPLATE — status, kind, apiVersion, creationTimestamp
        (~17-28). GENUINELY BENIGN. SUPPRESS these.
     2. LABELS — spec.volumeClaimTemplates[].metadata.labels. A REAL LOSS with a
        stated business consequence [...] FIX this.
   Suppressing the whole path would silently bury the labels defect inside the
   boilerplate suppression. Write the suppression rule against the specific
   runtime keys, never against the whole volumeClaimTemplates subtree."

Both halves are implemented, and both are pinned here — the labels tests would
pass just as well under a blanket subtree suppression, so the tests that matter
most are the ones asserting the suppression stayed narrow.

WHY THE LABELS ARE NOT COSMETIC. A StatefulSet's PVCs inherit the TEMPLATE's
labels and nothing else gives them any. Uncaptured, every generated volume
arrives with no team / environment / cost-centre metadata — which is what quota
enforcement and cost allocation select on. A PVC is also the one object here
that outlives its workload. The previous round rated this LOW
(ERRORS_ANALYSIS/Workload_Metadata_Labels_Lost #20); error.txt says that verdict
is superseded, and it is right.
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

VCT_LABELS = {"team": "platform-data", "environment": "dev", "cost-centre": "cc-4471"}


def _sts(vct_metadata=None, vct_extra=None):
    """A StatefulSet shaped like a live-cluster dump, which is what the
    converter is actually fed."""
    return {
        "apiVersion": "apps/v1", "kind": "StatefulSet", "metadata": {"name": "postgres"},
        "spec": {
            "serviceName": "postgres",
            "volumeClaimTemplates": [{
                "metadata": {"name": "data", **(vct_metadata or {})},
                "spec": {"accessModes": ["ReadWriteOnce"],
                         "resources": {"requests": {"storage": "1Gi"}}},
                **(vct_extra or {}),
            }],
            "template": {"metadata": {}, "spec": {
                "containers": [{"name": "postgres", "image": "postgres:16"}]}},
        },
    }


def _values(doc):
    ctx = conv.MicroserviceContext(namespace="platform-data", name="postgres")
    conv.handle_workload(conv.sanitize_object(doc), ctx, "StatefulSet")
    return ctx.values


# ---------------------------------------------------------------------------
# 1. The REAL LOSS half — labels are carried
# ---------------------------------------------------------------------------

def test_vct_labels_are_captured():
    v = _values(_sts(vct_metadata={"labels": VCT_LABELS}))
    assert v["volumeClaimTemplates"]["data"].get("labels") == VCT_LABELS


def test_vct_annotations_are_captured():
    v = _values(_sts(vct_metadata={"annotations": {"volume.beta.kubernetes.io/x": "y"}}))
    assert v["volumeClaimTemplates"]["data"].get("annotations") == \
        {"volume.beta.kubernetes.io/x": "y"}


@pytest.mark.parametrize("label", sorted(VCT_LABELS))
def test_each_governance_label_survives_individually(label):
    v = _values(_sts(vct_metadata={"labels": {label: VCT_LABELS[label]}}))
    assert v["volumeClaimTemplates"]["data"]["labels"] == {label: VCT_LABELS[label]}


def test_no_labels_key_when_the_template_has_none():
    assert "labels" not in _values(_sts())["volumeClaimTemplates"]["data"]


def test_chart_owned_identity_labels_are_still_excluded():
    """Same rule as pod-template labels: the chart computes these and carrying
    raw copies would contradict what selectors match on."""
    v = _values(_sts(vct_metadata={"labels": {
        **VCT_LABELS, "app": "postgres", "app.kubernetes.io/name": "postgres"}}))
    assert v["volumeClaimTemplates"]["data"]["labels"] == VCT_LABELS


# ---------------------------------------------------------------------------
# 2. The BENIGN half — runtime boilerplate is stripped, and only that
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("key,value", [
    ("status", {"phase": "Pending"}),
    ("kind", "PersistentVolumeClaim"),
    ("apiVersion", "v1"),
])
def test_embedded_runtime_fields_are_stripped(key, value):
    """`status` is server-populated and must never reach a chart; kind and
    apiVersion on an embedded template are redundant. The outer object already
    got this treatment — these are the same fields one level down, which is why
    they escaped."""
    cleaned = conv.sanitize_object(_sts(vct_extra={key: value}))
    assert key not in cleaned["spec"]["volumeClaimTemplates"][0]


def test_embedded_creationTimestamp_is_stripped():
    cleaned = conv.sanitize_object(_sts(vct_metadata={"creationTimestamp": None}))
    assert "creationTimestamp" not in cleaned["spec"]["volumeClaimTemplates"][0]["metadata"]


def test_the_strip_does_NOT_take_the_labels_with_it():
    """The single most important assertion in this file. error.txt's whole
    warning is that a rule written against the volumeClaimTemplates subtree
    would bury the labels defect inside the boilerplate suppression."""
    cleaned = conv.sanitize_object(_sts(
        vct_metadata={"labels": VCT_LABELS, "creationTimestamp": None},
        vct_extra={"status": {"phase": "Pending"}}))
    vct = cleaned["spec"]["volumeClaimTemplates"][0]
    assert vct["metadata"]["labels"] == VCT_LABELS
    assert vct["metadata"]["name"] == "data"
    assert "status" not in vct and "creationTimestamp" not in vct["metadata"]


def test_the_strip_does_not_touch_the_templates_spec():
    cleaned = conv.sanitize_object(_sts(vct_extra={"status": {"phase": "Pending"}}))
    assert cleaned["spec"]["volumeClaimTemplates"][0]["spec"] == {
        "accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": "1Gi"}}}


def test_a_statefulset_with_no_vcts_is_unharmed():
    doc = _sts()
    del doc["spec"]["volumeClaimTemplates"]
    assert "volumeClaimTemplates" not in conv.sanitize_object(doc)["spec"]


def test_other_kinds_are_untouched_by_the_statefulset_rule():
    dep = {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "x"},
           "spec": {"volumeClaimTemplates": [{"metadata": {"name": "d"},
                                              "status": {"phase": "Pending"}}]}}
    # Not valid Kubernetes, but it proves the rule is scoped to the kind rather
    # than firing on any object that happens to carry the key.
    assert "status" in conv.sanitize_object(dep)["spec"]["volumeClaimTemplates"][0]


# ---------------------------------------------------------------------------
# 3. Template
# ---------------------------------------------------------------------------

def test_template_renders_the_vct_labels(tmp_path):
    vf = tmp_path / "values.yaml"
    vf.write_text(yaml.safe_dump({
        "workload": {"type": "statefulset"},
        "image": {"repository": "postgres", "tag": "16"},
        "volumeClaimTemplates": {"data": {
            "accessModes": ["ReadWriteOnce"], "size": "1Gi", "labels": VCT_LABELS}},
    }), encoding="utf-8")
    out = subprocess.run(
        ["helm", "template", "postgres", str(CHART_DIR), "--namespace", "platform-data",
         "-f", str(vf)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert out.returncode == 0, out.stderr
    sts = next(d for d in yaml.safe_load_all(out.stdout) if d and d.get("kind") == "StatefulSet")
    vct = sts["spec"]["volumeClaimTemplates"][0]
    assert vct["metadata"]["labels"] == VCT_LABELS
    assert vct["metadata"]["name"] == "data"
    assert "status" not in vct


# ---------------------------------------------------------------------------
# 4. Corpus coverage — both halves, in one manifest
# ---------------------------------------------------------------------------

def test_the_corpus_vct_carries_labels_AND_runtime_noise():
    """Both must be present in the same template. Labels alone would leave the
    suppression untested; noise alone would leave the loss untested."""
    stses = [d for f in SHOWCASE.rglob("*.yaml")
             for d in yaml.safe_load_all(f.read_text(encoding="utf-8"))
             if d and d.get("kind") == "StatefulSet"]
    vcts = [v for d in stses for v in (d["spec"].get("volumeClaimTemplates") or [])]
    assert any((v.get("metadata") or {}).get("labels") for v in vcts), \
        "no volumeClaimTemplate in the corpus carries labels"
    assert any("status" in v or "creationTimestamp" in (v.get("metadata") or {}) for v in vcts), \
        "no volumeClaimTemplate in the corpus carries runtime noise to strip"
