"""
Regression tests for ERRORS_NEW_ANALYSIS/VolumeMounts_Dict_Key_Ordering_Drift.

2 CHANGED errors, described as cosmetic. error.txt offers two options and picks
one:

  "A. SUPPRESS / FIX THE COMPARISON — teach the fidelity checker to compare
      mappings order-insensitively. RECOMMENDED. If the checker is currently
      doing a string or ordered comparison on dicts, that is itself the defect,
      and fixing it silences an entire class of future false positives rather
      than this one instance.
   B. NORMALIZE THE OUTPUT — sort keys consistently when emitting [...] treats
      the symptom."

Option A, and the recommendation was right — but the diagnosis in the report is
not. Measured directly, the checker was ALREADY order-insensitive for both
things the report names:

    dict key order differs      -> NONE
    list entry order differs    -> NONE

The actual trigger is narrower and more interesting: a list of volumeMounts
where several entries SHARE A NAME and differ only by subPath. `_by_name` bailed
to positional comparison the moment names repeated, so a pure reordering of
those entries read as a wholesale CHANGED of the entire list — which is exactly
what the report shows, a raw=[...] render=[...] dump of the whole thing rather
than a per-field diff.

That shape is not exotic. Mounting one volume several times under different
subPaths is the standard way to project individual ConfigMap keys onto
individual file paths, and it is the SAME shape as the previous round's
VolumeMount_SubPath_Lost_Duplicate_Name_Collision — where the CONVERTER lost
data for the same reason the CHECKER now produced noise: both used the bare
name as an identity that isn't one.

named_list_to_map already keys these '<name>:<subPath or mountPath>'. _by_name
now does the same. This is the third time in this batch that a defect turned out
to be the converter and the checker disagreeing about object identity — see
folder 6 (ports keyed on number vs number+protocol).

DO NOT CONFLATE the two volumeMount defects, as error.txt warns: the previous
one LOST DATA, this one loses nothing. Same field, entirely different severity.
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

MOUNTS = [
    {"name": "config", "mountPath": "/etc/dex/config.yaml", "subPath": "config.yaml"},
    {"name": "config", "mountPath": "/etc/dex/connectors.yaml", "subPath": "connectors.yaml"},
    {"name": "config", "mountPath": "/etc/dex/theme.css", "subPath": "theme.css"},
]


def _diff(raw_mounts, rendered_mounts):
    return conv._fidelity_diff(
        "Deployment",
        {"spec": {"volumeMounts": raw_mounts}},
        {"spec": {"volumeMounts": rendered_mounts}})


# ---------------------------------------------------------------------------
# 1. What the report SAID the defect was — already handled before this folder
# ---------------------------------------------------------------------------

def test_dict_key_order_was_never_the_problem():
    """Recorded because the report's stated root cause ("Helm/YAML outputs
    sorted keys vs Python insertion order") is not what was happening, and the
    next reader should not go looking for a bug that was never there."""
    assert _diff([{"name": "cfg", "mountPath": "/etc/x"}],
                 [{"mountPath": "/etc/x", "name": "cfg"}]) == []


def test_list_entry_order_with_unique_names_was_never_the_problem():
    assert _diff([{"name": "a", "mountPath": "/a"}, {"name": "b", "mountPath": "/b"}],
                 [{"name": "b", "mountPath": "/b"}, {"name": "a", "mountPath": "/a"}]) == []


# ---------------------------------------------------------------------------
# 2. The actual defect
# ---------------------------------------------------------------------------

def test_reordered_duplicate_name_mounts_are_not_a_change():
    """THE defect. Same three mounts, different order, nothing lost."""
    assert _diff(MOUNTS, list(reversed(MOUNTS))) == []


def test_it_reports_per_field_rather_than_dumping_the_whole_list():
    """The symptom that identified the cause: a positional fallback cannot say
    WHICH entry differs, so it dumps raw=[...] render=[...] for everything. A
    real difference must now name the one field that changed."""
    changed = [dict(m) for m in MOUNTS]
    changed[1]["mountPath"] = "/etc/dex/CHANGED.yaml"
    diffs = _diff(MOUNTS, changed)
    assert len(diffs) == 1
    severity, path, _detail = diffs[0]
    assert severity == "CHANGED"
    assert path.endswith("mountPath"), path
    assert "connectors.yaml" in path, f"the diff does not identify which mount: {path}"


def test_a_dropped_duplicate_name_mount_is_still_reported():
    """The severity guard. The previous round's defect was that one of these
    mounts silently disappeared; a checker that paired them too eagerly would
    hide exactly that."""
    diffs = _diff(MOUNTS, MOUNTS[:2])
    assert [d[0] for d in diffs] == ["LOST"]
    assert "theme.css" in diffs[0][1]


def test_an_added_mount_is_still_reported():
    extra = MOUNTS + [{"name": "config", "mountPath": "/etc/dex/extra", "subPath": "extra"}]
    assert [d[0] for d in _diff(MOUNTS, extra)] == ["ADDED"]


def test_the_checker_identity_matches_the_converters():
    """The root cause stated as an invariant: converter and checker must agree
    on what identifies a mount. named_list_to_map keys '<name>:<subPath>'; so
    does _by_name now."""
    assert set(conv.named_list_to_map(MOUNTS)) == set(conv._by_name(MOUNTS))


# ---------------------------------------------------------------------------
# 3. The fallback is still conservative where it has to be
# ---------------------------------------------------------------------------

def test_duplicate_names_with_no_discriminator_fall_back_to_positional():
    """If the composite is ALSO ambiguous, pairing would be guesswork. Better a
    coarse CHANGED than a confident wrong answer."""
    ambiguous = [{"name": "c"}, {"name": "c"}]
    assert conv._by_name(ambiguous) is None


def test_unnamed_entries_still_fall_back():
    assert conv._by_name([{"mountPath": "/a"}, {"mountPath": "/b"}]) is None


def test_order_sensitive_lists_are_untouched():
    """`args` is the canonical case where order IS the meaning. _by_name must
    keep refusing to index it."""
    assert conv._by_name(["-c", "sleep 1"]) is None
    diffs = conv._fidelity_diff("Deployment",
                                {"spec": {"args": ["a", "b"]}},
                                {"spec": {"args": ["b", "a"]}})
    assert diffs and diffs[0][0] == "CHANGED"


def test_containers_and_ports_still_pair_by_their_own_identity():
    """_by_name serves several lists. The duplicate-name branch must not change
    how the unique-name ones behave."""
    assert set(conv._by_name([{"name": "app"}, {"name": "sidecar"}])) == {"app", "sidecar"}
    assert set(conv._by_name([{"containerPort": 9125, "protocol": "TCP"},
                              {"containerPort": 9125, "protocol": "UDP"}])) == \
        {"#port:9125/TCP", "#port:9125/UDP"}


# ---------------------------------------------------------------------------
# 4. Corpus coverage
# ---------------------------------------------------------------------------

def test_the_corpus_contains_duplicate_name_subpath_mounts():
    """There were NO subPath mounts in the corpus at all before this folder —
    so neither this fix nor the previous round's data-loss fix had any standing
    coverage."""
    found = False
    for f in SHOWCASE.rglob("*.yaml"):
        for d in yaml.safe_load_all(f.read_text(encoding="utf-8")):
            if not d or d.get("kind") not in ("Deployment", "StatefulSet", "DaemonSet"):
                continue
            for c in (d["spec"]["template"]["spec"].get("containers") or []):
                mounts = c.get("volumeMounts") or []
                names = [m["name"] for m in mounts]
                if len(names) != len(set(names)) and all(m.get("subPath") for m in mounts
                                                         if names.count(m["name"]) > 1):
                    found = True
    assert found, "no duplicate-name subPath mounts in the corpus"


def test_the_corpus_mounts_are_not_already_in_sorted_order():
    """If the raw order matched the chart's sorted-key output, no reordering
    would occur and the coverage would be inert."""
    for f in SHOWCASE.rglob("*.yaml"):
        for d in yaml.safe_load_all(f.read_text(encoding="utf-8")):
            if not d or d.get("kind") != "Deployment":
                continue
            for c in (d["spec"]["template"]["spec"].get("containers") or []):
                mounts = c.get("volumeMounts") or []
                subs = [m.get("subPath") for m in mounts if m.get("subPath")]
                if len(subs) > 1 and subs != sorted(subs):
                    return
    pytest.fail("corpus subPath mounts are in sorted order — the reorder is never exercised")
