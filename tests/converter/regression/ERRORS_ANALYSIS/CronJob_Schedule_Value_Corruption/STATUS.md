# STATUS — CronJob_Schedule_Value_Corruption (P1, folder 13/35)

**Result: FIXED.** 14 tests RED→GREEN, folders 1–12 still green (131 total),
chart's own 234 still green, fidelity unchanged at 0 lost / 0 changed.

The schedule was never corrupted. **Two CronJobs were collapsed into one**, and
the survivor was rendered under the *lost* object's Kubernetes name — so a
raw-vs-rendered comparison of that name shows one object's schedule replaced by
the other's. `"46 2 * * 6"` → `"6 2 * * 1"` is not a mangled string; it is two
different cron expressions belonging to two different objects.

---

## 1. error.txt: both hypotheses checked, one holds

error.txt states two competing hypotheses and says both must be checked.

**Hypothesis 2 — "subtract_defaults bleeding the value 6 (the default for
successfulJobsHistoryLimit) into the schedule string" — is FALSE.** Disproved
with the two values in question actually present:

```
a = {"cronjobs": {"cleanup": {"schedule": "46 2 * * 6", "successfulJobsHistoryLimit": 6}}}
b = {"cronjobs": {"cleanup": {"schedule": "6 2 * * 1",  "successfulJobsHistoryLimit": 6}}}

common_subtree([a, b])  -> {'cronjobs': {'cleanup': {'successfulJobsHistoryLimit': 6}}}
subtract_defaults(a, common) -> {'cronjobs': {'cleanup': {'schedule': '46 2 * * 6'}}}
subtract_defaults(b, common) -> {'cronjobs': {'cleanup': {'schedule': '6 2 * * 1'}}}
```

Both functions walk the tree key-by-key and compare whole values; there is no
path by which an integer under one key reaches a string under another.
`test_subtract_defaults_never_bleeds_a_history_limit_into_the_schedule` pins it.

I also ruled out the converter's own YAML dumper, since a cron expression is a
string full of YAML metacharacters (`*` opens an alias node, `@` is reserved,
`1:30` looks sexagesimal). Every form round-trips byte-identically —
`test_yaml_round_trip_preserves_every_schedule_form`.

**Hypothesis 1 — a merge collision — is TRUE, but not where error.txt points.**
It says "likely during the microservice merge at line 1370-1451". There is no
merge there; those lines are the body of `handle_cronjob`/`handle_job`. The
collision is a single line, the last one of `handle_cronjob`:

```python
ctx.values.setdefault("cronjobs", {})[sub_name] = entry     # was line 1421
```

`sub_name` is `short_subresource_name()`, which strips a leading
`"<microservice>-"`. A microservice is one **file**, so any two CronJobs in that
file differing only by the prefix reduce to the same key:

```
nightly-report-cleanup  ->  "cleanup"
cleanup                 ->  "cleanup"     <-- same key, second wins
```

Plain `=` on a dict: the second object replaces the first entirely — schedule,
command, args, env, history limits, all of it — with no warning. The chart then
re-adds the release prefix (`universal-chart.subresourceName`), so the single
surviving entry renders as `nightly-report-cleanup`: the name of the object that
was *deleted*, carrying the schedule of the object that *survived*.

**The other correction:** error.txt says the value is stored as
`ctx.data["cronjobs"][name]["cronSchedule"]`. Neither `ctx.data` nor
`cronSchedule` exists in this converter — it is `ctx.values["cronjobs"][key]["schedule"]`,
rendered by `templates/cronjob.yaml:20`. The line numbers cited (1065 for the
command, 1370-1451) are also from the original project's file, not this one.

## 2. Files changed

| File | Where | Change |
|---|---|---|
| `convert_to_universal_chart.py` | `MicroserviceContext` | new `sub_key_owner` field |
| `convert_to_universal_chart.py` | after `short_subresource_name` | new `rendered_subresource_name()` — mirrors the chart's `universal-chart.subresourceName` |
| `convert_to_universal_chart.py` | same place | new `claim_subresource_key()` — reserves a unique key, resolves collisions, warns |
| `convert_to_universal_chart.py` | `handle_cronjob` | `short_subresource_name(name, ctx)` → `claim_subresource_key(ctx, "cronjobs", name)` |
| `convert_to_universal_chart.py` | `handle_job` | same, for `"jobs"` |

No chart template changed. This one is converter-only — `cronjob.yaml` already
renders `{{ $spec.schedule | quote }}` faithfully, and there is no second,
unmentioned call site (the first folder in this campaign where that is true; see
§7).

### Which of two colliding objects gets the short key

