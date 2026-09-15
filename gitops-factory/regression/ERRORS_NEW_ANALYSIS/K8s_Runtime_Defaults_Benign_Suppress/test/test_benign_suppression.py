"""
Regression tests for ERRORS_NEW_ANALYSIS/K8s_Runtime_Defaults_Benign_Suppress.

The highest-leverage item in the batch — ~930 of 1,314 errors — and the most
dangerous, because it is a list of things to STOP LOOKING AT. error.txt is blunt
about the risk:

  "THREE ENTRIES IN THE SUPPRESSION LIST DO NOT BELONG AND MUST BE PULLED OUT
   BEFORE IMPLEMENTING. The bulk list was assembled by grouping adjacent field
   paths, and it swept in real losses:
     1. minReadySeconds (1) [...] K8s default is 0
     2. VCT labels (1) [...] PVCs otherwise lose governance labels
     3. allocateLoadBalancerNodePorts (62) [...] 3 are real LoadBalancer Services"

All three were FIXED rather than suppressed, in folders 7, 8 and 9 — plus
`parallelism` in folder 16, which the list also contains and which has the same
problem. So four of the list's entries are now moot: the fields are carried, and
there is nothing left to suppress.

WHAT THIS FOLDER IMPLEMENTS is deliberately small. Measured against a probe
manifest carrying every field on the report's benign list, most of the ladder
was ALREADY suppressed — by sanitize_object, by clean_annotations, by the
`{} == absent` rule, and by earlier folders in this batch. Nine remained; six of
those belong to folders 20, 21 and 22 and are left alone on purpose.

The rule added here is keyed on kind + exact path + THE VALUE. That is the whole
discipline: the report's list was assembled by grouping fields that sit near each
other in the pod spec, which is how it swept up three real losses. Grouping by
"does losing this do anything" cannot make that mistake.
"""
import importlib.util
import pathlib
import sys

import pytest

TEST_DIR = pathlib.Path(__file__).resolve().parent
GITOPS_DIR = TEST_DIR.parents[3]
CONVERTER_PY = GITOPS_DIR / "convert_to_universal_chart.py"


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


conv = _load(CONVERTER_PY, "convert_to_universal_chart")
benign = conv._is_benign_default_loss


# ---------------------------------------------------------------------------
# 1. What IS suppressed, and only at the stated value
# ---------------------------------------------------------------------------

def test_sessionAffinity_None_is_suppressed():
    assert benign("Service", ("spec", "sessionAffinity"), "None")


def test_sessionAffinity_ClientIP_is_NOT_suppressed():
    """The condition error.txt asks to verify: "confirm no Service sets ClientIP
    before applying it globally". A path-only rule would hide a lost ClientIP,
    which the previous round rated MEDIUM — "Load balancer session affinity
    broken". Scoping to the value is what makes the suppression safe."""
    assert not benign("Service", ("spec", "sessionAffinity"), "ClientIP")


def test_serviceaccount_secrets_and_pull_secrets_are_suppressed():
    assert benign("ServiceAccount", ("secrets",), [{"name": "sa-token-a1b2c"}])
    assert benign("ServiceAccount", ("imagePullSecrets",), [{"name": "sa-dockercfg-x9y8z"}])


def test_the_rule_is_scoped_to_the_kind():
    """A Deployment does not have spec.sessionAffinity, but if some CRD did, its
    loss would be nothing to do with a Service default."""
    assert not benign("Deployment", ("spec", "sessionAffinity"), "None")
    assert not benign("Pod", ("secrets",), [{"name": "x"}])


def test_the_rule_is_scoped_to_the_exact_path():
    """`sessionAffinity` nested somewhere else is a different field."""
    assert not benign("Service", ("spec", "template", "sessionAffinity"), "None")


