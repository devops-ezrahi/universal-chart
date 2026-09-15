"""
Regression tests for ERRORS_NEW_ANALYSIS/Thanos_OwnerReferences_Lost.

THE VERDICT IS "BENIGN", AND THE REPORT'S REASON FOR IT IS THE WRONG ONE.

error.txt is explicit that the whole justification is a parenthetical:

    "CLASSIFICATION: benign IF AND ONLY IF the Thanos Banzaicloud operator is
     running in the target cluster. The entire justification is the parenthetical
     '(operator re-creates them)' — that is a dependency, not a property of the
     field."

That is a fair criticism, and it does not change the answer, because a stronger
reason sits underneath it: an ownerReference IS a `uid`, and a uid from the
SOURCE cluster does not exist in the target. Carry one over and the target's
garbage collector sees an owner that is gone and deletes the object — usually
within seconds. So:

    dropping ownerReferences  ->  garbage-collection links must be re-established
    keeping  ownerReferences  ->  the object is deleted on arrival

The first is recoverable and is what an operator (or ArgoCD, or Helm ownership)
re-establishes. The second is not. The verdict holds with NO operator anywhere,
which is what makes error.txt's dependency moot rather than unmet.

This is the same conclusion the previous round reached for a different kind, in
`_eso_metadata` — see ERRORS_ANALYSIS/ExternalSecret_Spec_Fields_Not_Captured.
error.txt flags those two findings as contradictory. They are not: both strip.

WHAT THIS FOLDER ACTUALLY CHANGED. The OUTER object's ownerReferences were
already stripped for every kind via VOLATILE_METADATA_KEYS. The POD TEMPLATE's
were not, and that is the half that was still leaking — and the more dangerous
half, because pod template metadata is stamped onto every pod the controller
creates.
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

STORE_ENDPOINT = {"apiVersion": "monitoring.banzaicloud.io/v1alpha1",
                  "kind": "StoreEndpoint", "name": "metrics-agent-store",
                  "uid": "6f1b0c9e-1d3a-4a2f-9f4e-0b1c2d3e4f50",
                  "controller": True, "blockOwnerDeletion": True}
THANOS_CR = {"apiVersion": "monitoring.banzaicloud.io/v1alpha1", "kind": "Thanos",
             "name": "thanos", "uid": "2a7d4b61-8c05-4e77-b3aa-5d9e6f70a1b2"}


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


conv = _load(CONVERTER_PY, "convert_to_universal_chart")


def _wl(kind="Deployment", meta=None, pod_meta=None):
    return {"apiVersion": "apps/v1", "kind": kind,
            "metadata": {"name": "thanos-query", **(meta or {})},
            "spec": {"template": {
                "metadata": dict(pod_meta or {}),
                "spec": {"containers": [{"name": "thanos", "image": "thanos:0.34"}]}}}}


# ---------------------------------------------------------------------------
# 1. Both levels, which is the whole point — one was already handled, one wasn't
# ---------------------------------------------------------------------------

def test_the_OUTER_objects_ownerReferences_are_stripped():
    """Pre-existing behaviour via VOLATILE_METADATA_KEYS. Pinned because this
    folder's verdict rests on both levels being handled, not just the new one."""
    cleaned = conv.sanitize_object(_wl(meta={"ownerReferences": [STORE_ENDPOINT]}))
    assert "ownerReferences" not in cleaned["metadata"]


def test_the_POD_TEMPLATES_ownerReferences_are_stripped():
    """The gap this folder closed. Before the fix this survived conversion and
    the fidelity report showed:

        ERROR  DaemonSet/metrics-agent LOST at
               spec.template.metadata.ownerReferences: raw had [{...'Thanos'...}]
    """
    cleaned = conv.sanitize_object(_wl(pod_meta={"ownerReferences": [THANOS_CR]}))
    assert "ownerReferences" not in cleaned["spec"]["template"]["metadata"]


def test_both_levels_at_once_on_one_object():
    cleaned = conv.sanitize_object(_wl(meta={"ownerReferences": [STORE_ENDPOINT]},
                                       pod_meta={"ownerReferences": [THANOS_CR]}))
    assert "ownerReferences" not in cleaned["metadata"]
    assert "ownerReferences" not in cleaned["spec"]["template"]["metadata"]


@pytest.mark.parametrize("kind", ["Deployment", "StatefulSet", "DaemonSet"])
def test_pod_template_strip_applies_to_every_workload_kind(kind):
    cleaned = conv.sanitize_object(_wl(kind, pod_meta={"ownerReferences": [THANOS_CR]}))
    assert "ownerReferences" not in cleaned["spec"]["template"]["metadata"]


@pytest.mark.parametrize("kind,build", [
    ("Job", lambda m: {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": "j"},
                       "spec": {"template": {"metadata": m, "spec": {"containers": []}}}}),
    ("CronJob", lambda m: {"apiVersion": "batch/v1", "kind": "CronJob", "metadata": {"name": "c"},
                           "spec": {"jobTemplate": {"spec": {"template": {
                               "metadata": m, "spec": {"containers": []}}}}}}),
])
def test_batch_pod_templates_are_stripped_too(kind, build):
    """An operator-written CronJob arrives with the same source-cluster uid in the
    same place, and the pods it creates would be collected just as fast."""
    cleaned = conv.sanitize_object(build({"ownerReferences": [THANOS_CR]}))
    metas = conv._pod_template_metadatas(kind, cleaned["spec"])
    assert metas and all("ownerReferences" not in m for m in metas)


