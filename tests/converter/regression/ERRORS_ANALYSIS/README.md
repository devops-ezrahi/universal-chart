# ERRORS_ANALYSIS — defect campaign log

One folder per defect from the original round-trip fidelity report. Each folder
holds the source `error.txt`, a `test/` directory with a minimal fixture and
executable tests, and a `STATUS.md` recording exactly what was found, changed and
verified.

**Complete: all 36 folders, plus four defects found that have no folder — two
fixed, two recorded.**

The brief's final step — identify the two missing report issues (28/29) — is
answered in [`_MISSING_ISSUES_ANALYSIS.md`](_MISSING_ISSUES_ANALYSIS.md). Short
version: **nothing is failing**, so the answer cannot be read off the report;
it comes from what the checker structurally cannot see. The strongest
candidate is a hand-managed `Endpoints` object being dropped with the report
saying "No fidelity issues found" — it is not even counted as an object.

**Start here for the "why did this happen" explanation of every defect:**
[`README-EXPLAINED.md`](README-EXPLAINED.md) — root causes, what changed, and why.

For the CronJob/Job-specific view across all folders, see
[`README-cronjob.md`](README-cronjob.md).

Everything below was verified offline — no cluster was ever available. Every fix
is backed by tests that were shown RED before the change and GREEN after.

---

## Quick reference

```powershell
# the whole accumulated regression suite (all folders completed so far)
python -m pytest universal/gitops-factory/output-showcase/ERRORS_ANALYSIS -q

# the chart's own suite (must stay at 234)
helm unittest universal/Universal-chart

# regenerate the showcase with the fidelity check
cd universal/gitops-factory
python convert_to_universal_chart.py `
  --input namespaces-showcase --output output-showcase `
  --chart ../Universal-chart --verify-fidelity `
  --values-repo-url https://github.com/devops-ezrahi/homelab.git `
  --values-revision checker `
  --values-path universal/gitops-factory/output-showcase
```

Current state: **331 tests pass**, chart's own **234 tests pass**, `helm lint`
clean, fidelity report **0 lost / 0 changed / 0 missing / 0 extra**.

Two standing whole-showcase sweeps, runnable from `universal/gitops-factory`:

```powershell
python output-showcase/ERRORS_ANALYSIS/Main_Container_Name_Forced_To_Release_Name/test/compare_all_container_names.py
python output-showcase/ERRORS_ANALYSIS/Route_And_Service_Names_Replaced_By_Fullname/test/compare_all_object_names.py
```

---

## Results at a glance

| # | Folder | Verdict |
|---|---|---|
| 1 | StatefulSet_PVC_Retention_Policy_Lost | Fixed — converter + template + schema |
| 2 | StatefulSet_ServiceName_Hardcoded_To_Fullname | Fixed — captured unconditionally |
| 3 | VolumeMount_SubPath_Lost_Duplicate_Name_Collision | Fixed — three-sided, not two |
| 4 | Sidecar_Container_Name_Key_Collision | **Does not reproduce** — refuted with proof |
| 5 | Main_Container_Name_Forced_To_Release_Name | Fixed — 12 of 19 workloads were renamed |
| 6 | ServiceAccountName_Overridden_By_Release_Name | Fixed — Job/CronJob half only |
| 7 | Route_And_Service_Names_Replaced_By_Fullname | Fixed — new `primaryServiceName` helper |
| 8 | Missing_Objects_Not_Rendered_By_Any_Release | Already fixed by 7 — count 3 → 0 |
| 9 | Extra_Objects_Rendered_With_No_Raw_Source | Already fixed by 7 — count 3 → 0 |
| 10 | Image_Digest_Replaced_With_Mutable_Tag | Fixed — `split_image` now returns a digest |
| 11 | ImagePullPolicy_Always_Downgraded_To_IfNotPresent | Fixed — per-job pull policy |
| 12 | Pod_SecurityContext_Overridden_By_Release_Defaults | Fixed — closes root-cause row C |
| 13 | CronJob_Schedule_Value_Corruption | Fixed — not corruption; two CronJobs collapsed into one |
| 14 | CronJob_Merge_Overwrites_Command_And_Env | Already fixed by 13 — "has neither" refuted |
| 15 | Subtract_Defaults_Removes_Explicit_Zero | Fixed — in the chart; `subtract_defaults` exonerated |
| 16 | ContainerRestartPolicy_Not_Captured | Fixed — container-level field, absent from both sides |
| 25 | Service_AppProtocol_Lost | Fixed — converter + BOTH Service templates |
| 30 | Service_ExternalTrafficPolicy_Lost | **No impact here** (0 raw occurrences) — real `services.yaml` gap fixed |
| 31 | Service_SessionAffinity_Lost | **Cosmetic here** (300/300 are `None`) — real `services.yaml` gap fixed |
| 21 | ExternalSecret_Spec_Fields_Not_Captured | Fixed — whitelist removed; 2 of 5 claims refuted |
| 22 | Zero_Width_Unicode_In_SecretStoreRef | Fixed defensively — 0 occurrences here |
| 23 | ExternalSecret_ManagementPolicy_Changed | Already fixed by 21 — **every** clause of the mechanism refuted |
| 24 | SecretStore_Spec_Conditions_Not_Rendered | Labels fixed by 21; conditions **already worked** |
| 32 | Service_Selector_Injected_When_Absent | Checker was already right — found an asymmetry + a real unguarded risk |
| 33 | Legacy_ServiceAccount_Field_Lost | **Confirmed false positive** — checker taught to stop reporting it |
| 34 | ProgressDeadlineSeconds_Lost | **0 occurrences here** — real gap fixed anyway |
| 35 | Grafana_ConfigMap_Trailing_Newline | **Refuted** — byte-exact through the whole pipeline |
| 20 | Workload_Metadata_Labels_Lost | Fixed — **the report's largest defect**; 19 templates + most handlers |
| 28 | Annotations_Lost | Fixed — LOW rating holds, but only by luck; triage in STATUS §1 |
| 17 | ServiceAccount_Secrets_And_ImagePullSecrets_Lost | Fixed — unfinished analysis finished: 316 SAs, 0 custom |
| 27 | Empty_Map_Env_Var_Breaks_Generation | Fixed — both sides; `helm template` was **failing**, not just malformed |
| 29 | Empty_K8s_Default_Maps_Dropped | **Confirmed false positive** — checker suppresses `{}`/`[]` only |
| 26 | Container_Ports_Reordered_And_Auto_Named | Fixed — auto-naming real; ordering was a CHECKER bug |
| 18 | ConfigMap_Secret_Identical_Content_Duplicated | Rename KEPT (decision); warning made actionable |
| 19 | ConfigMap_Secret_References_Prefixed_With_Release | **Verified** — 0 dangling refs across all 24 releases |
| 22 | ExternalSecret_Name_Collision_Suffix | Mechanism refuted — no `-2` exists; real concern pinned |
| — | `_FOUND_Global_Defaults_Subtracted_But_Never_Written` | **Two P0s found, no error.txt** — both fixed |

---

## 1. StatefulSet_PVC_Retention_Policy_Lost — P0, irreversible data loss

**Defect.** `spec.persistentVolumeClaimRetentionPolicy` was neither extracted by
the converter nor rendered by the template. Kubernetes defaults both halves to
`Delete`, so a StatefulSet that declared `Retain` came back looking identical and
then destroyed its PVCs — and the data on them — on the first delete, scale-down
or ArgoCD recreate.

**What I changed.**