Decided by whether the chart can reproduce the raw name from that key, **not by
document order**. `rendered_subresource_name(key)` returns `fullname` when
`key == fullname` and `"<fullname>-<key>"` otherwise, exactly as the chart
helper does. So:

| raw name | short key | chart renders | entitled |
|---|---|---|---|
| `nightly-report-cleanup` | `cleanup` | `nightly-report-cleanup` | yes |
| `cleanup` | `cleanup` | `nightly-report-cleanup` | no |

The entitled object takes the short key; if it arrives second, the squatter is
evicted to a disambiguated key (and its `sub_full_image` / `sub_full_env` /
`sub_raw_image` tracker entries are re-keyed with it, or `build_minimal_values`
would later paste the evicted object's image tag and literal env onto whichever
object now holds the short key — the same swap this function exists to prevent).
`test_the_key_assignment_does_not_depend_on_document_order` runs the fixture in
both orders and asserts the same result.

### Why the loser is renamed rather than kept

This chart derives a subresource's Kubernetes name from its map key alone. Given
release `nightly-report`, **no key produces the object name `cleanup`.** So two
raw objects that reduce to one key cannot both keep their names — that is a
property of the chart, not a choice I made here. What I changed is which
failure you get: a visibly renamed CronJob that still runs its own schedule and
command, announced in `conflicts_and_warnings.txt`, instead of a silently
deleted one. `-2` matches the suffix convention this converter already uses for
ExternalSecret name collisions.

## 3. Tests

`CronJob_Schedule_Value_Corruption/test/` — 14 tests + `render_check.ps1`.

`fixture/background-jobs/nightly-report.yaml` is derived from
`namespaces-showcase/background-jobs/nightly-report.yaml` (same namespace, same
microservice file name, same container/image/resources shape). What it adds is
the shape the showcase never produces — more than one CronJob in one
microservice file:

| raw object | schedule | key | note |
|---|---|---|---|
| CronJob `nightly-report-cleanup` | `46 2 * * 6` | `cleanup` | error.txt's raw value |
| CronJob `cleanup` | `6 2 * * 1` | `cleanup` | error.txt's rendered value — collides |
| CronJob `nightly-report-archive` | `*/15 * * * *` | `archive` | control: no collision, `*` form |
| Job `nightly-report-seed` / Job `seed` | — | `seed` | guard: same helper, same collision |

### RED — before the fix

```
7 failed, 7 passed in 1.96s

AssertionError: CronJob/nightly-report-cleanup renders from values key 'cleanup', whose
  schedule is now '6 2 * * 1' - the value belonging to the OTHER raw CronJob.
  Expected '46 2 * * 6'.
  assert '6 2 * * 1' == '46 2 * * 6'

AssertionError: a raw CronJob was silently dropped: 3 CronJobs in, 2 values entries out
  -> ['archive', 'cleanup']

AssertionError: 3 raw CronJobs, 2 rendered:
  {'nightly-report-archive': '*/15 * * * *', 'nightly-report-cleanup': '6 2 * * 1'}

AssertionError: the CronJob key collision produced no warning; ctx.warnings=[]

AssertionError: key 'cleanup' resolves to '6 2 * * 1' in file order and '46 2 * * 6' reversed

AssertionError: a raw Job was silently dropped; keys=['seed']
```

The third line is error.txt's headline reproduced end to end: the object named
`nightly-report-cleanup` rendered with `6 2 * * 1` against a raw `46 2 * * 6`.

The seven that passed before the fix are the hypothesis-2 disproofs, the chart's
own rendering (which was never broken), and the fidelity-checker check.

### GREEN — after

```
14 passed in 1.96s
```

### Accumulated (folders 1–13 + the found P0)

```
131 passed in 42.01s
```

## 4. Validation chain

`render_check.ps1`, real output:

```
== 2. collision warning reached conflicts_and_warnings.txt ==
   [background-jobs/nightly-report] CronJob/nightly-report-cleanup and CronJob/cleanup both
   reduce to the 'cronjobs' key 'cleanup' - name collision. The second used to silently
   overwrite the first; both are kept now, but 'cleanup' renders as
   'nightly-report-cleanup-2' rather than its own name.
   [background-jobs/nightly-report] Job/nightly-report-seed and Job/seed both reduce to the
   'jobs' key 'seed' - name collision. ...

== 4. raw vs rendered, side by side ==
  RAW NAME                   RAW SCHEDULE   RENDERED NAME              RENDERED SCHEDULE
  cleanup                    6 2 * * 1      nightly-report-cleanup-2   6 2 * * 1   OK
  nightly-report-archive     */15 * * * *   nightly-report-archive     */15 * * * * OK
  nightly-report-cleanup     46 2 * * 6     nightly-report-cleanup     46 2 * * 6  OK

== 5. helm lint ==
1 chart(s) linted, 0 chart(s) failed

== 6. kubectl --dry-run=client ==
   SKIPPED - needs an API server for the OpenAPI schema; no cluster access here.

render_check: PASS
```

