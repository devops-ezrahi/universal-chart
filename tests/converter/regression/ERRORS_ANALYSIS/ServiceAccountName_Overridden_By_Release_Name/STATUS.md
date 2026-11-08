# STATUS — ServiceAccountName_Overridden_By_Release_Name (P0, folder 6/35)

**Result: FIXED** (the CronJob/Job half — the main-workload half was already
correct, proven by guard tests). 10 new tests RED→GREEN, folders 1–5 still
green, chart's own 234 tests still green, fidelity unchanged at 0 lost / 0
changed.

---

## 1. Hint verification — error.txt is wrong in a way that matters

| Hint | Reality |
|---|---|
| "converter captures serviceAccountName for CronJob/Job at **line 1079** but ONLY stores it at the release level (`ctx.values["serviceAccountName"]`)" | **Wrong.** It does not capture it at all. `serviceAccountName` appears **exactly once** in the whole converter — line 814, inside `handle_workload`. There is no line-1079 capture, and no `ctx.values["serviceAccountName"]` key anywhere. |
| "`_pod-spec.tpl` lines **283–289**, jobPodSpec helper, `{{ .universal-chart.serviceAccountName }}`" | Right idea, wrong coordinates: `jobPodSpec` began at **233** and the offending line was **235**, `serviceAccountName: {{ include "universal-chart.serviceAccountName" . }}`. |
| "the converter doesn't capture serviceAccountName from the pod spec into values. The template hardcodes serviceAccount.name to the release name" | True for **Jobs/CronJobs**, false for **Deployments/StatefulSets/DaemonSets** — that path captures at line 814 and renders correctly. |

The distinction matters: had I trusted the report, I would have gone looking for
a release-scope-vs-job-scope bug that does not exist, and might have "fixed" the
main workload path, which was already right. Two guard tests
(`test_main_workload_still_captures_its_service_account`,
`test_main_workload_renders_the_raw_service_account_not_the_release`) passed
**before and after**, pinning that.

The report's own example is a good check on this: *"Raw prometheus pod uses the
prometheus ServiceAccount, but rendered pod uses prometheus-prometheus."* I
reproduced that exact shape as a test — release `prometheus-prometheus`,
`serviceAccount.name: prometheus` — and it renders `prometheus` correctly today.

## 2. Files changed

| File | Line | Change |
|---|---|---|
| `convert_to_universal_chart.py` | 1299–1308 | `handle_cronjob`: capture the pod's `serviceAccountName`, remapped through `name_map` |
| `convert_to_universal_chart.py` | 1367–1370 | `handle_job`: same |
| `Universal-chart/templates/_pod-spec.tpl` | 234–239 | `jobPodSpec`: `.job.serviceAccountName \| default (include "universal-chart.serviceAccountName" .)` |

Both converter sites read `serviceAccountName` **or** the deprecated
`serviceAccount` alias, matching what `handle_workload` already does at line 814,
and both remap through `ctx.name_map["ServiceAccount"]` so a reference follows
the object if the collision resolver renamed it.
`test_converter_remaps_a_renamed_service_account` pins that.

## 3. Tests created

`ERRORS_ANALYSIS/ServiceAccountName_Overridden_By_Release_Name/test/`

- `fixture/background-jobs/nexus-backup.yaml` — derived from the real
  `db-migrate.yaml`, reshaped to error.txt's own example: file/release slug
  `nexus-backup`, Job and CronJob both running as the pre-existing `nexus` SA,
  plus a third Job (`nexus-verify`) that names **no** SA as a control.
- `test_serviceaccountname.py` — 10 tests over 5 layers: converter capture
  (Job/CronJob), no-invention, name remapping, template render + fallback, two
  main-workload guards, end-to-end CLI, and checker visibility.
- `render_check.ps1` — converter → `helm template` → per-object raw-vs-rendered
  SA table.

### RED — before the fix

```
FAILED test_converter_captures_job_service_account_name
FAILED test_converter_captures_cronjob_service_account_name
FAILED test_converter_remaps_a_renamed_service_account
FAILED test_job_and_cronjob_render_the_per_job_service_account
FAILED test_round_trip_service_account_names_match_the_raw_manifests
========================= 5 failed, 5 passed in 1.90s =========================
```

The defect verbatim, matching error.txt's headline:

```
AssertionError: handle_job dropped the pod's serviceAccountName; the chart will fall
  back to the release-level SA and the pod will reference a nonexistent
  ServiceAccount. entry keys = ['args','backoffLimit','command','containerName',
  'image','restartPolicy']
AssertionError: jobPodSpec ignored the per-job serviceAccountName; rendered
  'nexus-backup', expected 'nexus'
AssertionError: Job nexus-backup: SA 'nexus-backup', expected 'nexus'
```

