"""
Regression tests for ERRORS_ANALYSIS/Legacy_ServiceAccount_Field_Lost.

Reported: the deprecated pod-spec key `serviceAccount:` is dropped, 15-24
occurrences. The report's own Discoveries section already calls it a false
positive with "Severity: NONE", and it is right — Kubernetes treats
`serviceAccount` as a deprecated alias for `serviceAccountName` and the API
server keeps both populated with the same value. Rendering only the modern key
is correct.

The brief says the fix for a genuine false positive is usually to teach the
FIDELITY CHECKER to stop reporting it, and that is what this folder does. Before
this change the checker compared the raw pod spec key-by-key, so a raw manifest
carrying both keys produced a permanent `LOST spec.template.spec.serviceAccount`
on every workload — noise that trains people to skim the report, which is how
the real ServiceAccount defect (folder 6, pods pointing at a ServiceAccount that
does not exist) got missed in the first place.

The suppression is deliberately narrow: it applies ONLY when the modern key is
present with the SAME value. A pod spec where the two disagree, or one carrying
only the legacy key, is still reported — because those are cases where something
really is wrong.

Grep proof (STATUS.md §1): 0 pod specs in this repo use the legacy key; 265 use
`serviceAccountName`. So nothing here is affected either way, and this is a
checker change, not a converter one.
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


def _deployment(pod_spec: dict) -> dict:
    return {"apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": "grafana"},
            "spec": {"template": {"spec": pod_spec}}}


def _diff(raw_pod, rendered_pod):
    return conv._fidelity_diff("Deployment", _deployment(raw_pod), _deployment(rendered_pod))


def _lost(diffs):
    return [d[1] for d in diffs if d[0] == "LOST"]


# --------------------------------------------------------------------------
# The grep proof
# --------------------------------------------------------------------------

def test_no_pod_spec_in_this_repo_uses_the_legacy_key():
    legacy = []
    for d in RAW_DIRS:
        root = GITOPS_DIR / d
        if not root.is_dir():
            continue
        for f in root.rglob("*.yaml"):
            for doc in yaml.safe_load_all(f.read_text(encoding="utf-8")):
                if not doc:
                    continue
                spec = doc.get("spec") or {}
                for path in (("template", "spec"),
                             ("jobTemplate", "spec", "template", "spec")):
                    cur = spec
                    for k in path:
                        cur = cur.get(k) if isinstance(cur, dict) else None
                    if isinstance(cur, dict) and cur.get("serviceAccount"):
                        legacy.append(f"{f.relative_to(GITOPS_DIR)}: {doc['metadata']['name']}")
    assert not legacy, (
        "the deprecated serviceAccount key now appears in the raw manifests; "
        "re-check this folder's classification:\n" + "\n".join(legacy))


# --------------------------------------------------------------------------
# The suppression, and its limits
# --------------------------------------------------------------------------

def test_the_legacy_alias_is_not_reported_when_the_modern_key_agrees():
    diffs = _diff({"serviceAccount": "grafana-sa", "serviceAccountName": "grafana-sa"},
                  {"serviceAccountName": "grafana-sa"})
    assert not any("serviceAccount" in p for p in _lost(diffs)), (
        f"the deprecated alias is still reported as a loss; diffs={diffs}")


def test_a_disagreement_between_the_two_keys_is_still_reported():
    """The line the suppression must not cross. If the two keys hold different
    values, something upstream is wrong and the checker must say so."""
    diffs = _diff({"serviceAccount": "old-sa", "serviceAccountName": "new-sa"},
                  {"serviceAccountName": "new-sa"})
    assert any("serviceAccount" in p for p in _lost(diffs)), (
        f"a disagreeing legacy value was silently suppressed; diffs={diffs}")


def test_the_legacy_key_alone_is_still_reported():
    """A pod spec with only the deprecated key and no modern one is a real
    loss — there is nothing else carrying the value."""
    diffs = _diff({"serviceAccount": "grafana-sa"}, {})
    assert any("serviceAccount" in p for p in _lost(diffs)), f"diffs={diffs}"


def test_the_modern_key_is_still_compared():
    """The guard that keeps folder 6's defect visible. Suppressing the alias
    must not suppress the field that actually matters."""
    diffs = _diff({"serviceAccount": "grafana-sa", "serviceAccountName": "grafana-sa"}, {})
    assert any(p.endswith("serviceAccountName") for p in _lost(diffs)), (
        f"serviceAccountName is no longer compared; diffs={diffs}")


def test_a_changed_modern_value_is_still_reported():
    diffs = _diff({"serviceAccount": "grafana-sa", "serviceAccountName": "grafana-sa"},
                  {"serviceAccountName": "storefront"})
    assert any(d[0] == "CHANGED" and "serviceAccountName" in d[1] for d in diffs), f"diffs={diffs}"


def test_the_suppression_applies_to_job_and_cronjob_pod_specs_too():
    raw = {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": "j"},
           "spec": {"template": {"spec": {"serviceAccount": "sa", "serviceAccountName": "sa"}}}}
    rendered = yaml.safe_load(yaml.safe_dump(raw))
    rendered["spec"]["template"]["spec"].pop("serviceAccount")
    diffs = conv._fidelity_diff("Job", raw, rendered)
    assert not any("serviceAccount" in d[1] for d in diffs if d[0] == "LOST"), f"diffs={diffs}"
