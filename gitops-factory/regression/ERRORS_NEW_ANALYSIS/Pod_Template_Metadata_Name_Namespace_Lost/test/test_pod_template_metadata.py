"""
Regression tests for ERRORS_NEW_ANALYSIS/Pod_Template_Metadata_Name_Namespace_Lost.

Benign, and error.txt's reasoning is sound on each sub-field individually rather
than as a group:

  "* spec.template.metadata.name — Kubernetes IGNORES this field entirely. Pod
     names are generated from the controller name plus a hash; a name set on the
     pod template has no effect whatsoever. Safe to drop unconditionally.
   * spec.template.metadata.namespace — inherited from the object's own
     namespace. Setting it explicitly is redundant, and setting it to a DIFFERENT
     namespace is invalid. Safe to drop.
   * ownerReferences — operator-managed [...] Safe to drop, PROVIDED the operator
     is actually running in the target cluster."

The third has a dependency the first two do not, and it belongs to folder 23
(Thanos_OwnerReferences_Lost). Only `name`, `namespace` and the pod template's
own `creationTimestamp` are handled here — the last because it is pod-template
metadata by definition and appeared in the same measurement, even though the
report files it under the generic creationTimestamp row.

REMOVED AT THE BOUNDARY, NOT SUPPRESSED IN THE CHECKER. sanitize_object already
strips the OUTER object's runtime metadata and (since folder 8) the embedded
volumeClaimTemplates'. The pod template is the third embedded object with the
same problem, and handling it the same way keeps one rule in one place — and
keeps the junk out of the generated values, not merely out of the comparison.
"""
import importlib.util
import pathlib
import sys

import pytest
import yaml

TEST_DIR = pathlib.Path(__file__).resolve().parent
GITOPS_DIR = TEST_DIR.parents[3]
CONVERTER_PY = GITOPS_DIR / "convert_to_universal_chart.py"
SHOWCASE = GITOPS_DIR / "namespaces-showcase"


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


conv = _load(CONVERTER_PY, "convert_to_universal_chart")


def _wl(kind="Deployment", pod_meta=None):
    return {"apiVersion": "apps/v1", "kind": kind, "metadata": {"name": "thanos-query"},
            "spec": {"template": {
                "metadata": dict(pod_meta or {}),
                "spec": {"containers": [{"name": "thanos", "image": "thanos:0.34"}]}}}}


# ---------------------------------------------------------------------------
# 1. The three fields
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("key,value", [
    ("name", "thanos-query"),
    ("namespace", "observability"),
    ("creationTimestamp", None),
])
def test_each_inert_pod_template_field_is_stripped(key, value):
    cleaned = conv.sanitize_object(_wl(pod_meta={key: value, "labels": {"app": "thanos"}}))
    assert key not in cleaned["spec"]["template"]["metadata"]


def test_all_three_at_once():
    cleaned = conv.sanitize_object(_wl(pod_meta={
        "name": "thanos-query", "namespace": "observability", "creationTimestamp": None}))
    assert cleaned["spec"]["template"]["metadata"] == {}


@pytest.mark.parametrize("kind", ["Deployment", "StatefulSet", "DaemonSet"])
def test_it_applies_to_every_workload_kind(kind):
    cleaned = conv.sanitize_object(_wl(kind, pod_meta={"name": "x"}))
    assert "name" not in cleaned["spec"]["template"]["metadata"]


# ---------------------------------------------------------------------------
# 2. What must survive — the strip is next to real, load-bearing metadata
# ---------------------------------------------------------------------------

def test_pod_template_LABELS_survive():
    """The same metadata block carries the governance labels of folder 5. A rule
    written against `spec.template.metadata` rather than against the three
    specific keys would take them with it."""
    cleaned = conv.sanitize_object(_wl(pod_meta={
        "name": "thanos-query",
        "labels": {"team": "observability", "critical": "true"}}))
    assert cleaned["spec"]["template"]["metadata"]["labels"] == \
        {"team": "observability", "critical": "true"}


def test_pod_template_ANNOTATIONS_survive():
    """Including the injector annotations that made folder 5 a P1."""
    cleaned = conv.sanitize_object(_wl(pod_meta={
        "namespace": "observability",
        "annotations": {"sidecar.istio.io/inject": "false"}}))
    assert cleaned["spec"]["template"]["metadata"]["annotations"] == \
        {"sidecar.istio.io/inject": "false"}


def test_the_OBJECTS_own_name_is_untouched():
    """Only the pod TEMPLATE's name is inert. The object's own name is its
    identity, and stripping it would be catastrophic."""
    assert conv.sanitize_object(_wl(pod_meta={"name": "x"}))["metadata"]["name"] == \
        "thanos-query"


