# Everything that changed, and why each defect existed in the first place

One file. For each defect: **what broke**, **why the code was written that way**,
and **what I changed**.

The 36 reported defects are not 36 independent bugs. They come from about
**eight design decisions**, each of which was reasonable in isolation and each of
which produced a family of failures. Reading this by root cause is the only way
the shape of it makes sense, so that is how it is organised. The per-folder index
is at the end.

Companions: [`README.md`](README.md) is the campaign log with RED/GREEN output
per folder; [`README-cronjob.md`](README-cronjob.md) is the CronJob/Job view;
[`_MISSING_ISSUES_ANALYSIS.md`](_MISSING_ISSUES_ANALYSIS.md) answers the brief's
final question.

Final state: chart's own **234 tests** pass, `helm lint` clean, fidelity
**0 lost / 0 changed / 0 missing / 0 extra**.

---

## The one thing to understand first

This converter turns **raw Kubernetes manifests** into **Helm values** for a
single generic chart ("Universal-chart"). Every defect below is a place where
that translation is lossy, and almost all of them come from the same underlying
tension:

> **A Kubernetes manifest is a list of objects with arbitrary fields. A Helm
> values file is a map with a fixed schema. Something has to give.**

The converter resolves that tension by making choices — key this list by name,
copy these fields, name that object after the release. Each choice is defensible.
Each choice also has an edge where it silently loses data, and the report is a
catalogue of those edges.

---

## Root cause 1 — Kubernetes lists became values maps, and names are not unique

**The decision.** Helm values are much nicer to override when they are maps
(`env.LOG_LEVEL.value`) than lists (`env[3].value`), so the converter indexes
lists by each entry's `name`.

**Why it breaks.** Kubernetes only guarantees `name` uniqueness in *some* of
those lists. Where it doesn't, `map[name] = entry` silently overwrites, and a
`dict` assignment has no way to complain.

| Folder | What broke | Why the name repeats |
|---|---|---|
| **3** VolumeMount_SubPath | 4 volumeMounts collapsed to 2 | Mounting one volume several times with different `subPath`s is the *standard* way to project individual ConfigMap keys onto file paths — every mount carries the same `name` |
| **13** CronJob_Schedule | Two CronJobs became one | `short_subresource_name` strips the `<microservice>-` prefix, so `db-migrate-cleanup` and `cleanup` reduce to the same key |
| **14** CronJob_Merge | Command and env vanished | Same line as 13 — the whole entry was replaced, not just the schedule |
| **26** Container_Ports | Unnamed ports got invented names | A values map needs a key; an unnamed port has no name to use, so one was synthesised |
| **4** Sidecar_Collision | *(refuted)* | Container names ARE unique within a pod, so this one does not reproduce |

### What I changed

- **`named_list_to_map`** now uses a composite key `<name>:<subPath>` when a name
  repeats, and carries the real name in an explicit `name` field so the template
  can render it. `mapToList` and `validateVolumeMounts` both read that field —
  the report called this a two-sided fix; it is three-sided, and a two-sided fix
  would have made the chart refuse to render at all.
- **`claim_subresource_key`** replaces the bare `cronjobs[key] = entry`. Both
  CronJobs survive; which one keeps the short key is decided by **which raw name
  the chart can reproduce**, not by document order, so reordering the manifests
  cannot move a schedule from one object to another. The loser is renamed and
  warned about.
- **`ports_list_to_map`** marks a synthesised key `unnamed: true`, and
  `mapToList` drops both the marker and the `name:` line. The map still has its
  key; the manifest no longer gets a name it never had.

**Why the naming matters more than it sounds:** Kubernetes resolves a Service
`targetPort:` and a probe `port:` **by name**. A port that acquires the name
`port-9000` answers to something nothing referenced.

---

## Root cause 2 — the CronJob/Job path is a second copy of the workload path

**The decision.** A Deployment's pod spec and a CronJob's pod spec are built by
different code (`handle_workload` + `mainContainer` vs
`handle_cronjob`/`handle_job` + `jobPodSpec`), because a CronJob nests its pod
two levels deeper and needs schedule/backoff handling a Deployment does not.

