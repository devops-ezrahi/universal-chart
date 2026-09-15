"""
Regression tests for ERRORS_ANALYSIS/ServiceAccount_Secrets_And_ImagePullSecrets_Lost.

error.txt is explicit that this analysis was never finished, and that the report
carries two contradictory ratings — LOW #19 ("cluster auto-provisions dockercfg
secrets") and CRITICAL/High in Discoveries ("real credentials ... ImagePullBackOff")
— *precisely because the deciding question was never answered*:

  > "If the migration target is still OpenShift, this is cosmetic. If the target
  > is vanilla Kubernetes or the SA carries a MANUALLY-added pull secret for a
  > private registry, this is CRITICAL."

The target is **vanilla Kubernetes** (recorded decision). error.txt then asks for
the per-object check: does each imagePullSecret match OpenShift's
auto-generated `<sa>-dockercfg-XXXXX` pattern, or is it operator-added?

Answer, over every ServiceAccount in all three manifest trees (STATUS.md §1):

    ServiceAccounts scanned: 316
      imagePullSecrets  auto-generated: 278   CUSTOM: 0
      secrets           auto-generated: 556   CUSTOM: 0

100% auto-generated. So on this data the loss is cosmetic — and more than
cosmetic-but-harmless, **carrying them would be actively wrong**: those Secrets
are created by OpenShift's integrated-registry controller and do not exist on a
vanilla cluster, so the reference would point at an object that never appears.

The code gap is nevertheless real and is the CRITICAL half of the report: a
custom credential would have been dropped exactly the same way. It is now
carried, and both halves are tested from one fixture.
"""
import importlib.util
import pathlib
import re
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
FIXTURE = FIXTURE_DIR / "shop-web" / "api-gateway.yaml"

NS = "shop-web"
RELEASE = "api-gateway"
AUTO = "api-gateway-dockercfg-x9z8y"
CUSTOM = "acme-registry-creds"
RAW_DIRS = ["namespaces", "namespaces-showcase", "namespaces-live-demo"]


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


def _helm_template(values: dict, release=RELEASE):
    with tempfile.TemporaryDirectory() as tmp:
        vf = pathlib.Path(tmp) / "values.yaml"
        vf.write_text(yaml.safe_dump(values, sort_keys=False), encoding="utf-8")
        proc = subprocess.run(["helm", "template", release, str(CHART_DIR), "-f", str(vf)],
                              capture_output=True, text=True)
    assert proc.returncode == 0, f"helm template failed:\n{proc.stderr}"
    return [d for d in yaml.safe_load_all(proc.stdout) if d]


def _sa(docs):
    for d in docs:
        if d.get("kind") == "ServiceAccount":
            return d
    raise AssertionError("chart rendered no ServiceAccount")


# --------------------------------------------------------------------------
# The finished analysis
# --------------------------------------------------------------------------

def test_every_pull_secret_in_this_repo_is_openshift_auto_generated():
    """The check error.txt asks for, as a test. A custom credential appearing in
    the raw manifests fails here — which is the moment the severity flips from
    cosmetic to CRITICAL."""
    custom = []
    for d in RAW_DIRS:
        root = GITOPS_DIR / d
        if not root.is_dir():
            continue
        for f in root.rglob("*.yaml"):
            for doc in yaml.safe_load_all(f.read_text(encoding="utf-8")):
                if not doc or doc.get("kind") != "ServiceAccount":
                    continue
                for s in (doc.get("imagePullSecrets") or []):
                    if not conv.AUTO_SA_SECRET_RE.match(s.get("name", "")):
                        custom.append(f"{f.relative_to(GITOPS_DIR)}: "
                                      f"{doc['metadata']['name']} -> {s.get('name')}")
    assert not custom, (
        "a CUSTOM imagePullSecret now exists in the raw manifests. Dropping it "
        "would cause ImagePullBackOff on every pod using that ServiceAccount:\n"
        + "\n".join(custom))


def test_the_auto_generated_pattern_matches_what_openshift_produces():
    assert conv.AUTO_SA_SECRET_RE.match("api-gateway-dockercfg-x9z8y")
    assert conv.AUTO_SA_SECRET_RE.match("api-gateway-token-a1b2c")
    assert not conv.AUTO_SA_SECRET_RE.match("acme-registry-creds")
    assert not conv.AUTO_SA_SECRET_RE.match("dockercfg"), "must not match a bare word"
    assert not conv.AUTO_SA_SECRET_RE.match("my-dockercfg-x"), "suffix is 5+ chars"


# --------------------------------------------------------------------------
# Capture — custom carried, auto-generated dropped
# --------------------------------------------------------------------------

def test_a_custom_pull_secret_is_carried():
    ctx = conv.MicroserviceContext(namespace=NS, name=RELEASE)
    conv.handle_serviceaccount(_doc("ServiceAccount"), ctx, RELEASE)
    got = [s["name"] for s in ctx.values["serviceAccount"].get("imagePullSecrets", [])]
    assert CUSTOM in got, (
        f"the hand-added registry credential was dropped; got {got}. Every pod "
        f"using this ServiceAccount would fail ImagePullBackOff.")


def test_the_openshift_generated_pull_secret_is_dropped():
    ctx = conv.MicroserviceContext(namespace=NS, name=RELEASE)
    conv.handle_serviceaccount(_doc("ServiceAccount"), ctx, RELEASE)
    got = [s["name"] for s in ctx.values["serviceAccount"].get("imagePullSecrets", [])]
    assert AUTO not in got, (
        f"{AUTO} names a Secret OpenShift's registry controller creates. It does "
        f"not exist on the target, so the reference points at nothing.")


def test_token_secrets_are_never_carried():
    ctx = conv.MicroserviceContext(namespace=NS, name=RELEASE)
    conv.handle_serviceaccount(_doc("ServiceAccount"), ctx, RELEASE)
    assert "secrets" not in ctx.values["serviceAccount"], (
        "ServiceAccount token references are created and rotated by Kubernetes; "
        "declaring them from a chart fights the controller that owns them")


def test_a_service_account_with_only_auto_secrets_gets_no_block_at_all():
    doc = _doc("ServiceAccount")
    doc["imagePullSecrets"] = [{"name": AUTO}]
    ctx = conv.MicroserviceContext(namespace=NS, name=RELEASE)
    conv.handle_serviceaccount(doc, ctx, RELEASE)
    assert "imagePullSecrets" not in ctx.values["serviceAccount"], (
        "an empty imagePullSecrets list must not be invented")


# --------------------------------------------------------------------------
# Render
# --------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_the_template_renders_custom_pull_secrets():
    values = {"workload": {"type": "none"}, "service": {"enabled": False},
              "serviceAccount": {"create": True, "name": RELEASE,
                                 "imagePullSecrets": [{"name": CUSTOM}]}}
    got = _sa(_helm_template(values)).get("imagePullSecrets")
    assert got == [{"name": CUSTOM}], f"template dropped the pull secret; got {got}"


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_nothing_is_rendered_when_there_are_none():
    values = {"workload": {"type": "none"}, "service": {"enabled": False},
              "serviceAccount": {"create": True, "name": RELEASE}}
    sa = _sa(_helm_template(values))
    assert "imagePullSecrets" not in sa
    assert "secrets" not in sa


# --------------------------------------------------------------------------
# End to end
# --------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_round_trip_keeps_the_custom_credential_and_drops_the_generated_ones(tmp_path):
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

    sa = _sa(_helm_template(merged))
    names = [s["name"] for s in sa.get("imagePullSecrets", [])]
    assert names == [CUSTOM], f"expected only the custom credential; got {names}"
    assert "secrets" not in sa
