# STATUS — Helm_Naming_Prefix_Collisions (TIER 1, folder 10/26)

**Result: FIXED — idempotence guard added, zero churn.** Severity was disputed
three ways in one session; the dispute is resolved below and the answer is
"neither Critical nor benign". The doubling reproduced exactly, the fix removes
it, and the regenerated `output-showcase` is **byte-identical** — names that
were already sensible are untouched. 15 new tests RED→GREEN, showcase fidelity
0 lost / 0 changed, chart's 234 green.

---

## 1. The disputed severity, settled

| Pass | Rating |
|---|---|
| executive summary | Name Prefix Collisions ~10, **High** |
| parallel pass | Helm Naming Prefix Collisions ~15, **Critical** |
| benign list | "Collision rename reference updates (9 CHANGED fields) — Internally consistent after rename" |

**Both readings are half right, and neither is the useful one.**

The benign reading is correct about **correctness**. `NamespaceRegistry.claim`
produced a genuinely unique name, and every reference followed it — the fidelity
report's own annotation on the kafbat line says "Expected/consistent", which is
the checker having already verified it. Nothing was broken.

The Critical reading is picking up something real, but misnaming it. The defect
is not a dangling reference; it is that the name was **twice as long as it
needed to be**:

```
interconn-kafka-kafbat  ->  interconn-kafka-kafbat-interconn-kafka-kafbat
```

Which matters for one concrete reason: `validate_resource_names` rejects
over-length names at conversion time (commit `02a11ab`), and most Kubernetes
object names cap at 63 characters. The source name here is already 22. Any
self-named object beyond ~31 characters doubles past the limit and **fails the
whole conversion run** — a hard stop, arriving as a name-length error that says
nothing about the real cause.

So: **a name-quality bug with a real failure mode at the length limit, not a
correctness bug.** That is the verdict this folder records.

## 2. Root cause

`NamespaceRegistry.claim`, one unconditional line:

```python
new_name = f"{owner}-{name}"
```

When a microservice declares an object under its own name — extremely common,
`ServiceAccount/interconn-kafka-kafbat` inside microservice
`interconn-kafka-kafbat` — and another microservice already claimed that name,
the prefix is applied to a name that already carries it.

Reproduced directly against the real class:

```
=== the report's example: owner == name ===
  final name : interconn-kafka-kafbat-interconn-kafka-kafbat
  doubled?   : True

=== name already carries the owner as a prefix ===
  owner='kafka', name='kafka-config' -> kafka-kafka-config
```

The previous round fixed the *CronJob/Job* doubling (`short_subresource_name`,
commit `d85496a`) by making the subresource key strictly the raw name. It never
touched this function, which is a different code path serving every other kind.

## 3. Files changed

| File | Lines | Change |
|---|---|---|
| `universal/gitops-factory/convert_to_universal_chart.py` | ~537–560 | `NamespaceRegistry.claim`: idempotent prefix + free-name walk |

One function, no template change, no corpus change (see §6).

### The fix, and the trap inside it

```python
base = name if (name == owner or name.startswith(f"{owner}-")) else f"{owner}-{name}"
new_name = base
n = 2
while (kind, new_name) in self.claims:
    new_name = f"{base}-{n}"
    n += 1
```

**The `while` loop is the part that matters.** Skipping the prefix naively hands
back a name that is *already claimed* — which is the exact collision this
function exists to resolve, and would silently give two microservices ownership
of one object. That is a far worse defect than a long name. So the guard picks a
base and then walks it until the name is actually free.

More of the test file is spent on that than on the doubling, deliberately.

**A numeric suffix rather than a prefix** when the prefix would add nothing:
`interconn-kafka-kafbat-2` is shorter, stays under the limit, and is honest
about what it is — a disambiguation, not a namespace.

**Deliberately not the entitlement swap.** `claim_subresource_key` solves the
neighbouring CronJob problem by deciding *which* claimant deserves the plain
name and moving the other aside. That is arguably the more principled answer
here too — the microservice actually named `interconn-kafka-kafbat` has the
strongest claim to `ServiceAccount/interconn-kafka-kafbat`. It was rejected for
this folder because `claim()` returns immediately and earlier callers have
already consumed their result; retroactively renaming a previous claimant means
mutating another microservice's `name_map` after its values are built. That is a
real change to conversion semantics, not an idempotence guard, and it belongs in
a deliberate redesign rather than in a defect fix.

## 4. RED → GREEN

```
BEFORE                                            AFTER
interconn-kafka-kafbat-interconn-kafka-kafbat  ->  interconn-kafka-kafbat-2
kafka-kafka-config                             ->  kafka-config-2
svc-b-app-config                               ->  svc-b-app-config   (unchanged)
```

The third line is the one that makes this safe to ship.

