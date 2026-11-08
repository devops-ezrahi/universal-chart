# STATUS — Subtract_Defaults_Removes_Explicit_Zero (P1, folder 15/35)

**Result: FIXED — one-sided, in the chart. error.txt's named cause
(`subtract_defaults`) is NOT at fault, and that half is refuted with output.**

10 tests RED→GREEN, folders 1–14 still green (149 total), chart's own 234 still
green, fidelity unchanged at 0 lost / 0 changed.

---

## 1. error.txt: the symptom is real, the diagnosis is not

error.txt says *"Two-sided fix required: (1) `subtract_defaults` must compare
against the ACTUAL chart default, not treat 0 as absent."*

**`subtract_defaults` does not treat 0 as absent.** It compares values, not
truthiness — `elif val != dval` — so a 0 is removed only when the default is
*also* 0, and never because 0 is falsy. Pinned four ways:

```python
subtract_defaults({"a": 0}, {"a": 3}) == {"a": 0}   # kept
subtract_defaults({"a": 0}, {"a": 0}) == {}         # removed, default is also 0
subtract_defaults({"a": 0}, {})       == {"a": 0}   # kept
subtract_defaults({"a": 3}, {"a": 0}) == {"a": 3}   # kept
```

And **removal is not loss.** `common_subtree` does promote a shared 0 into
`defaults.yaml`, and `subtract_defaults` does remove it from the microservice's
own file — but that layer is applied *underneath* at render time, so the 0
arrives intact. `test_subtract_defaults_removes_a_shared_zero_but_the_layer_supplies_it`
walks that whole path and asserts the merged result is still 0. Subtraction is
only ever a loss when something is subtracted and never layered back — which
does happen elsewhere, see §6.

The converter is likewise fine: `handle_cronjob`/`handle_job` capture with
`if spec.get(k) is not None`, which is zero-safe. Both capture tests passed
before and after the fix.

**The real and only cause is Go template's `default`, which treats 0 as empty:**

```gotemplate
successfulJobsHistoryLimit: {{ $spec.successfulJobsHistoryLimit | default 3 }}
failedJobsHistoryLimit:     {{ $spec.failedJobsHistoryLimit | default 1 }}
backoffLimit:               {{ $spec.jobTemplate.backoffLimit | default 3 }}   # cronjob.yaml
backoffLimit:               {{ $spec.backoffLimit | default 3 }}               # job.yaml
```

A values file holding a literal 0 renders the fallback anyway. One-sided.

**Two more corrections.** error.txt locates the bug at `_pod-spec.tpl` line 277
in a `{{- if not (hasKey . "backoffLimit") }}` block — that block does not exist
in this chart, and `_pod-spec.tpl` never mentions `backoffLimit` at all; the
field is rendered in `cronjob.yaml:32` and `job.yaml:23`. It also gives the wrong
numbers for this chart: the fallback was **3**, not 6, so the symptom here is
`0 → 3`, not `0 → 6`.

## 2. Files changed

| File | Where | Change |
|---|---|---|
| `Universal-chart/templates/cronjob.yaml` | 22–23 | `successfulJobsHistoryLimit` / `failedJobsHistoryLimit` via `hasKey`, fallbacks 3 / 1 |
| `Universal-chart/templates/cronjob.yaml` | 32–33 | `$jt := $spec.jobTemplate \| default dict`; `backoffLimit` via `hasKey`, fallback **6** |
| `Universal-chart/templates/job.yaml` | 23 | same, fallback **6** |

No converter change. No values/schema change — the fields were already declared.

### The one deliberate behaviour change: backoffLimit's fallback, 3 → 6

This is error.txt's root-cause row F (*"Chart template has wrong defaults …
must match Kubernetes defaults"*) and it is worth being explicit about, because
it changes rendering for values files that do **not** set the field.

Kubernetes' own default for `backoffLimit` is **6**. This chart used 3 — a number
nothing chose, which meant a Job with no `backoffLimit` in its values silently
retried half as often as the same Job applied with `kubectl`, and the fidelity
checker would report the invented value as an ADDED field that disagrees with the
raw dump. `successfulJobsHistoryLimit: 3` and `failedJobsHistoryLimit: 1` already
match Kubernetes and are unchanged.

**Blast radius in practice: none for converted releases.** A raw cluster dump
always carries `backoffLimit`, so the converter always captures it and the
fallback never fires. Only a hand-written values file that omits the field is
affected, and it now gets Kubernetes' answer instead of the chart's.
`test_the_fallback_matches_kubernetes_when_nothing_is_declared` pins all three.

I chose to match Kubernetes rather than omit the field entirely: omitting is
equally faithful, but it changes the shape of the rendered YAML for every release
and makes `kubectl diff` against an existing cluster noisier for no gain.

## 3. Tests

`Subtract_Defaults_Removes_Explicit_Zero/test/` — 10 tests + `render_check.ps1`.

Fixtures derive from the two real showcase manifests that already carry these
fields — `namespaces-showcase/background-jobs/nightly-report.yaml` (CronJob,
`backoffLimit: 2`) and `.../db-migrate.yaml` (Job + Secret, `backoffLimit: 1`).
Each limit is changed to an explicit, meaningful 0:

| object | field | raw |
|---|---|---|
| CronJob `nightly-report` | `successfulJobsHistoryLimit` | `0` — keep no completed pods |
| CronJob `nightly-report` | `failedJobsHistoryLimit` | `0` — keep no failed pods |
| CronJob `nightly-report` | `jobTemplate.backoffLimit` | `0` — never retry |
| Job `db-migrate` | `backoffLimit` | `0` — never retry a failed migration |

