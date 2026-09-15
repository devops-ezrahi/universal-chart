"""
Regression tests for ERRORS_ANALYSIS/Empty_K8s_Default_Maps_Dropped.

~600 occurrences — the single largest source of noise in the fidelity report,
about a third of its ~1,960 Medium/Low LOST lines. `resources: {}`,
`securityContext: {}`, `creationTimestamp: null`, `dnsPolicy: ClusterFirst`,
`terminationGracePeriodSeconds: 30`.

error.txt's recommended disposition is to fix the **fidelity checker**, not the
converter, and that is right for the empty maps: `resources: {}` and an absent
`resources:` key produce byte-identical objects once the API server is done, so
reporting the difference is reporting nothing.

That noise is not free. It is the same report where the StatefulSet PVC-retention
defect (folder 1, irreversible data loss) and the ServiceAccount defect
(folder 6, pods that never start) were sitting, and 600 known-ignorable lines is
what makes a report get skimmed.

**One item in the group is NOT noise, and error.txt says to check it:**
`immutable: false` on ConfigMaps. `false` is the default so dropping it is safe,
but `immutable: true` being dropped would be a real change — an immutable
ConfigMap that comes back mutable. Verified: this repo has **no** ConfigMap with
an `immutable` key at all (STATUS.md §1), and the suppression is scoped so a
`true` could never be hidden anyway — it only ever skips `{}` and `[]`.

The Kubernetes-populated defaults are a different story from the empty maps and
are NOT suppressed: `dnsPolicy`, `terminationGracePeriodSeconds` and
`schedulerName` are all captured by `handle_workload` and round-trip today, so
there is nothing to hide. Tests below pin that.
"""
import importlib.util
import pathlib
import sys

import yaml

TEST_DIR = pathlib.Path(__file__).resolve().parent
GITOPS_DIR = TEST_DIR.parents[3]
CONVERTER_PY = GITOPS_DIR / "convert_to_universal_chart.py"
RAW_DIRS = ["namespaces", "namespaces-showcase", "namespaces-live-demo"]


def _load_converter():
    spec = importlib.util.spec_from_file_location("convert_to_universal_chart", CONVERTER_PY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["convert_to_universal_chart"] = mod
    spec.loader.exec_module(mod)
    return mod


conv = _load_converter()


def _pod(raw_spec, rendered_spec):
    def wrap(s):
        return {"apiVersion": "apps/v1", "kind": "Deployment",
                "metadata": {"name": "d"}, "spec": {"template": {"spec": s}}}
    return conv._fidelity_diff("Deployment", wrap(raw_spec), wrap(rendered_spec))


def _lost(diffs):
    return [d[1] for d in diffs if d[0] == "LOST"]


# --------------------------------------------------------------------------
# The suppression
# --------------------------------------------------------------------------

def test_an_empty_map_is_not_reported_as_lost():
    diffs = _pod({"containers": [{"name": "c", "resources": {}}]},
                 {"containers": [{"name": "c"}]})
    assert not _lost(diffs), f"empty resources reported as a loss; {diffs}"


def test_an_empty_list_is_not_reported_as_lost():
    diffs = _pod({"containers": [{"name": "c", "volumeMounts": []}]},
                 {"containers": [{"name": "c"}]})
    assert not _lost(diffs), f"empty volumeMounts reported as a loss; {diffs}"


def test_an_empty_security_context_is_not_reported():
    diffs = _pod({"securityContext": {}, "containers": [{"name": "c"}]},
                 {"containers": [{"name": "c"}]})
    assert not _lost(diffs), diffs


# --------------------------------------------------------------------------
# The limits — the line the suppression must not cross
# --------------------------------------------------------------------------

def test_a_populated_map_is_still_reported():
    diffs = _pod({"containers": [{"name": "c", "resources": {"limits": {"cpu": "1"}}}]},
                 {"containers": [{"name": "c"}]})
    assert any("resources" in p for p in _lost(diffs)), diffs


def test_false_zero_and_empty_string_are_still_reported():
    """These are NOT empty containers — they are deliberate values, and dropping
    one is exactly the defect folder 15 was about. `immutable: false` lives in
    this group and must stay visible if it is ever the thing that changed."""
    for key, val in (("immutable", False), ("terminationGracePeriodSeconds", 0),
                     ("nodeName", "")):
        raw = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "c"}, key: val}
        rendered = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "c"}}
        diffs = conv._fidelity_diff("ConfigMap", raw, rendered)
        assert any(key in p for p in _lost(diffs)), f"{key}={val!r} was suppressed: {diffs}"


def test_immutable_true_would_be_reported():
    raw = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "c"}, "immutable": True}
    rendered = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "c"}}
    assert any("immutable" in p for p in _lost(conv._fidelity_diff("ConfigMap", raw, rendered)))


def test_this_repo_has_no_immutable_configmaps():
    """error.txt asks for this one specifically before suppressing the group."""
    found = []
    for d in RAW_DIRS:
        root = GITOPS_DIR / d
        if not root.is_dir():
            continue
        for f in root.rglob("*.yaml"):
            for doc in yaml.safe_load_all(f.read_text(encoding="utf-8")):
                if doc and doc.get("kind") == "ConfigMap" and "immutable" in doc:
                    found.append(f"{f.name}: {doc['metadata']['name']}={doc['immutable']}")
    assert not [x for x in found if x.endswith("=True")], (
        "an immutable ConfigMap now exists; confirm it round-trips: " + str(found))


# --------------------------------------------------------------------------
# The K8s-populated defaults are NOT suppressed, because they are not lost
# --------------------------------------------------------------------------

def test_the_kubernetes_populated_defaults_still_round_trip():
    """293 `terminationGracePeriodSeconds: 30`, 227 `dnsPolicy: ClusterFirst`
    and 227 `schedulerName: default-scheduler` in this repo. handle_workload
    captures all three, so there is nothing to suppress — and suppressing them
    would hide a genuinely changed value."""
    doc = {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "d"},
           "spec": {"replicas": 1, "template": {"spec": {
               "dnsPolicy": "ClusterFirst",
               "terminationGracePeriodSeconds": 30,
               "schedulerName": "default-scheduler",
               "containers": [{"name": "c", "image": "nginx:1.27"}]}}}}
    ctx = conv.MicroserviceContext(namespace="ns", name="d")
    conv.handle_workload(doc, ctx, "Deployment")
    assert ctx.values["dnsPolicy"] == "ClusterFirst"
    assert ctx.values["terminationGracePeriodSeconds"] == 30
    assert ctx.values["schedulerName"] == "default-scheduler"


def test_a_changed_default_is_still_reported():
    diffs = _pod({"terminationGracePeriodSeconds": 30, "containers": [{"name": "c"}]},
                 {"terminationGracePeriodSeconds": 60, "containers": [{"name": "c"}]})
    assert any(d[0] == "CHANGED" and "terminationGracePeriodSeconds" in d[1] for d in diffs), diffs


def test_this_repo_has_no_empty_maps_to_suppress():
    """Scope check: the suppression is defensive here. 0 `resources: {}` and 0
    `securityContext: {}` in the raw manifests, so the showcase report does not
    move — the ~600 belong to the original project."""
    hits = 0
    for d in RAW_DIRS:
        root = GITOPS_DIR / d
        if not root.is_dir():
            continue
        for f in root.rglob("*.yaml"):
            text = f.read_text(encoding="utf-8")
            hits += text.count("resources: {}") + text.count("securityContext: {}")
    assert hits == 0, f"empty maps now exist in the raw manifests: {hits}"
