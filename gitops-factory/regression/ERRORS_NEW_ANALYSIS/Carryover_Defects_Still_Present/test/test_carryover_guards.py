"""
Regression tests for ERRORS_NEW_ANALYSIS/Carryover_Defects_Still_Present.

error.txt's own statement of purpose: "THIS FOLDER EXISTS TO STOP THESE FROM
BEING LOST BETWEEN THE TWO ANALYSIS ROUNDS." Nine defects from the previous
round appear again, three with HIGHER counts, and it cannot tell whether that is
regression or measurement: "right now nobody can tell whether the PVC retention
defect is 3x worse or simply 3x more visible."

Two things are needed and this file is the second of them.

  1. Settle regression-vs-measurement. Done in ../../Summary_Vs_Report_Data_Mismatch
     (folder 1): the new report was produced by the CURRENT converter, so the
     increases are more input, not worse code. Cross-checked here by asserting
     each of the nine is handled today.

  2. Make "still present?" a question a machine answers. The reason nine
     defects survived a round that fixed all of them is that each was proven
     once, in its own fixture, against a corpus that does not contain the
     field — so the fidelity report read 0 both before and after and nothing
     could ever detect them coming back. These are the guards. One per
     carryover, each failing loudly if the fix is undone.

Read alongside the corpus change made by this folder: platform-data/postgres.yaml
now DECLARES persistentVolumeClaimRetentionPolicy, so the round-trip fidelity
check itself is a tripwire for carryover #1 and not only these unit tests.
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


def _ctx():
    return conv.MicroserviceContext(namespace="platform-data", name="postgres")


def _sts(spec_extra=None, pod_spec=None, containers=None):
    return {
        "apiVersion": "apps/v1", "kind": "StatefulSet",
        "metadata": {"name": "postgres"},
        "spec": {
            "serviceName": "postgres-headless",
            **(spec_extra or {}),
            "template": {"metadata": {}, "spec": {
                **(pod_spec or {}),
                "containers": containers or [{"name": "postgres", "image": "postgres:16"}],
            }},
        },
    }


# ---------------------------------------------------------------------------
# Carryover 1 — persistentVolumeClaimRetentionPolicy (8 -> 23)
# The only carryover whose loss is IRREVERSIBLE. Kubernetes defaults both
# halves to Delete, so a StatefulSet that declared Retain and converts without
# it destroys its PVC on the first delete, scale-down or ArgoCD re-sync.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("policy", [
    {"whenDeleted": "Retain", "whenScaled": "Retain"},
    {"whenDeleted": "Retain", "whenScaled": "Delete"},
    {"whenDeleted": "Delete", "whenScaled": "Retain"},
    {"whenDeleted": "Retain"},
    {"whenScaled": "Retain"},
])
def test_carryover_1_pvc_retention_policy_survives_every_combination(policy):
    """Each half is independently optional, and a half the raw manifest left
    out must stay out rather than being normalised to Delete."""
    ctx = _ctx()
    conv.handle_workload(_sts(spec_extra={"persistentVolumeClaimRetentionPolicy": policy}),
                         ctx, "StatefulSet")
    assert ctx.values.get("persistentVolumeClaimRetentionPolicy") == policy


def test_carryover_1_no_policy_is_never_invented():
    """Defaulting to Retain would be safer for data and wrong for fidelity: it
    would override every consumer who wants the Kubernetes default and turn a
    0-noise report into permanent false ADDEDs."""
    ctx = _ctx()
    conv.handle_workload(_sts(), ctx, "StatefulSet")
    assert "persistentVolumeClaimRetentionPolicy" not in ctx.values


def test_carryover_1_the_showcase_corpus_now_actually_carries_the_field():
    """The reason this defect could recur unnoticed. Without a raw manifest
    declaring it, the round-trip check reports 0 whether the converter handles
    it or not — which is exactly what previous STATUS.md §6 recorded and what
    nobody generalised."""
    text = (SHOWCASE / "platform-data" / "postgres.yaml").read_text(encoding="utf-8")
    assert "persistentVolumeClaimRetentionPolicy" in text, \
        "corpus coverage for the irreversible carryover defect was removed"
    sts = next(d for d in yaml.safe_load_all(text) if d and d.get("kind") == "StatefulSet")
    assert sts["spec"]["persistentVolumeClaimRetentionPolicy"] == \
        {"whenDeleted": "Retain", "whenScaled": "Retain"}


# ---------------------------------------------------------------------------
# Carryover 2 — StatefulSet serviceName (7, exact match between both rounds)
# ---------------------------------------------------------------------------

def test_carryover_2_governing_service_name_is_carried_verbatim():
    """The headless Service that gives each pod its stable DNS name. Hardcoded
    to the release fullname, peer discovery in a clustered system resolves to
    one load-balanced VIP and cannot work at all."""
    ctx = _ctx()
    conv.handle_workload(_sts(), ctx, "StatefulSet")
    assert ctx.values.get("serviceName") == "postgres-headless"


def test_carryover_2_serviceName_captured_even_when_it_matches_the_workload():
    """Storing it only on mismatch would lean on the chart's fullname resolving
    back to the workload name — the same implicit assumption that caused the
    defect."""
    ctx = _ctx()
    conv.handle_workload(_sts(spec_extra={"serviceName": "postgres"}), ctx, "StatefulSet")
    assert ctx.values.get("serviceName") == "postgres"


# ---------------------------------------------------------------------------
# Carryover 3 — serviceAccount lost (25 -> 29)
# error.txt asks for a CLASSIFICATION CORRECTION on this one: the summary
# looked for the wrong path and reported 0; there are 29. The previous round's
# severity (NONE, the deprecated alias is harmless) still holds.
# ---------------------------------------------------------------------------

def test_carryover_3_serviceAccountName_is_carried():
    ctx = _ctx()
    ctx.claim_actions["ServiceAccount"]["db-sa"] = "own"
    conv.handle_workload(_sts(pod_spec={"serviceAccountName": "db-sa"}), ctx, "StatefulSet")
    assert ctx.values["serviceAccount"]["name"] == "db-sa"


def test_carryover_3_deprecated_serviceAccount_alias_is_read_as_a_fallback():
    """This is the field the summary counted as 0. It is the pre-1.9 alias for
    serviceAccountName; Kubernetes still honours it, so a pod declaring only
    the alias must not silently fall back to the release's own SA."""
    ctx = _ctx()
    ctx.claim_actions["ServiceAccount"]["legacy-sa"] = "own"
    conv.handle_workload(_sts(pod_spec={"serviceAccount": "legacy-sa"}), ctx, "StatefulSet")
    assert ctx.values["serviceAccount"]["name"] == "legacy-sa"


