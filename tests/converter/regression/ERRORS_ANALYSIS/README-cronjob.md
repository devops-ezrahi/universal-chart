# CronJob & Job defects — what is fixed so far

Scope note up front: the campaign is at **19 of 35 folders**. The three
CronJob-titled folders are 13, 14 and 16, and **all three are now done**.

The other six fixes here are CronJob/Job code paths that fell inside the scope of
folders 3, 5, 6, 10, 11 and 12. In every one of those cases `error.txt` discussed
only the main Deployment container, and the CronJob/Job path turned out to carry
the same defect through a **separate** mechanism. Fixing the Deployment half
alone would have left CronJobs and Jobs silently broken.

That is five of the first twelve folders with an unmentioned job-path call site,
and folder 13's defect turned out to live in that path outright. It is the single
most reliable pattern in this campaign: if a defect touches a container, an image,
a ServiceAccount or a security setting, `jobPodSpec` and `handle_cronjob` /
`handle_job` need checking too.

| # | Defect | CronJob/Job status | From folder |
|---|---|---|---|
| A | volumeMount `subPath` collision | **Fixed** | 3 |
| B | container renamed to the map key | **Fixed** | 5 |
| C | per-job `serviceAccountName` dropped | **Fixed** | 6 |
| D | image digest dropped in the inline job image | **Fixed** | 10 |
| E | per-job `imagePullPolicy` dropped | **Fixed** | 11 |
| F | per-job pod **and** container `securityContext` dropped | **Fixed** | 12 |
| G | two CronJobs/Jobs collapsed onto one map key | **Fixed** | 13 |
| H | command / args / env lost with the collapsed entry | **Fixed by G** | 14 |
| I | explicit `0` on backoffLimit / history limits replaced by a fallback | **Fixed** | 15 |
| J | container-level `restartPolicy` (native sidecar marker) captured by nobody | **Fixed** | 16 |
| 16 | `containerRestartPolicy` not captured | not started | — |

**Root-cause row C is now closed.** `error.txt` for folder 6 named a three-field
defect — *"Chart uses release-level defaults instead of per-resource values |
Affects: CronJob/Job SA, securityContext, imagePullPolicy"*. All three
(C, E, F above) are now captured per-job with a fallback to the release value.

Run the CronJob-relevant tests:

```powershell
python -m pytest universal/gitops-factory/output-showcase/ERRORS_ANALYSIS -q -k "cronjob or job"
```

---

## A. volumeMount `subPath` collision — CronJob and Job paths (folder 3)

**The defect.** `named_list_to_map` keyed volumeMounts by mount **name**. A
volume mounted several times with different `subPath`s — the standard way to
project individual ConfigMap keys onto individual file paths — collapsed to
whichever mount came last.

**Why CronJobs were affected separately.** `error.txt` named one call site (the
main container). There are **three**, and two of them are the job paths:

| line | function | what it converts |
|---|---|---|
| 659 | `handle_main_container` | Deployment/StatefulSet/DaemonSet container |
| ~1310 | `handle_cronjob` | the CronJob's job-template container |
| ~1386 | `handle_job` | the Job's container |

**What fixed it.** The change is inside the shared helper
`named_list_to_map` (`convert_to_universal_chart.py:519`), so all three call
sites are covered at once. A duplicated mount name switches to a composite key
`<name>:<subPath>` and carries the real name as an explicit `name` field;
`mapToList` (`_helpers.tpl:172`) and `validateVolumeMounts` (`_helpers.tpl:242`)
read it back with `$spec.name | default $key`.

CronJob and Job volumeMounts are rendered by `mapToList` at
`_pod-spec.tpl:275`, so they pick the fix up through the same path.

**Test.** `VolumeMount_SubPath_Lost_Duplicate_Name_Collision/test/`
→ `test_cronjob_and_job_volume_mounts_also_survive_duplicate_names`

That test exists specifically to stop someone "optimising" the fix back into
`handle_main_container` only.

---

## B. CronJob/Job container renamed to the map key (folder 5)

