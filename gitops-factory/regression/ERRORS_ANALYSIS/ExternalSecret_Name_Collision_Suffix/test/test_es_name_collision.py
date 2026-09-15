"""
Regression tests for ERRORS_ANALYSIS/ExternalSecret_Name_Collision_Suffix.

Reported: `test-ke` renders as `test-ke-2`, because "the naming convention adds
-2 suffix".

**No `-2` suffix convention exists in this converter.** The collision resolver
is `NamespaceRegistry.claim`, and it renames to `<owner>-<name>` — so the same
collision here produces `<microservice>-test-ke`, never `test-ke-2`. The
numeric-suffix form appears in exactly one place in this codebase, and it is one
I added for folder 13 (`claim_subresource_key`, for CronJob/Job map keys), which
did not exist when the report was written and does not touch ExternalSecrets.

So the *mechanism* is misdescribed. The *concern* is real and is the same one as
folders 18 and 19: a renamed object breaks anything that referred to it by its
original name.

For an ExternalSecret that concern has an extra layer the report does not
mention. An ExternalSecret is a factory: it creates a **Secret**, whose name
comes from `spec.target.name` and defaults to the ExternalSecret's own name. So
renaming the ExternalSecret silently renames the Secret it produces, and every
`secretKeyRef` pointing at that Secret — in a different release, or in a
manifest that was never part of this conversion — stops resolving. That is worse
than a renamed ConfigMap, because the broken reference is one level removed from
the thing that was renamed.
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


def _load_converter():
    spec = importlib.util.spec_from_file_location("convert_to_universal_chart", CONVERTER_PY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["convert_to_universal_chart"] = mod
    spec.loader.exec_module(mod)
    return mod


conv = _load_converter()


def _helm_template(values: dict, release="app"):
    values = dict(values)
    values.setdefault("externalSecretsApiVersion", "external-secrets.io/v1beta1")
    values.setdefault("workload", {"type": "none"})
    values.setdefault("service", {"enabled": False})
    with tempfile.TemporaryDirectory() as tmp:
        vf = pathlib.Path(tmp) / "values.yaml"
        vf.write_text(yaml.safe_dump(values, sort_keys=False), encoding="utf-8")
        proc = subprocess.run(["helm", "template", release, str(CHART_DIR), "-f", str(vf)],
                              capture_output=True, text=True)
    assert proc.returncode == 0, f"helm template failed:\n{proc.stderr}"
    return [d for d in yaml.safe_load_all(proc.stdout) if d]


# --------------------------------------------------------------------------
# The correction
# --------------------------------------------------------------------------

def test_the_collision_resolver_uses_owner_prefix_not_a_numeric_suffix():
    reg = conv.NamespaceRegistry("ns")
    reg.claim("ExternalSecret", "test-ke", conv.content_hash({"a": 1}), "alpha")
    name, action, note = reg.claim(
        "ExternalSecret", "test-ke", conv.content_hash({"a": 2}), "beta")
    assert action == "renamed"
    assert name == "beta-test-ke", f"expected an owner prefix, got {name}"
    assert name != "test-ke-2"
    assert "beta-test-ke" in note


def test_no_numeric_suffix_convention_exists_for_claimed_objects():
    """Ten colliding claimants, to make sure nothing falls back to counting."""
    reg = conv.NamespaceRegistry("ns")
    names = [reg.claim("ExternalSecret", "test-ke", conv.content_hash({"n": i}), f"ms{i}")[0]
             for i in range(10)]
    assert names[0] == "test-ke"
    assert all(n == f"ms{i}-test-ke" for i, n in enumerate(names) if i)
    assert not any(n.rstrip("0123456789").endswith("-") and n[-1].isdigit() for n in names[1:])


def test_external_secret_is_in_the_claimed_kinds():
    """If it were not, two microservices would both render `test-ke` and the
    last applied would win — silently worse than a rename."""
    assert "ExternalSecret" in conv.NS_RAW_NAME_KINDS


# --------------------------------------------------------------------------
# The concern that IS real: what the rename does to the Secret it produces
# --------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_a_renamed_external_secret_renames_the_secret_it_creates():
    """The extra layer: the produced Secret's name defaults to the
    ExternalSecret's, so a rename propagates to something no manifest mentions."""
    docs = _helm_template({"externalSecrets": {"beta-test-ke": {
        "secretStoreRef": {"name": "vault", "kind": "SecretStore"}}}})
    es = next(d for d in docs if d["kind"] == "ExternalSecret")
    assert es["metadata"]["name"] == "beta-test-ke"
    assert es["spec"]["target"]["name"] == "beta-test-ke", (
        "the Secret this ExternalSecret creates is named after it, so the rename "
        "propagates to a Secret that no raw manifest ever named")


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_an_explicit_target_name_pins_the_secret_through_a_rename():
    """The mitigation, and the thing to reach for when a rename is unavoidable:
    `target.name` decouples the produced Secret from the ExternalSecret's name,
    so consumers keep resolving."""
    docs = _helm_template({"externalSecrets": {"beta-test-ke": {
        "secretStoreRef": {"name": "vault", "kind": "SecretStore"},
        "target": {"name": "test-ke"}}}})
    es = next(d for d in docs if d["kind"] == "ExternalSecret")
    assert es["metadata"]["name"] == "beta-test-ke"
    assert es["spec"]["target"]["name"] == "test-ke", (
        "an explicit target.name must survive the rename of its ExternalSecret")


def test_the_converter_carries_an_explicit_target_name():
    doc = {"apiVersion": "external-secrets.io/v1beta1", "kind": "ExternalSecret",
           "metadata": {"name": "test-ke"},
           "spec": {"secretStoreRef": {"name": "vault", "kind": "SecretStore"},
                    "target": {"name": "test-ke", "creationPolicy": "Owner"}}}
    ctx = conv.MicroserviceContext(namespace="ns", name="beta")
    entry = conv.build_externalsecret_entry(doc, ctx)
    assert entry["target"]["name"] == "test-ke"


def test_a_secret_store_ref_follows_a_renamed_secret_store():
    """The reference direction that already worked, pinned so the rename cannot
    start pointing at a store that no longer exists under that name."""
    ctx = conv.MicroserviceContext(namespace="ns", name="beta")
    ctx.name_map["SecretStore"]["vault"] = "beta-vault"
    doc = {"kind": "ExternalSecret", "metadata": {"name": "test-ke"},
           "spec": {"secretStoreRef": {"name": "vault", "kind": "SecretStore"}}}
    entry = conv.build_externalsecret_entry(doc, ctx)
    assert entry["secretStoreRef"] == {"name": "beta-vault", "kind": "SecretStore"}


def test_a_cluster_secret_store_ref_is_not_remapped():
    """ClusterSecretStores are deduped globally without renaming, so remapping
    one would point at a name that does not exist."""
    ctx = conv.MicroserviceContext(namespace="ns", name="beta")
    ctx.name_map["SecretStore"]["vault"] = "beta-vault"
    doc = {"kind": "ExternalSecret", "metadata": {"name": "test-ke"},
           "spec": {"secretStoreRef": {"name": "vault", "kind": "ClusterSecretStore"}}}
    entry = conv.build_externalsecret_entry(doc, ctx)
    assert entry["secretStoreRef"] == {"name": "vault", "kind": "ClusterSecretStore"}