# ---------------------------------------------------------------------------
# 2. The three the report wanted suppressed and that must never be
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind,path,value,folder", [
    ("Deployment", ("spec", "minReadySeconds"), 10, "7 — K8s default is 0, not 10"),
    ("StatefulSet", ("spec", "volumeClaimTemplates", "[data]", "metadata", "labels"),
     {"team": "platform-data"}, "8 — PVCs lose quota/cost-allocation labels"),
    ("Service", ("spec", "allocateLoadBalancerNodePorts"), False,
     "9 — an explicit false flips LoadBalancer behaviour"),
    ("Job", ("spec", "parallelism"), 4, "16 — a batch job's throughput"),
])
def test_the_entries_that_do_not_belong_are_not_suppressed(kind, path, value, folder):
    """Each of these appears in the report's bulk suppression list and each is a
    real loss, fixed in the folder named. If any ever starts returning True, a
    defect this batch fixed has been quietly re-hidden."""
    assert not benign(kind, path, value), f"suppressing a real loss — see folder {folder}"


# ---------------------------------------------------------------------------
# 3. The folders this one must not pre-empt
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind,path,value,folder", [
    ("Deployment", ("spec", "template", "metadata", "name"), "probe", "22"),
    ("Deployment", ("spec", "template", "metadata", "namespace"), "ns-probe", "22"),
    ("Secret", ("immutable",), False, "remaining noise"),
])
def test_other_folders_verdicts_are_left_open(kind, path, value, folder):
    """These are on the report's benign list and plausibly suppressible.
    Deciding them from inside a bulk rule is exactly the mistake that produced
    the bad entries above — each has its own folder with its own stated
    condition to check. If a later folder decides to suppress one, it updates
    this test along with the rule."""
    assert not benign(kind, path, value), f"folder {folder}'s verdict was pre-empted"


def test_folder_20_decided_to_PRESERVE_enableServiceLinks():
    """Decided after this folder deferred it. The Kubernetes default is TRUE, so
    an explicit `false` is a deliberate opt-out — it is now carried by the
    converter and rendered by the chart, so there is nothing to suppress."""
    assert not benign("Deployment",
                      ("spec", "template", "spec", "enableServiceLinks"), False)


def test_folder_21_decided_to_SUPPRESS_shareProcessNamespace_false():
    """The other half of the pair this folder refused to decide in bulk, and it
    went the opposite way: this field's Kubernetes default IS false, and the
    chart declares it in values.yaml so it cannot be rendered conditionally
    without inventing it on every pod. `true` is never suppressed."""
    assert benign("Deployment",
                  ("spec", "template", "spec", "shareProcessNamespace"), False)
    assert not benign("Deployment",
                      ("spec", "template", "spec", "shareProcessNamespace"), True)


# ---------------------------------------------------------------------------
# 4. The ladder items that were already handled elsewhere
# ---------------------------------------------------------------------------

def test_creationTimestamp_is_removed_at_the_source_not_suppressed():
    """166 errors in the ladder. sanitize_object strips it from the raw document
    on the way in, so it never reaches the comparison — a stronger answer than
    suppression, because it also keeps it out of the generated values."""
    cleaned = conv.sanitize_object({
        "apiVersion": "v1", "kind": "ConfigMap",
        "metadata": {"name": "c", "creationTimestamp": None}, "data": {}})
    assert "creationTimestamp" not in cleaned["metadata"]


def test_checksum_annotations_are_dropped_at_the_source():
    """~10-14 in the ladder; folder 19's subject, already handled by
    pod_template_annotations."""
    assert conv.pod_template_annotations(
        {"annotations": {"checksum/config": "abc", "prometheus.io/scrape": "true"}}) == \
        {"prometheus.io/scrape": "true"}


def test_argocd_instance_label_is_handled_by_folder_3():
    """92 in the ladder — the one label row the report marks unavoidable."""
    assert "argocd.argoproj.io/instance" in conv.FIDELITY_HELM_LABELS
    assert "argocd.argoproj.io/instance" in conv.CHART_OWNED_LABELS


def test_volumeclaimtemplate_runtime_fields_are_stripped_not_suppressed():
    """17 in the ladder; folder 8 removed them at the source and deliberately
    kept the LABELS, which the same ladder row would have taken with them."""
    cleaned = conv.sanitize_object({
        "apiVersion": "apps/v1", "kind": "StatefulSet", "metadata": {"name": "s"},
        "spec": {"volumeClaimTemplates": [{
            "metadata": {"name": "data", "labels": {"team": "x"}, "creationTimestamp": None},
            "status": {"phase": "Pending"}}]}})
    vct = cleaned["spec"]["volumeClaimTemplates"][0]
    assert "status" not in vct
    assert vct["metadata"]["labels"] == {"team": "x"}
