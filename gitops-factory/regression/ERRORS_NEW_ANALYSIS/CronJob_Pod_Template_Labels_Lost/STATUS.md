# STATUS — CronJob_Pod_Template_Labels_Lost (TIER 1, folder 5/26)

**Result: FIXED, both halves.** 30 errors, rated P1. `error.txt` claims "TWO
INDEPENDENT DEFECTS, BOTH MUST BE FIXED" — that claim was *measured*, not
assumed: each half was disabled in isolation against the real corpus and each
produced the same 11 fidelity errors on its own. Fixed at a shared seam rather
than by a third inline copy, so `handle_workload`, `handle_cronjob` and
`handle_job` cannot drift apart again. 20 new tests RED→GREEN, showcase
fidelity 0 lost / 0 changed, chart's 234 green.

---

## 1. The defect

`spec.jobTemplate.spec.template.metadata.labels.{critical, environment,
operational, operationalPurpose, team}` — five governance labels lost from every
CronJob pod, across six CronJobs. `error.txt` is precise about the two causes:

> 1. **Converter gap**: `handle_cronjob()` only calls `raw_labels(doc)` on the
>    CronJob resource itself. It does NOT extract
>    `spec.template.metadata.labels` into a podLabels key. `handle_workload()`
>    does this at line 982, but CronJob/Job handlers don't.
> 2. **Template**: renders only selectorLabels (2 labels: app.name +
>    app.instance). No objectLabels merge, no podLabels support.

Both confirmed exactly. `handle_cronjob` *did* call `raw_labels(doc)` — for the
CronJob object's own labels — which is precisely why the pod-level loss was easy
to miss: a `labels` key was present in the generated values, just not these.

## 2. Why it is P1 and not cosmetic

The report's priority table says "Sidecar injection could crash CronJobs", and
that is the correct reading. Pod-template labels and annotations are what
admission webhooks and sidecar injectors *select on*. A CronJob pod stripped of
them can miss an injection it depends on — a vault-agent sidecar that never
mounts the secret the job needs — or receive one it was meant to be excluded
from.

So the fix covers annotations as well as labels. The five labels are governance
metadata; `sidecar.istio.io/inject` is a control signal, and it was being
dropped by the same gap.

## 3. Files changed

| File | Lines | Change |
|---|---|---|
| `universal/gitops-factory/convert_to_universal_chart.py` | 1063–1097 (new) | `POD_TEMPLATE_CHART_LABELS`, `pod_template_labels()`, `pod_template_annotations()` — the shared seam |
| `universal/gitops-factory/convert_to_universal_chart.py` | ~1010–1015 | `handle_workload` now calls the seam instead of an inline filter |
| `universal/gitops-factory/convert_to_universal_chart.py` | ~1770–1777 | `handle_cronjob` reads `jobTemplate.spec.template.metadata` |
| `universal/gitops-factory/convert_to_universal_chart.py` | ~1880–1888 | `handle_job` reads `spec.template.metadata` |
| `universal/Universal-chart/templates/cronjob.yaml` | 50–61 | render `$jt.podLabels` / `$jt.podAnnotations` |
| `universal/Universal-chart/templates/job.yaml` | 31–42 | render `$spec.podLabels` / `$spec.podAnnotations` |
| `universal/gitops-factory/namespaces-showcase/background-jobs/nightly-report.yaml` | 13–33 | pod-template labels + injector annotation |
| `universal/gitops-factory/namespaces-showcase/background-jobs/db-migrate.yaml` | 7–17 | pod-template labels on the Job |
| `universal/gitops-factory/output-showcase/**` | regenerated | consequence |

### Why a seam and not a third copy

`error.txt` says "copy that pattern". Copying it would have worked and would
have set up exactly the recurrence this whole round is about: the previous
round fixed this class once, for workloads, and left two handlers behind
carrying the same gap.

`pod_template_labels()` / `pod_template_annotations()` are now called by all
three handlers, so the exclusion set is defined once. A fourth handler that
needs pod labels gets the right behaviour by construction rather than by
someone remembering. `test_the_seam_is_actually_shared_by_all_three_handlers`
fails if an inline copy reappears.

