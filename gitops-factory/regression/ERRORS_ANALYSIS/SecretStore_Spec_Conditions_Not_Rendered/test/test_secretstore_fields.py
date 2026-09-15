"""
Regression tests for ERRORS_ANALYSIS/SecretStore_Spec_Conditions_Not_Rendered.

Reported: "SecretStore metadata labels missing, and spec-level condition fields
are absent. Why (labels): build_secretstore_entry never captures labels. Why
(conditions): secretstore.yaml template doesn't render registration or
conditions from values."

**The labels half is right. The conditions half is not** —
`build_secretstore_entry` already listed `conditions` among the four spec keys it
copied, and `secretstore.yaml` already had `{{- with $spec.conditions }}`. Both
halves of that path worked before this campaign touched anything, and the guard
test below passed before the fix as well as after.

What was actually wrong, beyond labels, is the same whitelist as the
ExternalSecret folders: four spec keys copied, everything else dropped. So
`conditions` survived only because somebody had thought to list it, and the next
SecretStore field would not have.

`ownerReferences` is a separate matter and the report has it backwards — see
ExternalSecret_Spec_Fields_Not_Captured's STATUS §1. It is stripped from every
object on purpose, because an ownerReference is a source-cluster `uid` that would
make the target cluster's garbage collector delete the object.

Shares ExternalSecret_Spec_Fields_Not_Captured's fixture: one raw manifest, one
root cause, three reports.
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
FIXTURE = (TEST_DIR.parent.parent / "ExternalSecret_Spec_Fields_Not_Captured"
           / "test" / "fixture" / "identity-auth" / "session-store.yaml")

CONDITIONS = [{"namespaceSelector": {"matchLabels": {"shared-store": "true"}}}]
LABELS = {"argocd.argoproj.io/instance": "vault-identity-auth", "team": "platform"}

# argocd.argoproj.io/instance is deliberately NOT carried: a later round moved it
# into CHART_OWNED_LABELS because ArgoCD stamps it at sync time, naming the
# Application that owns the object, and a chart rendering its own guess would
# fight ArgoCD tracking on the next sync.
CARRIED_LABELS = {k: v for k, v in LABELS.items() if k != "argocd.argoproj.io/instance"}


def _load_converter():
    spec = importlib.util.spec_from_file_location("convert_to_universal_chart", CONVERTER_PY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["convert_to_universal_chart"] = mod
    spec.loader.exec_module(mod)
    return mod


conv = _load_converter()


def _doc(kind):
    for d in yaml.safe_load_all(FIXTURE.read_text(encoding="utf-8")):
        if d and d.get("kind") == kind:
            return d
    raise AssertionError(f"no {kind} in fixture")


def _helm_template(values: dict, release="session-store"):
    values = dict(values)
    values.setdefault("externalSecretsApiVersion", "external-secrets.io/v1beta1")
    with tempfile.TemporaryDirectory() as tmp:
        vf = pathlib.Path(tmp) / "values.yaml"
        vf.write_text(yaml.safe_dump(values, sort_keys=False), encoding="utf-8")
        proc = subprocess.run(["helm", "template", release, str(CHART_DIR), "-f", str(vf)],
                              capture_output=True, text=True)
    assert proc.returncode == 0, f"helm template failed:\n{proc.stderr}"
    return [d for d in yaml.safe_load_all(proc.stdout) if d]


def _store(docs):
    for d in docs:
        if d.get("kind") == "SecretStore":
            return d
    raise AssertionError("chart rendered no SecretStore")


# --------------------------------------------------------------------------
# The half of the report that is wrong — guards, green before and after
# --------------------------------------------------------------------------

def test_conditions_were_already_captured():
    assert conv.build_secretstore_entry(_doc("SecretStore"))["conditions"] == CONDITIONS


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_conditions_were_already_rendered():
    values = {"workload": {"type": "none"}, "service": {"enabled": False},
              "secretStores": {"s": {"conditions": CONDITIONS, "provider": {"vault": {}}}}}
    assert _store(_helm_template(values))["spec"]["conditions"] == CONDITIONS


# --------------------------------------------------------------------------
# The half that is right — labels, plus the whitelist behind it
# --------------------------------------------------------------------------

def test_metadata_labels_are_captured():
    entry = conv.build_secretstore_entry(_doc("SecretStore"))
    assert entry.get("labels") == CARRIED_LABELS, f"labels dropped; entry keys = {sorted(entry)}"


def test_unlisted_spec_fields_survive():
    """The real defect: four keys were copied and everything else dropped, so
    `conditions` only survived because somebody had listed it."""
    doc = _doc("SecretStore")
    doc["spec"]["controller"] = "eso-controller-a"
    doc["spec"]["somethingEsoAddsLater"] = {"x": 1}
    entry = conv.build_secretstore_entry(doc)
    assert entry.get("controller") == "eso-controller-a"
    assert entry.get("somethingEsoAddsLater") == {"x": 1}


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_the_template_renders_labels_and_unlisted_fields():
    values = {"workload": {"type": "none"}, "service": {"enabled": False},
              "secretStores": {"session-store-vault": {
                  "labels": LABELS, "controller": "eso-controller-a",
                  "conditions": CONDITIONS, "provider": {"vault": {}}}}}
    ss = _store(_helm_template(values))
    for k, v in LABELS.items():
        assert ss["metadata"]["labels"].get(k) == v, (
            f"SecretStore lost label {k}; got {ss['metadata']['labels']}")
    assert ss["spec"].get("controller") == "eso-controller-a"
    assert ss["spec"]["conditions"] == CONDITIONS


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_identity_labels_still_win_over_declared_ones():
    values = {"workload": {"type": "none"}, "service": {"enabled": False},
              "secretStores": {"s": {"labels": {"app.kubernetes.io/name": "hijacked",
                                                "mine": "kept"},
                                     "provider": {"vault": {}}}}}
    labels = _store(_helm_template(values))["metadata"]["labels"]
    assert labels["app.kubernetes.io/name"] != "hijacked"
    assert labels["mine"] == "kept"


def test_owner_references_stay_stripped_on_purpose():
    """REFUTES the report's ownerReferences claim, same as the ExternalSecret
    folder: a source-cluster uid would make the target's garbage collector
    delete the object."""
    assert "ownerReferences" in conv.VOLATILE_METADATA_KEYS
    cleaned = conv.sanitize_object(_doc("SecretStore"))
    assert "ownerReferences" not in cleaned["metadata"]