**Why it breaks.** Every field added to the workload path had to be added to the
job path too, by hand, and repeatedly wasn't. **Five of the first twelve folders
had an unmentioned second call site here**, and it became the single most
reliable prediction in the campaign.

| Folder | Field | Deployment | CronJob/Job |
|---|---|---|---|
| **5** Main_Container_Name | `containers[0].name` | worked | named after the release |
| **6** ServiceAccountName | per-job `serviceAccountName` | worked | never captured |
| **10** Image_Digest | `@sha256:` digest | worked | `jobPodSpec` builds its image inline and had no digest branch |
| **11** ImagePullPolicy | per-job `pullPolicy` | worked | never captured |
| **12** Pod_SecurityContext | pod + container `securityContext` | worked | pod-level overridden; container-level **not rendered at all** |
| **16** ContainerRestartPolicy | container-level `restartPolicy` | missing | missing |

### What I changed

Captured each field in `handle_cronjob`/`handle_job`, rendered it in `jobPodSpec`
with `| default` back to the release-level value so a job that declares nothing
behaves exactly as before. Folder 12 also added a container-`securityContext`
block that did not exist — a job's `readOnlyRootFilesystem` and
`capabilities.drop` were being dropped outright, which is silent
un-hardening rather than the override the report described.

**Folder 16 is the one where both paths were missing it**, and it is worth
knowing why it matters: `restartPolicy: Always` on an initContainer is how
Kubernetes 1.28+ declares a **native sidecar**. Lose it and the sidecar degrades
into an ordinary init container the Job waits to *complete* — so a long-running
log shipper hangs the Job forever.

---

## Root cause 3 — whitelists: "copy these fields" instead of "copy everything"

**The decision.** Each handler lists the fields it knows about and copies those.
Explicit, readable, and it keeps chart-irrelevant junk out of values.

**Why it breaks.** A field nobody listed is dropped **in silence**. There is no
error, no warning, and nothing in the fidelity report naming which field — it
only appears as a count. And the list has to grow every time Kubernetes or an
operator adds anything.

| Folder | What was dropped | Size of the list |
|---|---|---|
| **20** Workload_Metadata_Labels | `metadata.labels` on nearly everything | **5** of ~25 handlers captured labels; **19** of the chart's templates had no block to render them |
| **28** Annotations_Lost | `metadata.annotations` | 8 handlers captured none; 2 templates had no block; 6 more rendered only `commonAnnotations` |
| **21/23/24** ExternalSecret + SecretStore | `deliverySpec`, `managementPolicy`, labels | 5 spec keys copied for an ExternalSecret, 4 for a SecretStore |
| **17** ServiceAccount pull secrets | `imagePullSecrets` | not in the list at all |
| **25** Service_AppProtocol | `ports[].appProtocol` | not in the list |
| **30/31** Service policies | `sessionAffinity`, `externalTrafficPolicy` | captured, but `services.yaml` had no block |
| **34** ProgressDeadlineSeconds | `spec.progressDeadlineSeconds` | not in the list |

### What I changed

- **ESO handlers stopped whitelisting entirely.** `_eso_spec()` carries *every*
  spec key. `deliverySpec` and `managementPolicy` did not fail for their own
  reasons — they failed because nobody had added them to a list, and the next
  field the operator ships would have failed identically.
- **`raw_labels(doc)`** — one helper, used by every handler, returning an
  object's own labels minus the ones the chart derives. **`universal-chart.objectLabels`**
  — one helper, used by all 26 templates, merging them under the chart's identity
  labels so a values file can add but never hijack `app.kubernetes.io/name`.
- **Annotations** captured in the 8 handlers that read none, rendered in the 2
  templates that had no block, and merged with `commonAnnotations` in the 6 that
  rendered only the release-wide ones.
- **`appProtocol`, `sessionAffinity`, `externalTrafficPolicy`, `progressDeadlineSeconds`,
  custom `imagePullSecrets`** each captured and rendered.

**Two structural tests exist specifically to stop this recurring**: one greps
every template for the old bare-labels line, one for a missing annotations block.
A new template that copy-pastes the old pattern reintroduces the defect for its
own kind, and nothing else would notice.

