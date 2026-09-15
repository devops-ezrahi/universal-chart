# STATUS — Main_Container_Name_Forced_To_Release_Name (P0, folder 5/35)

**DECISION: Option A — preserve the raw container name** (user, 2026-08-16).

**Result: FIXED**, and the checker suppression **removed**. 10 new tests
RED→GREEN, folders 1–4 still green, chart's own 234 tests still green.

This is the first folder whose defect is exercised by the real showcase data, so
it has a hard empirical before/after: **12 of 19 workloads rendered a renamed
container before the fix, 0 after.**

---

## 1. Were the error.txt hints accurate?

| Hint | Reality |
|---|---|
| `_pod-spec.tpl:119` — `- name: {{ include "universal-chart.name" . }}` | **Exact**, line and content. |
| `convert_to_universal_chart.py:613 handle_main_container()` | Function was at **655** (drift from folders 1–3 edits). It never read `c["name"]`. |
| "All 14 cases follow this same pattern" | Consistent with what I measured here — every workload, mechanically. |

**error.txt undercounts the call sites, exactly like folder 3.** It discusses
only the main container. `jobPodSpec` (`_pod-spec.tpl:247`) names the Job/CronJob
container `{{ .job.name }}` — which is the **jobs/cronjobs map key**, not the
container name. So Jobs and CronJobs are renamed by a *second* mechanism. Fixing
only the main container would have left those broken, and — worse — invisible,
because the checker suppression matched on the path tail and covered both.

Measured: `db-migrate`'s container `migrate` rendered as `db-migrate`;
`nightly-report`'s container `report` rendered as `nightly-report`.

## 2. Files changed

| File | Line | Change |
|---|---|---|
| `convert_to_universal_chart.py` | 655–664 | `handle_main_container`: capture `containerName` |
| `convert_to_universal_chart.py` | 1293–1298 | `handle_cronjob`: capture `jobTemplate.containerName` |
| `convert_to_universal_chart.py` | 1351–1354 | `handle_job`: capture `containerName` |
| `convert_to_universal_chart.py` | 2390–2399 | `_fidelity_ignored_path`: **removed** the `containers[0].name` suppression |
| `Universal-chart/templates/_pod-spec.tpl` | 118–124 | `mainContainer`: `.Values.containerName \| default (include "universal-chart.name" .)` |
| `Universal-chart/templates/_pod-spec.tpl` | 246–249 | `jobPodSpec`: `.job.containerName \| default .job.name` |
| `Universal-chart/values.yaml` | 36–41 | Declare `containerName: ""` with rationale |
| `Universal-chart/values.schema.json` | 31 | `"containerName": { "type": "string" }` |

## 3. Removing the suppression is the real prize

`_fidelity_ignored_path` used to contain:

```python
if path[-3:] == ("containers", "[0]", "name"):
    return True
```

That is why the committed fidelity report read **0 lost / 0 changed** while 12
of 19 workloads were in fact rendering a renamed container. The suppression was
not neutral — it made the checker structurally incapable of reporting that
`containers[0]` had changed *for any reason*, including genuinely vanishing.

It is now gone, replaced by a comment explaining why it no longer needs to
exist. Two tests pin this:
`test_fidelity_checker_no_longer_suppresses_container_name` and
`test_fidelity_checker_reports_a_renamed_container`.

**And the report still reads 0 lost / 0 changed** — this time because the names
actually match, not because the check was switched off. That is the single most
important line in this file.

## 4. Tests created

`ERRORS_ANALYSIS/Main_Container_Name_Forced_To_Release_Name/test/`

- `fixture/shop-web/storefront.yaml` — **verbatim slice** of the real showcase
  manifest (container `nginx`, release `storefront`). Nothing invented.
- `fixture/background-jobs/db-migrate.yaml` — verbatim Job (`migrate`) plus a
  CronJob (`report`), pinning the two call sites error.txt omits.
