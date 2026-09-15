"""
Regression tests for ERRORS_ANALYSIS/ConfigMap_Secret_Identical_Content_Duplicated.

Three microservices in one namespace each declare a ConfigMap called
`zookeeper-config` with byte-identical content. `NamespaceRegistry.claim` gives
the first claimant the name and renames the rest to `<owner>-<name>`, so one
shared cluster object becomes three.

**Recorded decision: keep the rename, make the warning actionable.** Not
auto-sharing, and the report's own analysis says why better than a summary
could:

  > "The converter doesn't auto-share because it can't know if the original
  > cluster actually had a shared Secret or two separate ones that happened to
  > have identical content. It takes the safe path."

That is right, and the asymmetry matters: auto-sharing two objects that were
genuinely independent couples two releases forever — a change to one silently
changes the other, and nothing in the rendered output would show it. Leaving
them split costs a duplicate object and a warning. The reversible mistake is the
one to make.

What the warning did NOT do was tell the operator what to do about it. It
reported the rename and noted "(identical content)" as a bare parenthetical.
It now names the action — move the object into the namespace's `shared.yaml` —
and says why the converter will not do it for them. Differing content gets a
different message, because there is no decision to make there.

SUPERSEDED END TO END (fork merge, promote_shared_candidates): a namespace-
scoped name is unique within its namespace, so byte-identical declarations of
one name ARE one source object, and the converter now lifts them into the shared
release before building. The fixture's three identical ConfigMaps therefore
render once, from `shared`; the differing Secret still takes the rename path.
`NamespaceRegistry.claim` keeps the rename + advice as a safety net, which the
unit tests below still cover.
"""
import importlib.util
import pathlib
import subprocess
import sys

import pytest

TEST_DIR = pathlib.Path(__file__).resolve().parent
GITOPS_DIR = TEST_DIR.parents[3]
CHART_DIR = GITOPS_DIR.parent
CONVERTER_PY = GITOPS_DIR / "convert_to_universal_chart.py"
FIXTURE_DIR = TEST_DIR / "fixture"