def test_carryover_3_serviceAccountName_wins_over_the_deprecated_alias():
    ctx = _ctx()
    ctx.claim_actions["ServiceAccount"]["modern"] = "own"
    conv.handle_workload(
        _sts(pod_spec={"serviceAccountName": "modern", "serviceAccount": "legacy"}),
        ctx, "StatefulSet")
    assert ctx.values["serviceAccount"]["name"] == "modern"


# ---------------------------------------------------------------------------
# Carryover 4 — image changed (12 -> 9): a digest pin replaced by a tag
# ---------------------------------------------------------------------------

def test_carryover_4_digest_pin_survives_the_split():
    """A digest is the strongest form of image reference. Replaced by a mutable
    tag, the workload silently starts running whatever that tag points at."""
    repo, tag, digest = conv.split_image(
        "ghcr.io/acme/api@sha256:" + "a" * 64)
    assert repo == "ghcr.io/acme/api"
    assert digest == "sha256:" + "a" * 64
    assert not tag


@pytest.mark.parametrize("image", [
    "ghcr.io/acme/api@sha256:" + "b" * 64,
    "ghcr.io/acme/api:v1.2.3@sha256:" + "b" * 64,
    "ghcr.io/acme/api:v1.2.3",
    "registry.internal:5000/acme/api:v2",
    "registry.internal:5000/acme/api@sha256:" + "c" * 64,
    "postgres",
])
def test_carryover_4_every_reference_shape_reassembles_to_itself(image):
    """The invariant is round-trip, not a particular decomposition.

    `repo:tag@digest` is deliberately NOT split three ways — split_image leaves
    the tag inside the repository and the chart's image helper takes the
    `.Values.image.digest` branch, `printf "%s@%s"`, reproducing the original
    string exactly. Asserting a tidier decomposition instead would fail against
    correct code, which is what a first draft of this test did.

    What must never happen is the thing the defect actually was: a digest pin
    coming out the far end as a mutable tag."""
    repo, tag, digest = conv.split_image(image)
    if digest:
        rendered = f"{repo}@{digest}"
    elif "@" in repo:
        rendered = repo
    else:
        rendered = f"{repo}:{tag}" if tag else repo
    assert rendered == image, f"{image} did not survive split + reassembly"
    if "@" in image:
        assert "sha256:" in rendered, "a digest pin degraded to a tag"