Full converter run over the real showcase:

```
Converted 20 microservices across 10 namespaces.
Auto-resolved conflicts / warnings: 19
Rendered 24 releases.
No duplicate resource ownership detected across any of the 24 rendered releases.
  122 objects matched (28 legitimately relocated).
  No fidelity issues found.
```

| Step | Result |
|---|---|
| Fidelity before / after | 0 lost / 0 changed, unchanged |
| Warning count before / after | 19 / 19, unchanged |
| Generated-output churn | **none new** — still the same 14 files / 23 insertions carried from folders 1–12 |
| `helm lint` | 0 failed |
| Chart's own suite | 28 suites, 234 tests passed |

The showcase has exactly one CronJob (`nightly-report`) and one Job
(`db-migrate`), each alone in its microservice file, so nothing collides and the
generated output is byte-identical. Expected, and the reason the fixture exists.

## 5. What could NOT be verified without a live cluster

- **That the CronJob actually fires on Saturday at 02:46.** Verified only that
  the rendered `spec.schedule` string equals the raw one; the cron parser is
  kube-controller-manager's.
- **That two CronJobs with these names can coexist.** Both rendered names are
  under the 52-character CronJob limit, but uniqueness is enforced by the API
  server at apply time.
- **What happens to the renamed object on a real migration.** `cleanup` becomes
  `nightly-report-cleanup-2`; on the cluster that is a *new* CronJob, and the
  original `cleanup` would be left orphaned unless deleted by hand. ArgoCD's
  `prune: false` (see CLAUDE.md) means it will not remove it. This needs an
  operator decision per occurrence — which is exactly why the rename is warned
  about rather than done quietly.
- **The counted occurrences** — the CronJobs in the original report
  (`jenkins-backup`) are not in this repo.

## 6. OBSERVED, DEFERRED

- **`CronJob_Merge_Overwrites_Command_And_Env` (folder 14) shares this line.**
  Its error.txt says so itself: *"Same defect class is suspected as the cause of
  the CronJob schedule corruption."* The overwrite replaced the whole entry, so
  fixing it necessarily restores command and env too —
  `test_each_cronjob_keeps_its_own_command_history_limit_and_args` pins that
  here so folder 14 cannot silently regress it. I did **not** treat folder 14 as
  done: its error.txt makes a stronger claim this mechanism does not explain —
  *"The values YAML has neither"*, i.e. **both** commands absent, not
  last-writer-wins. That is a different symptom and folder 14 must account for
  it separately.
- **`Sidecar_Container_Name_Key_Collision` (folder 4) is the same bug shape** in
  `sidecars`/`initContainers`, keyed by container name. It was refuted there on
  this repo's data; the mechanism (`dict[key] = entry` on a non-unique key)
  is identical and worth a second look if it resurfaces.
- **`handle_job`'s key collision is not any folder's named defect.** I fixed it
  in the same helper rather than leaving an identical silent-overwrite two lines
  below the one being fixed; the assertion here (`test_jobs_collide_the_same_way`)
  is a count guard only, since this folder is about the schedule.
- **A CronJob with no `schedule` at all gets an invented `"0 0 * * *"`**
  (`entry = {"schedule": spec.get("schedule", "0 0 * * *")}`). Fabricating a
  midnight schedule for a manifest that had none is arguably worse than failing.
  Untouched — no folder covers it and it is not this defect.
- Folders 1–12 deferred items still stand.

## 7. Note for the campaign log

**This is the first folder in the run where the CronJob/Job path was the *only*
site rather than the forgotten second one.** Folders 5, 6, 10, 11 and 12 all had
a working main-workload path and a missing job path. Here the chart is correct,
the workload path is not involved, and the whole defect is one line in the job
path. The pattern still holds — the job path is where this converter breaks —
but it is worth recording that it broke on its own this time.

## 8. Reproducing

```powershell
python -m pytest universal/gitops-factory/output-showcase/ERRORS_ANALYSIS/CronJob_Schedule_Value_Corruption/test/test_cronjob_schedule.py -v
& universal\gitops-factory\output-showcase\ERRORS_ANALYSIS\CronJob_Schedule_Value_Corruption\test\render_check.ps1
```
