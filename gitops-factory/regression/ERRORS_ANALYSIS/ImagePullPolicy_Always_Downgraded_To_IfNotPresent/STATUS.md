# STATUS — ImagePullPolicy_Always_Downgraded_To_IfNotPresent (P1, folder 11/35)

**Result: FIXED** (the CronJob/Job half; the Deployment half already worked).
10 tests RED→GREEN, folders 1–10 still green, chart's own 234 still green,
fidelity unchanged at 0 lost / 0 changed.

---

## 1. error.txt names the wrong mechanism

error.txt is confident about the cause and wrong about it:

> *"root cause row E | `subtract_defaults` removes `Always` as 'same as default'"*
> *"the chart's `_helpers.tpl universal-chart.image` only renders pullPolicy if it's NOT IfNotPresent (line 126)"*

Both checked, both false:

- **`subtract_defaults` does not compare against the chart's `values.yaml`.** It
  compares a microservice's values against the *generated* `defaults.yaml` /
  `base` layer. `Always` is only removed if a layer underneath supplies it again.
  `test_pull_policy_stays_in_the_base_values_not_stripped_as_a_default` pins that.
- **`universal-chart.image` does not render `pullPolicy` at all.** It builds the
  image reference only. `imagePullPolicy` is rendered unconditionally at
  `_pod-spec.tpl:126` (main container) and `:263` (job container) — there is no
  "only if it differs from IfNotPresent" condition anywhere in the chart.

The observed symptom was real; the diagnosis was not. The actual mechanism is the
same one as folder 6:

**`imagePullPolicy` is captured in exactly one place in the whole converter —
`handle_main_container`, line 693.** `handle_cronjob` and `handle_job` never
captured it, and `jobPodSpec` rendered the **release-level**
`.Values.image.pullPolicy`. So a CronJob asking for `Always` inherited whatever
the main workload declared.

This is root-cause row **C** from folder 6's error.txt — *"Chart uses
release-level defaults instead of per-resource values | Affects: CronJob/Job SA,
securityContext, imagePullPolicy"*. That row names three fields; the SA was
folder 6, this is the pullPolicy, and `securityContext` remains for folder 12.

## 2. Files changed

| File | Line | Change |
|---|---|---|
| `convert_to_universal_chart.py` | ~1341 | `handle_cronjob`: capture `image.pullPolicy` on the job template |
| `convert_to_universal_chart.py` | ~1408 | `handle_job`: same |
| `_pod-spec.tpl` | 263 | `imagePullPolicy: {{ (.job.image).pullPolicy \| default .Values.image.pullPolicy }}` |

**pullPolicy goes in `base`, not minimal** — unlike the tag and digest (folder
10). It is a structural property of how the workload should behave, not a
per-environment version pin, so it belongs in the shared comprehensive file.

The `(.job.image).pullPolicy` parenthesised form is deliberate: it is nil-safe
when a job entry has no `image` block at all, which `test_job_falls_back_to_the_release_pull_policy_when_unset`
exercises.

## 3. Tests

`ImagePullPolicy_Always_Downgraded_To_IfNotPresent/test/` — 10 tests.

`fixture/background-jobs/elastic-storage.yaml` reproduces error.txt's own example
(`CronJob/elastic-storage-retention`) and turns on the **mix**, which is what
makes the defect visible at all:

| object | raw `imagePullPolicy` |
|---|---|
| Deployment `elastic-storage` | `IfNotPresent` (the release-level value) |
| Job `elastic-storage-seed` | `Always` |
| CronJob `elastic-storage-retention` | `Always` |

With all three the same, the inheritance bug is invisible — which is very likely
why it survived. The load-bearing test is
`test_job_and_cronjob_render_their_own_pull_policy`.

### RED — before

```
4 failed, 6 passed in 2.01s

AssertionError: handle_job dropped imagePullPolicy; the Job will inherit the
  release-level policy and stop re-pulling. entry image = {'repository': 'busybox'}
AssertionError: handle_cronjob dropped imagePullPolicy; jobTemplate image = {'repository': 'busybox'}
AssertionError: Job inherited the release-level pull policy; got 'IfNotPresent'
```

The 6 that passed are the Deployment-path guards and the two tests that disprove
error.txt's stated mechanism — they passed before and after, which is the point.

### GREEN — after

```
10 passed in 2.07s
```

### Accumulated (folders 1–11 + the found P0)

```
106 passed in 34.94s
```

## 4. Validation chain

| Step | Result |
|---|---|
| Converter re-run | `122 objects matched (28 relocated). No fidelity issues found.` |
| Round-trip, per object | Deployment `IfNotPresent`, Job `Always`, CronJob `Always` — all match raw |
| Fidelity before/after | unchanged, 0 lost / 0 changed |
| Generated-output diff | **no new churn** — 14 files, identical to folder 10's state |
| `helm lint` | 0 failed |
| Chart's own suite | 28 suites, 234 tests passed (incl. `cronjob_test.yaml`, `job_test.yaml`) |

No showcase Job or CronJob declares `imagePullPolicy`, so the real output is
unchanged — expected, and the reason a fixture was required.

## 5. The compounding error.txt warned about is now closed

> *"combined with the image-digest-to-tag defect this means the workload can run
> an entirely different image than intended"*

Both halves are fixed: folder 10 keeps the immutable digest, and this folder
keeps `Always` on the jobs that asked for it. A digest-pinned workload is no
longer re-pointed at a mutable tag, and a job that wanted to re-pull still does.

## 6. What could NOT be verified without a live cluster

- **That the kubelet actually re-pulls.** `imagePullPolicy: Always` causing a
  registry round-trip on every pod start is kubelet behaviour. Verified only that
  the field reaches the rendered manifest with the raw value.
- **That any real workload was running a stale image.** That depends on what the
  target registry served and what each node had cached.
- **The 3 counted CronJobs** (`elastic-storage-retention` and friends) — those
  manifests are not in this repo.

## 7. OBSERVED, DEFERRED

- **`Pod_SecurityContext_Overridden_By_Release_Defaults` (folder 12, next)** is
  the last third of root-cause row C. `_pod-spec.tpl:247` still renders
  `{{ .Values.podSecurityContext }}` inside `jobPodSpec`, with no per-job value
  and no capture in `handle_cronjob`/`handle_job`. Same shape as this fix and
  folder 6's. Untouched.
- **`securityContext` on the job's CONTAINER** is not rendered by `jobPodSpec` at
  all — only the pod-level one is. Distinct from folder 12's pod-level concern;
  noting it so it is not missed.
- Folders 1–10 deferred items still stand.

## 8. Reproducing

```powershell
python -m pytest universal/gitops-factory/output-showcase/ERRORS_ANALYSIS/ImagePullPolicy_Always_Downgraded_To_IfNotPresent/test/test_image_pull_policy.py -v
```