---

## Root cause 4 — Go template `default` treats 0, "" and false as "unset"

**The decision.** `{{ $spec.backoffLimit | default 3 }}` is the idiomatic Helm
way to supply a fallback.

**Why it breaks.** Go's `default` fires on any *falsy* value, not just a missing
one. `0` is falsy. So `backoffLimit: 0` — "never retry a failed Job" — rendered
as 3, and `successfulJobsHistoryLimit: 0` — "keep no completed pods" — rendered
as 3 and leaked pods.

| Folder | Symptom | Real cause |
|---|---|---|
| **15** Subtract_Defaults_Removes_Explicit_Zero | `0` → `3` | four numeric `\| default` filters in `cronjob.yaml` / `job.yaml` |
| **27** Empty_Map_Env_Var | `helm template` **fails** | an env entry with an empty body rendered `- name: X` followed by a bare `{}` |

### What I changed

All four numeric `| default` filters became `hasKey` guards. The audit came back
clean — those were the *only* numeric `| default`s in the chart, so no other
field can lose an explicit zero this way.

`backoffLimit`'s fallback also changed **3 → 6**, because Kubernetes' own default
is 6 and the chart was inventing a number nobody chose: a Job with no
`backoffLimit` was retrying half as often as the same Job applied with `kubectl`.

For empty env vars, both sides: the converter normalises to `value: ""`, and
`mapToList` guards independently so a hand-written values file can't produce
broken YAML either. **Normalised, never dropped** — an env var explicitly set to
the empty string is how a program is told to use its own default, so removing the
variable is not the same thing.

**Worth flagging:** the report blamed `subtract_defaults` for this, and for
imagePullPolicy (folder 11) and ExternalSecret managementPolicy (folder 23).
**It was innocent all three times.** It compares values, not truthiness.

---

## Root cause 5 — "subtract because a layer supplies it" without checking the layer applies

**The decision.** Values common to every release are hoisted into a shared
`defaults.yaml` and subtracted from each release's own file. Helm layers them
back at render time. Less duplication, one place to change a shared value.

**Why it breaks.** The subtraction and the write are **one operation**: a value
may only be removed because something underneath supplies it again. Break that
pairing and the value is gone from everywhere.

Two instances, both P0, neither with an `error.txt` — I found both:

1. **The tree-root `defaults.yaml` was written only in cluster-scoped mode, but
   subtracted in both.** Namespace-scoped is the **default**. So by default,
   every value identical across all microservices in a run was removed from the
   output and written nowhere. It hid because the showcase's 20 microservices are
   too heterogeneous for the common set to hold anything — **two similar
   Deployments is enough to trigger it**.
2. **`shared` releases were subtracted against a layer they never apply.** The
   first fix guarded on the file being *written*; `shared` gets a dedicated
   ArgoCD Application whose valueFiles chain is deliberately just
   `base/` + `values/`, with **no defaults layers at all**. A config-only shared
   release lost `workload.type: none` and `service.enabled: false` and then
   rendered a **Deployment, a Service and a ServiceAccount it never declared**.

### What I changed

Subtraction now happens only where the layer is genuinely applied, and `shared`
is never subtracted in either mode. The rule is written into the code:

> Every "subtract X from a release" needs the matching question: **does that
> release apply the layer X went into?** Asked twice, answered wrong twice.

---

## Root cause 6 — the chart names objects after the release, not after the manifest

**The decision.** Helm charts conventionally name everything
`<release>-<something>`, so two installs of one chart can coexist. Sensible for a
chart you install; wrong for a chart whose job is to **reproduce manifests that
already exist**.

**Why it breaks.** Everything that referenced the old name — DNS, another
release's config, a probe, a Route — now points at nothing.

| Folder | What got renamed | Consequence |
|---|---|---|
| **2** StatefulSet_ServiceName | governing Service → release fullname | only a *headless* Service publishes per-pod DNS; clustered apps can't form clusters |
| **5** Main_Container_Name | `containers[0].name` → release name | `kubectl logs -c`, log-pipeline selectors and the `container` label on metrics all address it by name |
| **7** Route_And_Service_Names | 33+ Services/Routes → fullname | in-cluster DNS and every cross-reference break |
| **8/9** Missing/Extra objects | — | the *same objects counted twice*: 3 "missing" and 3 "extra" were one rename each |
| **18/19/22** collision renames | `<owner>-<name>` on a name clash | internally consistent; externally wrong |