**The defect.** `jobPodSpec` named the container `{{ .job.name }}` — which is the
**`cronjobs` / `jobs` map key**, not the container's name. So:

| raw container | rendered as | key it was renamed to |
|---|---|---|
| `migrate` | `db-migrate` | the Job map key |
| `report` | `nightly-report` | the CronJob map key |

Both of those are real showcase manifests
(`namespaces-showcase/background-jobs/db-migrate.yaml` and `nightly-report.yaml`),
so this was live, not hypothetical.

**Why it is a separate mechanism.** The Deployment path renames via
`include "universal-chart.name"` in `mainContainer`. The job path renames via
`.job.name` in `jobPodSpec`. Two different template expressions, two different
converter functions — fixing one does nothing for the other. `error.txt` for
folder 5 mentions only the first.

**What fixed it.**

| File | Line | Change |
|---|---|---|
| `convert_to_universal_chart.py` | 1318 | `handle_cronjob` captures `job_template["containerName"]` |
| `convert_to_universal_chart.py` | 1390 | `handle_job` captures `entry["containerName"]` |
| `_pod-spec.tpl` | 258 | `- name: {{ .job.containerName \| default .job.name }}` |

The `| default .job.name` fallback keeps the old behaviour for any hand-written
values file that sets no container name.

**Tests.** `Main_Container_Name_Forced_To_Release_Name/test/`

- `test_converter_records_cronjob_container_name`
- `test_converter_records_job_container_name`
- `test_job_and_cronjob_render_the_raw_container_name`
- `test_job_falls_back_to_the_map_key_when_unset` (the guard)

**Measured before/after**, rendering the same values through the pre-fix template
recovered from `git show HEAD:`:

```
background-jobs      Job      db-migrate       raw=migrate  before=db-migrate      after=migrate  OK
background-jobs      CronJob  nightly-report   raw=report   before=nightly-report  after=report   OK
background-jobs-prd  Job      db-migrate       raw=migrate  before=db-migrate      after=migrate  OK
background-jobs-prd  CronJob  nightly-report   raw=report   before=nightly-report  after=report   OK
```

All four job containers in the showcase were being renamed. They are not now.

One naming quirk worth knowing, and **not** a defect: the CronJob *object* is
still prefixed with the release fullname when the map key differs from it, so
`nightly-report` under release `db-migrate` renders as
`db-migrate-nightly-report`. That is `universal-chart.cronJobName` doing what its
docstring says — the 52-character Job-name budget — and it is deliberate.

---

## C. Per-job `serviceAccountName` dropped (folder 6)

**The defect.** `handle_cronjob` and `handle_job` never read the pod spec's
`serviceAccountName`, and `jobPodSpec` rendered the **release-level**
`universal-chart.serviceAccountName` helper instead.

So a CronJob that runs as a pre-existing shared identity — a backup or
maintenance ServiceAccount owned by something else — rendered as the release's
own SA:

```
raw:       serviceAccountName: nexus
rendered:  serviceAccountName: nexus-backup     <- does not exist
```

The pod cannot start, and whatever RBAC was bound to the real SA never applies.

**What fixed it.**

| File | Line | Change |
|---|---|---|
| `convert_to_universal_chart.py` | 1329 | `handle_cronjob` captures `serviceAccountName`, remapped through `name_map` |
| `convert_to_universal_chart.py` | 1394 | `handle_job` same |
| `_pod-spec.tpl` | 239 | `.job.serviceAccountName \| default (include "universal-chart.serviceAccountName" .)` |

Both converter sites read `serviceAccountName` **or** the deprecated
`serviceAccount` alias, matching what `handle_workload` already did, and both
remap through `ctx.name_map["ServiceAccount"]` so the reference follows the
object if collision resolution renamed it.

**Raw vs rendered**, from `render_check.ps1`:

```
Job      nexus-backup               raw SA=nexus     rendered=nexus          OK
Job      nexus-backup-nexus-verify  raw SA=<unset>   rendered=nexus-backup   OK (falls back to release SA)
CronJob  nexus-backup-nexus-prune   raw SA=nexus     rendered=nexus          OK
```

