# STATUS — ExternalSecret_Spec_Fields_Not_Captured (P2, folder 21/35)

**Result: FIXED — and this one fix closes three folders.** Two of the report's
five claims are refuted with passing guard tests.

11 tests RED→GREEN, all other folders still green (216 total), chart's own 234
still green, fidelity unchanged at 0 lost / 0 changed.

---

## 1. The five claims, audited one at a time

| # | Report claims | Verdict |
|---|---|---|
| 16 | `metadata.labels` never captured, template renders only helper labels | **TRUE** |
| 18 | `metadata.ownerReferences` lost | **REFUTED** — dropped on purpose, and the report has the consequence backwards |
| 19 | `spec.deliverySpec` captured at line 493 but not rendered | **half true** — it was never captured either |
| 21 | `spec.target.template.annotations` not captured | **REFUTED** — already works |
| 22 | `spec.target.template.labels` captured but not rendered | **REFUTED** — already rendered |

**Claims 21 and 22 already worked.** `target` was captured as a whole blob and
`externalsecret.yaml` renders it with `toYaml`, so its labels *and* annotations
round-tripped before this campaign touched anything.
`test_target_template_labels_and_annotations_already_round_trip` and
`test_the_chart_already_renders_the_whole_target_template` passed before the fix
as well as after — that is what makes them evidence rather than assertion.

**Claim 18 is backwards, and acting on it would have caused an outage.** An
`ownerReference` is a `uid`. A uid from the SOURCE cluster does not exist in the
target, so an object carrying one names an owner that is gone — and the API
server's garbage collector deletes it, usually within seconds of being applied.
`sanitize_object` strips `ownerReferences` from **every** object
(`VOLATILE_METADATA_KEYS`) for exactly that reason. Dropping it does not break
garbage collection; **keeping it would delete the object.**
`test_owner_references_are_deliberately_dropped_before_any_handler_runs` pins
the whole chain: the key is in the volatile list, `sanitize_object` removes it,
and the builder never sees it. The chart templates still render an
`ownerReferences` key if a values file sets one by hand, which is the only
context where it can be meaningful.

## 2. The actual root cause: a whitelist, not five separate misses

`build_externalsecret_entry` copied five spec keys. `build_secretstore_entry`
copied four. Everything else was dropped in silence — and neither read
`metadata.labels`. So `deliverySpec` and `managementPolicy` did not fail for
their own reasons; they failed because nobody had added them to a list, and the
next field the External Secrets Operator ships would fail the same way, with
nothing in the fidelity report naming it.

The fix inverts the default: carry every spec key through, and drop only what
there is a positive reason to drop.

| File | Change |
|---|---|
| `convert_to_universal_chart.py` | new `_eso_spec()` — every spec key, no whitelist |
| `convert_to_universal_chart.py` | new `_eso_metadata()` — `labels` + `annotations`, with the ownerReferences reasoning recorded |
| `convert_to_universal_chart.py` | `build_secretstore_entry`, `build_externalsecret_entry`, `build_clustersecretstore_entry` rewritten on top of both |
| `templates/externalsecret.yaml` | render the object's own labels (merged UNDER the chart's identity labels), `ownerReferences`, and `omit`-everything-else at spec level |
| `templates/secretstore.yaml` | same |

**Why labels are merged rather than appended.** Other templates in this chart
emit `{{ include "universal-chart.labels" }}` and then a second `{{ toYaml
$spec.labels }}` under the same `labels:` key — a duplicate YAML key where the
last one wins, which lets a values file silently hijack
`app.kubernetes.io/name`. These two templates build one merged map with the
chart's identity labels winning, matching the rule
`universal-chart.labels` already documents for `commonLabels`.
`test_the_charts_own_identity_labels_still_win` pins it.

`build_clustersecretstore_entry` had the identical whitelist and is fixed with
the same helper. No folder names ClusterSecretStore; leaving the same bug in the
function next door would only have meant rediscovering it. Its *metadata* is
deliberately not carried — a ClusterSecretStore is cluster-scoped and deduped
across namespaces, so one namespace's labels are not the shared object's to set.

## 3. Tests

`ExternalSecret_Spec_Fields_Not_Captured/test/` — 11 tests. Fixture derives from
`namespaces-showcase/identity-auth/session-store.yaml` (same namespace, same file
name, same SecretStore + ExternalSecret pair, same Vault provider block) with
everything the report names added.

### RED — before

```
7 failed, 4 passed in 2.01s

AssertionError: metadata.labels dropped; entry keys =
  ['data', 'refreshInterval', 'secretStoreRef', 'target']
AssertionError: entry keys = ['data', 'refreshInterval', 'secretStoreRef', 'target']
  (deliverySpec / managementPolicy / an unknown future field: all absent)
AssertionError: entry keys = ['conditions', 'provider', 'refreshInterval']   (SecretStore labels)
AssertionError: ExternalSecret lost label argocd.argoproj.io/instance; got
  {'app.kubernetes.io/instance': 'session-store', ... }   (only chart labels)
```

The 4 that passed are the guards proving claims 21, 22 and the SecretStore
`conditions` claim wrong.

### GREEN — after

```
11 passed in 2.14s
```

Accumulated: `216 passed`.

## 4. Validation chain

| Step | Result |
|---|---|
| Full showcase converter run | `122 objects matched (28 relocated). No fidelity issues found.` |
| Fidelity before / after | 0 lost / 0 changed, unchanged |
| Generated-output churn | none new — the showcase's one SecretStore/ExternalSecret pair declares no labels and no extra spec fields |
| `helm lint` | 0 failed |
| Chart's own suite | 234 passed |

## 5. What could NOT be verified without a live cluster

- **That ArgoCD resumes tracking the ExternalSecret** once
  `argocd.argoproj.io/instance` is present. Verified only that the label is
  rendered.
- **That the operator honours `deliverySpec` / `managementPolicy`.** Those are
  ESO controller behaviour, and whether a given field exists at all depends on
  the operator version the target runs — the pass-through carries whatever the
  raw manifest had, which is the right answer for a round trip but is not a
  validation.
- **That `omit` at spec level cannot produce an invalid manifest.** It renders
  only keys the source CRD already accepted, but that reasoning is about the
  source cluster, not the target.
- **The report's counted objects** (`ExternalSecret/mongo-password`) are not in
  this repo.

## 6. OBSERVED, DEFERRED

- **The duplicate-`labels:`-key pattern is still in the other templates** —
  `job.yaml`, `cronjob.yaml`, `services.yaml` and others emit two `labels:`
  blocks under one key, so a values file *can* override an identity label there.
  Harmless today, wrong in principle, and it belongs with
  `Workload_Metadata_Labels_Lost` rather than here. Untouched.
- **`ownerReferences` is stripped globally**, so if a real deployment ever
  needs one it must come from a values file by hand. Worth knowing before
  someone reports it as a loss again.
- Earlier folders' deferred items still stand.

## 7. Reproducing

```powershell
python -m pytest universal/gitops-factory/output-showcase/ERRORS_ANALYSIS/ExternalSecret_Spec_Fields_Not_Captured/test -v
```