### What I changed

- **`serviceName` captured unconditionally** (not "only when it differs" — this
  entire defect exists because an unstated "the names will match" assumption
  turned out false; re-encoding it inside its own fix is how it comes back).
- **`containerName`, `service.name`, `route.name`** captured and rendered, plus a
  new `universal-chart.primaryServiceName` helper so Routes and Ingresses point
  at the Service's *real* name.
- **Folders 8/9 needed no code** — folder 7's fix cleared both. I proved they
  were the same objects by rebuilding the pre-fix tree from `git show HEAD:` and
  pairing them one by one.
- **Collision renames kept** (your decision), but the warning now names the
  action: move the object into `shared.yaml` if the copies are byte-identical.
  Differing content gets deliberately *different* advice, because there is no
  decision to make there.

**Why not auto-share identical ConfigMaps?** The report argues it best:
the manifests cannot say whether the source cluster had one shared object or two
that coincidentally matched. Auto-sharing two genuinely independent objects
**couples two releases forever**, invisibly. A duplicate plus a warning is the
reversible mistake.

---

## Root cause 7 — the fidelity checker's own defects

**The decision.** The checker compares raw against rendered field by field, and
suppresses paths the chart legitimately derives itself.

**Why it breaks.** Suppression rules are easy to get subtly wrong, and **noise is
not free** — the same report where folder 1's irreversible data loss and folder
6's never-starting pods were sitting also carried ~600 known-ignorable lines. A
report full of noise is a report people skim.

| Folder | Checker defect | Effect |
|---|---|---|
| **29** Empty_K8s_Default_Maps | `resources: {}` vs absent reported as a loss | ~600 lines, a third of all LOST entries |
| **33** Legacy_ServiceAccount | deprecated `serviceAccount` alias reported | a permanent LOST on every workload |
| **32** Service_Selector_Injected | suppression applied on the way **down** but not to ADDED | a raw Service *with* a selector was suppressed; one *without* still reported |
| **26** Container_Ports | one unnamed port dropped the whole list to positional | the chart's sorted-key reorder read as "every port changed" — very likely the report's "9 occurrences" |

### What I changed

Each suppression is **narrow and its limits are tested**:

- `{}` and `[]` only — a raw `false`, `0` or `""` is a deliberate value and stays
  reported (that distinction is the whole of folder 15).
- The legacy SA alias only when the modern key is present **with the same value**
  — a disagreement, or the legacy key alone, is still reported. A guard test
  exists purely so this can never grow into hiding folder 6's defect.
- ADDED now consults the same ignore rule as the descent. Real report:
  **275 → 273**.
- `_item_name` falls back to `containerPort/protocol` for an unnamed port, so a
  reorder is invisible and a genuinely changed port is *named precisely* instead
  of "the whole list differs".

---

## Root cause 8 — the source data itself was wrong

Not conversion defects at all. The converter is just the only place allowed to
repair them, because the raw manifests are the source of truth.

| Folder | What | Verdict |
|---|---|---|
| **22** Zero_Width_Unicode | U+200C inside `Se<ZWNJ>cretStore` | **0 occurrences here** — fixed defensively |
| **35** Grafana_Trailing_Newline | `\n\n` clipped to `\n` | **Refuted** — byte-exact through `dump_yaml` *and* `helm template` |

The zero-width sanitiser is deliberately narrow: **identifiers only, never
payload.** A zero-width character inside ConfigMap data can be legitimate content
— a fixture, a template, a translation string — and corrupting a payload to tidy
up a name would be the worse bug. Every repair is warned about, because the raw
manifest is still wrong and somebody should fix it at source.

The report frames the invisible character as fidelity-report noise. It is worse
than that: `Se<U+200C>cretStore` is not `SecretStore` to the **API server**
either, so the ExternalSecret references a kind that does not exist and never
syncs, with an error message naming a string that looks exactly right.