def test_carryover_4_registry_port_is_not_mistaken_for_a_tag():
    """`registry:5000/api` — the colon belongs to the host, not to a tag."""
    repo, tag, digest = conv.split_image("registry.internal:5000/acme/api:v2")
    assert repo == "registry.internal:5000/acme/api"
    assert tag == "v2"


# ---------------------------------------------------------------------------
# Carryover 5 — configMap.name broken (7 -> 8)
# Renaming on collision is deliberate; what must hold is that every reference
# follows the rename. A reference left pointing at the old name is a pod that
# never starts.
# ---------------------------------------------------------------------------

def test_carryover_5_volume_references_follow_a_configmap_rename():
    ctx = _ctx()
    ctx.name_map["ConfigMap"]["app-config"] = "api-app-config"
    ctx.name_map["Secret"]["app-secret"] = "api-app-secret"
    vols = conv.named_list_to_map([
        {"name": "cfg", "configMap": {"name": "app-config"}},
        {"name": "sec", "secret": {"secretName": "app-secret"}},
    ])
    out = conv.remap_volumes(vols, ctx)
    assert out["cfg"]["configMap"]["name"] == "api-app-config"
    assert out["sec"]["secret"]["secretName"] == "api-app-secret"


def test_carryover_5_env_valueFrom_references_follow_a_rename():
    ctx = _ctx()
    ctx.name_map["Secret"]["db-creds"] = "postgres-db-creds"
    out = conv.env_list([
        {"name": "PW", "valueFrom": {"secretKeyRef": {"name": "db-creds", "key": "password"}}},
    ], ctx)
    assert out[0]["valueFrom"]["secretKeyRef"]["name"] == "postgres-db-creds"


def test_carryover_5_an_unrenamed_reference_is_left_alone():
    """The remap must be a lookup, not a rewrite rule — otherwise a reference to
    an object owned by another release gets mangled."""
    ctx = _ctx()
    vols = conv.named_list_to_map([{"name": "cfg", "configMap": {"name": "shared-ca"}}])
    assert conv.remap_volumes(vols, ctx)["cfg"]["configMap"]["name"] == "shared-ca"


# ---------------------------------------------------------------------------
# Carryover 6 — data.grafana.ini changed (2 -> 4)
# The whitespace half was fixed and tested last round. Pinned here so a
# regression in blob handling is attributed to this carryover rather than
# rediscovered. The quoting/colon half is folder 25, deliberately not touched.
# ---------------------------------------------------------------------------

def test_carryover_6_configmap_blob_survives_the_yaml_round_trip_byte_exactly():
    blob = "[server]\nroot_url = https://grafana.example.com\n\n[auth]\ndisable_login_form = true\n"
    ctx = _ctx()
    conv.handle_configmap({
        "apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "grafana"},
        "data": {"grafana.ini": blob},
    }, ctx, "grafana")
    dumped = conv.dump_yaml(ctx.values)
    assert yaml.safe_load(dumped)["configMaps"]["grafana"]["data"]["grafana.ini"] == blob