- `test_container_name.py` — 10 tests: converter capture on all three paths;
  template render + fallback on all three; checker suppression removed; checker
  reports a rename; full round-trip.
- `compare_all_container_names.py` — renders **every** release in
  `output-showcase` and compares `container[0]` against the raw manifest. Exits
  non-zero on any mismatch. Reusable as a standing check.
- `render_check.ps1` — fixture chain (Part A) + whole-showcase sweep (Part B).

### RED — before the fix

```
FAILED test_converter_records_main_container_name
FAILED test_converter_records_job_container_name
FAILED test_converter_records_cronjob_container_name
FAILED test_deployment_renders_the_raw_container_name
FAILED test_job_and_cronjob_render_the_raw_container_name
FAILED test_fidelity_checker_no_longer_suppresses_container_name
FAILED test_fidelity_checker_reports_a_renamed_container
FAILED test_round_trip_container_names_match_the_raw_manifests
========================= 8 failed, 2 passed in 2.89s =========================
```

```
AssertionError: converter discarded the raw container name; the chart will name the
   container after the release and `kubectl logs -c nginx` will fail
AssertionError: _pod-spec.tpl ignored .Values.containerName; rendered ['storefront'],
   expected ['nginx']
AssertionError: assert ['db-migrate'] == ['migrate']
AssertionError: _fidelity_ignored_path still suppresses containers[0].name
AssertionError: checker did not report the container rename; diffs=[]
```

That last line is the suppression in action: a container renamed from `nginx` to
`storefront` produced **zero** diffs.

### GREEN — after the fix

```
10 passed in 2.69s
```

### Accumulated regression suite (folders 1–5)

```
49 passed in 10.65s
```

## 5. Validation chain

**1. Converter re-run:** `122 objects matched (28 relocated). No fidelity issues found.`

**2–3. Whole-showcase raw vs rendered — BEFORE and AFTER.**

The "before" column is not asserted from memory: I copied the chart, restored
`_pod-spec.tpl` from `git show HEAD:`, and re-rendered the same values through
it.

| namespace | kind | object | raw c[0] | **before** | **after** |
|---|---|---|---|---|---|
| background-jobs | Job | db-migrate | migrate | `db-migrate` ❌ | `migrate` ✅ |
| background-jobs | CronJob | nightly-report | report | `nightly-report` ❌ | `report` ✅ |
| background-jobs-prd | Job | db-migrate | migrate | `db-migrate` ❌ | `migrate` ✅ |
| background-jobs-prd | CronJob | nightly-report | report | `nightly-report` ❌ | `report` ✅ |
| identity-auth | Deployment | dex | dex | `dex` ✅ | `dex` ✅ |
| identity-auth-prd | Deployment | dex | dex | `dex` ✅ | `dex` ✅ |
| identity-auth-prd | Deployment | opa | opa | `opa` ✅ | `opa` ✅ |
| platform-data | StatefulSet | postgres | postgres | `postgres` ✅ | `postgres` ✅ |
| platform-data | StatefulSet | redis-cache | redis | `redis` ✅ | `redis` ✅ |
| platform-data-prd | StatefulSet | postgres | postgres | `postgres` ✅ | `postgres` ✅ |
| platform-data-prd | StatefulSet | redis-cache | redis | `redis` ✅ | `redis` ✅ |
| platform-ops | DaemonSet | log-agent | fluent-bit | `log-agent` ❌ | `fluent-bit` ✅ |
| platform-ops | DaemonSet | metrics-agent | node-exporter | `metrics-agent` ❌ | `node-exporter` ✅ |
| platform-ops-prd | DaemonSet | log-agent | fluent-bit | `log-agent` ❌ | `fluent-bit` ✅ |
| platform-ops-prd | DaemonSet | metrics-agent | node-exporter | `metrics-agent` ❌ | `node-exporter` ✅ |
| shop-web | Deployment | api-gateway | traefik | `api-gateway` ❌ | `traefik` ✅ |
| shop-web | Deployment | storefront | nginx | `storefront` ❌ | `nginx` ✅ |
| shop-web-prd | Deployment | api-gateway | traefik | `api-gateway` ❌ | `traefik` ✅ |
| shop-web-prd | Deployment | storefront | nginx | `storefront` ❌ | `nginx` ✅ |