def _load_converter():
    spec = importlib.util.spec_from_file_location("convert_to_universal_chart", CONVERTER_PY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["convert_to_universal_chart"] = mod
    spec.loader.exec_module(mod)
    return mod


conv = _load_converter()


def _claim_twice(content_a, content_b):
    reg = conv.NamespaceRegistry("platform-data")
    reg.claim("ConfigMap", "zookeeper-config", conv.content_hash(content_a), "zookeeper-zbs")
    return reg.claim("ConfigMap", "zookeeper-config", conv.content_hash(content_b), "zookeeper")


# --------------------------------------------------------------------------
# The decision: rename, not auto-share
# --------------------------------------------------------------------------

def test_identical_content_is_still_renamed_not_shared():
    name, action, _note = _claim_twice({"a": 1}, {"a": 1})
    assert action == "renamed"
    # Was `zookeeper-zookeeper-config`. ERRORS_NEW_ANALYSIS/Helm_Naming_Prefix_Collisions
    # made `claim` idempotent: when the microservice prefix is already the start of
    # the object name, prefixing doubles it (`zookeeper-zookeeper-config`) and can
    # push past the 63-character limit, so a numeric suffix is used instead. The
    # DECISION this test guards — rename rather than auto-share — is unchanged.
    assert name == "zookeeper-config-2"


def test_shared_yaml_is_still_the_only_thing_that_shares():
    """The escape hatch the warning now points at has to actually work."""
    reg = conv.NamespaceRegistry("platform-data")
    reg.claim("ConfigMap", "zookeeper-config", conv.content_hash({"a": 1}), "_shared")
    name, action, note = reg.claim(
        "ConfigMap", "zookeeper-config", conv.content_hash({"a": 1}), "zookeeper")
    assert action == "reference_shared"
    assert name == "zookeeper-config", "the referring release must keep the original name"
    assert "shared" in note


def test_the_first_claimant_keeps_the_original_name():
    reg = conv.NamespaceRegistry("platform-data")
    name, action, note = reg.claim(
        "ConfigMap", "zookeeper-config", conv.content_hash({"a": 1}), "zookeeper-zbs")
    assert (name, action, note) == ("zookeeper-config", "own", None)


def test_the_same_owner_claiming_twice_is_not_a_collision():
    reg = conv.NamespaceRegistry("platform-data")
    h = conv.content_hash({"a": 1})
    reg.claim("ConfigMap", "c", h, "ms")
    assert reg.claim("ConfigMap", "c", h, "ms") == ("c", "own", None)


# --------------------------------------------------------------------------
# The warning: actionable for identical, distinct for differing
# --------------------------------------------------------------------------

def test_identical_content_warning_names_the_action():
    _n, _a, note = _claim_twice({"a": 1}, {"a": 1})
    assert "identical content" in note
    assert "BYTE-IDENTICAL" in note
    assert "shared.yaml" in note, f"the warning does not say what to do: {note}"
    assert "zookeeper-config-2" in note


def test_differing_content_gets_a_different_message():
    _n, _a, note = _claim_twice({"a": 1}, {"a": 2})
    assert "DIFFERING content" in note
    assert "shared.yaml" not in note, (
        "sharing is NOT the advice when the two objects differ; suggesting it "
        f"would be wrong: {note}")
    assert "no action is needed" in note


def test_the_warning_names_both_microservices():
    _n, _a, note = _claim_twice({"a": 1}, {"a": 1})
    assert "zookeeper-zbs" in note and "zookeeper" in note
    assert "platform-data" in note


# --------------------------------------------------------------------------
# End to end — the report's own zookeeper case
# --------------------------------------------------------------------------

def test_the_fixture_reproduces_the_reported_case(tmp_path):
    out = tmp_path / "out"
    proc = subprocess.run(
        [sys.executable, str(CONVERTER_PY), "--input", str(FIXTURE_DIR), "--output", str(out),
         "--chart", str(CHART_DIR), "--skip-verify"],
        capture_output=True, text=True, cwd=str(GITOPS_DIR),
    )
    assert proc.returncode == 0, f"converter failed:\n{proc.stdout}\n{proc.stderr}"

    report = (out / "report" / "conflicts_and_warnings.txt").read_text(encoding="utf-8")
    # Three microservices declare the ConfigMap byte-identically: promoted, not renamed.
    assert "ConfigMap/zookeeper-config: byte-identical in" in report, report
    assert "ConfigMap/zookeeper-config: name collision" not in report, report
    # The Secret collides too, but with DIFFERING content for one of the pair.
    assert "Secret/zookeeper-auth" in report
    assert "DIFFERING content" in report, "the contrast case is not being exercised"


def test_the_promoted_object_is_created_once_by_the_shared_release(tmp_path):
    """One source object, one rendered copy — owned by `shared`, never orphaned.
    Full reference-integrity coverage lives in
    ConfigMap_Secret_References_Prefixed_With_Release."""
    out = tmp_path / "out"
    subprocess.run(
        [sys.executable, str(CONVERTER_PY), "--input", str(FIXTURE_DIR), "--output", str(out),
         "--chart", str(CHART_DIR), "--skip-verify"],
        capture_output=True, text=True, cwd=str(GITOPS_DIR), check=True,
    )
    import yaml
    owners = {}
    for vf in sorted((out / "platform-data" / "values").glob("*.yaml")):
        vals = yaml.safe_load(vf.read_text(encoding="utf-8")) or {}
        base = out / "base" / f"{vf.stem}.yaml"
        if base.exists():
            vals = conv.deep_merge(
                yaml.safe_load(base.read_text(encoding="utf-8")) or {}, vals)
        for n in (vals.get("configMaps") or {}):
            owners.setdefault(n, []).append(vf.stem)
    copies = {n: o for n, o in owners.items() if "zookeeper-config" in n}
    assert copies == {"zookeeper-config": ["shared"]}, (
        f"expected one copy, owned by the shared release; got {copies}")