# ---------------------------------------------------------------------------
# Carryover 7 — Route spec.to.name changed (2 -> 8)
# ---------------------------------------------------------------------------

def test_carryover_7_route_backend_follows_a_service_rename():
    ctx = _ctx()
    ctx.name_map["Service"]["api-service"] = "web-api-service"
    entry = conv.build_route_entry({
        "apiVersion": "route.openshift.io/v1", "kind": "Route",
        "metadata": {"name": "api-route"},
        "spec": {"host": "api.example.com", "to": {"kind": "Service", "name": "api-service"}},
    }, ctx)
    assert entry["serviceName"] == "web-api-service"


def test_carryover_7_route_backend_is_not_replaced_by_the_release_fullname():
    """The original defect: every object rendered as the release fullname, so a
    Route stopped pointing at the '-service' object and pointed at itself."""
    ctx = _ctx()
    entry = conv.build_route_entry({
        "apiVersion": "route.openshift.io/v1", "kind": "Route",
        "metadata": {"name": "api-route"},
        "spec": {"to": {"kind": "Service", "name": "api-service"}},
    }, ctx)
    assert entry["serviceName"] == "api-service"


# ---------------------------------------------------------------------------
# Carryover 8 — sidecar containers lost (28 -> 66)
# ---------------------------------------------------------------------------

def test_carryover_8_every_sidecar_survives_under_its_own_name():
    """Pods losing auth proxies, operators and exporters entirely."""
    ctx = _ctx()
    conv.handle_workload(_sts(containers=[
        {"name": "postgres", "image": "postgres:16"},
        {"name": "exporter", "image": "postgres-exporter:0.15"},
        {"name": "auth-proxy", "image": "cloud-sql-proxy:2.8"},
    ]), ctx, "StatefulSet")
    assert set(ctx.values["sidecars"]) == {"exporter", "auth-proxy"}


def test_carryover_8_sidecars_do_not_collide_with_the_main_container_key():
    """The original defect was a name-key collision, so the shape that matters
    is a sidecar sharing a prefix with the main container."""
    ctx = _ctx()
    conv.handle_workload(_sts(containers=[
        {"name": "postgres", "image": "postgres:16"},
        {"name": "postgres-exporter", "image": "exp:1"},
    ]), ctx, "StatefulSet")
    assert "postgres-exporter" in ctx.values["sidecars"]
    assert ctx.values["containerName"] == "postgres"


# ---------------------------------------------------------------------------
# Carryover 9 — Route/Service rendered by NO release (90, exact)
# Corpus-dependent, so what is guarded here is the checker's ability to SEE it.
# A checker that cannot report an orphan is worse than one that reports 90.
# ---------------------------------------------------------------------------

def test_carryover_9_the_checker_can_still_report_an_orphaned_object():
    ignored = conv._fidelity_ignored_path("Route", ("metadata", "labels"))
    assert ignored is False, \
        "Route metadata.labels is suppressed — an orphaned Route's labels would be invisible"


def test_carryover_9_selector_suppression_stayed_scoped_to_selectors():
    """_fidelity_ignored_path exists for paths the chart derives itself. If it
    ever widens to whole objects or whole label sets, every carryover above
    becomes unobservable at once — which is how a report reaches 0 while the
    defects are all still there."""
    # Workload/Service selectors are carried now, so they are compared too.
    assert conv._fidelity_ignored_path("Deployment", ("spec", "selector", "matchLabels")) is False
    assert conv._fidelity_ignored_path("Service", ("spec", "selector")) is False
    assert conv._fidelity_ignored_path("PodDisruptionBudget", ("spec", "selector", "matchLabels")) is True
    # ...but nothing broader.
    assert conv._fidelity_ignored_path("Deployment", ("metadata", "labels")) is False
    assert conv._fidelity_ignored_path("Service", ("spec",)) is False
    assert conv._fidelity_ignored_path("Deployment", ("spec", "template", "spec")) is False