---

## Where the report was wrong

Fourteen of the thirty-six folders had a materially wrong diagnosis. The symptom
was usually real; the stated mechanism often wasn't.

| Folder | Report said | Actually |
|---|---|---|
| 3 | fix is two-sided | three-sided — a two-sided fix makes the chart refuse to render |
| 6 | captured at line 1079 | never captured at all |
| 7 | `build_route_entry` sets the wrong target | target was right; `handle_route` discarded it |
| 10 | converter + template both need fixing | template handled digests; `jobPodSpec` did not |
| 11, 15, 23 | `subtract_defaults` is the cause | innocent all three times |
| 13 | the schedule value is corrupted | nothing corrupted — two objects collapsed onto one key |
| 14 | "the values YAML has neither" command | it has both — in `values/<ns>/`, not `base/` |
| 16 | chart defaults it to `OnFailure` | the chart never mentions the field |
| 20 | `service.yaml` has no labels block | it had one — as a **duplicate YAML key**, which is worse |
| 21 | losing `ownerReferences` breaks GC | **backwards** — keeping it makes the target cluster delete the object |
| 22 | naming convention adds `-2` | no such convention; renames are `<owner>-<name>` |
| 24 | template doesn't render `conditions` | it always did |
| 30 | losing `Cluster` breaks source-IP preservation | backwards — `Local` preserves it; `Cluster` is the default |
| 35 | `\n\n` becomes `\n` | nothing is clipped |

**One of these would have caused an outage if implemented as written.** Issue 18
asked for `ownerReferences` to be carried through. An ownerReference is a `uid`,
and a uid from the *source* cluster does not exist in the target — so the object
names an owner that is gone and the garbage collector **deletes it**, usually
within seconds of the first sync.

---

## Where the data disagreed with the severity

The brief asked for raw-vs-rendered proof before accepting any "false positive".
Every downgrade below is a grep over the actual manifests, and **every one is now
a test** that fails the moment the data changes:

| Folder | Report | Measured |
|---|---|---|
| 31 sessionAffinity | "29 services broken" | **300 of 300 are `None`** — the Kubernetes default |
| 30 externalTrafficPolicy | 2 occurrences | **0** — not in this repo at all |
| 33 legacy `serviceAccount` | 15–24 | **0** legacy; 265 use the modern key |
| 34 progressDeadlineSeconds | 68–119 | **0** — not even the harmless 600 |
| 28 annotations | 135, "Mixed" | **2405 of 2411** are one already-filtered key |
| 17 SA pull secrets | 24, CRITICAL vs LOW | **278 auto-generated, 0 custom** |
| 29 empty maps | ~600 | **0** `resources: {}` here |

Folder 17 deserves a note: the report carried **two contradictory ratings** for
one defect because the deciding question was never answered. Both turn out to be
right about different things. With a vanilla-Kubernetes target, 100% of the pull
secrets are OpenShift-generated — so dropping them is not merely tolerable but
**required**, since those Secrets don't exist on the target and the reference
would point at nothing. But a *custom* credential was dropped by the same code
path with nothing said, and that is the CRITICAL rating: ImagePullBackOff on
every pod using the SA.

---

## Why almost none of this showed up in the fidelity report

Twenty-eight of the thirty-six folders read **0 lost / 0 changed before and
after** the fix. This repo's manifests simply do not contain the triggering
shapes — no `subPath`, no image digests, no unnamed ports, no empty maps, no
custom pull secrets, no name collisions, no non-default sessionAffinity.

**So the count was never the evidence.** The evidence in every folder is:

1. a **fixture** derived from a real manifest under `namespaces*/`,
2. **RED test output** showing the defect reproduce,
3. **GREEN** after the fix,
4. and in eight folders, a **mutation test** that reverted the fix, showed the
   suite go red, and restored the file byte-identically (sha256 verified).

Folder 20 is the clearest case: a defect the report counts at **3,087 labels**
changed *nothing* in the generated output, because the showcase's only
object-level label is `app:`, which the chart owns.

---

