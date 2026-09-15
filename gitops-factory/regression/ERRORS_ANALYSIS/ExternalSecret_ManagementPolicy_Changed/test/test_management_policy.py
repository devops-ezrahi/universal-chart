"""
Regression tests for ERRORS_ANALYSIS/ExternalSecret_ManagementPolicy_Changed.

Reported: raw `spec.managementPolicy: Default` renders as `Create`, because
"build_externalsecret_entry captures it at line 489, but subtract_defaults
removes 'Default' as it equals the default value. Chart defaults to 'Create'."

**Every clause of that is false**, and the tests below say so one at a time:

  * the converter never captured `managementPolicy` at all — it copied a fixed
    whitelist of five spec keys and dropped the rest;
  * `subtract_defaults` compares values, not names. The literal string
    "Default" is just a string to it; it is removed only when the layer
    underneath holds the same string, and then that layer supplies it again
    (proved for real in folder 15);
  * the chart contains no `Create` default anywhere. Its only ESO default of
    that shape is `creationPolicy | default "Owner"`, which is a different
    field.

So nothing changed `Default` into `Create`. The field was dropped on the floor
by a whitelist, and the rendered manifest simply had no `managementPolicy` line
at all. Same root cause and same fix as
ExternalSecret_Spec_Fields_Not_Captured — see that folder's STATUS §1.

This file uses that folder's fixture rather than duplicating it: one raw
manifest, one defect, two reports.
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


def test_the_fixture_declares_it():
    assert _doc("ExternalSecret")["spec"]["managementPolicy"] == "Default"


def test_the_converter_carries_management_policy():
    ctx = conv.MicroserviceContext(namespace="identity-auth", name="session-store")
    entry = conv.build_externalsecret_entry(_doc("ExternalSecret"), ctx)
    assert entry.get("managementPolicy") == "Default", (
        f"managementPolicy dropped at capture; entry keys = {sorted(entry)}")


def test_subtract_defaults_does_not_treat_the_string_Default_as_a_default():
    """REFUTES the report's stated mechanism. subtract_defaults compares
    VALUES; the literal word "Default" carries no special meaning to it."""
    values = {"externalSecrets": {"es": {"managementPolicy": "Default"}}}
    assert conv.subtract_defaults(values, {}) == values
    assert conv.subtract_defaults(values, {"externalSecrets": {"es": {"managementPolicy": "Create"}}}) \
        == values
    # Removed only when the layer underneath holds the same value — and that
    # layer then supplies it again at render time (folder 15 proves the round trip).
    assert conv.subtract_defaults(values, values) == {}


def test_the_chart_has_no_Create_default():
    """REFUTES the other half. Nothing in the chart renders `Create`."""
    text = (CHART_DIR / "templates" / "externalsecret.yaml").read_text(encoding="utf-8")
    assert '"Create"' not in text and "'Create'" not in text, (
        "the chart does have a Create default after all; the report's mechanism "
        "needs re-examining")
    assert 'creationPolicy | default "Owner"' in text.replace("($spec.target).", ""), \
        "the only ESO default of this shape is creationPolicy -> Owner"


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_the_chart_renders_the_declared_policy_verbatim():
    for policy in ("Default", "Merge", "Create"):
        values = {"workload": {"type": "none"}, "service": {"enabled": False},
                  "externalSecrets": {"es": {
                      "managementPolicy": policy,
                      "secretStoreRef": {"name": "s", "kind": "SecretStore"}}}}
        es = next(d for d in _helm_template(values) if d["kind"] == "ExternalSecret")
        assert es["spec"].get("managementPolicy") == policy, (
            f"declared {policy}, rendered {es['spec'].get('managementPolicy')}")


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_nothing_is_invented_when_the_field_is_absent():
    values = {"workload": {"type": "none"}, "service": {"enabled": False},
              "externalSecrets": {"es": {"secretStoreRef": {"name": "s", "kind": "SecretStore"}}}}
    es = next(d for d in _helm_template(values) if d["kind"] == "ExternalSecret")
    assert "managementPolicy" not in es["spec"], (
        "the chart invented a managementPolicy the raw manifest never had")