| File | Where | Change |
|---|---|---|
| `convert_to_universal_chart.py` | 759 | Extract the field in the `if kind == "StatefulSet":` block |
| `Universal-chart/templates/statefulset.yaml` | 20–23 | Render it, guarded by `{{- with }}` |
| `Universal-chart/values.yaml` | 429 | Declare `persistentVolumeClaimRetentionPolicy: {}` |
| `Universal-chart/values.schema.json` | — | Type + enum `Retain`/`Delete`, `additionalProperties: false` |

**Judgement call.** The chart default is `{}` (field omitted), **not** `Retain`.
Defaulting to `Retain` is safer for data but would override the intent of anyone
who wants the Kubernetes default, and would show up as an invented field on every
raw-vs-rendered diff. The converter copies the raw value verbatim instead, so
`Retain` survives where declared and nothing is fabricated where it isn't. Both
behaviours are pinned by tests.

Also fixed a side effect I caused: my fixtures under `output-showcase/` made the
converter's stale-output scanner report them as stale generated files, so
`ERRORS_ANALYSIS` joined `report` in `NON_NAMESPACE_OUTPUT_DIRS` (line 231).

---

## 2. StatefulSet_ServiceName_Hardcoded_To_Fullname — P0

**Defect.** `statefulset.yaml:14` hardcoded `serviceName` to the release
fullname, and the converter never read it. A StatefulSet governed by a dedicated
headless Service (`<name>-headless`, the normal pattern for clustered data
systems) pointed instead at the regular ClusterIP Service. Only a headless
Service publishes per-pod DNS records, so peer discovery and cluster formation
break.

**What I changed.** Converter line 748 captures `spec.serviceName`;
`statefulset.yaml:14` became
`{{ .Values.serviceName | default (include "universal-chart.fullname" .) }}`;
plus values + schema entries.

**Deviation from error.txt.** It proposed capturing only when the name differs
from the workload name. I capture **unconditionally**. The conditional form is
correct today only because `fullnameOverride` is pinned elsewhere in the file —
and this entire defect exists because an unstated "the names will match"
assumption turned out to be false. Re-encoding that assumption inside its own fix
is how it comes back. A test pins the choice so it can't be quietly optimised
away.

**Sub-cases not fixable here.** error.txt splits this into three. Only (a) is
addressable: (b) `prometheus-operated` is created by an operator at runtime and
exists in no manifest; (c) is a cross-namespace `serviceName`, which the
Kubernetes API cannot express — error.txt itself says *"this one is broken in raw
too."*

---

## 3. VolumeMount_SubPath_Lost_Duplicate_Name_Collision — P0

**Defect.** `named_list_to_map` keyed volumeMounts by mount **name**. Mounting
one volume several times with different `subPath`s — the standard way to project
individual ConfigMap keys onto individual file paths — collapsed to whichever
mount came last. Containers started missing config files.

Measured on the fixture: **4 volumeMounts collapsed into 2.**

**What I changed.**

| File | Where | Change |
|---|---|---|
| `convert_to_universal_chart.py` | 101, 519 | `Counter` import; composite key + explicit `name` for duplicated names |
| `_helpers.tpl` | 172 | `mapToList`: `- name: {{ $spec.name \| default $name }}` |
| `_helpers.tpl` | 242 | `validateVolumeMounts`: resolve `$mountSpec.name \| default $mountKey` |

**error.txt undercounted twice.**

- It called the fix "TWO-SIDED". It is **three**-sided: `validateVolumeMounts`
  also keys off the map key and calls `fail`, so a converter + `mapToList` fix
  alone makes the chart refuse to render at all.
- It discussed only the main container. `named_list_to_map` is called for
  volumeMounts in **three** places (main container, CronJob, Job). The fix is
  inside the shared helper, so all three are covered.

**Design.** Key format `<name>:<subPath>`, with the real name carried as an
explicit `name` field rather than parsed back out of the key — string-splitting
in Go templates is brittle and mountPaths contain the separator characters.
Unique names keep their bare key and gain no `name` field, so existing values
files and GitOps overrides are unaffected.

---

## 4. Sidecar_Container_Name_Key_Collision — does not reproduce

This folder is **not** on the "suspected false positives" list, so I did not
assume it. I built the scenario it describes and ran it.

**Both claims fail.**

- *"The chart template only renders the main container. Sidecars are completely
  missing. This is the biggest functional gap."* — `_pod-spec.tpl:82-85` is a
  `range` over `.Values.sidecars` that renders every one.
- *"Multiple microservices sharing a base name overwrite each other."* — I built
  two namespaces sharing the microservice file name `api-gateway` with different
  sidecar sets, confirmed they really do share one `base/api-gateway.yaml`
  (asserted as a precondition), and got zero loss and zero leak:

```
shop-web      RAW ['traefik','oauth-proxy','log-shipper']  RENDERED [...,'log-shipper','oauth-proxy']
shop-web-eu   RAW ['traefik','log-shipper','rate-limiter'] RENDERED [...,'log-shipper','rate-limiter']
```

`common_subtree` only hoists what is identical in **every** member of a group,
and deep-merge only ever adds, so a namespace cannot inherit a sidecar it never
declared.

**Proof the tests aren't vacuous.** Nothing went RED naturally, so I deleted the
sidecar loop from `_pod-spec.tpl` on purpose. Four tests failed with exactly
error.txt's reported symptom (`rendered containers = ['api-gateway']`), and I
reverted byte-identically (`git diff` clean).

**No code changed.** Most likely the finding is real but stale — captured against
an older chart. I cannot confirm that from this repo, so I report what is true
now, with the evidence.

---

## 5. Main_Container_Name_Forced_To_Release_Name — P0, your decision

**You chose Option A: preserve the raw container name.**

**Defect.** The chart named its managed container after the release, so a raw
`nginx` rendered as `storefront`. The container still runs, but everything that
addresses it *by name* breaks: `kubectl logs -c` / `exec -c`, log-pipeline
`container="..."` filters, and the `container` label on kubelet metrics.

**This is the first folder the real showcase data exercises**, so it has a hard
before/after. I rebuilt the pre-fix template from `git show HEAD:` and rendered
the same values through it:

```
BEFORE:  19 workloads compared, 12 mismatches
AFTER:   19 workloads compared,  0 mismatches
```

e.g. `fluent-bit` → `log-agent`, `node-exporter` → `metrics-agent`, `traefik` →
`api-gateway`, `nginx` → `storefront`, `migrate` → `db-migrate`, `report` →
`nightly-report`.

**What I changed.**

| File | Where | Change |
|---|---|---|
| `convert_to_universal_chart.py` | 666, 1318, 1390 | Capture `containerName` on the main container, CronJob and Job paths |
| `convert_to_universal_chart.py` | 2483 | **Removed** the `containers[0].name` suppression from `_fidelity_ignored_path` |
| `_pod-spec.tpl` | 124 | `.Values.containerName \| default (include "universal-chart.name" .)` |
| `_pod-spec.tpl` | 258 | `.job.containerName \| default .job.name` |
| `values.yaml` / `values.schema.json` | — | Declare `containerName` |

**The real prize is the suppression removal.** `_fidelity_ignored_path` had been
skipping `containers[0].name`, which is why the report read *0 changed* while 12
of 19 workloads were rendering a renamed container. That suppression also made
the checker structurally unable to report `containers[0]` changing **for any
reason**, including vanishing. It is gone — and the report still reads 0, this
time because the names genuinely match.

