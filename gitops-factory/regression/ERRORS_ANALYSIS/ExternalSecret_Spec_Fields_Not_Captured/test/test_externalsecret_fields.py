"""
Regression tests for ERRORS_ANALYSIS/ExternalSecret_Spec_Fields_Not_Captured.

Reported lost: `metadata.labels`, `metadata.ownerReferences`, `spec.deliverySpec`,
`spec.target.template.labels` and `spec.target.template.annotations`.

Half the report is right and half is not — see STATUS.md §1 for the audit. What
is actually true:

  * metadata.labels          - not captured, not rendered.  REAL
  * metadata.ownerReferences - dropped ON PURPOSE, and the report has the
                               consequence backwards. An ownerReference is a
                               `uid` from the SOURCE cluster; carried into the
                               target it names an owner that does not exist and
                               the garbage collector deletes the object.
                               sanitize_object strips it from every object for
                               that reason.  REFUTED
  * spec.deliverySpec        - NOT captured (the report says line 493 captures
                               it; it does not) and not rendered.  REAL
  * spec.target.template.*   - already works. `target.template` is captured as
                               one blob and rendered with toYaml, so its labels
                               AND annotations round-trip today. The report is
                               wrong on both counts, and the guard tests below
                               pin the existing behaviour.

The root cause behind the three real ones is a **whitelist**: both
build_externalsecret_entry and build_secretstore_entry copy a fixed list of spec
keys and silently drop everything else, and neither reads metadata.labels. Any
ESO field the list does not name — today `deliverySpec` and `managementPolicy`,
tomorrow whatever the operator adds next — disappears without a word. The fix
inverts that: carry every spec key through unless there is a reason not to.
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
FIXTURE = FIXTURE_DIR / "identity-auth" / "session-store.yaml"

NS = "identity-auth"
RELEASE = "session-store"
LABELS = {"argocd.argoproj.io/instance": "vault-identity-auth", "team": "platform"}

# What survives conversion. `argocd.argoproj.io/instance` was moved into
# CHART_OWNED_LABELS by a later round: ArgoCD stamps it at sync time, naming the
# Application that owns the object, so a chart rendering its own guess would
# fight ArgoCD's tracking on the next sync. The report's field table calls it the
# one "Unavoidable — ArgoCD injects at deploy" row.
#
# This file's docstring still frames that label as a real loss; the reasoning
# there was about ArgoCD *ceasing to manage* the object, which is what happens
# when the label is stripped from a live object — not when a chart declines to
# invent one. Carrying the custom governance labels is what this folder is for,
# and that is unchanged and still asserted.
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


def _ctx():
    return conv.MicroserviceContext(namespace=NS, name=RELEASE)


def _helm_template(values: dict, release=RELEASE):
    values = dict(values)
    values.setdefault("externalSecretsApiVersion", "external-secrets.io/v1beta1")
    with tempfile.TemporaryDirectory() as tmp:
        vf = pathlib.Path(tmp) / "values.yaml"
        vf.write_text(yaml.safe_dump(values, sort_keys=False), encoding="utf-8")
        proc = subprocess.run(
            ["helm", "template", release, str(CHART_DIR), "-f", str(vf)],
            capture_output=True, text=True,
        )
    assert proc.returncode == 0, f"helm template failed:\n{proc.stderr}"
    return [d for d in yaml.safe_load_all(proc.stdout) if d]


def _rendered(docs, kind):
    for d in docs:
        if d.get("kind") == kind:
            return d
    raise AssertionError(f"chart rendered no {kind}")


# --------------------------------------------------------------------------
# Guards — what the report calls broken but is not. These passed before the fix.
# --------------------------------------------------------------------------

def test_target_template_labels_and_annotations_already_round_trip():
    entry = conv.build_externalsecret_entry(_doc("ExternalSecret"), _ctx())
    tpl = entry["target"]["template"]
    assert tpl["labels"] == {"generated-by": "external-secrets"}
    assert tpl["annotations"] == {"reloader.stakater.com/match": "true"}


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_the_chart_already_renders_the_whole_target_template():
    tpl = {"labels": {"a": "b"}, "annotations": {"c": "d"}}
    values = {"workload": {"type": "none"}, "service": {"enabled": False},
              "externalSecrets": {"es": {"secretStoreRef": {"name": "s", "kind": "SecretStore"},
                                         "target": {"template": tpl}}}}
    got = _rendered(_helm_template(values), "ExternalSecret")["spec"]["target"]["template"]
    assert got == tpl


def test_secretstore_conditions_already_round_trip():
    """The SecretStore folder claims the template does not render conditions.
    It does — `{{- with $spec.conditions }}` — and the converter captures them."""
    entry = conv.build_secretstore_entry(_doc("SecretStore"))
    assert entry["conditions"] == [
        {"namespaceSelector": {"matchLabels": {"shared-store": "true"}}}]


# --------------------------------------------------------------------------
# The real losses — capture
# --------------------------------------------------------------------------

def test_externalsecret_metadata_labels_are_captured():
    entry = conv.build_externalsecret_entry(_doc("ExternalSecret"), _ctx())
    assert entry.get("labels") == CARRIED_LABELS, (
        f"metadata.labels dropped; entry keys = {sorted(entry)}")


def test_owner_references_are_deliberately_dropped_before_any_handler_runs():
    """REFUTES the report's issue 18. An ownerReference is a `uid`, and a uid
    from the SOURCE cluster does not exist in the target — carry it over and the
    garbage collector sees a missing owner and deletes the object, usually
    within seconds. sanitize_object strips it from every object for exactly that
    reason. Dropping it does not break garbage collection; keeping it would
    break the object."""
    assert "ownerReferences" in conv.VOLATILE_METADATA_KEYS
    raw = _doc("ExternalSecret")
    assert raw["metadata"]["ownerReferences"], "fixture must carry one to be meaningful"
    cleaned = conv.sanitize_object(raw)
    assert "ownerReferences" not in cleaned["metadata"]
    entry = conv.build_externalsecret_entry(cleaned, _ctx())
    assert "ownerReferences" not in entry


def test_unlisted_spec_fields_are_carried_through():
    """deliverySpec and managementPolicy are the two the report names, but the
    point is the whitelist itself: anything ESO adds later must survive too."""
    doc = _doc("ExternalSecret")
    doc["spec"]["somethingEsoAddedLater"] = {"x": 1}
    entry = conv.build_externalsecret_entry(doc, _ctx())
    assert entry.get("deliverySpec") == {"delay": "5m"}, f"entry keys = {sorted(entry)}"
    assert entry.get("managementPolicy") == "Default"
    assert entry.get("somethingEsoAddedLater") == {"x": 1}, (
        "the spec whitelist is still dropping unknown fields")


def test_secretstore_metadata_labels_are_captured():
    entry = conv.build_secretstore_entry(_doc("SecretStore"))
    assert entry.get("labels") == CARRIED_LABELS, f"entry keys = {sorted(entry)}"


def test_the_secret_store_ref_remap_still_works():
    """Control: secretStoreRef must keep going through name_map, and the
    pass-through must not clobber the remapped value."""
    ctx = _ctx()
    ctx.name_map["SecretStore"]["session-store-vault"] = "renamed-vault"
    entry = conv.build_externalsecret_entry(_doc("ExternalSecret"), ctx)
    assert entry["secretStoreRef"] == {"name": "renamed-vault", "kind": "SecretStore"}


# --------------------------------------------------------------------------
# The real losses — render
# --------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_both_templates_render_labels_owner_references_and_extra_spec_fields():
    values = {
        "workload": {"type": "none"}, "service": {"enabled": False},
        "secretStores": {"session-store-vault": {
            "labels": LABELS,
            "ownerReferences": [{"apiVersion": "apps/v1", "kind": "Deployment",
                                 "name": "session-store", "uid": "u1"}],
            "conditions": [{"namespaceSelector": {"matchLabels": {"shared-store": "true"}}}],
            "provider": {"vault": {"server": "https://v", "path": "secret"}}}},
        "externalSecrets": {"session-store-signing-key": {
            "labels": LABELS,
            "ownerReferences": [{"apiVersion": "apps/v1", "kind": "Deployment",
                                 "name": "session-store", "uid": "u2"}],
            "managementPolicy": "Default",
            "deliverySpec": {"delay": "5m"},
            "secretStoreRef": {"name": "session-store-vault", "kind": "SecretStore"}}},
    }
    docs = _helm_template(values)

    es = _rendered(docs, "ExternalSecret")
    for k, v in LABELS.items():
        assert es["metadata"]["labels"].get(k) == v, (
            f"ExternalSecret lost label {k}; got {es['metadata']['labels']}")
    assert es["metadata"].get("ownerReferences"), "ExternalSecret lost ownerReferences"
    assert es["spec"].get("managementPolicy") == "Default"
    assert es["spec"].get("deliverySpec") == {"delay": "5m"}

    ss = _rendered(docs, "SecretStore")
    for k, v in LABELS.items():
        assert ss["metadata"]["labels"].get(k) == v, (
            f"SecretStore lost label {k}; got {ss['metadata']['labels']}")
    assert ss["metadata"].get("ownerReferences"), "SecretStore lost ownerReferences"
    assert ss["spec"].get("conditions")


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_the_charts_own_identity_labels_still_win():
    """commonLabels/user labels may ADD, never override the chart's identity
    labels — the same rule universal-chart.labels enforces everywhere else."""
    values = {"workload": {"type": "none"}, "chartLabels": True, "service": {"enabled": False},
              "externalSecrets": {"es": {
                  "labels": {"app.kubernetes.io/name": "hijacked", "mine": "kept"},
                  "secretStoreRef": {"name": "s", "kind": "SecretStore"}}}}
    labels = _rendered(_helm_template(values), "ExternalSecret")["metadata"]["labels"]
    assert labels["app.kubernetes.io/name"] != "hijacked"
    assert labels["mine"] == "kept"


# --------------------------------------------------------------------------
# End to end
# --------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_round_trip_no_eso_field_is_lost(tmp_path):
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

    docs = _helm_template(merged)
    es, ss = _rendered(docs, "ExternalSecret"), _rendered(docs, "SecretStore")
    raw_es, raw_ss = _doc("ExternalSecret"), _doc("SecretStore")

    for k, v in raw_es["metadata"]["labels"].items():
        if k == "argocd.argoproj.io/instance":
            # Deliberately not rendered — see CARRIED_LABELS at the top of this
            # file. ArgoCD stamps it itself at sync time.
            assert k not in es["metadata"].get("labels", {})
            continue
        assert es["metadata"]["labels"].get(k) == v, f"ExternalSecret label {k} lost"
    # ownerReferences intentionally does NOT survive — see
    # test_owner_references_are_deliberately_dropped_before_any_handler_runs.
    assert "ownerReferences" not in es["metadata"]
    assert es["spec"].get("deliverySpec") == raw_es["spec"]["deliverySpec"]
    assert es["spec"].get("managementPolicy") == raw_es["spec"]["managementPolicy"]
    assert es["spec"]["target"]["template"] == raw_es["spec"]["target"]["template"]

    for k, v in raw_ss["metadata"]["labels"].items():
        if k == "argocd.argoproj.io/instance":
            assert k not in ss["metadata"].get("labels", {})
            continue
        assert ss["metadata"]["labels"].get(k) == v, f"SecretStore label {k} lost"
    assert "ownerReferences" not in ss["metadata"]
    assert ss["spec"]["conditions"] == raw_ss["spec"]["conditions"]
