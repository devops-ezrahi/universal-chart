# STATUS — ExternalSecret_Name_Collision_Suffix (P2, folder 22/35)

**Result: the reported MECHANISM does not exist in this converter. The CONCERN
behind it is real, has an extra layer the report does not mention, and is now
pinned.** 8 tests.

---

## 1. There is no `-2` suffix

> "The ExternalSecret test-ke maps to a template name that collides with another
> resource. The naming convention adds -2 suffix."

No such convention exists here. The collision resolver is
`NamespaceRegistry.claim`, and it renames to `<owner>-<name>` — the same
collision produces `beta-test-ke`, never `test-ke-2`.
`test_no_numeric_suffix_convention_exists_for_claimed_objects` runs ten
colliding claimants to be sure nothing falls back to counting.

The numeric-suffix form appears in exactly one place in this codebase, and it is
one **I** added for folder 13 (`claim_subresource_key`, disambiguating CronJob
and Job map keys). It postdates the report and does not touch ExternalSecrets.

The cited line — *"Line 488: name = raw.get('metadata', {}).get('name', '')"* —
is also not where naming happens; `build_externalsecret_entry` never sets the
object's name at all. The map key does, and `claim()` decides it.

## 2. The concern IS real, and worse than for a ConfigMap

A renamed object breaks anything that referred to it by its old name — the same
issue as folders 18 and 19. For an ExternalSecret there is one more step, and the
report does not mention it:

**An ExternalSecret is a factory.** It creates a *Secret*, whose name comes from
`spec.target.name` and **defaults to the ExternalSecret's own name**. So renaming
the ExternalSecret silently renames the Secret it produces — and every
`secretKeyRef` pointing at that Secret stops resolving, including references in
other releases and in manifests that were never part of this conversion.

The broken reference is one level removed from the thing that was renamed, which
makes it harder to spot than a renamed ConfigMap.

`test_a_renamed_external_secret_renames_the_secret_it_creates` pins the
propagation so it is visible rather than surprising.

## 3. The mitigation, pinned

`spec.target.name` decouples the produced Secret from the ExternalSecret's own
name. Set it explicitly and a rename of the ExternalSecret leaves consumers
resolving:

| ExternalSecret | target.name | Secret created |
|---|---|---|
| `beta-test-ke` (renamed) | *(unset)* | `beta-test-ke` — consumers break |
| `beta-test-ke` (renamed) | `test-ke` | `test-ke` — consumers keep working |

Both rows are tests. `test_the_converter_carries_an_explicit_target_name`
confirms the converter preserves an explicit `target.name` through conversion, so
the mitigation survives the round trip — which it does thanks to folder 21's
whitelist removal.

## 4. Files changed

**None.** The rename is correct, the reference remapping is correct, and the
mitigation already works. What was missing was any test saying so, and any
record that the reported mechanism is wrong.

Two reference directions are also pinned because they are easy to break and
silent when broken: a `secretStoreRef` to a **SecretStore** follows the rename
(`test_a_secret_store_ref_follows_a_renamed_secret_store`), and one to a
**ClusterSecretStore** deliberately does not — those are deduped globally
without renaming, so remapping would point at a name that does not exist.

## 5. Tests

`ExternalSecret_Name_Collision_Suffix/test/` — 8 tests, no fixture needed
(`claim()` is a pure function and the rest is `helm template` over inline values).

```
8 passed
```

While writing these I found and removed a duplicate I had introduced earlier:
`OPENSHIFT_AUTO_SECRET` (added in folder 17) was a second copy of the existing
`AUTO_SA_SECRET_RE`, with a slightly different pattern. Consolidated onto one
constant, keeping the more permissive `{5,}` suffix — under-matching there means
carrying a dead reference rather than dropping a live one.

## 6. Validation chain

| Step | Result |
|---|---|
| Full showcase converter run | `122 objects matched (28 relocated). No fidelity issues found.` |
| Generated-output churn | none — the showcase has no ExternalSecret collisions |
| `helm lint` | 0 failed |
| Chart's own suite | 234 passed |

Reference integrity across every release is verified in folder 19.

## 7. What could NOT be verified without a live cluster

- **That the External Secrets Operator names the Secret from `target.name`.**
  Documented CRD behaviour; the chart's rendering of it is what is tested here.
- **Whether the report's `test-ke` was renamed by this mechanism at all.** The
  object is not in this repo, and `-2` is not something this code produces — so
  either the original project ran different code, or the report described the
  suffix from memory.
- **What referenced `test-ke` outside the conversion.** The unanswerable half,
  same as folder 19 §6.

## 8. OBSERVED, DEFERRED

- **Setting `target.name` automatically on rename would make this safe by
  construction** — the ExternalSecret gets a new name, the Secret it produces
  keeps the old one. It is tempting and it is not this folder's call: it changes
  what the chart renders for every collision, and the same argument as folder 18
  applies (the converter cannot know whether the two ExternalSecrets were meant
  to produce one Secret or two). Recorded as the obvious next improvement.
- Earlier folders' deferred items still stand.

## 9. Reproducing

```powershell
python -m pytest universal/gitops-factory/output-showcase/ERRORS_ANALYSIS/ExternalSecret_Name_Collision_Suffix/test -v
```