**error.txt undercounted again.** It named only the main container. `jobPodSpec`
names the Job/CronJob container `{{ .job.name }}` — the *map key*, not the
container name — so Jobs and CronJobs were renamed by a second mechanism.

---

## 6. ServiceAccountName_Overridden_By_Release_Name — P0

**Defect.** `handle_cronjob` and `handle_job` never read the pod spec's
`serviceAccountName`, and `jobPodSpec` rendered the release-level helper. A
CronJob running as a pre-existing shared `nexus` ServiceAccount rendered as
`nexus-backup` — a ServiceAccount that does not exist, so the pod never starts
and the RBAC bound to the real SA never applies.

**What I changed.** Converter lines 1329 and 1394 capture the field (reading
`serviceAccountName` **or** the deprecated `serviceAccount` alias, and remapping
through `name_map`); `_pod-spec.tpl:239` became
`.job.serviceAccountName | default (include "universal-chart.serviceAccountName" .)`.

**error.txt is wrong in a way that mattered.** It claims the converter *"captures
the serviceAccountName for CronJob/Job at line 1079 but ONLY stores it at the
release level (`ctx.values["serviceAccountName"]`)"*. There is no such capture and
no such key — `serviceAccountName` appeared exactly once in the whole converter,
in `handle_workload`. Had I trusted it I would have hunted a scope bug that
doesn't exist, and possibly "fixed" the main workload path, which was already
correct. Two guard tests pin that path, passing before and after.

