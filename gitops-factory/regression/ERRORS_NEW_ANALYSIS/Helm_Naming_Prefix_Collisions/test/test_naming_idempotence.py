"""
Regression tests for ERRORS_NEW_ANALYSIS/Helm_Naming_Prefix_Collisions.

Severity is disputed three ways inside one session — Critical (~15), High (~10),
and "Benign / internally consistent" (9) — and error.txt says which signal to
chase:

  "THE DOUBLED NAME IS THE SIGNAL WORTH CHASING: 'interconn-kafka-kafbat'
   becoming 'interconn-kafka-kafbat-interconn-kafka-kafbat' is not a collision
   rename, it is a prefix applied to a name that already carried it. That is an
   idempotence bug in the naming logic, and it is the same failure visible in
   Argo_Events_Sensor_Double_Prefix. Fixing the idempotence guard likely
   resolves both folders at once."

VERDICT ON SEVERITY: the "benign" reading is correct about CORRECTNESS and wrong
about cost. `NamespaceRegistry.claim` produced a unique name and every reference
followed it — the fidelity report's own annotation on the kafbat line says
"Expected/consistent", and that is verifiable. So nothing was broken. But the
name was twice as long as it needed to be, read as a bug to every human who saw
it (three separate passes of the report flagged it), and pushed against the
63-character limit that validate_resource_names enforces later — on a name that
was ALREADY 22 characters before doubling.

So: not a correctness bug, a name-quality bug with a real failure mode at the
length limit. Fixed by making the prefix idempotent.

WHAT THE FIX MUST NOT DO is the interesting half. Skipping the prefix naively
hands back a name that is already claimed — which is the exact collision this
function exists to resolve. The guard therefore picks a base and then WALKS it
until free. The tests below spend more effort on that than on the doubling.
"""
import importlib.util
import pathlib
import sys

import pytest

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


def _registry(*claims):
    reg = conv.NamespaceRegistry("interconn")
    for kind, name, owner in claims:
        reg.claim(kind, name, f"hash-{owner}", owner)
    return reg


# ---------------------------------------------------------------------------
# 1. The doubling
# ---------------------------------------------------------------------------

def test_the_reports_exact_example_no_longer_doubles():
    reg = _registry(("ServiceAccount", "interconn-kafka-kafbat", "some-other-ms"))
    final, action, _ = reg.claim("ServiceAccount", "interconn-kafka-kafbat",
                                 "hash-b", "interconn-kafka-kafbat")
    assert final != "interconn-kafka-kafbat-interconn-kafka-kafbat"
    assert action == "renamed"
    assert final == "interconn-kafka-kafbat-2"


def test_a_name_that_already_starts_with_the_owner_is_not_re_prefixed():
    reg = _registry(("ConfigMap", "kafka-config", "other"))
    final, _, _ = reg.claim("ConfigMap", "kafka-config", "hash-b", "kafka")
    assert final == "kafka-config-2"
    assert not final.startswith("kafka-kafka-")


@pytest.mark.parametrize("owner,name", [
    ("kafbat", "kafbat"),
    ("kafka", "kafka-config"),
    ("interconn-kafka", "interconn-kafka-kafbat"),
])
def test_no_produced_name_ever_repeats_the_owner_token_run(owner, name):
    reg = _registry(("ConfigMap", name, "squatter"))
    final, _, _ = reg.claim("ConfigMap", name, "hash-b", owner)
    assert f"{owner}-{owner}" not in final
    assert not final.startswith(f"{owner}-{owner}")


# ---------------------------------------------------------------------------
# 2. What the fix must NOT break — uniqueness
# ---------------------------------------------------------------------------

def test_the_returned_name_is_never_one_that_is_already_claimed():
    """The trap in this fix. Skipping the prefix could hand back the very name
    that collided, silently making two microservices own one object — a far
    worse defect than a long name."""
    reg = _registry(("ConfigMap", "kafka-config", "other"))
    final, _, _ = reg.claim("ConfigMap", "kafka-config", "hash-b", "kafka")
    assert final != "kafka-config"


