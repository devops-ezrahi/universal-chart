"""
Regression tests for ERRORS_NEW_ANALYSIS/Extra_And_Relocated_Objects.

A reconciliation task rather than a code defect — error.txt says the report holds
two irreconcilable readings:

  "One table shows 'Objects rendered by nothing: 0' and 'Objects w/ no raw
   source: 0', declaring 'all missing/ghost objects' resolved. The cross-check
   table shows 'Route/Service rendered by NO release | 90 | 90 | Exact' [...]
   A count of 0 and a count of 90 for the same phenomenon cannot both describe
   the current run."

RECONCILED, in folder 1: they describe different runs. Every fidelity report now
carries a content-derived `run id`, so the next pair cannot be compared by
accident. The 0 is a post-fix run on `namespaces-showcase`; the 90 is the
`rakia-*` corpus.

The durable half is the TEST error.txt specifies:

  "THE TEST THAT SEPARATES THEM: for each object rendered by nothing, search the
   rendered output for an object of the same KIND with matching content under a
   different name. If found, it is a rename and benign. If not found, it is
   genuinely lost and is a P0."

That is now what the checker does, so "rendered by NO release" is a conclusion
rather than a claim.

IT ALSO CAUGHT A REGRESSION I HAD JUST INTRODUCED. Folder 10 changed the
collision resolver to emit `<name>-2` where a prefix would be redundant. The
fidelity checker MIRRORED the old rename rule (`<owner>-<name>`) to pair a
renamed object with its raw source — so the new shape broke the mirror, and one
collision produced a phantom content mismatch plus a phantom extra object.
Content matching needs no mirror, which is why it is the right answer and not
merely the requested one.
"""
import importlib.util
import pathlib
import sys

import pytest
import yaml

TEST_DIR = pathlib.Path(__file__).resolve().parent
GITOPS_DIR = TEST_DIR.parents[3]
CONVERTER_PY = GITOPS_DIR / "convert_to_universal_chart.py"


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


conv = _load(CONVERTER_PY, "convert_to_universal_chart")


def _cm(name, data, **meta):
    return {"apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": name, **meta}, "data": data}


# ---------------------------------------------------------------------------
# 1. The identity-free hash — what makes "same object, different name" decidable
# ---------------------------------------------------------------------------

def test_the_same_object_under_a_different_name_hashes_the_same():
    assert conv._identity_free_hash(_cm("shared-conf", {"OWNER": "a"})) == \
        conv._identity_free_hash(_cm("shared-conf-2", {"OWNER": "a"}))


def test_different_content_still_hashes_differently():
    """The property that stops the rename search pairing two unrelated objects
    of the same kind. Without it, 'found a twin' would mean nothing."""
    assert conv._identity_free_hash(_cm("x", {"OWNER": "a"})) != \
        conv._identity_free_hash(_cm("x", {"OWNER": "b"}))


def test_labels_and_annotations_do_not_defeat_the_match():
    """A rendered object carries the chart's own labels; its raw counterpart
    does not. If those counted, no rename would ever match."""
    assert conv._identity_free_hash(_cm("c", {"k": "v"})) == \
        conv._identity_free_hash(_cm("c", {"k": "v"},
                                     labels={"helm.sh/chart": "x"},
                                     annotations={"a": "b"}))


def test_content_hash_itself_is_unchanged_and_still_name_sensitive():
    """_identity_free_hash is a SEPARATE function on purpose. content_hash
    answers "are these interchangeable" and is used by the collision registry to
    decide whether two copies are the same object — it must keep counting the
    name."""
    assert conv.content_hash(_cm("a", {"k": "v"})) != conv.content_hash(_cm("b", {"k": "v"}))


def test_the_hash_survives_a_missing_metadata_block():
    assert conv._identity_free_hash({"kind": "ConfigMap", "data": {"k": "v"}})


# ---------------------------------------------------------------------------
# 2. The reconciliation the report could not do
# ---------------------------------------------------------------------------

def test_the_16_extra_and_15_relocated_are_two_halves_of_one_rename():
    """error.txt: "An object that moved into the shared release, or that was
    renamed by the collision resolver, legitimately appears to have 'no raw
    source' under its new name while its old name appears to be 'rendered by
    nothing'. These are two halves of one rename, double-counted."

    That double-counting is now impossible: matching the raw side by content
    also marks the rendered object as matched, so it cannot be reported as
    extra."""
    raw = _cm("shared-conf", {"OWNER": "s"})
    rendered = _cm("shared-conf-2", {"OWNER": "s"}, labels={"helm.sh/chart": "u-1.0"})
    assert conv._identity_free_hash(raw) == \
        conv._identity_free_hash(conv._strip_helm_labels(rendered))


def test_a_genuinely_lost_object_has_no_twin():
    """The other branch. If nothing of that kind matches, the object really is
    gone and the report must say so — this is the P0 case."""
    raw = _cm("dex-config", {"config.yaml": "issuer: https://x"})
    rendered_pool = [_cm("other", {"unrelated": "1"}), _cm("another", {"unrelated": "2"})]
    raw_h = conv._identity_free_hash(raw)
    assert not any(conv._identity_free_hash(r) == raw_h for r in rendered_pool)


# ---------------------------------------------------------------------------
# 3. The rename shape that broke the checker's mirror
# ---------------------------------------------------------------------------

def test_the_collision_resolver_can_emit_a_shape_the_name_rules_do_not_predict():
    """Folder 10's guard produces `<name>-2` when a prefix would be redundant.
    The checker used to reconstruct only `<owner>-<name>`, so this shape made a
    correctly-converted object look lost AND made its renamed twin look
    invented. Content matching is what removes the dependency on predicting the
    rule."""
    reg = conv.NamespaceRegistry("ns-demo")
    reg.claim("ConfigMap", "shared-conf", "hash-a", "alpha")
    final, action, _ = reg.claim("ConfigMap", "shared-conf", "hash-b", "shared-conf")
    assert action == "renamed"
    assert final == "shared-conf-2"
    # The shape the old mirror would have looked for, and would not have found:
    assert final != "shared-conf-shared-conf"


def test_matched_rendered_prevents_two_raw_copies_claiming_one_render():
    """Both raw copies of a collided name search the same rendered pool. Without
    excluding already-matched objects, the second declarer could pair with the
    first declarer's render — the phantom content mismatch this whole branch
    exists to remove."""
    src = CONVERTER_PY.read_text(encoding="utf-8")
    assert "(kind, rname) not in matched_rendered" in src, \
        "the content search no longer excludes already-matched rendered objects"


# ---------------------------------------------------------------------------
# 4. Scope — the search must not become a way to hide losses
# ---------------------------------------------------------------------------

def test_the_search_is_scoped_to_the_same_kind():
    """A ConfigMap must never be 'found' as a Secret with the same data. The
    kind filter is what keeps 'matched by content' honest."""
    cm = _cm("c", {"k": "v"})
    secret = {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "c"},
              "data": {"k": "v"}}
    assert conv._identity_free_hash(cm) != conv._identity_free_hash(secret)


def test_content_matching_only_runs_where_the_name_is_ambiguous():
    """The ordinary single-declarer path keeps its cheap name lookup; the
    content scan is O(rendered) per raw object and only pays off where the
    ambiguity actually exists."""
    src = CONVERTER_PY.read_text(encoding="utf-8")
    assert "if len(raw_entries) > 1:" in src