## 5. Zero churn — verified at corpus scale

Regenerated `output-showcase` diffed against the previous folder's run:

```
$ diff -r <f09> <f10>
  run id:      2c7d5a6418ef-dbdc941a2021  ->  2add15b764c6-dbdc941a2021
  output path: .../f09                    ->  .../f10
```

**Every generated file byte-identical.** Only the run id moved — and it moved on
the *converter* half while the *input* half (`dbdc941a2021`) stayed fixed, which
is exactly the attribution folder 1 built the fingerprint for: same input,
changed code, so a reader knows immediately which side to look at.

## 6. Why no corpus coverage was added — stated plainly

Every previous folder in this batch added a raw manifest so the standing
fidelity check becomes the tripwire. This one did not, and the reason is not
oversight.

Triggering the doubling needs a namespace where two microservices declare the
same-kind, same-named object *and* one of those microservices is itself named
after the object. In `namespaces-showcase` that means writing something like
"`db-migrate.yaml` declares `ConfigMap/nightly-report`" — a manifest no real
cluster would contain, added solely to trip a code path. That makes the corpus
less representative of real input, which is the thing that gives it its value.

The trade is acceptable **here specifically** because this defect is
deterministic and fully observable from a pure function: `claim()` takes three
strings and returns a name. The 15 unit tests cover it exhaustively, including
the collision, three-way and suffix-walk cases that a corpus fixture could not
express as clearly. That is not true of the field-loss defects in folders 3–9,
where the corpus is the only realistic detector — which is why they all got one.

## 7. Tests created

`ERRORS_NEW_ANALYSIS/Helm_Naming_Prefix_Collisions/test/test_naming_idempotence.py`
— 15 tests in 4 groups:

1. **The doubling** (5) — the report's exact example; a name already prefixed
   by the owner; and a parametrised sweep asserting no produced name ever
   repeats the owner token run.
2. **Uniqueness, which the fix must not break** (4) — the returned name is never
   one already claimed; a three-way collision on a self-named object stays
   unique; the suffix walk skips an already-taken number; collisions stay
   per-kind rather than global.
3. **The ordinary case is untouched** (5) — an unrelated owner still gets the
   plain prefix with its warning; first claim, same-owner reclaim, shared
   ownership and promotion-to-shared all unchanged.
4. **The length limit** (1) — the concrete failure mode: a 41-character
   self-named object stays under 63 instead of doubling to 83.

## 8. One test bug of mine

`test_a_long_self_named_object_...` asserted `len(long_name) == 40`; the string
is 41. That was a self-check on a comment, not on behaviour. Replaced with the
property the test actually depends on — `len(long_name) * 2 + 1 > 63` — so it
states its own premise instead of hardcoding a miscounted constant.

## 9. Validation chain

| Check | Result |
|---|---|
| Pre-fix probe | `interconn-kafka-kafbat-interconn-kafka-kafbat` |
| Post-fix probe | `interconn-kafka-kafbat-2` |
| Converter re-run, showcase | 124 objects matched, **no fidelity issues** |
| Regenerated output vs previous run | **byte-identical** |
| Folder's own tests | 15 passed |
| All `ERRORS_NEW_ANALYSIS` tests | 171 passed |
| `helm unittest Universal-chart` | 234 passed, 28 suites |

## 10. What could NOT be verified

- **The 25 affected objects across 4 namespaces** (ConfigMaps, Secrets, Service,
  ServiceAccounts). Not in this repo.
- **That every reference followed the rename in the real run.** The fidelity
  report's own annotation asserts it for the kafbat line, and
  `test_carryover_5_*` (folder 2) pins the reference-remapping machinery
  generally — but the claim cannot be re-verified against manifests that are not
  here. This is also the open question in folder 12
  (`Secret_ConfigMap_Rename_Reference_Inconsistency`), which is where it belongs.
- **Whether any real name actually breached 63 characters.** The mechanism is
  proven; the occurrence is not observable from here.

## 11. OBSERVED, DEFERRED

- **`Argo_Events_Sensor_Double_Prefix` (folder 11)** — `error.txt` predicts
  "Fixing the idempotence guard likely resolves both folders at once". That is
  the next folder and the prediction is now testable. Deliberately **not**
  checked here.
- **`Secret_ConfigMap_Rename_Reference_Inconsistency` (folder 12)** — the
  "are all references consistent?" question is the same one this folder's
  benign-reading rests on. Folder 12 owns it.
- **The entitlement swap** (§3) remains the better long-term design for
  `claim()`. Recorded, not attempted.

## 12. Reproducing

```powershell
python -m pytest universal/gitops-factory/output-showcase/ERRORS_NEW_ANALYSIS/Helm_Naming_Prefix_Collisions/test/test_naming_idempotence.py -v
python <scratch>/probe_naming.py
```
