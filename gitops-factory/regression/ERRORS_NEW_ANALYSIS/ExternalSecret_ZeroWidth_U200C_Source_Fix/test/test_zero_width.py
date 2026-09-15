"""
Regression tests for ERRORS_NEW_ANALYSIS/ExternalSecret_ZeroWidth_U200C_Source_Fix.

A defect in the SOURCE DATA rather than in the converter, and the two analysis
rounds disagree about where to fix it:

  previous round: "Do not modify raw manifests; fix Helm templates/values or the
                   converter script" -> sanitize inside the converter.
  this report:    "Fix the raw source manifests in the cluster/upstream. [...]
                   The chart's auto-stripping works but is a workaround, not a
                   fix."

  error.txt: "Both are defensible; pick one deliberately. The strongest answer is
              BOTH — repair the upstream manifest so the source of truth is
              clean, AND keep a defensive strip in the converter so a future
              paste cannot reintroduce it silently."

BOTH, with the observation that a defensive strip is only a workaround while it
is SILENT. It was not: `_sanitize_identifiers` already recorded every removal in
conflicts_and_warnings.txt with the words "Fix the raw manifest at source". What
was missing is the other half of error.txt's ask:

  "SEARCH WIDER THAN THIS ONE FIELD: grep every raw manifest for the whole
   zero-width family — U+200B, U+200C, U+200D, U+FEFF. Only secretStoreRef.kind
   surfaced because that is where a mismatch happened to be detectable; the same
   paste could have landed anywhere."

The strip covered exactly three fields — metadata.name, secretStoreRef.name,
secretStoreRef.kind — so a stray character in serviceAccountName, a volume's
claimName or a Route's backing Service name went through untouched and unseen.
Now: identifier-valued keys are stripped anywhere in the document, and
everything else that carries one is REPORTED without being touched.

That last distinction is the important one and is why this is not simply "strip
everything". ConfigMap/Secret data may legitimately contain a zero-width
character — a test fixture, a translation string, a template — and silently
rewriting payload to tidy up a name is a worse bug than the one being fixed.
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

ZWSP, ZWNJ, ZWJ, BOM = "​", "‌", "‍", "﻿"


# ---------------------------------------------------------------------------
# 1. The whole family, not just the one that surfaced
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ch,label", [
    (ZWSP, "U+200B ZERO WIDTH SPACE"),
    (ZWNJ, "U+200C ZERO WIDTH NON-JOINER"),
    (ZWJ, "U+200D ZERO WIDTH JOINER"),
    (BOM, "U+FEFF BOM"),
    ("⁠", "U+2060 WORD JOINER"),
    ("­", "U+00AD SOFT HYPHEN"),
])
def test_every_named_zero_width_character_is_stripped(ch, label):
    assert conv.strip_zero_width(f"Secret{ch}Store") == "SecretStore", label


def test_non_strings_pass_through_untouched():
    for value in (None, 7, True, {"a": 1}, ["x"]):
        assert conv.strip_zero_width(value) is value or conv.strip_zero_width(value) == value


def test_a_clean_string_is_returned_unchanged():
    assert conv.strip_zero_width("SecretStore") == "SecretStore"


# ---------------------------------------------------------------------------
# 2. The report's exact field — and the wider sweep
# ---------------------------------------------------------------------------

def _es(**overrides):
    doc = {
        "apiVersion": "external-secrets.io/v1beta1", "kind": "ExternalSecret",
        "metadata": {"name": "session-store-signing-key"},
        "spec": {"secretStoreRef": {"name": "session-store-vault", "kind": "SecretStore"}},
    }
    for path, value in overrides.items():
        node = doc
        *parts, last = path.split(".")
        for p in parts:
            node = node[p]
        node[last] = value
    return doc


def test_the_reported_field_is_repaired_and_warned_about():
    notes = []
    cleaned = conv.sanitize_object(_es(**{"spec.secretStoreRef.kind": f"Secret{ZWNJ}Store"}), notes)
    assert cleaned["spec"]["secretStoreRef"]["kind"] == "SecretStore"
    assert any("U+200C" in n for n in notes)
    assert any("Fix the raw manifest at source" in n for n in notes), \
        "a silent strip is what makes it a workaround rather than a stopgap"


def test_the_warning_names_the_OBJECT_not_the_repaired_value():
    """It used to print `ExternalSecret/SecretStore` for a kind-field repair —
    the cleaned VALUE in the position where a reader expects the object's name.
    A warning whose whole purpose is to point at one line must point at the
    right one."""
    notes = []
    conv.sanitize_object(_es(**{"spec.secretStoreRef.kind": f"Secret{ZWNJ}Store"}), notes)
    assert any("session-store-signing-key" in n for n in notes)
    assert not any("ExternalSecret/SecretStore" in n for n in notes)


@pytest.mark.parametrize("path,key", [
    ("spec.template.spec", "serviceAccountName"),
    ("spec.template.spec", "priorityClassName"),
])
def test_identifiers_beyond_the_three_original_fields_are_now_stripped(path, key):
    """error.txt's "search wider than this one field". Before this, only
    metadata.name and the two secretStoreRef fields were covered."""
    doc = {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "api"},
           "spec": {"template": {"spec": {key: f"api{ZWNJ}sa",
                                          "containers": [{"name": "api"}]}}}}
    notes = []
    cleaned = conv.sanitize_object(doc, notes)
    assert cleaned["spec"]["template"]["spec"][key] == "apisa"
    assert any(key in n for n in notes)


def test_a_nested_reference_name_is_stripped():
    doc = {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "api"},
           "spec": {"template": {"spec": {"containers": [{"name": "api"}], "volumes": [
               {"name": "cfg", "configMap": {"name": f"app{ZWSP}config"}}]}}}}
    cleaned = conv.sanitize_object(doc, [])
    assert cleaned["spec"]["template"]["spec"]["volumes"][0]["configMap"]["name"] == "appconfig"


# ---------------------------------------------------------------------------
# 3. What must NOT be stripped
# ---------------------------------------------------------------------------

def test_configmap_data_is_reported_but_never_rewritten():
    """The line this fix must not cross. Payload may legitimately contain a
    zero-width character; silently corrupting it to tidy up a name would be a
    worse bug than the one being fixed."""
    doc = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "c"},
           "data": {"template.txt": f"hello{ZWNJ}world"}}
    notes = []
    cleaned = conv.sanitize_object(doc, notes)
    assert cleaned["data"]["template.txt"] == f"hello{ZWNJ}world", "payload was rewritten"
    assert any("deliberately NOT stripped" in n for n in notes), \
        "payload containing an invisible character was neither fixed nor reported"


def test_a_key_named_name_inside_data_is_not_treated_as_an_identifier():
    doc = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "c"},
           "data": {"name": f"a{ZWNJ}b"}}
    assert conv.sanitize_object(doc, [])["data"]["name"] == f"a{ZWNJ}b"


def test_label_values_are_not_rewritten():
    """Labels are compared, not resolved. A zero-width character there is a
    problem to report, not one to silently repair — and rewriting it would
    change what a selector matches."""
    doc = {"apiVersion": "v1", "kind": "ConfigMap",
           "metadata": {"name": "c", "labels": {"team": f"a{ZWNJ}b"}}, "data": {}}
    assert conv.sanitize_object(doc, [])["metadata"]["labels"]["team"] == f"a{ZWNJ}b"


# ---------------------------------------------------------------------------
# 4. The sweep itself
# ---------------------------------------------------------------------------

def test_find_zero_width_reports_path_and_codepoint():
    hits = conv.find_zero_width({"spec": {"secretStoreRef": {"kind": f"Secret{ZWNJ}Store"}}})
    assert hits == [("spec.secretStoreRef.kind", "U+200C")]


def test_find_zero_width_walks_lists_and_reports_nothing_when_clean():
    assert conv.find_zero_width({"a": [{"b": "clean"}, {"c": "also clean"}]}) == []
    assert conv.find_zero_width({"a": [{"b": f"x{BOM}y"}]}) == [("a[0].b", "U+FEFF")]


def test_find_zero_width_mutates_nothing():
    doc = {"spec": {"kind": f"Secret{ZWNJ}Store"}}
    conv.find_zero_width(doc)
    assert doc["spec"]["kind"] == f"Secret{ZWNJ}Store"


# ---------------------------------------------------------------------------
# 5. Corpus coverage — the one raw manifest that is knowingly wrong
# ---------------------------------------------------------------------------

def test_the_corpus_still_carries_the_zero_width_character():
    """Deliberate, and the only place in this repo where a raw manifest is
    knowingly broken. Deleting the character removes the only standing coverage
    for the strip — which is precisely how a defensive measure rots."""
    text = (SHOWCASE / "identity-auth" / "session-store.yaml").read_text(encoding="utf-8")
    assert ZWNJ in text, "the ZWNJ coverage was 'cleaned up' out of the corpus"


def test_the_corpus_character_is_where_the_report_says_it_is():
    doc = next(d for d in yaml.safe_load_all(
        (SHOWCASE / "identity-auth" / "session-store.yaml").read_text(encoding="utf-8"))
        if d and d.get("kind") == "ExternalSecret")
    assert ZWNJ in doc["spec"]["secretStoreRef"]["kind"]


def test_no_OTHER_corpus_manifest_carries_an_invisible_character():
    """The wider sweep, run over the corpus. One known instance; anything else
    is an accident that should be found now rather than in a report."""
    unexpected = []
    for f in SHOWCASE.rglob("*.yaml"):
        if f.name == "session-store.yaml":
            continue
        for d in yaml.safe_load_all(f.read_text(encoding="utf-8")):
            if d:
                unexpected += [(f.name, w, c) for w, c in conv.find_zero_width(d)]
    assert not unexpected, f"stray invisible characters: {unexpected}"