**Scope.** This fixes the *reference* half for Jobs/CronJobs. It does not address
renamed or lost SA **objects** (the "18 wrong/lost" half of error.txt's 27).

---

## 7. Route_And_Service_Names_Replaced_By_Fullname — P1

**Defect.** The primary Service and Route were both named
`universal-chart.fullname`, so raw `control-station-client-service` and
`control-station-client-route` both rendered as `control-station-client`. The
suffixed names ceased to exist, and the Route and Service collided on one name.

**What I changed.**

| File | Where | Change |
|---|---|---|
| `_helpers.tpl` | 121 | New `universal-chart.primaryServiceName` helper |
| `service.yaml` | 5 | Use the helper |
| `route.yaml` | 6, 22 | Route's own name; `to.name` via the helper |
| `routes.yaml` | 28 | Extra-Route default via the helper |
| `ingress.yaml` | 30 | Backend `service.name` via the helper |
| `convert_to_universal_chart.py` | 897, 957, 1249 | Record Route name, Service name, Ingress backend |

**Why a helper.** Every reference to the primary Service must resolve in one
place. Renaming the Service in `service.yaml` alone would leave Route, extra
Routes and Ingress pointing at the old fullname — turning a naming defect into a
**dangling-reference** defect, which is strictly worse. The Ingress backend isn't
in error.txt; it's in scope because the Service rename *requires* it.

**Correction to error.txt.** It blames `build_route_entry` for setting
`spec_to_name = release_fullname`. No such assignment exists — that function
already captured the correct target. The loss happened one function later, in
`handle_route`, which **deliberately popped it** with a comment asserting the
primary Route "always targets this release's own fullname Service". That
assumption was the defect.

**Fixture note.** My first fixture had a Route and an Ingress in one microservice
and failed to render: the chart deliberately refuses both
(*"ingress.enabled and route.enabled cannot both be true — pick one"*). That is a
genuine chart constraint, so I reshaped the fixture into two microservices rather
than touching the chart.

---

## 8 & 9. Missing / Extra objects — already fixed by folder 7

Both were verified, not assumed. The brief asked specifically: *verify 8 and 9
cancel out; if only one count drops, they were not the same objects.*

I rebuilt the pre-fix tree (`git show HEAD:` for the converter and every chart
file I had touched) and ran both versions over the same fixture:

```
BEFORE   objects in raw but rendered by nothing:  3
         objects rendered with no raw source:     3
AFTER    both: 0
```

The pairing is exact, object by object:

| counted MISSING | counted EXTRA |
|---|---|
| `Route/control-station-client-route` | `Route/control-station-client` |
| `Service/control-station-client-service` | `Service/control-station-client` |
| `Service/storefront-service` | `Service/storefront` |

**Both counts moved together — confirmed to be the same objects counted twice.**
A standing test asserts `missing == extra` so any future divergence fails loudly.

Tests for both folders live in **one module** in folder 8
(`test_missing_and_extra_objects.py`), deliberately, so the two halves of one
defect cannot drift apart.

**error.txt's second sub-cause got a different answer.** It blames 1 Deployment +
1 PVC on a release whose name exceeded the limit and was *"SKIPPED entirely"*.
This converter does not silently skip — `validate_resource_names` aborts the run
before writing anything. A test pins that loud behaviour.

---

## FOUND: global defaults subtracted but never written — P0, no error.txt

Discovered while verifying folders 8/9. Belongs to no folder; strong candidate
for one of the two missing report issues (28/29).

**Defect.** The tree-root `defaults.yaml` is written **only** in cluster-scoped
mode, but the subtraction of `global_defaults` from every microservice's values
ran in **both** modes. Namespace-scoped is the default. So by default, every
value identical across all microservices in a run was deleted from the output and
written nowhere.

The code's justification was set-theoretically sound but defeated by **ordering**:
`namespace_defaults` is computed in Pass 3 from `ctx.values` *after* the
subtraction has already emptied them, so nothing removed can reappear.

**Impact.** Two similar Deployments in one namespace is enough. The generated base
file came out as:

```yaml
ingress: {...}
nameOverride: storefront
service: {name: storefront-service}
```

No `workload`, no `image`, no `ports`, no `containerName`, no `service.ports` — a
Service rendering with **zero ports**. Fidelity on that input: 2 fields LOST, 4
CHANGED. The same fixture with **one** microservice is perfectly clean, which
isolates the trigger to homogeneity.

**It was not dormant.** It was actively eating the `ingress.serviceName` that
folder 7 had just added for `dex`, `api-gateway` and `storefront` — those three
values files showed no diff after folder 7 because the value was being swallowed.
They only appeared once this was fixed.

**Fix.** Converter line 3067: both subtraction loops moved *inside* the same
`if not NAMESPACE_SCOPED_ONLY:` block as the write. The subtraction and the write
are one operation — a value may only be removed because a layer underneath
supplies it again.

A guard test asserts cluster-scoped mode **still writes the layer it subtracts**,
so the fix is a correction rather than a feature deletion.

---

## 10. Image_Digest_Replaced_With_Mutable_Tag — P1

**Defect.** `split_image` recognised `repo:tag` but not `repo@sha256:<digest>`.
Given a digest it returned `(repo, "")` — digest discarded — so the chart fell
through to `repository:{{ .Chart.AppVersion }}` and rendered `postgres:2.0.0`.
An immutable pin became a mutable tag.

**Converter-only for the main container** — the chart's `universal-chart.image`
helper had handled `.Values.image.digest` all along. The one folder so far where
the template side needed nothing… except `jobPodSpec`, which builds its image
inline and had no digest branch. Same omission pattern as folders 3, 5 and 6.

`split_image` now returns **(repository, tag, digest)**. Three parts rather than
folding the digest into the repository string, so it can route like a tag — into
the per-namespace minimal values, not the shared base, since re-pinning one
namespace must not drag every other namespace along. A digest wins over a tag.

Nine parametrised cases cover the parsing, including `registry:5000/ns/app`,
where the pre-existing `/`-in-tag guard stops a registry port being read as a tag.

## 11. ImagePullPolicy_Always_Downgraded_To_IfNotPresent — P1

**error.txt names the wrong mechanism.** It blames `subtract_defaults` removing
`Always` as "same as default", and claims `universal-chart.image` only renders
`pullPolicy` when it differs from `IfNotPresent`. Both checked, both false:
`subtract_defaults` compares against the *generated* defaults, not the chart's
`values.yaml`, and `universal-chart.image` does not render `pullPolicy` at all.

**The real cause** is the same as folder 6: `imagePullPolicy` was captured in
exactly one place — `handle_main_container` — and `jobPodSpec` rendered the
release-level `.Values.image.pullPolicy`. A CronJob asking for `Always` inherited
the workload's `IfNotPresent` and never re-pulled.

The fixture makes it visible by setting the release to `IfNotPresent` and both
jobs to `Always`. With all three the same the bug is invisible — very likely why
it survived.

**Closes the compounding risk error.txt warned about**: with folder 10, a
digest-pinned workload is no longer re-pointed at a mutable tag, *and* a job that
wanted to re-pull still does.

## 12. Pod_SecurityContext_Overridden_By_Release_Defaults — P1

**Defect.** `handle_cronjob` / `handle_job` never captured the job's
`securityContext`, and `jobPodSpec` rendered the release-level
`.Values.podSecurityContext`. A job declaring `runAsUser/fsGroup: 1000` came out
as the workload's `2000` and could no longer read files its own PVC owns.

**Plus a loss error.txt does not mention**, found by reading the template:
`jobPodSpec` rendered **no container-level `securityContext` at all**, so a job
container's `allowPrivilegeEscalation: false`, `readOnlyRootFilesystem: true` and
`capabilities.drop: [ALL]` were dropped outright — silent un-hardening, not an
override.

**Two more error.txt corrections.** Line 852 does not capture `podSecurityContext`
"per-container" — line 836 reads the pod-level field, correctly scoped, and it
already worked. And the chart's `values.yaml` declares `podSecurityContext: {}`;
the 2000/2000 was *generated* data in the original project, not a chart default.

**This closes root-cause row C** — *"Chart uses release-level defaults instead of
per-resource values | Affects: CronJob/Job SA, securityContext, imagePullPolicy"*.
SA was folder 6, pullPolicy folder 11, securityContext this one.

---

## 13. CronJob_Schedule_Value_Corruption — P1

**Defect.** Reported as a corrupted cron string: raw `"46 2 * * 6"` rendered as
`"6 2 * * 1"`. It is neither corruption nor a string bug. `handle_cronjob` ended
with

```python
ctx.values.setdefault("cronjobs", {})[sub_name] = entry
```

where `sub_name` strips a leading `"<microservice>-"`. A microservice is one
*file*, so two CronJobs in that file differing only by the prefix —
`nightly-report-cleanup` and `cleanup` — reduced to the same key and the second
plainly overwrote the first. The chart then re-added the release prefix, so the
one surviving entry rendered under the *deleted* object's name while carrying
the *survivor's* schedule. Two objects collapsed into one, silently.

**What I changed.** Converter only; no template touched.

| File | Where | Change |
|---|---|---|
| `convert_to_universal_chart.py` | `MicroserviceContext` | new `sub_key_owner` field |
| `convert_to_universal_chart.py` | after `short_subresource_name` | `rendered_subresource_name()` — mirrors the chart's `universal-chart.subresourceName` |
| `convert_to_universal_chart.py` | same | `claim_subresource_key()` — unique key, collision resolution, warning |
| `convert_to_universal_chart.py` | `handle_cronjob`, `handle_job` | use it instead of the bare assignment |

Which colliding object keeps the short key is decided by whether the chart can
reproduce its raw name from that key — **not by document order**, which is
asserted both ways round. The loser is kept under a `-2` key and named in
`conflicts_and_warnings.txt`: this chart derives a subresource's name from its
map key alone, so no key can produce the bare name `cleanup` under release
`nightly-report`. Two such objects cannot both keep their names; what changed is
that you now get a visibly renamed CronJob running its own schedule instead of a
silently deleted one.

**error.txt offered two hypotheses; I checked both.** The `subtract_defaults`
one — that the integer `6` from `successfulJobsHistoryLimit` bled into the
schedule string — is false, disproved with those exact values in `STATUS.md` §1.
So is any YAML-dumper theory: every cron form, including `*/15 * * * *` and
`@daily`, round-trips byte-identically.

**Shares a line with folder 14.** The overwrite replaced the whole entry, so
command and env are restored too, and a test pins that here. Folder 14 is *not*
closed: its error.txt claims **both** commands vanish, which last-writer-wins
does not explain.

---

## 14. CronJob_Merge_Overwrites_Command_And_Env — P1

**Already fixed by folder 13**, and half the report's claim is wrong. Same
outcome shape as folders 8 and 9, which folder 7 cleared.

The overwrite is the line folder 13 replaced: the whole `cronjobs[key]` entry was
replaced, so command, args and env went with the schedule. Restoring the entry
restores all of it — there is no separate command defect. Proved not vacuous by
reverting `claim_subresource_key` to the original assignment and re-running these
tests, which produce error.txt's issue 10 verbatim:

```
AssertionError: one CronJob's STAGE was lost during the merge; got ['prod']
AssertionError: CronJob/db-migrate-backup's command is gone;
  got [['/bin/bash', '-c', "echo 'Hello from jenkins-backup-2!'"]]

7 failed, 1 passed        restored: True  sha256 52c6c12395c5ecc6
```

**Refuted: "the values YAML has neither".** It has both — one layer up. `base/`
holds only what *every* namespace running the microservice agrees on, so two
namespaces with different commands push `command` out of `base/` and into each
namespace's own `values/<ns>/<slug>.yaml`:

```
base\db-migrate.yaml                       (no command here)
background-jobs\values\db-migrate.yaml     - echo 'Hello from jenkins-backup-1!'
background-jobs\values\db-migrate.yaml     - echo 'Hello from jenkins-backup-2!'
background-jobs-prd\values\db-migrate.yaml - echo 'Hello from jenkins-backup-1-prd!'
background-jobs-prd\values\db-migrate.yaml - echo 'Hello from jenkins-backup-2-prd!'
```

Literal env splits the same way for an independent reason — `STAGE: dev` is moved
to the namespace's minimal override by design, while `valueFrom` entries stay
structural. **Any audit of command or env fidelity must merge `base/` with
`values/<ns>/` before comparing.** Both rules now have tests, so if the layering
ever becomes a real loss a test says so instead of a reader needing to know it.

---

## 15. Subtract_Defaults_Removes_Explicit_Zero — P1

**Defect.** A CronJob or Job declaring `backoffLimit: 0` ("never retry") or
`successfulJobsHistoryLimit: 0` ("keep no completed pods") rendered the chart's
fallback instead. One-sided, and **not** where error.txt points.

`subtract_defaults` is exonerated. It compares values, not truthiness
(`elif val != dval`), so a 0 is removed only when the default is also 0 — and
removal is not loss, because that layer is applied underneath at render time. All
six converter-side tests passed before the fix; the four that failed were all
`helm template` assertions. The cause is Go template's `default`, which treats 0
as empty:

```gotemplate
successfulJobsHistoryLimit: {{ $spec.successfulJobsHistoryLimit | default 3 }}
backoffLimit:               {{ $spec.jobTemplate.backoffLimit | default 3 }}
```

**What I changed.** `cronjob.yaml` and `job.yaml` only — all four numeric
`| default` filters became `hasKey` guards. That audit came back clean: those
were the only numeric `| default`s in the whole chart, so no other field can lose
an explicit 0 this way.

**One deliberate behaviour change.** `backoffLimit`'s fallback was **3**;
Kubernetes' own default is **6**. A Job with no `backoffLimit` in its values was
silently retrying half as often as the same Job applied with `kubectl`, and the
fidelity checker would report the invented number as an ADDED field disagreeing
with the raw dump. It is 6 now. No converted release is affected — a raw cluster
dump always carries the field — but a hand-written values file that omits it will
render 6 on the next sync.

**Second P0 found while probing this.** See the row below.

---

## — Second `_FOUND` P0: the `shared` release skips the layer it was subtracted against

Found while checking error.txt's `subtract_defaults` claim, and fixed under
`_FOUND_Global_Defaults_Subtracted_But_Never_Written` rather than folder 15.

The first fix paired the subtraction with *writing* the root `defaults.yaml`.
But `shared` is the one release that never **applies** it —
`build_shared_application` gives it a dedicated Application with a chain of
`base/` + `values/` and nothing else, deliberately. Written is not applied, and
for that release the two diverge by design.

```
AssertionError: the shared release rendered
  ['ConfigMap', 'Deployment', 'Service', 'ServiceAccount'];
  it declared only a ConfigMap
```

A config-only shared release lost `workload.type: none` and
`service.enabled: false` into a file it never reads, then fell back to the
chart's defaults — the exact phantom-object failure that Application exists to
prevent, arriving through the back door. Fixed by never subtracting from
`shared` in either mode.

**The rule this establishes:** every "subtract X from a release" needs the
matching question *"does that release apply the layer X went into?"* — asked
twice now, answered wrong twice.

---

## 16. ContainerRestartPolicy_Not_Captured — P1

**Defect.** The **container-level** `restartPolicy` was captured by nobody and
rendered by nothing. Only the pod-level field round-tripped.

| path | before |
|---|---|
| `spec.template.spec.restartPolicy` | already worked |
| `spec.template.spec.containers[*].restartPolicy` | **the gap** |
| `spec.template.spec.initContainers[*].restartPolicy` | already worked (pass-through) |

`Always` on an initContainer is how Kubernetes 1.28+ declares a **native
sidecar**. Drop it and the sidecar degrades into an ordinary init container the
workload waits to *complete* — a long-running log shipper then hangs the Job
forever.

**Two more error.txt corrections.** It blames a bad default at `_pod-spec.tpl`
line 244 (`containerRestartPolicy | default "OnFailure"`). No such line exists;
the chart never mentions the field, and line 234's `job.restartPolicy | default
"OnFailure"` is the *pod-level* field, which works. Nothing replaced `Always` —
nothing rendered anything.

**What I changed.** `handle_main_container` / `handle_cronjob` / `handle_job`
capture it; `_pod-spec.tpl` renders it in both `mainContainer` and `jobPodSpec`;
values + schema declare it. The key is `containerRestartPolicy` because
`.Values.restartPolicy` is already the pod's.

---

## 25. Service_AppProtocol_Lost — P2

**Defect.** `spec.ports[*].appProtocol` was neither captured nor rendered. A mesh
reads it to pick an L7 handler; `protocol` is L4 and says nothing about it, so a
port that declared `grpc` came back as opaque TCP.

**error.txt names one template. There are two.** The primary Service renders
through `service.yaml`; every additional Service renders through `services.yaml`,
a separate copy of the same port loop. Fixing one leaves the other broken — the
Service-template version of the CronJob/Job "there is always a second call site"
pattern that has now caught six folders.

**Grep proof:** `appProtocol` appears **0 times** in this repo's raw manifests, so
the generated output is unchanged and the fixture is the evidence.

---

## 30 & 31. The two Service P3s — downgraded with proof, and fixed anyway

Both were flagged as likely false positives. Both are, **here** — and both sit on
top of the same real code gap.

**31, sessionAffinity.** The report's own sources disagree ("affinity broken, 29
services" vs "K8s default, safe to drop"), and the deciding number — how many
Services carry a *non-None* value — appears nowhere. So I counted:

```
300 sessionAffinity: None      <- every single one
  0 non-None
  0 sessionAffinityConfig
```

`None` IS the Kubernetes default, so the reported loss is cosmetic.

**30, externalTrafficPolicy.** `grep` returns **0 occurrences**, and the report's
"Why" is backwards: `Local` preserves the client source IP, `Cluster` does not —
and `Cluster` is the default, so losing it changes nothing.

**Neither downgrade is a one-time claim.** Both counts are now tests: the moment a
non-default value lands in the raw manifests the suite fails and the severity gets
looked at again.

**The real gap.** `build_service_entry` captures both fields and `service.yaml`
renders both — but `templates/services.yaml` had neither block, so **every Service
beyond the primary one silently lost them**. A second sticky Service dropped its
affinity; a second LoadBalancer dropped `Local`. Unexercised by this repo's data,
real in the code, fixed and tested with the values that would actually matter.

---

## 21–24. The External Secrets family — one whitelist behind four folders

**One fix closes three of them**, and half the reported detail turns out to be
wrong. The audit, claim by claim, is in each folder's STATUS §1.

**The root cause is a whitelist.** `build_externalsecret_entry` copied five spec
keys, `build_secretstore_entry` four, `build_clustersecretstore_entry` four —
and everything else was dropped in silence. Neither read `metadata.labels`. So
`deliverySpec` and `managementPolicy` did not fail for their own reasons; they
failed because nobody had added them to a list, and the next field ESO ships
would fail the same way with nothing in the fidelity report naming it. The fix
inverts the default: carry every spec key, drop only what there is a positive
reason to drop.

**Two claims are refuted, and one of them would have caused an outage.**

- `spec.target.template.labels` and `.annotations` **already round-tripped** —
  `target` was captured whole and rendered with `toYaml`. Two guard tests pass
  before *and* after the fix, which is what makes them evidence.
- `metadata.ownerReferences` is dropped **on purpose**, and the report has the
  consequence backwards. An ownerReference is a `uid`; a uid from the source
  cluster does not exist in the target, so the object names an owner that is
  gone and the garbage collector **deletes it**, usually within seconds. Acting
  on the report here would have destroyed objects on first sync.
- Folder 24's `conditions` claim: the converter captured it and the template
  rendered it, before this campaign started.
- Folder 23's mechanism is wrong in **all three clauses** — the converter never
  captured the field, `subtract_defaults` treats `"Default"` as an ordinary
  string, and the chart contains no `Create` default anywhere. Nothing turned
  `Default` into `Create`; the line was simply absent.

**`subtract_defaults` has now been accused three times — folders 11, 15 and 23 —
and been innocent all three.**

**Labels are merged, not appended.** The other templates in this chart emit two
`labels:` blocks under one key (a duplicate YAML key where the last wins, so a
values file can hijack `app.kubernetes.io/name`). These two build one merged map
with the chart's identity labels winning, matching the rule
`universal-chart.labels` already documents. The older pattern elsewhere is
recorded for `Workload_Metadata_Labels_Lost`.

**22 — the invisible character.** Zero occurrences in this repo (scanned for
U+200B/200C/200D/2060/FEFF/00AD across all three manifest trees), so it is
defensive. The report frames it as fidelity-report noise; it is worse than that —
`Se<U+200C>cretStore` is not `SecretStore` to the **API server** either, so the
ExternalSecret references a kind that does not exist and never syncs, with an
error message naming a string that looks exactly right. The sanitiser is narrow
on purpose: **identifiers only, never payload**, because a zero-width character
inside ConfigMap data can be legitimate content and corrupting a payload to tidy
up a name would be the worse bug. Every repair is warned about, because the raw
manifest is still wrong.

---

## 32–35. The P3 batch — four "likely false positives", four different answers

The brief said to be skeptical of false-positive classifications and to bring
raw-vs-rendered proof before accepting one. Each of these four was checked
against the actual data, and no two came out the same way.

| # | Report says | Proof | Answer |
|---|---|---|---|
| 33 | legacy `serviceAccount` lost, 15–24 | 0 pod specs use it; 265 use `serviceAccountName` | **False positive** — checker fixed |
| 34 | `progressDeadlineSeconds` lost, 68–119 | **0 occurrences**, not even 600 | **No impact** — real gap fixed anyway |
| 35 | `\n\n` clipped to `\n`, 2 | byte-exact through `dump_yaml` **and** `helm template` | **Refuted** |
| 32 | checker false positive | checker was already right — but asymmetric | **Confirmed, and 2 defects found behind it** |

**33 — the only one fixed where the brief says to fix a false positive: in the
checker.** Kubernetes keeps `serviceAccount` and `serviceAccountName` in sync, so
rendering only the modern key is correct — but comparing key-by-key put a
permanent `LOST spec.template.spec.serviceAccount` on every workload. That noise
is not harmless: it is the same report where folder 6's real defect lived, and a
report full of known-ignorable lines is a report people skim. The suppression is
matched on *values*, so it fires only when both keys are present with the same
string; a disagreement, a legacy key alone, or any change to
`serviceAccountName` itself is still reported. A guard test exists purely so this
can never grow into hiding folder 6's defect.

**34 — the field is absent entirely**, so the LOW rating holds for a stronger
reason than the report gave. The gap is still real, and it is now tested with
**120** — a deliberately short deadline for fast rollback detection — rather
than the harmless 600 the report cites. Omitted when unset rather than defaulted,
so nothing is invented on a raw-vs-rendered diff.

**35 — refuted, and the broader check was the valuable part.** error.txt warned
that trailing-newline loss in a PEM key or a shell script *would* break things.
This repo holds **23 PEM blobs and 10 shell scripts**, and a permanent test now
walks every real ConfigMap and Secret payload asserting byte-equality — because
the failure mode is a CHANGED field whose two values print identically, which
nothing else in the suite would catch.

**32 — the classification was right and two defects were hiding behind it.**
error.txt flagged its own template reading as self-contradictory and told the
next agent to re-read. Neither quoted condition exists: the selector is injected
**unconditionally**. From there:

- *The suppression was asymmetric.* `_fidelity_ignored_path` gated only the
  descent, so a raw Service WITH a selector was suppressed and one WITHOUT still
  produced `ADDED spec.selector` — the exact case the folder is named for.
  Fixed; real report moved **275 → 273**.
- *The behavioural risk was unguarded.* A selector-less Service is normally
  backed by hand-managed Endpoints, and the injected selector takes them over.
  This repo has 2 such Services and **zero** Endpoints objects, so nothing breaks
  here — but the chart cannot express a selector-less Service, the converter
  dropped the fact silently, and the checker ignores that path by design. Three
  layers each correct alone, adding up to a silent behaviour change.
  `handle_service` now warns; warnings **19 → 21**.

---

## 20. Workload_Metadata_Labels_Lost — P2, the report's largest defect

~634 label fields / 3,087 labels. Two independent halves, and the report is right
about both: most converter handlers never read `metadata.labels`, and nearly
every template rendered the identity helper alone. Measured before the fix:
**19 of the chart's templates** had no own-labels block, and only **5** handlers
captured labels.

**What I changed.** One helper on each side.
`universal-chart.objectLabels` builds ONE merged map with the chart's identity
labels winning — applied to all 26 templates. `raw_labels(doc)` in the converter
returns an object's own labels minus the chart-owned set, wired into every
handler. Excluding the identity labels rather than carrying them is the same rule
`podLabels` already followed: the chart computes them from `Release.Name`, and a
raw copy would either be redundant or contradict what the selector matches.

**Two corrections.**

- The report rates this *"LOW / cosmetic-metadata-only"*.
  `argocd.argoproj.io/instance` is in the same bucket, and losing it makes ArgoCD
  **stop managing the object** while it stays on the cluster — which the report's
  own MEDIUM #12 says about ExternalSecrets without connecting the two rows.
- Issue 33 says `service.yaml` has no `$spec.labels` block. It had one, as a
  **second `labels:` entry under the same key** — a duplicate YAML key. Some
  parsers reject it; Helm keeps the last, so a values file could overwrite
  `app.kubernetes.io/name` and drift metadata away from what the selector
  matches. Six templates had that shape. Worse than missing, not better.

**Two of the tests are structural, and they are what makes this stick.** One
greps every template for the old bare-helper line; the other for the duplicate
key. A new template that copy-pastes either pattern reintroduces the defect for
its own kind, and nothing else in the suite would notice.

**Why a 3,087-label defect changes nothing in the generated output:** this
showcase's manifests carry exactly one object-level label, `app: <name>`, which
is chart-owned and correctly filtered. Invisible here for the same reason it was
invisible in the fidelity report — the data does not exercise it. The fixture is
the evidence, not the count.

---

## 28. Annotations_Lost — P2, and the triage the report skipped

error.txt spotted the tell in the report's own wording: *"metadata.annotations |
135 | **Mixed**: revisions, OpenShift-specific"*. **"Mixed" means the bucket was
never triaged.** So I triaged it — every annotation key in all three manifest
trees:

```
  2405  kubectl.kubernetes.io/last-applied-configuration   (server-generated, safe)
     3  traefik.ingress.kubernetes.io/router.entrypoints   <-- NOT SAFE TO DROP
     3  traefik.ingress.kubernetes.io/router.tls           <-- NOT SAFE TO DROP
```

Zero `argocd.argoproj.io/*`, zero `prometheus.io/*`, zero `checksum/*`. **2405 of
2411 are the one key already filtered**, so the LOW rating holds.

**It holds by accident, and that is the finding.** The six that are NOT safe —
they decide which Traefik entrypoint serves the route and whether TLS is on —
all sit on **Ingress**, one of the few kinds whose handler already captured
annotations. Eight other handlers captured none, and two templates had no
annotations block at all. The same six on a NetworkPolicy or an HPA would have
vanished. **Whether an annotation survived depended on which kind carried it, and
nothing said so.**

**Two sides, and the second is the subtle one.** Eight handlers now capture
(`handle_workload` read none — that is every Deployment, StatefulSet and
DaemonSet). But six templates — deployment, daemonset, statefulset, hpa, pdb,
vpa — rendered `.Values.commonAnnotations` **only**, so a captured annotation
reached values.yaml and rendered nowhere. That is why every capture test passed
while the round-trip failed:

```
Deployment: argocd.argoproj.io/sync-wave='2' -> None
Deployment: prometheus.io/scrape='true'      -> None
HorizontalPodAutoscaler: argocd.argoproj.io/sync-wave='3' -> None
```

`deployment.kubernetes.io/revision` is now filtered as server-generated — a
rollout counter from the source cluster produces a permanent ArgoCD diff, which
trains operators to ignore drift.

**The triage is a test, not a claim.** It re-runs on every test run, so an
`argocd.argoproj.io/sync-wave` landing in the raw manifests fails the suite —
exactly when the LOW rating stops being safe.

---

## 17, 27, 29 — an unfinished analysis, an unanswered question, and the report's biggest noise source

**17 — the analysis the report abandoned mid-way.** error.txt says so
explicitly, which is why the report carries two contradictory ratings for the
same defect (LOW #19 vs CRITICAL/High in Discoveries): the deciding question was
never answered. With the target being **vanilla Kubernetes**, I ran the check it
asks for over every ServiceAccount:

```
ServiceAccounts scanned: 316
  imagePullSecrets  auto-generated pattern: 278   CUSTOM: 0
  secrets           auto-generated pattern: 556   CUSTOM: 0
```

**Both ratings are right, about different things.** 100% auto-generated, so on
this data it is cosmetic — and stronger than that: those Secrets come from
OpenShift's integrated-registry controller and **do not exist on a vanilla
cluster**, so carrying them would point 278 ServiceAccounts at objects that never
appear. Dropping them is correct, not merely tolerable. But a **custom**
credential was dropped by the same code path with nothing said, and that is the
CRITICAL rating — ImagePullBackOff on every pod using the SA. Now carried;
auto-generated ones still dropped, deliberately silently (278 warnings would bury
the conflicts file, which is the same noise problem as 29 and 33).

**27 — the question nobody answered, and a worse symptom than reported.**
error.txt records the original session asking *"converter side, chart side, or
both for defense in depth?"* and never getting a reply. Answer: **both** — the
converter cannot police hand-written values, and the template cannot fix a wrong
values file. Normalised to `value: ""` rather than dropped, because an env var
set to the empty string is how a program is told to use its own default;
removing the variable is not the same thing.

And the report undersells it. With the guard removed, `helm template` does not
render a malformed entry — it **fails**:

```
AssertionError: helm template failed:      (x3)
```

No manifest at all. A broken deploy, not a cosmetic diff.

**29 — the report's largest single noise source, and the one item in it that
isn't noise.** ~600 occurrences, about a third of all Medium/Low LOST lines.
`resources: {}` and an absent `resources:` are byte-identical to Kubernetes, so
the checker now skips them — the disposition error.txt recommends and the brief
directs for a genuine false positive.

**That noise was never free.** It is the same report where folder 1's
irreversible PVC-retention loss and folder 6's never-starting pods were sitting;
600 known-ignorable lines is what makes a report get skimmed.

error.txt flags `immutable: false` on ConfigMaps as possibly-not-noise and asks
for it to be checked first. Checked: **no ConfigMap in this repo carries the key
at all**, and the suppression is scoped to `{}` and `[]` so a `true` could never
have been hidden anyway. `false`, `0` and `""` stay reported — that distinction
is the whole of folder 15. And the Kubernetes-populated defaults the report
groups in here (`dnsPolicy`, `terminationGracePeriodSeconds`, `schedulerName`)
are **not** suppressed, because they are not lost: `handle_workload` captures all
three and they round-trip today.

---

## 26. Container_Ports_Reordered_And_Auto_Named — P2, and an assumption of mine that was wrong

One report row, two symptoms, and error.txt calls the split correctly:
*"Port ordering is generally cosmetic to Kubernetes, but auto-naming is NOT."*

**Auto-naming — real, fixed.** `ports_list_to_map` keyed every port by
`name or port-<number>`, and `mapToList` rendered that key straight back as
`name:`. A port the raw manifest left **unnamed** came out named `port-9000`.
Kubernetes resolves a Service `targetPort:` and a probe `port:` **by name**, so
the object acquired a name nothing referenced and that never existed. The
synthetic key stays as bookkeeping — a values map needs one — but it is now
marked `unnamed: true` and the template drops both the marker and the `name:`
line.

**Ordering — cosmetic, and I was wrong about why it didn't matter.** I assumed
the fidelity checker already paired ports by name and wrote that as a test. **The
test failed.** `_by_name` bails out to positional comparison as soon as *any*
entry in a list is unnamed — so one unnamed port dropped the whole list to
positional, and the chart's sorted-key reorder was reported as a single CHANGED
covering every port at once:

```
AssertionError: reordering alone was reported as a difference
```

That is very likely what the report's "9 occurrences" actually are: **not nine
broken ports, one noisy comparison repeated.** It also hid detail — a genuinely
changed `containerPort` read as "the whole list differs" instead of being named.
`_item_name` now falls back to `containerPort/protocol` for an unnamed port, so
a reorder is invisible and a real change is pinpointed.

Preserving declaration order itself would mean turning the ports map into a
list — a values-schema change for every consumer of this chart, to fix something
Kubernetes attaches no meaning to. Deliberately not done.

---

## 18, 19, 22 — the naming/collision family, and one check that runs forever

**18 — the decision was to keep the rename, and the report argues that case
better than I could:** *"The converter doesn't auto-share because it can't know
if the original cluster actually had a shared Secret or two separate ones that
happened to have identical content."* The asymmetry settles it — auto-sharing
two objects that were genuinely independent **couples two releases forever**, and
nothing in the rendered output would show it. A duplicate object plus a warning
is the reversible mistake.

What was missing was the operator's next step. The warning noted
`(identical content)` as a bare parenthetical and stopped. It now names the
action — move the object into the namespace's `shared.yaml` — and says why the
converter will not do it for them. Differing content gets deliberately different
advice, because there is no decision to make there; suggesting `shared.yaml` for
two objects that differ would be actively wrong, and a test asserts the word does
not appear.

**19 — verified, and my first version of the check was wrong.** error.txt asks
for "`helm template` per release, confirm every reference resolves". I did that
and six failed — `shared-ca-bundle`, `platform-ops-alert-thresholds` and friends.
None were broken: they are declared in `shared.yaml`, and `reference_shared` is
exactly the mechanism that lets a microservice point at an object the *shared*
release creates. A ConfigMap is namespace-scoped, so per-release resolution asks
a question the cluster never asks.

Resolved per **namespace** instead, the answer is **0 dangling references** across
all 24 releases of the real showcase, and 0 in the three-way `zookeeper-config`
collision fixture — which is the residual risk error.txt specifically names
(*"that ConfigMap may be rendered by the wrong release or not at all"*). This is
now a standing sweep like folders 5 and 7: it runs against the real manifests on
every test run.

**22 — the mechanism does not exist.** There is no `-2` suffix convention in this
converter; `claim()` renames to `<owner>-<name>`, and ten colliding claimants
produce ten owner-prefixed names, never a counter. The only numeric-suffix code
here is one **I** added for folder 13, which postdates the report.

The concern behind it is real and has a layer the report misses: **an
ExternalSecret is a factory.** The Secret it creates takes its name from
`spec.target.name`, which defaults to the ExternalSecret's own — so renaming the
ExternalSecret silently renames the Secret, and every `secretKeyRef` pointing at
it stops resolving, including from releases and manifests outside this
conversion. The broken reference is one level removed from the rename, which
makes it harder to spot than a renamed ConfigMap. Setting `target.name`
explicitly pins the produced Secret through a rename; both behaviours are now
tests.

---

## Afterword — what a clean report is worth

The fidelity report ends this campaign at **0 lost / 0 changed / 0 missing /
0 extra**. It also *started* it that way for most folders: twenty-eight of the
thirty-six had a `0 before, 0 after` count, because this repo's manifests do not
contain the shapes that trigger the defects — no `subPath`, no image digests, no
unnamed ports, no empty maps, no custom pull secrets, no name collisions.

**So the count was never the evidence.** The evidence in every folder is the RED
test output against a fixture derived from a real manifest, and in eight folders
a mutation test that reverted the fix and showed the suite going red before
restoring the file byte-identically.

Three things this run kept re-learning, worth carrying forward:

1. **Assume a second call site until you have grepped for it.** Five of the first
   twelve folders had an unmentioned CronJob/Job path; folder 25 found the same
   shape in the Service templates; folder 26 found it in the checker.
2. **`subtract_defaults` was accused three times and was innocent three times**
   (folders 11, 15, 23). A reported mechanism is a hypothesis, not a finding.
3. **Acting on a report without checking it can do damage.** Issue 18 asked for
   `ownerReferences` to be carried; doing so would have made the target cluster's
   garbage collector delete the objects on first sync.

Four defects turned up that no folder covers. Two are fixed
(`_FOUND_Global_Defaults_Subtracted_But_Never_Written`, both P0). Two are
recorded and deliberately not fixed, because fixing them means design decisions
outside any folder's scope — see
[`_MISSING_ISSUES_ANALYSIS.md`](_MISSING_ISSUES_ANALYSIS.md).

---

## Cross-cutting notes

**error.txt hints are directionally right and specifically unreliable.** Line
numbers drift by 2–6 lines (transcribed from screenshots). More importantly,
several files understate the defect:

| Folder | error.txt says | Actually |
|---|---|---|
| 3 | fix is two-sided | three-sided; one call site named, three exist |
| 5 | main container only | Jobs/CronJobs renamed by a second mechanism |
| 6 | captured at line 1079, stored at release scope | never captured at all |
| 7 | `build_route_entry` sets the wrong target | target was correct; `handle_route` discarded it |
| 10 | converter + template both need fixing | template already handled digests; `jobPodSpec` did not |
| 11 | `subtract_defaults` strips `Always`; helper hides it | neither is true — the field was never captured for jobs |
| 12 | `podSecurityContext` captured per-container at 852 | captured pod-level at 836, correctly; jobs were the gap |
| 13 | value corrupted downstream, likely `subtract_defaults` | nothing was corrupted; two objects collapsed onto one map key |
| 14 | the values YAML has **neither** command | it has both — in `values/<ns>/`, not `base/` |
| 15 | `subtract_defaults` treats 0 as absent | it compares values, not truthiness; the chart's `\| default` is the whole bug |
| 16 | chart defaults `containerRestartPolicy` to `OnFailure` | the chart never mentions the field; it was absent from both sides |
| 25 | fix `service.yaml` | there are two Service templates; `services.yaml` needed it too |
| 30 | losing `Cluster` breaks source-IP preservation | backwards — `Local` preserves it, `Cluster` is the default |
| 21 | `target.template.labels/annotations` lost | both already round-tripped |
| 21 | losing `ownerReferences` breaks GC | backwards — **keeping** it makes the target cluster delete the object |
| 23 | `subtract_defaults` strips `"Default"`; chart defaults to `Create` | neither is true; no `Create` exists in the chart |
| 24 | template doesn't render `conditions` | it always did |
| 32 | template has `if not $spec.selector` | neither quoted condition exists; it is unconditional |
| 35 | `\\n\\n` becomes `\\n` | nothing is clipped — byte-exact through dump_yaml AND helm |
| 20 | `service.yaml` has no `$spec.labels` block | it had one — as a DUPLICATE YAML key, which is worse |
| 20 | LOW / "cosmetic-metadata-only" | `argocd.argoproj.io/instance` is in the same bucket |
| 28 | "Mixed: revisions, OpenShift-specific" | "Mixed" = never triaged; 2405 of 2411 were one already-filtered key |
| 17 | two contradictory ratings, LOW vs CRITICAL | both right — cosmetic on this data, CRITICAL for the case that never occurs here |
| 27 | "a malformed or empty env entry" | `helm template` **fails outright**; no manifest at all |
| 26 | 9 ports reordered + auto-named | one noisy comparison repeated, not nine broken ports |
| 22 | "the naming convention adds -2 suffix" | no such convention; renames are `<owner>-<name>` |

A pattern worth naming: **five of the first twelve folders turned out to have a
second, unmentioned call site in the CronJob/Job path**, folder 13's defect lives
in that path outright, and folder 25 found the same shape in the Service templates
(`service.yaml` and `services.yaml` are two copies of one port loop). **Assume a
second call site until you have grepped for it.** If a defect touches a container,
an image, a ServiceAccount or a security setting, assume `jobPodSpec` and
`handle_cronjob`/`handle_job` need checking too.

Every hint was verified before being trusted, and every correction is recorded in
the relevant `STATUS.md`.

**Most defects are not exercised by this repo's data.** The showcase fidelity
report read 0 before and after for folders 1–4 and 6–9, because the raw manifests
here don't contain the triggering shapes (no `subPath`, no multi-container pods,
no `-headless` Services, no `persistentVolumeClaimRetentionPolicy`). That is why
every folder has a fixture derived from a real manifest, and why RED test output
— not the fidelity count — is the evidence a defect was real. Folder 5 is the
exception: 12 of 19 real workloads were affected.

**Raw manifests under `namespaces*/` were never modified.** Fixtures are copies
under `ERRORS_ANALYSIS/`. Generated values were never hand-edited — the generator
was fixed and the tree regenerated.

**A trap for later folders.** Any script that renders releases must special-case
the namespace's `shared` release: it gets a dedicated Application that skips the
`defaults.yaml` layers. Getting this wrong manufactures phantom "extra" objects —
it briefly did in my folder-7 sweep. Folders 8/9 avoid it by using the
converter's own `--verify-fidelity` rather than re-implementing the layering.

---

## What could not be verified — no cluster, on purpose

Stated per folder in each `STATUS.md`. The recurring limits:

- **Runtime behaviour.** That Kubernetes retains a PVC, that pod DNS resolves,
  that a container starts and finds its files, that `kubectl logs -c` works, that
  Elasticsearch forms a cluster. Rendered manifests are verified; behaviour is not.
- **API-server acceptance.** `kubectl apply --dry-run=client` still fetches the
  OpenAPI schema from a live API server, so it cannot run offline. The
  `render_check.ps1` scripts attempt it and report SKIP, never a pass.
- **The specific workloads error.txt counts** (`jaeger-es-master`, `zookeeper`,
  `amdi-client`, `data-sender`, `interconn-kafka-*`, …). None of those manifests
  are in this repo. The mechanisms are fixed and covered by tests; those objects
  are unverified.
- **Migration side effects.** Folders 5 and 7 change rendered object and container
  names. Objects already deployed under the old names are not renamed — with
  `prune: false` on the generated ApplicationSets they will linger. Anything
  keyed on the old names (dashboards, log filters, alerts) changes too. That is
  intended, but it is a rollout concern no rendered manifest can show.

---

## Remaining

Folders 17–24, 26–29 and 32–35, in the blast-radius order given in the brief. Three
stop-and-ask decisions are already made and recorded in
`STATUS.md` files / project memory:

- **17 ServiceAccount secrets** — target is **vanilla Kubernetes**; all 834
  entries match the OpenShift auto-generated pattern, so dropping them is
  correct. Only custom pull secrets get carried.
- **18 duplicate ConfigMaps** — keep the rename, make the identical-content
  warning actionable. No auto-sharing.
- **27 empty-map env var** — fix **both** sides; normalize `{}` to `{value: ""}`,
  never drop.

After folder 35: re-run the fidelity report and identify anything still failing
with no folder — the two missing issues (28/29). One candidate is already found
and fixed (see above).
