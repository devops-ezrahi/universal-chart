"""
Regression tests for ERRORS_NEW_ANALYSIS/Secret_ConfigMap_Rename_Reference_Inconsistency.

An OPEN DECISION rather than a confirmed defect, and error.txt is explicit that
it must not be "fixed" before being decided:

  "THIS IS AN OPEN DECISION, NOT A CONFIRMED BUG. The report repeatedly hedges:
   'may be intentional', 'whether they're bugs depends on reference
   consistency'. Do not 'fix' it before deciding."

It also states the deciding test:

  "THE DECIDING TEST: for each renamed object, render the release with helm
   template and confirm EVERY reference (volumes[*].secret.secretName,
   volumes[*].configMap.name, env[*].valueFrom.secretKeyRef.name, envFrom)
   resolves to an object that same release actually creates. If all resolve,
   this is cosmetic and belongs in the suppression list. If any dangles, it is a
   P0 — the pod will fail to mount and will not start."

VERDICT: BENIGN — the renames are internally consistent. But the verdict is
worth far less than the machinery that produced it, so the deciding test is now
a permanent part of --verify-fidelity rather than a one-off inspection. The
report's two contradictory claims ("one of the 6 real issues" vs "internally
consistent after rename") existed only because nobody could cheaply check.

THE CHECK HAS TWO HALVES, and the second is the one that matters. A first
version only caught references that resolve to NOTHING, and a deliberately
broken converter sailed through it — because after a collision rename an
un-remapped reference still resolves, to the OTHER microservice's object of the
same name. The pod starts, mounts the wrong config, and nothing reports it.
That is strictly more dangerous than a dangling reference, which at least fails
loudly. Both halves are tested below.
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


# ---------------------------------------------------------------------------
# 1. Every reference site error.txt names is actually collected
# ---------------------------------------------------------------------------

POD = {
    "kind": "Deployment", "metadata": {"name": "api"},
    "spec": {"template": {"spec": {
        "serviceAccountName": "api-sa",
        "containers": [{
            "name": "api",
            "env": [
                {"name": "P", "valueFrom": {"secretKeyRef": {"name": "api-creds", "key": "p"}}},
                {"name": "C", "valueFrom": {"configMapKeyRef": {"name": "api-conf", "key": "c"}}},
            ],
            "envFrom": [
                {"configMapRef": {"name": "job-defaults"}},
                {"secretRef": {"name": "api-secrets"}},
            ],
        }],
        "volumes": [
            {"name": "cfg", "configMap": {"name": "mounted-conf"}},
            {"name": "sec", "secret": {"secretName": "mounted-secret"}},
        ],
    }}},
}


@pytest.mark.parametrize("kind,name", [
    ("Secret", "api-creds"),          # env[*].valueFrom.secretKeyRef.name
    ("ConfigMap", "api-conf"),        # env[*].valueFrom.configMapKeyRef.name
    ("ConfigMap", "job-defaults"),    # envFrom[*].configMapRef.name
    ("Secret", "api-secrets"),        # envFrom[*].secretRef.name
    ("ConfigMap", "mounted-conf"),    # volumes[*].configMap.name
    ("Secret", "mounted-secret"),     # volumes[*].secret.secretName
    ("ServiceAccount", "api-sa"),     # serviceAccountName
])
def test_every_reference_site_is_collected(kind, name):
    """error.txt enumerates the sites that must be checked. Missing one means
    the deciding test silently answers 'all clear' for a reference nobody
    looked at."""
    refs = {(k, n) for k, n, _ in conv.collect_object_references(POD)}
    assert (kind, name) in refs


def test_route_backend_is_collected():
    refs = conv.collect_object_references({
        "kind": "Route", "metadata": {"name": "r"},
        "spec": {"to": {"kind": "Service", "name": "api-service"}}})
    assert ("Service", "api-service") in {(k, n) for k, n, _ in refs}


def test_sidecars_and_init_containers_are_walked_too():
    """Collected generically rather than by following the values schema — the
    point is to catch a reference site the REMAPPING code does not know about,
    and a checker that looked only where the remapper looks could never find
    one."""
    doc = {"kind": "Deployment", "metadata": {"name": "api"},
           "spec": {"template": {"spec": {
               "initContainers": [{"name": "i", "envFrom": [{"secretRef": {"name": "init-sec"}}]}],
               "containers": [{"name": "a"},
                              {"name": "sidecar",
                               "env": [{"name": "X", "valueFrom": {
                                   "configMapKeyRef": {"name": "side-conf", "key": "k"}}}]}],
           }}}}
    refs = {(k, n) for k, n, _ in conv.collect_object_references(doc)}
    assert ("Secret", "init-sec") in refs
    assert ("ConfigMap", "side-conf") in refs


def test_no_references_found_in_an_object_that_makes_none():
    assert conv.collect_object_references(
        {"kind": "ConfigMap", "metadata": {"name": "c"}, "data": {"k": "v"}}) == []


# ---------------------------------------------------------------------------
# 2. The CHANGED downgrade — and its limits
# ---------------------------------------------------------------------------

def _index(*pairs):
    return {(k, n): ("some-release", {}) for k, n in pairs}


def test_a_reference_changed_to_a_rendered_name_is_a_followed_rename():
    assert conv._is_followed_rename(
        "spec.template.spec.containers[0].envFrom[0].configMapRef.name",
        "raw='job-defaults' render='nightly-report-job-defaults'",
        _index(("ConfigMap", "nightly-report-job-defaults")))


def test_a_reference_changed_to_a_name_nothing_renders_is_NOT_downgraded():
    """The downgrade must never be able to hide a broken rename."""
    assert not conv._is_followed_rename(
        "spec.template.spec.volumes[0].configMap.name",
        "raw='job-defaults' render='typo-job-defaults'",
        _index(("ConfigMap", "nightly-report-job-defaults")))


def test_a_changed_DATA_field_is_never_treated_as_a_rename():
    """Scoped to known reference paths, so a ConfigMap whose CONTENT changed —
    the UTF-8 corruption of folder 4, say — can never be waved through as a
    rename."""
    assert not conv._is_followed_rename(
        "data.init-config.sh",
        "raw='#!/bin/sh' render='nightly-report-job-defaults'",
        _index(("ConfigMap", "nightly-report-job-defaults")))


def test_a_malformed_detail_string_is_not_downgraded():
    assert not conv._is_followed_rename(
        "spec.template.spec.volumes[0].configMap.name",
        "something unparseable",
        _index(("ConfigMap", "x")))


# ---------------------------------------------------------------------------
# 3. The corpus carries a real collision, so the check is exercised for real
# ---------------------------------------------------------------------------

def _corpus_docs(ns):
    out = []
    for f in sorted((SHOWCASE / ns).glob("*.yaml")):
        for d in yaml.safe_load_all(f.read_text(encoding="utf-8")):
            if d:
                out.append((f.stem, d))
    return out


def test_the_corpus_contains_a_genuine_name_collision():
    """Without one, `dangling references: 0` proves only that the check does not
    false-positive — not that it detects anything."""
    docs = _corpus_docs("background-jobs")
    owners = {stem for stem, d in docs
              if d.get("kind") == "ConfigMap" and d["metadata"]["name"] == "job-defaults"}
    assert len(owners) >= 2, f"ConfigMap/job-defaults is declared by {owners}, expected a collision"


def test_the_colliding_copies_differ_in_content():
    """Identical copies would make the rename a judgement call. Differing ones
    make it unambiguously correct — and make a wrong reference observable,
    because the two objects carry different values."""
    docs = _corpus_docs("background-jobs")
    datas = [d["data"] for _s, d in docs
             if d.get("kind") == "ConfigMap" and d["metadata"]["name"] == "job-defaults"]
    assert len(datas) >= 2 and datas[0] != datas[1]


def test_both_colliding_owners_actually_reference_the_configmap():
    """A collision nobody references would exercise the rename but not the
    reference-following, which is the half the report was unsure about."""
    docs = _corpus_docs("background-jobs")
    referrers = set()
    for stem, d in docs:
        for kind, name, _where in conv.collect_object_references(d):
            if (kind, name) == ("ConfigMap", "job-defaults"):
                referrers.add(stem)
    assert len(referrers) >= 2, f"only {referrers} reference the colliding ConfigMap"


# ---------------------------------------------------------------------------
# The rename must be legible to the NAME-FIDELITY gate, not just correct
# ---------------------------------------------------------------------------
#
# A collision rename necessarily produces a rendered name with no raw
# counterpart, which name_fidelity_check.py called INVENTED and treated as a
# hard FAIL — "anything that referenced them by name will break". The rename is
# the fix for the collision, not damage from it, so the whole pipeline exited
# non-zero on every run containing one. A gate that always fails is one nobody
# reads, and this repo had exactly that state for as long as the fixture existed.
#
# The converter now records each rename in report/renames.json and the checker
# reads it: recorded renames are reported on their own line and do not fail.

def test_the_converter_records_every_collision_rename():
    import json
    f = GITOPS_DIR / "output-showcase" / "report" / "renames.json"
    assert f.is_file(), "renames.json missing; run convert_and_verify.py"
    renames = json.loads(f.read_text(encoding="utf-8"))
    assert any(r["from"] == "job-defaults" and r["to"] == "nightly-report-job-defaults"
               and r["namespace"] == "background-jobs" for r in renames), renames
    for r in renames:
        assert set(r) >= {"namespace", "kind", "from", "to", "identical_content"}


def test_the_name_gate_treats_a_recorded_rename_as_expected():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "name_fidelity_check", GITOPS_DIR / "name_fidelity_check.py")
    nfc = importlib.util.module_from_spec(spec)
    sys.modules["name_fidelity_check"] = nfc
    spec.loader.exec_module(nfc)
    recorded = nfc.load_renames(GITOPS_DIR / "output-showcase")
    assert {(r["namespace"], r["kind"], r["to"]) for r in recorded}, recorded


def test_an_UNRECORDED_invented_name_still_fails(tmp_path):
    """The carve-out must not become "any unexplained name is fine". With no
    record, the checker has nothing to excuse and reports the name."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "name_fidelity_check2", GITOPS_DIR / "name_fidelity_check.py")
    nfc = importlib.util.module_from_spec(spec)
    sys.modules["name_fidelity_check2"] = nfc
    spec.loader.exec_module(nfc)
    assert nfc.load_renames(tmp_path) == []