Both microservices sit in one namespace and share `backoffLimit: 0` on purpose,
so `common_subtree` really does hoist it and `subtract_defaults` really does
remove it — the exact mechanism error.txt blames, exercised rather than argued
about.

### RED — before

```
4 failed, 6 passed in 2.47s

test_cronjob_renders_an_explicit_zero_verbatim
  AssertionError: values said 0, chart rendered 3
test_job_renders_an_explicit_zero_verbatim
  AssertionError: values said 0, chart rendered 3
test_the_fallback_matches_kubernetes_when_nothing_is_declared
  AssertionError: the chart's backoffLimit fallback does not match Kubernetes' own (6); got 3
test_round_trip_every_explicit_zero_survives_the_full_layering
  assert 3 == 0
```

**All six converter-side tests passed before the fix.** That is the refutation of
error.txt's diagnosis, not an accident of test design: the four that failed are
all `helm template` assertions.

### GREEN — after

```
10 passed in 2.33s
```

### Accumulated (folders 1–15 + the found P0)

```
149 passed in 44.85s
```

## 4. Validation chain

`render_check.ps1`, real output:

```
== 2. where the zeros ended up across the layers ==
   background-jobs\defaults.yaml        (none here - supplied by a lower layer)
   base\nightly-report.yaml             failedJobsHistoryLimit: 0
   base\nightly-report.yaml             backoffLimit: 0
   base\nightly-report.yaml             successfulJobsHistoryLimit: 0
   base\db-migrate.yaml                 backoffLimit: 0

== 4. raw vs rendered ==
  FIELD                                   RAW   RENDERED
  CronJob successfulJobsHistoryLimit        0          0  OK
  CronJob failedJobsHistoryLimit            0          0  OK
  CronJob backoffLimit                      0          0  OK
  Job backoffLimit                          0          0  OK

== 5. helm lint ==
1 chart(s) linted, 0 chart(s) failed
```

Note step 2: the zeros are in `base/`, not `defaults.yaml`. With only two
microservices whose *other* values differ, `common_subtree` had nothing to hoist
here — so the layering test in the pytest file, which drives `common_subtree`
directly, is the one that actually exercises the hoist.

| Step | Result |
|---|---|
| Full showcase converter run | `122 objects matched (28 relocated). No fidelity issues found.` |
| Fidelity before / after | 0 lost / 0 changed, unchanged |
| Generated-output churn | none new — the showcase's CronJob and Job both declare non-zero limits |
| `helm lint` | 0 failed |
| Chart's own suite | 28 suites, 234 tests passed — none asserted the fallback, only explicit values |

## 5. What could NOT be verified without a live cluster

- **That `backoffLimit: 0` actually stops the retry**, and that
  `successfulJobsHistoryLimit: 0` actually reaps completed pods. Both are
  kube-controller-manager behaviour. Verified only that the rendered manifest
  carries the raw value.
- **That the fallback change is invisible on the real cluster.** It is invisible
  for every converted release *in this repo*, because both showcase objects
  declare the field. A values file elsewhere that omits it will render 6 rather
  than 3 on the next sync — a real, if small, rollout change.
- **The report's counted objects** (`jenkins-backup`) are not in this repo.

## 6. OBSERVED, DEFERRED

- **A REAL "subtracted but never layered back" case exists, and it is not this
  one.** In `--cluster-scoped` mode `main()` subtracts `global_defaults` from
  every `shared` release's values, but `build_shared_application` gives `shared`
  a dedicated Application that deliberately **skips the defaults.yaml layers**.
  Reproduced with a two-namespace fixture:

  ```
  --- root defaults.yaml ---        --- base/shared.yaml ---
  workload:                         configMaps:
    type: none                        common-config: {...}
  service:                          nameOverride: shared
    enabled: false                  # workload.type and service.enabled GONE
  ```

  `shared` then renders with the chart's own `workload.type` and
  `service.enabled` defaults — a phantom Deployment and Service from a release
  that declared neither. This belongs to
  `_FOUND_Global_Defaults_Subtracted_But_Never_Written`, whose earlier fix paired
  the write and the subtraction for regular microservices but did not account for
  the one release that skips the layer even when the file *is* written. Recorded
  here, fixed there — not touched from this folder.
- **`ImagePullPolicy_Always_Downgraded_To_IfNotPresent` (folder 11) and
  `ExternalSecret_ManagementPolicy_Changed`** are named in error.txt as sharing
  this mechanism. Folder 11 is already closed and its cause was a missing
  capture, not `default`. The ExternalSecret one is still open; the `| default`
  audit below is the thing to check there.
- **The `| default <number>` audit came back clean.** Those four sites were the
  only numeric `| default` filters in the whole chart, so no other field can lose
  an explicit 0 this way. String `| default`s (`concurrencyPolicy`,
  `restartPolicy`, `terminationMessagePath`) are unaffected: an empty string is
  not a valid value for any of them.
- Folders 1–14 deferred items still stand.

## 7. Reproducing

```powershell
python -m pytest universal/gitops-factory/output-showcase/ERRORS_ANALYSIS/Subtract_Defaults_Removes_Explicit_Zero/test/test_explicit_zero.py -v
& universal\gitops-factory\output-showcase\ERRORS_ANALYSIS\Subtract_Defaults_Removes_Explicit_Zero\test\render_check.ps1
```
