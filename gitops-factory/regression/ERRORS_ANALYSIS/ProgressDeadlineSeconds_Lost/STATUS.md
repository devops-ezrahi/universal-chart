# STATUS — ProgressDeadlineSeconds_Lost (P3, folder 34/35)

**Result: NO IMPACT here — proved with a grep — and the real code gap FIXED
anyway.** 8 tests RED→GREEN.

---

## 1. The check error.txt asks for, answered

> "If ANY Deployment in the raw manifests sets a non-600 value ... that one IS a
> real behavioural change. Grep the raw manifests for `progressDeadlineSeconds:`
> and confirm every value is 600 before closing this out."

```
progressDeadlineSeconds - every distinct value across
namespaces/, namespaces-showcase/, namespaces-live-demo/:
    NONE FOUND
```

Not 600, not anything: the field is absent from every manifest in this repo. So
the report's LOW classification holds here for a stronger reason than it gave —
there is nothing to lose at all.

`test_this_repo_sets_no_progress_deadline_at_all` re-runs that scan on every test
run and fails on the first **non-600** value, which is the moment the
classification stops being safe.

## 2. The gap is real even so

`handle_workload` never read the field and `deployment.yaml` never rendered it.
A Deployment that shortened it — 120s so a stuck rollout is marked failed in two
minutes rather than ten — would have silently got 600. Tested with 120 rather
than the harmless 600 the report cites.

| File | Change |
|---|---|
| `convert_to_universal_chart.py` `handle_workload` | capture `progressDeadlineSeconds` |
| `templates/deployment.yaml` | render it, omitted when unset |
| `values.yaml` / `values.schema.json` | declare it (`""` = omit) |

**Omitted rather than defaulted to 600.** Rendering a number the values file
never declared shows up on every raw-vs-rendered diff as an invented field, and
Kubernetes applies the same 600 anyway. Same reasoning as folder 15's
`backoffLimit`, opposite conclusion — there the field is always rendered, so the
fallback had to be *correct*; here it can simply be absent.

**Captured whatever the value is, including 600.**
`test_it_is_captured_even_when_it_equals_the_kubernetes_default` pins that:
deciding which values are "worth" keeping is how fields go missing, and the
fidelity checker compares raw against rendered, not against Kubernetes.

## 3. Tests

`ProgressDeadlineSeconds_Lost/test/` — 8 tests. Fixture derives from
`namespaces-showcase/shop-web/storefront.yaml` with `progressDeadlineSeconds: 120`
and `revisionHistoryLimit: 5` (the latter as a control — the sibling field that
already worked and must keep working).

### RED — before

```
4 failed, 4 passed in 1.83s

AssertionError: progressDeadlineSeconds dropped; values keys =
  ['containerName', 'image', 'ports', 'revisionHistoryLimit', 'workload']
KeyError: 'progressDeadlineSeconds'        (template)
KeyError: 'progressDeadlineSeconds'        (round trip)
```

Note `revisionHistoryLimit` present in that key list and the new field absent —
the two sit one line apart in `handle_workload`.

### GREEN — after

```
8 passed in 2.20s
```

## 4. Validation chain

| Step | Result |
|---|---|
| Full showcase converter run | `122 objects matched (28 relocated). No fidelity issues found.` |
| Fidelity before / after | 0 lost / 0 changed, unchanged |
| Generated-output churn | none — 0 occurrences in the data |
| `helm lint` | 0 failed |
| Chart's own suite | 234 passed |

## 5. What could NOT be verified without a live cluster

- **That a 120s deadline actually marks a stuck rollout failed in two minutes.**
  That is the Deployment controller. Verified only that the rendered manifest
  carries the raw value.
- **The report's 68-119 occurrences.** Not in this repo.

## 6. OBSERVED, DEFERRED

- **`minReadySeconds` has the identical gap** — not captured by `handle_workload`,
  not rendered by `deployment.yaml`, sitting in the same part of the same spec.
  No folder in the index names it, and it is a different field, so it is
  recorded rather than fixed. It appears 0 times in this repo's manifests.
- Earlier folders' deferred items still stand.

## 7. Reproducing

```powershell
python -m pytest universal/gitops-factory/output-showcase/ERRORS_ANALYSIS/ProgressDeadlineSeconds_Lost/test -v
```