The 5 that passed while RED are the guards (main workload path already correct;
no-invention; checker visibility).

### GREEN — after the fix

```
10 passed in 1.95s
```

### Accumulated regression suite (folders 1–6)

```
59 passed in 11.86s
```

Relevant here: folder 5 also edits `jobPodSpec` and both `handle_cronjob` and
`handle_job`, so a regression was a live possibility rather than a formality.

## 4. Validation chain

**1. Converter re-run:** `122 objects matched (28 relocated). No fidelity issues found.`

**2–3. `helm template` + raw-vs-rendered, per object:**

```
   Job      nexus-backup               raw SA=nexus     rendered=nexus          OK
   Job      nexus-backup-nexus-verify  raw SA=<unset>   rendered=nexus-backup   OK (falls back to release SA)
   CronJob  nexus-backup-nexus-prune   raw SA=nexus     rendered=nexus          OK
```

Before the fix, rows 1 and 3 rendered `nexus-backup` — a ServiceAccount that
does not exist in that namespace, so neither pod could start.

The middle row is the guard: a job that names no SA must still inherit the
release's, and it does.

**4. Fidelity report before vs after: unchanged**, 0 lost / 0 changed, no count
moved in either direction.

**5. Generated-output diff:** unchanged from folder 5 — the same 10 base files,
no new lines. **No showcase Job or CronJob declares a `serviceAccountName`**, so
this fix adds nothing to the real output. That is the expected result and the
reason a fixture was required.

**6. `helm lint`:** `1 chart(s) linted, 0 chart(s) failed`.

**7. Chart's own suite:** `28 suites, 234 tests passed`, including
`cronjob_test.yaml` and `job_test.yaml` which assert the release-level SA
fallback — preserved by the `| default`.

## 5. What is fixed vs what error.txt counts

error.txt totals **27 errors: 18 wrong/lost ServiceAccount objects + 9 reference
mismatches**. This fix addresses the *reference* half for Jobs/CronJobs — a pod
pointing at the wrong SA name.

It does **not** address:

- **Renamed SA objects** — e.g. `interconn-kafka-kafbat` →
  `interconn-kafka-kafbat-interconn-kafka-kafbat` (the doubled name in the HIGH
  table). That is the collision resolver renaming the SA *object*, which belongs
  to `Route_And_Service_Names_Replaced_By_Fullname` (folder 7) /
  `ConfigMap_Secret_Identical_Content_Duplicated` (folder 18). Not touched.
- **SA objects that are lost entirely** — the "18 wrong/lost" half. Nothing in
  this repo reproduces it; if it survives, it should surface as an
  `objects in raw but rendered by nothing` count in folder 8.

## 6. What could NOT be verified without a live cluster

- **That the pod actually starts.** The rendered manifest names the right
  ServiceAccount; whether that SA exists in the target namespace, and whether
  its RoleBindings grant what the workload needs, is cluster state.
- **That RBAC behaves.** A correct SA name with no RoleBinding behind it fails
  just as hard, and that is invisible from a rendered manifest.
- **The 27 counted errors** — none of those manifests are in this repo.
- **Whether `nexus` exists in the target namespace at all.** The converter now
  faithfully reproduces the raw reference; if the raw manifest itself pointed at
  a missing SA, the render reproduces that faithfully too. That is correct
  behaviour for a fidelity tool but is not the same as the deployment working.

## 7. OBSERVED, DEFERRED

- **`Legacy_ServiceAccount_Field_Lost` (folder 31)** — both new capture sites
  read `serviceAccountName` **or** the deprecated `serviceAccount` alias, so the
  *value* is never lost from a Job/CronJob. What is not emitted is the
  deprecated `serviceAccount:` key itself in the rendered output. That is
  exactly what folder 31 is about; noting the overlap, not acting on it.
- **`Pod_SecurityContext_Overridden_By_Release_Defaults` (folder 12)** —
  error.txt's root-cause rows C and K name `securityContext` alongside the SA as
  the same class of "release-level default used instead of per-resource value",
  and I confirmed `jobPodSpec:239` uses `.Values.podSecurityContext`, not a
  per-job value. Same shape, same fix pattern. Left for folder 12.
- **`ImagePullPolicy_Always_Downgraded_To_IfNotPresent` (folder 11)** — row C
  names it too; `jobPodSpec` uses `.Values.image.pullPolicy` for the job's
  container. Left for folder 11.
- Folders 1–5 deferred items still stand.

## 8. Reproducing

```powershell
python -m pytest universal/gitops-factory/output-showcase/ERRORS_ANALYSIS/ServiceAccountName_Overridden_By_Release_Name/test/test_serviceaccountname.py -v
& universal/gitops-factory/output-showcase/ERRORS_ANALYSIS/ServiceAccountName_Overridden_By_Release_Name/test/render_check.ps1
```