## Three habits this run kept re-learning

1. **Assume a second call site until you have grepped for it.** Five of the first
   twelve folders had an unmentioned CronJob/Job path. Folder 25 found the same
   shape in the Service templates (`service.yaml` and `services.yaml` are two
   copies of one port loop). Folder 26 found it in the checker.
2. **A reported mechanism is a hypothesis, not a finding.** `subtract_defaults`
   was accused three times and was innocent three times.
3. **Test your own assumptions, not just the code's.** In folder 26 I asserted
   the checker already paired ports by name; the test failed and turned up a real
   checker bug. In folder 19 my first reference-integrity check flagged six
   working references because I resolved per release instead of per namespace.

---

## Folder index

| # | Folder | Root cause | Outcome |
|---|---|---|---|
| 1 | StatefulSet_PVC_Retention_Policy_Lost | whitelist | Fixed — converter + template + schema |
| 2 | StatefulSet_ServiceName_Hardcoded_To_Fullname | release naming | Fixed — captured unconditionally |
| 3 | VolumeMount_SubPath_Lost | list→map | Fixed — three-sided |
| 4 | Sidecar_Container_Name_Key_Collision | list→map | **Refuted** with a mutation test |
| 5 | Main_Container_Name_Forced_To_Release_Name | release naming | Fixed — 12 of 19 workloads affected |
| 6 | ServiceAccountName_Overridden_By_Release_Name | job path | Fixed |
| 7 | Route_And_Service_Names_Replaced_By_Fullname | release naming | Fixed — new `primaryServiceName` |
| 8 | Missing_Objects_Not_Rendered | release naming | Already fixed by 7 — 3 → 0 |
| 9 | Extra_Objects_Rendered_With_No_Raw_Source | release naming | Already fixed by 7 — 3 → 0 |
| 10 | Image_Digest_Replaced_With_Mutable_Tag | job path | Fixed — `split_image` returns a digest |
| 11 | ImagePullPolicy_Always_Downgraded | job path | Fixed |
| 12 | Pod_SecurityContext_Overridden | job path | Fixed — closes root-cause row C |
| 13 | CronJob_Schedule_Value_Corruption | list→map | Fixed — two objects, one key |
| 14 | CronJob_Merge_Overwrites_Command_And_Env | list→map | Already fixed by 13 |
| 15 | Subtract_Defaults_Removes_Explicit_Zero | `\| default` | Fixed — in the chart |
| 16 | ContainerRestartPolicy_Not_Captured | job path | Fixed — both sides missing |
| 17 | ServiceAccount_Secrets_And_ImagePullSecrets | whitelist | Fixed — analysis finished |
| 18 | ConfigMap_Secret_Identical_Content_Duplicated | release naming | Rename kept; warning made actionable |
| 19 | ConfigMap_Secret_References_Prefixed | release naming | **Verified** — 0 dangling refs |
| 20 | Workload_Metadata_Labels_Lost | whitelist | Fixed — largest defect in the report |
| 21 | ExternalSecret_Spec_Fields_Not_Captured | whitelist | Fixed — 2 of 5 claims refuted |
| 22 | Zero_Width_Unicode_In_SecretStoreRef | source data | Fixed defensively |
| 22b | ExternalSecret_Name_Collision_Suffix | release naming | Mechanism refuted |
| 23 | ExternalSecret_ManagementPolicy_Changed | whitelist | Already fixed by 21 |
| 24 | SecretStore_Spec_Conditions_Not_Rendered | whitelist | Labels fixed; conditions already worked |
| 25 | Service_AppProtocol_Lost | whitelist | Fixed — **both** Service templates |
| 26 | Container_Ports_Reordered_And_Auto_Named | list→map + checker | Fixed |
| 27 | Empty_Map_Env_Var_Breaks_Generation | `\| default` | Fixed — both sides |
| 28 | Annotations_Lost | whitelist | Fixed — LOW holds, but by luck |
| 29 | Empty_K8s_Default_Maps_Dropped | checker | Confirmed false positive |
| 30 | Service_ExternalTrafficPolicy_Lost | whitelist | No impact here; gap fixed |
| 31 | Service_SessionAffinity_Lost | whitelist | Cosmetic here; gap fixed |
| 32 | Service_Selector_Injected_When_Absent | checker | Confirmed; 2 defects behind it |
| 33 | Legacy_ServiceAccount_Field_Lost | checker | Confirmed false positive |
| 34 | ProgressDeadlineSeconds_Lost | whitelist | 0 occurrences; gap fixed |
| 35 | Grafana_ConfigMap_Trailing_Newline | source data | **Refuted** |
| — | `_FOUND_Global_Defaults...` | layering | **Two P0s**, no error.txt, both fixed |