The middle row is the guard: a job naming no SA must still inherit the release's.

**Correction to error.txt.** It claims the converter *"captures the
serviceAccountName for CronJob/Job at line 1079 but ONLY stores it at the release
level"*. Neither the line nor the key exists — `serviceAccountName` appeared
exactly **once** in the whole converter, inside `handle_workload`. It was never
captured for jobs at all. Trusting the hint would have sent me hunting a scope
bug that does not exist.

**Tests.** `ServiceAccountName_Overridden_By_Release_Name/test/`

- `test_converter_captures_cronjob_service_account_name`
- `test_converter_captures_job_service_account_name`
- `test_job_and_cronjob_render_the_per_job_service_account`
- `test_job_falls_back_to_the_release_service_account_when_unset` (the guard)
- `test_converter_omits_service_account_when_the_job_does_not_set_one`

---

## D. Image digest dropped in the inline job image (folder 10)

`jobPodSpec` does **not** use the `universal-chart.image` helper — it builds the
image reference inline:

```gotemplate
image: {{ if .job.image }}{{ printf "%s:%s" .job.image.repository
          (.job.image.tag | default .Chart.AppVersion) }}{{ else }}...
```

No digest branch. So even after `split_image` was taught to return a digest
(folder 10's converter fix), a digest-pinned CronJob still fell back to
`Chart.AppVersion` and rendered `:2.0.0`.

`_pod-spec.tpl:262` now mirrors `universal-chart.image`: digest wins, then a
repository that already contains `@`, then `repo:tag`.

## E. Per-job `imagePullPolicy` dropped (folder 11)

`imagePullPolicy` was captured in exactly **one** place in the whole converter —
`handle_main_container`, line 693 — and `jobPodSpec` rendered the release-level
`.Values.image.pullPolicy`. A CronJob asking for `Always` inherited whatever the
main workload declared and never re-pulled; on a mutable tag it runs whatever the
node last cached.

| File | Change |
|---|---|
| `handle_cronjob` / `handle_job` | capture `image.pullPolicy` (into **base**, not minimal — it is structural, unlike a tag or digest) |
| `_pod-spec.tpl:266` | `imagePullPolicy: {{ (.job.image).pullPolicy \| default .Values.image.pullPolicy }}` |

The parenthesised `(.job.image)` is nil-safe for a job entry with no `image`
block at all.

Note `error.txt` blamed `subtract_defaults` and the image helper. Neither was the
cause — see that folder's `STATUS.md` §1.

## F. Per-job `securityContext` dropped, pod **and** container (folder 12)

Two distinct losses in one template:

- **Pod level** — `jobPodSpec` rendered `.Values.podSecurityContext`, so a job
  declaring `runAsUser/fsGroup: 1000` came out as the workload's `2000` and could
  no longer read files its own PVC owns. That is `error.txt`'s headline symptom,
  reproduced exactly in the RED output.
- **Container level** — `jobPodSpec` rendered **no container `securityContext` at
  all**. A job container's `allowPrivilegeEscalation: false`,
  `readOnlyRootFilesystem: true` and `capabilities.drop: [ALL]` were dropped
  outright. Silent un-hardening, not an override. `error.txt` does not mention
  this one; it was found by reading the template.

| File | Change |
|---|---|
| `handle_cronjob` / `handle_job` | capture `podSecurityContext` (pod level) and `securityContext` (container level) |
| `_pod-spec.tpl:244` | `{{- with (.job.podSecurityContext \| default .Values.podSecurityContext) }}` |
| `_pod-spec.tpl:267` | new block rendering the job container's `securityContext` |

**Caveat worth carrying into the migration.** The generated ApplicationSets set
`ignoreDifferences` on exactly these paths (`runAsUser`, `runAsGroup`, `fsGroup`,
`seLinuxOptions`, and the container-level ones). So the *rendered* manifest is now
correct, but ArgoCD is deliberately configured not to reconcile drift on them —
sensible on an SCC-mutating cluster, but it means a cluster that rewrites these
values will not be corrected back.


## G. Two CronJobs/Jobs collapsed onto one map key (folder 13)

**The reported symptom** was a corrupted cron string: raw `"46 2 * * 6"` rendered
as `"6 2 * * 1"`. Nothing was corrupted. `handle_cronjob` ended with

```python
ctx.values.setdefault("cronjobs", {})[sub_name] = entry
```

and `sub_name` is `short_subresource_name()`, which strips a leading
`"<microservice>-"`. A microservice is one **file**, so two CronJobs in that file
differing only by that prefix reduce to the same key:

```
nightly-report-cleanup  ->  "cleanup"
cleanup                 ->  "cleanup"     <-- second plainly overwrites the first
```

The whole entry went, not just the schedule — command, args, env, history limits.
Then the chart re-added the release prefix, so the single survivor rendered as
`nightly-report-cleanup`: **the deleted object's name carrying the surviving
object's schedule.** That is the entire "46 2 * * 6 -> 6 2 * * 1".

| File | Change |
|---|---|
| `MicroserviceContext` | new `sub_key_owner` field |
| `rendered_subresource_name()` | new — mirrors the chart's `universal-chart.subresourceName` |
| `claim_subresource_key()` | new — unique key, order-independent collision resolution, warning |
| `handle_cronjob` / `handle_job` | use it instead of the bare `dict[key] = entry` |

No template changed. `cronjob.yaml:20` already rendered
`{{ $spec.schedule | quote }}` faithfully — the first folder in this campaign
where the job path was the *only* site rather than the forgotten second one.

**Which object keeps the short key is not document order.** It is whichever raw
name the chart can reproduce from that key, computed the same way the chart
computes it. Reversing the manifests in the file gives the same result, and a
test asserts it both ways.

**The loser is renamed, not dropped.** Under release `nightly-report` no map key
can produce the bare object name `cleanup` — this chart derives a subresource's
name from its key alone. So two raw objects that reduce to one key cannot both
keep their names; that is the chart's shape, not a choice made here. What changed
is the failure mode: a visibly renamed CronJob still running its own schedule and
command, announced in `conflicts_and_warnings.txt`, instead of a silent deletion.

```
[background-jobs/nightly-report] CronJob/nightly-report-cleanup and CronJob/cleanup both
reduce to the 'cronjobs' key 'cleanup' - name collision. The second used to silently
overwrite the first; both are kept now, but 'cleanup' renders as
'nightly-report-cleanup-2' rather than its own name.
```

**`handle_job` had the identical bug two lines below** and is fixed by the same
helper.

`error.txt`'s alternative hypothesis — `subtract_defaults` bleeding the integer
`6` from `successfulJobsHistoryLimit` into the schedule string — is false, and is
disproved with those exact values in that folder's `STATUS.md` §1.

---


## H. Command, args and env went with the collapsed entry (folder 14)

Not a second defect — the same line as G. The overwrite replaced the **whole**
`cronjobs[key]` entry, so restoring the entry restores command, args and env too.
This folder adds the coverage from that side and a mutation test proving it: with
`claim_subresource_key` reverted, error.txt's issue 10 reproduces verbatim.

```
AssertionError: one CronJob's STAGE was lost during the merge; got ['prod']
AssertionError: CronJob/db-migrate-backup's command is gone;
  got [['/bin/bash', '-c', "echo 'Hello from jenkins-backup-2!'"]]
7 failed, 1 passed        restored: True  sha256 52c6c12395c5ecc6
```

**One half of the report is wrong: "the values YAML has neither" command.** It has
both, one layer up. `base/<slug>.yaml` carries only what every namespace running
that microservice agrees on, so two namespaces with different commands push
`command` into each namespace's own `values/<ns>/<slug>.yaml`. Literal env moves
out for a separate reason — `build_minimal_values` deliberately relocates values
like `STAGE: dev` to the minimal override while `valueFrom` entries stay
structural.

**Practical rule for reading generated output:** a CronJob's command or env being
absent from `base/` proves nothing. Merge `base/` with `values/<ns>/` before
concluding anything is lost.

---

## I. Explicit `0` replaced by the chart's fallback (folder 15)

`backoffLimit: 0` means "never retry a failed Job"; `successfulJobsHistoryLimit: 0`
means "keep no completed pods". Both rendered the chart's fallback instead,
because Go template's `default` treats `0` as empty:

```gotemplate
successfulJobsHistoryLimit: {{ $spec.successfulJobsHistoryLimit | default 3 }}
failedJobsHistoryLimit:     {{ $spec.failedJobsHistoryLimit | default 1 }}
backoffLimit:               {{ $spec.jobTemplate.backoffLimit | default 3 }}   # cronjob.yaml
backoffLimit:               {{ $spec.backoffLimit | default 3 }}               # job.yaml
```

All four are `hasKey` guards now. The prediction recorded here in an earlier
round — that `| default` and not `subtract_defaults` was the real mechanism —
held: `subtract_defaults` compares values rather than truthiness, and every
converter-side test passed before the fix.

**`backoffLimit`'s fallback also changed, 3 to 6**, to match Kubernetes' own
default. The chart was inventing a number nobody chose, so a Job with no
`backoffLimit` in its values retried half as often as the same Job applied with
`kubectl`. Converted releases are unaffected — a raw dump always carries the
field.

The `| default <number>` audit came back clean: those four were the only numeric
default filters in the chart.

---

## J. Container-level `restartPolicy` never captured (folder 16)

The pod-level `restartPolicy` always round-tripped. The **container**-level one
— a different field at a different path — was captured by no handler and rendered
by no template:

```
spec.template.spec.restartPolicy                  <- pod level, already worked
spec.template.spec.containers[*].restartPolicy    <- the gap
spec.template.spec.initContainers[*].restartPolicy <- already worked (pass-through)
```

`Always` on an initContainer is how Kubernetes 1.28+ declares a **native
sidecar**. Lose it and the sidecar becomes an ordinary init container that the
Job waits to COMPLETE — so a log shipper hangs the Job forever rather than
running alongside it. On a regular container (1.33+) it restarts that container
in place instead of failing the pod.

Captured now in `handle_main_container`, `handle_cronjob` and `handle_job`;
rendered in both `mainContainer` and `jobPodSpec`. The values key is
`containerRestartPolicy` because `.Values.restartPolicy` is already the pod's —
reusing the name would have made the two overwrite each other.

`error.txt` blamed a wrong chart default (`containerRestartPolicy | default
"OnFailure"` at `_pod-spec.tpl:244`). No such line exists. Nothing replaced
`Always`; nothing rendered anything at all.

**This closes the last of the three CronJob-titled folders.**

---
## Still broken in the CronJob/Job path

Nothing below has been touched.

**The dedicated CronJob folder still open:**
- **16 — ContainerRestartPolicy_Not_Captured.** `Always` becomes `OnFailure` on
  CronJob/Job. Note `jobPodSpec:234` already renders
  `{{ .job.restartPolicy | default "OnFailure" }}` and `handle_cronjob` captures
  `pod_tpl_spec["restartPolicy"]` — so the **pod-level** restartPolicy round-trips
  today. The folder is about the **container-level** `restartPolicy` field
  (the Kubernetes 1.28+ sidecar-container feature), which is a different field
  and is not captured.

---

## Verification state

At the time of writing: **183 tests pass** across all completed folders, the
chart's own **234 tests pass**, `helm lint` is clean, and the showcase fidelity
report reads **0 lost / 0 changed / 0 missing / 0 extra**.

None of the fixes above changed the generated showcase output, because no
showcase Job or CronJob declares a `serviceAccountName`, an `imagePullPolicy`, a
`securityContext`, a digest-pinned image or a duplicated volume mount — and the
showcase's one CronJob and one Job each sit alone in their microservice file, so
nothing collides. Every one
of them needed a fixture, and the RED test output — not the fidelity count — is
the evidence the defect was real.

Nothing about the CronJob behaviour was verified against a running cluster —
there is none. What is verified is that the rendered manifests carry the values
the raw manifests declared. Whether a CronJob then fires on time, mounts its
files, or is admitted by the API server is not observable here.