```
BEFORE:  19 workloads compared, 12 mismatches
AFTER:   19 workloads compared,  0 mismatches
```

The 7 that matched before did so by coincidence — the container happened to
share the release/file name (`dex`, `opa`, `postgres`), or the release slug
happened to equal the container name (`redis-cache`'s container is `redis`, file
`redis.yaml`).

**4. Fidelity report: unchanged, still 0 lost / 0 changed** — now genuinely,
with the suppression gone. No other count moved.

**5. Generated-output diff:** 10 base files, each gaining one line:

```
+containerName: nginx        (base/storefront.yaml)
+    containerName: migrate  (base/db-migrate.yaml, under jobs.db-migrate)
+      containerName: report (base/nightly-report.yaml, under cronjobs.*.jobTemplate)
```

Plus folder 2's two `serviceName:` lines. All values changes; no rendered object
gained or lost a field other than the corrected container name.

**6. `helm lint`:** passes plain and with `--set containerName=nginx`
(the latter also exercises the new schema entry).

**7. Chart's own suite:** `28 suites, 234 tests passed` — including
`deployment_test.yaml` and `cronjob_test.yaml`, which assert container names and
still pass thanks to the `| default` fallback.

## 6. What could NOT be verified without a live cluster

- **That `kubectl logs -c nginx` now works.** That is the whole point of the
  fix, and it needs a running pod. What I verified is that the rendered
  `containers[0].name` equals the raw manifest's, which is the field `kubectl`
  matches on.
- **That log pipelines / Prometheus `container` labels recover.** Same reason —
  those read from the running kubelet.
- **The 14 cases error.txt counts** (`interconn-kafka-entity-operator`,
  `zookeeper-zbs`, `jaeger-es-master`, `rfr-httpproxy-redis-redis-dba`) — none of
  those manifests are in this repo. The mechanism is fixed and covered by tests;
  those specific workloads are unverified.
- **Whether any downstream consumer depended on the OLD (release) name.** If
  something out there greps for `container="storefront"`, this fix changes it
  back to `nginx`. That is the intent, but it is a behavioural change on the
  target and worth knowing before rollout.

## 7. OBSERVED, DEFERRED

- **`ImagePullPolicy_Always_Downgraded_To_IfNotPresent` (folder 11)** — while
  editing `mainContainer` I saw `imagePullPolicy: {{ .Values.image.pullPolicy }}`
  rendered unconditionally with no `| default`, and `jobPodSpec:249` uses
  `.Values.image.pullPolicy` for the **job's** container rather than the job's
  own image settings. Relevant to that folder. Not touched.
- **CronJob name prefixing** — `nightly-report` renders as
  `db-migrate-nightly-report` when it shares a release with another job
  (`universal-chart.cronJobName`). By design and documented in `_helpers.tpl`,
  but it is a rename, and folders 7/9 concern renames. Noting it so it is not
  mistaken for a new defect. Not touched.
- Folders 1–4 deferred items still stand.

## 8. Reproducing

```powershell
python -m pytest universal/gitops-factory/output-showcase/ERRORS_ANALYSIS/Main_Container_Name_Forced_To_Release_Name/test/test_container_name.py -v
& universal/gitops-factory/output-showcase/ERRORS_ANALYSIS/Main_Container_Name_Forced_To_Release_Name/test/render_check.ps1

# standing whole-showcase check (run from universal/gitops-factory):
python output-showcase/ERRORS_ANALYSIS/Main_Container_Name_Forced_To_Release_Name/test/compare_all_container_names.py
```