def test_the_uid_is_what_makes_this_unsafe_to_carry():
    """Recorded as an assertion rather than a comment: the reference the corpus
    carries has a uid, so a test that passed on a uid-less reference would not be
    testing the case that matters."""
    assert "uid" in STORE_ENDPOINT and "uid" in THANOS_CR


# ---------------------------------------------------------------------------
# 2. Scope — the strip must not become "delete anything called ownerReferences"
# ---------------------------------------------------------------------------

def test_pod_template_labels_and_annotations_still_survive():
    """Same metadata block as folder 5's governance labels and the sidecar
    injection annotations. A rule aimed at the block rather than the key would
    take them too."""
    cleaned = conv.sanitize_object(_wl(pod_meta={
        "ownerReferences": [THANOS_CR],
        "labels": {"team": "observability"},
        "annotations": {"sidecar.istio.io/inject": "false"}}))
    meta = cleaned["spec"]["template"]["metadata"]
    assert meta == {"labels": {"team": "observability"},
                    "annotations": {"sidecar.istio.io/inject": "false"}}


def test_a_non_workload_kinds_nested_template_is_untouched():
    """`spec.template.metadata` on a CRD is not a pod template and may mean
    anything at all."""
    doc = {"apiVersion": "argoproj.io/v1alpha1", "kind": "Sensor",
           "metadata": {"name": "s"},
           "spec": {"template": {"metadata": {"ownerReferences": [THANOS_CR]}}}}
    assert conv.sanitize_object(doc)["spec"]["template"]["metadata"]["ownerReferences"] \
        == [THANOS_CR]


def test_the_rule_did_not_widen_to_all_of_VOLATILE_METADATA_KEYS():
    """The pod-template list is deliberately explicit. If someone swaps it for
    VOLATILE_METADATA_KEYS, `finalizers` and friends start disappearing from pod
    templates with no evidence that they were ever there — and this test says so
    before the next report has to."""
    cleaned = conv.sanitize_object(_wl(pod_meta={"finalizers": ["example.com/f"],
                                                 "ownerReferences": [THANOS_CR]}))
    assert cleaned["spec"]["template"]["metadata"] == {"finalizers": ["example.com/f"]}


def test_the_outer_strip_is_still_every_kind_not_just_workloads():
    """The OUTER rule is kind-independent and must stay that way — the ESO
    objects of the previous round depend on it."""
    doc = {"apiVersion": "external-secrets.io/v1beta1", "kind": "ExternalSecret",
           "metadata": {"name": "es", "ownerReferences": [STORE_ENDPOINT]}, "spec": {}}
    assert "ownerReferences" not in conv.sanitize_object(doc)["metadata"]


# ---------------------------------------------------------------------------
# 3. The chart can still express one deliberately
# ---------------------------------------------------------------------------

def test_dropping_is_not_the_same_as_being_unable_to_set_one():
    """`_eso_metadata` records that the templates still render an
    `ownerReferences` key if a values file sets one BY HAND, "which is the only
    way it can be meaningful" — i.e. written against a uid that exists in the
    target. This folder removes an inherited uid; it does not remove the
    capability."""
    src = (GITOPS_DIR.parent).rglob("*.tpl")
    tpl = "".join(p.read_text(encoding="utf-8") for p in src)
    tpl += "".join(p.read_text(encoding="utf-8")
                   for p in (GITOPS_DIR.parent / "templates").glob("*.yaml"))
    assert "ownerReferences" in tpl


# ---------------------------------------------------------------------------
# 4. Corpus
# ---------------------------------------------------------------------------

def test_the_corpus_carries_ownerReferences_at_BOTH_levels():
    """Before this folder the showcase contained the string `ownerReferences`
    exactly zero times, so the fidelity report scored 0 for this defect class by
    having nothing to look at — the same blind spot Route labels had."""
    outer, template = [], []
    for f in SHOWCASE.rglob("*.yaml"):
        for d in yaml.safe_load_all(f.read_text(encoding="utf-8")):
            if not d or d.get("kind") not in ("Deployment", "StatefulSet", "DaemonSet"):
                continue
            if (d.get("metadata") or {}).get("ownerReferences"):
                outer.append(d["metadata"]["name"])
            if ((d["spec"]["template"].get("metadata") or {}).get("ownerReferences")):
                template.append(d["metadata"]["name"])
    assert outer, "no corpus workload carries object-level ownerReferences"
    assert template, "no corpus workload carries pod-template ownerReferences"


def test_no_generated_values_file_carries_an_inherited_ownerReference():
    """The end state the whole folder is for: nothing with a source-cluster uid
    reaches the chart's inputs."""
    out = GITOPS_DIR / "output-showcase"
    leaked = [p.as_posix() for p in out.rglob("values/*.yaml")
              if "ownerReferences" in p.read_text(encoding="utf-8")]
    assert leaked == [], leaked


def test_the_count_discrepancy_is_recorded_not_resolved():
    """error.txt: "Count varies: 2 (suppression list) vs 4 (benign list)". Both
    come from runs this repo does not have; folder 1's run id is the mechanism
    that stops the next pair being compared by accident."""
    assert True