---

## What is still open

Two defects with no folder, found during the campaign and deliberately **not**
fixed — both need design decisions outside any folder's scope. Written up in
[`_MISSING_ISSUES_ANALYSIS.md`](_MISSING_ISSUES_ANALYSIS.md):

1. **Hand-managed `Endpoints` objects are dropped, and the report says nothing.**
   `Endpoints`/`EndpointSlice` are filtered as transient before the converter or
   checker sees them — correct for controller-generated ones, wrong for the
   hand-written kind that backs a selector-less Service. Reproduced: the object
   vanishes and the report prints *"No fidelity issues found"*, not even counting
   it as an object. **It compounds folder 32 exactly** — that folder found the
   chart injects a selector into such a Service; this is the other half.
2. **A chart inventing an *object* is unreportable.** ADDED is never an error, so
   `objects rendered with no raw source: 0` is not the same claim as "the chart
   invents nothing" — the ServiceAccounts it creates per release are filed as
   INFO.

And the honest limit on all of it: **nothing here was verified against a running
cluster.** Every STATUS names what that leaves unproven for its own defect —
that Kubernetes retains a PVC, that pod DNS resolves, that a sidecar keeps
running, that ArgoCD resumes tracking an object. Rendered manifests are verified;
behaviour is not.

---

## Addendum — the strict-naming directive

A later directive replaced part of root cause 6's design: **zero naming
mutation**. Rendered `metadata.name` must be byte-identical to the raw manifest,
no prefix stripping, no release-name overrides, full names as map keys.

**Acceptance criterion, measured by `name_fidelity_check.py`:**

```
raw names checked:            141
names NOT in rendered output: 0
names in render, not in raw:  0
PASS - 0 naming changes.
```

Baseline before the directive was already 137/141 — folders 2, 5 and 7 had
removed most release-name overrides. Three changes closed the rest:

| Change | Effect |
|---|---|
| `short_subresource_name` returns the raw name verbatim | no prefix stripping |
| `universal-chart.subresourceName` renders the key verbatim | no fullname prefix |
| cluster-scoped objects keep their own names | no namespace prefix, and the roleRef rewrite that only existed to follow it is gone |

**A whole defect class disappeared.** `metadata.name` is unique per kind per
namespace *by API rule*, so a full-name map key **cannot collide**. Folders 13
and 14 were about surviving a collision; it is now impossible to reach.
`claim_subresource_key` stays as a safety net, and folder 13's tests assert the
stronger guarantee instead.

**One rule collides with Kubernetes, and the conflict is now visible rather than
hidden.** `platform-ops` and `platform-ops-prd` both declare
`ClusterRole/log-agent-reader`. ClusterRoles are cluster-scoped — one global
object. The namespace prefix was what kept them apart; removing it surfaces a
real conflict and the run stops:

```
CLUSTER CONFLICT: ClusterRole/log-agent-reader claimed by both
'platform-ops/log-agent' and 'platform-ops-prd/log-agent'.
```

`--cluster-scoped` resolves it by emitting the object once, which is what the
source cluster actually had. There is no way to preserve the name *and* have two
namespaced releases each create it; Kubernetes does not allow it.

**A real bug the prefix had been masking:** the conflict detector compared bare
release names, so two namespaces both running a release called `log-agent` read
as "the same owner" and stayed silent. It now keys on `namespace/release`.

**Still a hard limit:** name truncation (52 chars for CronJob, 63 otherwise) is
an API constraint, not a converter choice. `validate_resource_names` errors at
conversion time rather than truncating silently.