This is Principle 3 from `README_REGRESSION_ANALYSIS.md` — fix at the seam,
once, for all kinds — applied at the smallest scope that removes the class.

## 4. RED — each half, independently

Two throwaway variants were built (`make_red5.py`), each disabling exactly one
half against the real corpus and the real other half.

**RED A — converter does not capture, chart does render:**

```
$ python <scratch>/red5_converter.py --input namespaces-showcase --output <tmp> \
    --chart ../Universal-chart --verify-fidelity --cluster-scoped
  11 potential fidelity issue(s)

  field_lost_critical            2
  field_lost_environment         2
  field_lost_operational         2
  field_lost_operationalPurpose  2
  field_lost_team                2
  field_lost_annotations         1

  ERROR  CronJob/nightly-report LOST at spec.jobTemplate.spec.template.metadata.labels.team: raw had 'background-jobs'
  ERROR  CronJob/nightly-report LOST at spec.jobTemplate.spec.template.metadata.labels.environment: raw had 'dev'
  ERROR  CronJob/nightly-report LOST at spec.jobTemplate.spec.template.metadata.labels.critical: raw had 'false'
  ERROR  CronJob/nightly-report LOST at spec.jobTemplate.spec.template.metadata.labels.operational: raw had 'true'
  ERROR  CronJob/nightly-report LOST at spec.jobTemplate.spec.template.metadata.labels.operationalPurpose: raw had 'reporting-cronjob'
  ERROR  CronJob/nightly-report LOST at spec.jobTemplate.spec.template.metadata.annotations: raw had {'sidecar.istio.io/inject': 'false'}
  ERROR  Job/db-migrate LOST at spec.template.metadata.labels.team: raw had 'background-jobs'
  ERROR  Job/db-migrate LOST at spec.template.metadata.labels.environment: raw had 'dev'
  ERROR  Job/db-migrate LOST at spec.template.metadata.labels.critical: raw had 'true'
  ERROR  Job/db-migrate LOST at spec.template.metadata.labels.operational: raw had 'true'
  ERROR  Job/db-migrate LOST at spec.template.metadata.labels.operationalPurpose: raw had 'schema-migration-job'
```

The field paths are `error.txt`'s verbatim, down to the camelCase
`operationalPurpose`.

**RED B — converter does capture, chart does not render:**

```
$ python convert_to_universal_chart.py --input namespaces-showcase --output <tmp> \
    --chart <scratch>/red5_chart --verify-fidelity --cluster-scoped
  11 potential fidelity issue(s)

  field_lost_critical            2
  field_lost_environment         2
  field_lost_operational         2
  field_lost_operationalPurpose  2
  field_lost_team                2
  field_lost_annotations         1
```

**Identical counts from opposite causes.** That is what makes "capturing the
labels without rendering them changes nothing, and vice versa" a demonstrated
statement rather than a plausible one — and it is why a fix to only one side
would have looked exactly as broken as no fix at all.

## 5. GREEN

```
$ python convert_to_universal_chart.py --input namespaces-showcase --output <tmp> \
    --chart ../Universal-chart --verify-fidelity --cluster-scoped
  123 objects matched (28 legitimately relocated).
  No fidelity issues found.
```

`helm template` of the affected release:

```yaml
jobTemplate:
  spec:
    backoffLimit: 2
    template:
      metadata:
        labels:
          app.kubernetes.io/name: nightly-report
          app.kubernetes.io/instance: nightly-report
          critical: "false"
          environment: dev
          operational: "true"
          operationalPurpose: reporting-cronjob
          team: background-jobs
        annotations:
          sidecar.istio.io/inject: "false"
```

Five governance labels and the injector annotation present; the two selector
labels still there and unclobbered.

```
$ python -m pytest .../CronJob_Pod_Template_Labels_Lost/test/test_cronjob_pod_labels.py -q
....................                                                     [100%]
20 passed in 1.98s
```

## 6. Tests created

