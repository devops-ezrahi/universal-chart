# STATUS — Pod_SecurityContext_Overridden_By_Release_Defaults (P1, folder 12/35)

**Result: FIXED** (the CronJob/Job half; the workload half already worked).
11 tests RED→GREEN, folders 1–11 still green, chart's own 234 still green,
fidelity unchanged at 0 lost / 0 changed.

**This closes root-cause row C.** That row named three fields —
*"Chart uses release-level defaults instead of per-resource values | Affects:
CronJob/Job SA, securityContext, imagePullPolicy"*. SA was folder 6, pullPolicy
folder 11, securityContext is this one. All three are now per-resource.

---

## 1. Two corrections to error.txt

**"The converter captures podSecurityContext per-container at line 852."**
It does not. Line 836 reads `pod_spec.get("securityContext")` — the **pod-level**
field, correctly scoped — and line 728 separately captures the main container's
own `securityContext`. The two are distinct and both were already right for
Deployments/StatefulSets/DaemonSets. `test_converter_captures_workload_pod_security_context`
guards that; it passed before and after.

**"the chart's `.Values.podSecurityContext` ... defaults to 2000/2000 from
defaults.yaml."** The chart's `values.yaml` declares `podSecurityContext: {}`.
There is no 2000/2000 default in this chart. In the original project that value
came from a *generated* `defaults.yaml` — `common_subtree` hoisting a value that
happened to be shared. So the "2000" is data, not a chart default. My fixture
supplies it explicitly rather than pretending the chart invents it.

**What is actually wrong** is the same shape as folders 6 and 11:
`handle_cronjob` / `handle_job` never captured the job's `securityContext`, and
`jobPodSpec` rendered the release-level `.Values.podSecurityContext`.

**And one loss error.txt does not mention:** `jobPodSpec` rendered **no
container-level `securityContext` at all**. A job container's
`allowPrivilegeEscalation: false`, `readOnlyRootFilesystem: true` and
`capabilities.drop: [ALL]` were dropped outright — a silent un-hardening, not an
override. Found by reading the template rather than the report.

## 2. Files changed

| File | Where | Change |
|---|---|---|
| `convert_to_universal_chart.py` | `handle_cronjob` | capture `podSecurityContext` (pod level) and `securityContext` (container level) |
| `convert_to_universal_chart.py` | `handle_job` | same |
| `_pod-spec.tpl` | 244–250 | `{{- with (.job.podSecurityContext \| default .Values.podSecurityContext) }}` |
| `_pod-spec.tpl` | 267–273 | render the job container's `securityContext` (new block) |

The `| default` keeps the release-level value as the fallback, so a job that
declares nothing behaves exactly as before —
`test_job_falls_back_to_the_release_pod_security_context_when_unset` pins it, and
`test_no_security_context_is_invented_when_nothing_sets_one` pins that an absent
value stays absent rather than becoming `{}`.

## 3. Tests

`Pod_SecurityContext_Overridden_By_Release_Defaults/test/` — 11 tests.

`fixture/background-jobs/elastic-storage.yaml` makes the inheritance visible by
setting the release and the jobs to **different** values, which is the only way
this defect shows:

| object | pod `securityContext` | container `securityContext` |
|---|---|---|
| Deployment `elastic-storage` | `runAsUser/Group/fsGroup: 2000` | `allowPrivilegeEscalation: false`, `readOnlyRootFilesystem: true` |
| Job `elastic-storage-seed` | **1000** | + `capabilities.drop: [ALL]` |
| CronJob `elastic-storage-retention` | **1000** | hardening |

### RED — before

```
6 failed, 5 passed in 2.20s

AssertionError: handle_job dropped the pod-level securityContext; the Job will inherit
  the release-level one. entry keys = ['backoffLimit','command','containerName','image','restartPolicy']
AssertionError: container-level securityContext lost; got None
AssertionError: Job inherited the release-level securityContext;
  got {'fsGroup': 2000, 'runAsGroup': 2000, 'runAsUser': 2000}
AssertionError: Job pod securityContext: raw={'runAsUser': 1000, 'runAsGroup': 1000,
  'fsGroup': 1000} rendered={'fsGroup': 2000, 'runAsGroup': 2000, 'runAsUser': 2000}
```

That last line is error.txt's headline symptom reproduced exactly: **1000 → 2000**.

### GREEN — after

```
11 passed in 2.25s
```

### Accumulated (folders 1–12 + the found P0)

```
117 passed in 37.61s
```

## 4. Validation chain

| Step | Result |
|---|---|
| Converter re-run | `122 objects matched (28 relocated). No fidelity issues found.` |
| Round-trip, per object | Deployment 2000, Job 1000, CronJob 1000 — pod **and** container level, all match raw |
| Fidelity before/after | unchanged, 0 lost / 0 changed |
| Generated-output diff | **no new churn** — still 14 files, same as folders 10–11 |
| `helm lint` | 0 failed |
| Chart's own suite | 28 suites, 234 tests passed |

No showcase Job or CronJob declares a `securityContext`, so the real output is
unchanged — expected, and why the fixture was required.

## 5. What could NOT be verified without a live cluster

- **That file ownership actually works.** The functional harm error.txt describes
  — a container unable to read its own data because `fsGroup` changed — is
  kubelet and CSI behaviour. Verified only that the rendered manifest carries the
  raw UID/GID.
- **That the hardening is enforced.** `readOnlyRootFilesystem` and
  `capabilities.drop` are enforced by the container runtime.
- **Interaction with Pod Security Admission / SCCs.** A namespace policy could
  reject or mutate these values at admission; not observable offline.
- **The counted occurrences** (report issues 6, 15, 24, 39) — those manifests are
  not in this repo.

## 6. OBSERVED, DEFERRED

- **`Empty_K8s_Default_Maps_Dropped` (folder 29)** — error.txt closes with a
  warning not to confuse this defect with `"securityContext: {} | 109 | Empty
  security context"`. Confirmed distinct: the converter's `if
  pod_spec.get("securityContext")` is falsy for `{}`, so an empty map is dropped
  rather than carried. That is folder 29's, and deliberately untouched here —
  `test_no_security_context_is_invented_when_nothing_sets_one` locks in the
  current behaviour so folder 29 changes it deliberately or not at all.
- **The generated ApplicationSets carry `ignoreDifferences` on exactly these
  paths** (`.spec.template.spec.securityContext.runAsUser/runAsGroup/fsGroup/
  seLinuxOptions` and the container-level ones). So even with the correct values
  rendered, ArgoCD is configured not to reconcile drift on them. That is
  deliberate (an SCC-mutating cluster would otherwise fight the chart forever),
  but it means this fix improves the *rendered* manifest while ArgoCD still
  tolerates the cluster changing it. Worth knowing; not a defect, not touched.
- Folders 1–11 deferred items still stand.

## 7. Reproducing

```powershell
python -m pytest universal/gitops-factory/output-showcase/ERRORS_ANALYSIS/Pod_SecurityContext_Overridden_By_Release_Defaults/test/test_pod_security_context.py -v
```