def test_three_way_collision_on_a_self_named_object_stays_unique():
    reg = _registry(("ConfigMap", "kafbat", "ms-a"))
    second, _, _ = reg.claim("ConfigMap", "kafbat", "h2", "kafbat")
    third, _, _ = reg.claim("ConfigMap", "kafbat", "h3", "kafbat-again")
    assert len({"kafbat", second, third}) == 3, (second, third)


def test_the_suffix_walk_skips_an_already_taken_number():
    reg = _registry(("ConfigMap", "kafbat", "ms-a"), ("ConfigMap", "kafbat-2", "ms-b"))
    final, _, _ = reg.claim("ConfigMap", "kafbat", "h3", "kafbat")
    assert final == "kafbat-3"


def test_collisions_are_per_kind_not_global():
    """A ConfigMap and a Secret may share a name; renaming one because the other
    exists would be inventing a collision."""
    reg = _registry(("ConfigMap", "shared", "ms-a"))
    final, action, _ = reg.claim("Secret", "shared", "h", "ms-b")
    assert (final, action) == ("shared", "own")


# ---------------------------------------------------------------------------
# 3. The ordinary case is untouched — no churn
# ---------------------------------------------------------------------------

def test_an_unrelated_owner_still_gets_the_plain_prefix():
    """The whole point of keeping the change narrow: names that were already
    sensible must render byte-identically. Verified at corpus scale too — the
    regenerated output-showcase was byte-identical after this fix."""
    reg = _registry(("ConfigMap", "app-config", "svc-a"))
    final, action, note = reg.claim("ConfigMap", "app-config", "h2", "svc-b")
    assert final == "svc-b-app-config"
    assert action == "renamed"
    assert note and "name collision" in note


def test_first_claim_is_unchanged():
    reg = conv.NamespaceRegistry("interconn")
    assert reg.claim("ConfigMap", "app-config", "h", "svc-a") == ("app-config", "own", None)


def test_same_owner_reclaiming_is_still_a_no_op():
    reg = _registry(("ConfigMap", "app-config", "svc-a"))
    assert reg.claim("ConfigMap", "app-config", "h", "svc-a")[:2] == ("app-config", "own")


def test_shared_ownership_semantics_are_unchanged():
    """`_shared` has its own two branches above the rename path and neither
    should be reachable through the new code."""
    reg = _registry(("ConfigMap", "ca-bundle", "_shared"))
    final, action, note = reg.claim("ConfigMap", "ca-bundle", "h", "svc-a")
    assert (final, action) == ("ca-bundle", "reference_shared")
    assert "already owned by the namespace-shared release" in note


def test_promotion_to_shared_is_unchanged():
    reg = _registry(("ConfigMap", "ca-bundle", "svc-a"))
    final, action, note = reg.claim("ConfigMap", "ca-bundle", "h", "_shared")
    assert (final, action) == ("ca-bundle", "own")
    assert "promoted to namespace-shared" in note


# ---------------------------------------------------------------------------
# 4. The name stays inside Kubernetes' limit
# ---------------------------------------------------------------------------

def test_a_long_self_named_object_no_longer_doubles_past_the_limit():
    """The concrete failure mode behind the "Critical" rating. A 40-character
    name doubles to 81, past the 63-character limit for most Kubernetes object
    names — which validate_resource_names rejects at conversion time, failing
    the whole run."""
    long_name = "interconn-kafka-kafbat-consumer-group-abc"
    # Long enough that doubling it definitely breaches the limit, which is the
    # premise of the test rather than an incidental fact about the string.
    assert len(long_name) * 2 + 1 > 63
    reg = _registry(("ServiceAccount", long_name, "squatter"))
    final, _, _ = reg.claim("ServiceAccount", long_name, "h", long_name)
    assert len(final) <= 63, f"{final} is {len(final)} chars"
    assert len(final) < len(f"{long_name}-{long_name}")