def test_a_JOB_pod_template_is_stripped_too():
    """Deferred while only Deployments had been observed losing these, then
    widened: a Job's pod template is the same PodTemplateSpec with the same inert
    keys. See `_pod_template_metadatas`."""
    doc = {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": "j"},
           "spec": {"template": {"metadata": {"name": "j", "namespace": "ns",
                                              "labels": {"team": "obs"}},
                                 "spec": {"containers": []}}}}
    meta = conv.sanitize_object(doc)["spec"]["template"]["metadata"]
    assert meta == {"labels": {"team": "obs"}}


def test_a_CRONJOB_pod_template_is_stripped_at_its_DEEPER_path():
    """The nesting is the part that silently fails: a CronJob's pod template is
    at spec.jobTemplate.spec.template, not spec.template. A rule written against
    the shallow path runs, reports nothing, and fixes nothing."""
    doc = {"apiVersion": "batch/v1", "kind": "CronJob", "metadata": {"name": "c"},
           "spec": {"jobTemplate": {"spec": {"template": {
               "metadata": {"name": "c", "namespace": "ns", "creationTimestamp": None,
                            "annotations": {"sidecar.istio.io/inject": "false"}},
               "spec": {"containers": []}}}}}}
    meta = conv.sanitize_object(doc)["spec"]["jobTemplate"]["spec"]["template"]["metadata"]
    assert meta == {"annotations": {"sidecar.istio.io/inject": "false"}}


def test_a_non_workload_kind_is_untouched():
    """Scoped to WORKLOAD_KINDS. A CRD with a `spec.template.metadata.name` that
    genuinely means something must not be edited."""
    doc = {"apiVersion": "argoproj.io/v1alpha1", "kind": "Sensor",
           "metadata": {"name": "s"},
           "spec": {"template": {"metadata": {"name": "trigger-name"}}}}
    assert conv.sanitize_object(doc)["spec"]["template"]["metadata"]["name"] == "trigger-name"


def test_a_workload_with_no_pod_template_metadata_is_unharmed():
    doc = _wl()
    del doc["spec"]["template"]["metadata"]
    assert conv.sanitize_object(doc)["spec"]["template"]["spec"]["containers"]


# ---------------------------------------------------------------------------
# 3. ownerReferences belongs to folder 23, not here
# ---------------------------------------------------------------------------

def test_pod_template_ownerReferences_were_DEFERRED_here_and_DECIDED_in_folder_23():
    """This test asserted the opposite until folder 23 ran, which is the deferred
    verdict mechanism working rather than a regression.

    error.txt groups ownerReferences into the same report lines (112-120) but
    their benign verdict has a DEPENDENCY the other three do not: "PROVIDED the
    operator is actually running in the target cluster". Folder 23 decided it,
    and on a stronger ground that needs no operator — an ownerReference is a
    `uid` from the SOURCE cluster, and pod template metadata is stamped onto
    every pod the controller creates, so carrying it hands each new pod an owner
    that does not exist and the collector deletes them.

    Kept here, pointed at folder 23, so the four keys of one rule are visible in
    the folder that owns three of them."""
    cleaned = conv.sanitize_object(_wl(pod_meta={
        "name": "thanos-query",
        "ownerReferences": [{"kind": "Thanos", "name": "thanos", "uid": "1111"}]}))
    assert "ownerReferences" not in cleaned["spec"]["template"]["metadata"]
    assert "name" not in cleaned["spec"]["template"]["metadata"]


def test_the_OBJECTS_own_ownerReferences_are_still_stripped_as_before():
    """Unchanged pre-existing behaviour, via VOLATILE_METADATA_KEYS — recorded so
    folder 23 knows which half is already handled."""
    doc = _wl()
    doc["metadata"]["ownerReferences"] = [{"kind": "Thanos", "name": "thanos"}]
    assert "ownerReferences" not in conv.sanitize_object(doc)["metadata"]


# ---------------------------------------------------------------------------
# 4. Corpus
# ---------------------------------------------------------------------------

def test_the_corpus_carries_the_inert_pod_template_metadata():
    """A corpus without them cannot tell "handled" from "never encountered"."""
    found = set()
    for f in SHOWCASE.rglob("*.yaml"):
        for d in yaml.safe_load_all(f.read_text(encoding="utf-8")):
            if not d or d.get("kind") not in ("Deployment", "StatefulSet", "DaemonSet"):
                continue
            meta = (d["spec"]["template"].get("metadata") or {})
            found |= {k for k in ("name", "namespace", "creationTimestamp") if k in meta}
    assert found == {"name", "namespace", "creationTimestamp"}, found


def test_the_count_discrepancy_the_report_flags_is_recorded_not_resolved():
    """error.txt: "an earlier summary claimed 98 'Pod template metadata' errors;
    the actual report contains 17 template.metadata.name entries." Both numbers
    are from runs this repo does not have. Folder 1's run id is what stops the
    next pair being compared by accident; nothing here can reconcile these two."""
    assert True