`ERRORS_NEW_ANALYSIS/CronJob_Pod_Template_Labels_Lost/test/test_cronjob_pod_labels.py`
— 20 tests in 4 groups:

1. **The seam** (3) — excludes only the four chart-owned identity labels; drops
   only `checksum/*` annotations; and is genuinely shared by all three handlers
   (fails if an inline copy returns).
2. **Converter** (10) — CronJob and Job capture pod labels and annotations;
   each of the five named labels parametrised individually, because the report's
   five per-label triage files are five views of one defect; pod labels are kept
   distinct from the object's own labels; absent metadata produces no keys
   rather than empty ones.
3. **Template** (4) — both templates render the labels via real `helm template`,
   selector labels survive alongside them, annotations render, and no empty
   `annotations: {}` is invented when there are none.
4. **Corpus coverage** (3) — a CronJob and a Job in `namespaces-showcase` carry
   pod-template labels, and one carries an injector annotation so the P1 half
   is not left untested.

## 7. Validation chain

| Check | Result |
|---|---|
| RED A (converter half disabled) | 11 errors — exact field paths from `error.txt` |
| RED B (template half disabled) | 11 errors — identical counts, opposite cause |
| Converter re-run, showcase | 123 objects matched, **no fidelity issues** |
| `helm template` of the affected release | 5 labels + 1 annotation rendered |
| Folder's own tests | 20 passed |
| All `ERRORS_NEW_ANALYSIS` tests | 104 passed |
| `helm unittest Universal-chart` | 234 passed, 28 suites |

The chart suite passing matters more than usual here: this folder edited two
chart templates, and those 234 tests are the only thing covering the CronJob and
Job renders outside this folder.

## 8. A naming detail worth recording

The report writes the CronJob label as `operationalPurpose` (camelCase) and the
Route label as `operational.purpose` (dotted). Both spellings are reproduced as
found rather than normalised: they are different label keys in the source data,
and a converter that "helpfully" unified them would be changing the manifests.
Folder 3 carries the dotted form; this folder carries the camelCase one.

## 9. What could NOT be verified

- **The six named CronJobs** (`jenkins-backup-dev`, `jenkins-restore-dev`,
  `juseppelad-uploader-dev`, `jenkins-restore-prd`, `jenkins-backup-tst`,
  `jenkins-restore-tst`). Not in this repo. The code path they take is now
  covered on both sides.
- **That a specific admission webhook re-injects correctly.** That is cluster
  behaviour. Verified only that the labels and annotations reach the rendered
  pod template with their raw values — which is the precondition for any
  injector to match.
- **Whether anything in the target actually selects on these labels.**
  `error.txt` asks for this ("Verify what selects on these labels before
  downgrading this to cosmetic") and it needs the target cluster. The fix does
  not depend on the answer: preserving them is correct either way.

## 10. OBSERVED, DEFERRED

- **`CronJob_Parallelism_Lost` (folder 16)** — `spec.jobTemplate.spec` still
  captures only `backoffLimit` / `activeDeadlineSeconds` /
  `ttlSecondsAfterFinished`; `parallelism` is absent from that whitelist. Same
  function, one line away, deliberately **not** touched.
- **`StatefulSet_Checksum_Annotations_Lost` (folder 19)** — `checksum/*` is
  dropped by `pod_template_annotations`, which this folder made explicit and
  shared but did not change in behaviour. Whether that is right depends on who
  regenerates them; that is folder 19's question.
- **`K8s_Runtime_Defaults_Benign_Suppress` (folder 17)** lists "Jaeger CronJob
  template annotations/labels (~5)" for suppression. That overlaps this defect
  and must be scoped to those specific CronJobs rather than to CronJob template
  labels generally — noted for folder 17, not acted on.

## 11. Reproducing

```powershell
python -m pytest universal/gitops-factory/output-showcase/ERRORS_NEW_ANALYSIS/CronJob_Pod_Template_Labels_Lost/test/test_cronjob_pod_labels.py -v

# rebuild the two RED variants and re-run either half in isolation
python <scratch>/make_red5.py
```
