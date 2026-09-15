# STATUS — CronJob_Merge_Overwrites_Command_And_Env (P1, folder 14/35)

**Result: FIXED BY FOLDER 13 — no further code change needed. One half of the
report's claim REFUTED with output.**

8 new tests, proved RED against the pre-folder-13 converter and GREEN against
the current one. Folders 1–13 still green (139 total), chart's own 234 still
green, fidelity unchanged at 0 lost / 0 changed.

This is the same outcome shape as folders 8 and 9, which folder 7's fix cleared.
No code changed here; the evidence is the mutation test in §3.

---

## 1. The two claims, answered separately

error.txt carries two issues, and they do not have the same answer.

### Issue 10 — "different env values for STAGE. Values has only one." — TRUE

Confirmed exactly. Reverting the converter to its pre-folder-13 form and running
this folder's tests:

```
AssertionError: one CronJob's STAGE was lost during the merge; got ['prod']
```

Same for the command:

```
AssertionError: CronJob/db-migrate-backup's command is gone;
  got [['/bin/bash', '-c', "echo 'Hello from jenkins-backup-2!'"]]
```

### Issue 9 — "The values YAML has NEITHER" — the loss is real, "neither" is not

The overwrite dropped exactly **one** of the two commands, never both. What makes
it look like neither is the values **layering**, and it is worth spelling out
because it will keep coming up while reading generated output:

`base/<slug>.yaml` holds only what **every namespace running that microservice
agrees on** (`common_subtree`). This fixture's `db-migrate` runs in
`background-jobs` and `background-jobs-prd` with different commands, so `command`
is excluded from base/ and pushed into each namespace's own
`values/<ns>/<slug>.yaml`. `render_check.ps1`, real output:

```
== 2. which layer holds each command ==
   base\db-migrate.yaml                       (no command here)
   background-jobs\values\db-migrate.yaml     - echo 'Hello from jenkins-backup-1!'
   background-jobs\values\db-migrate.yaml     - echo 'Hello from jenkins-backup-2!'
   background-jobs-prd\values\db-migrate.yaml - echo 'Hello from jenkins-backup-1-prd!'
   background-jobs-prd\values\db-migrate.yaml - echo 'Hello from jenkins-backup-2-prd!'
```

Read `base/` alone and both commands are missing. Read the layer the
ApplicationSet actually applies on top of it and both are there, one per
CronJob. `test_command_moves_out_of_base_when_namespaces_disagree` asserts both
halves of that at once — base carries `[None, None]`, and each namespace's own
file carries its own two — so if this layering ever does become a real loss, a
test says so rather than a reader having to know the rule.

The same split applies to `env`, for a second and independent reason: literal env
values are deliberately moved out of the structural values into the namespace's
minimal override (`build_minimal_values` / `ctx.sub_full_env`), while
`valueFrom` entries stay structural. So a CronJob's `STAGE: dev` is in neither
`base/` nor the structural block by design.
`test_the_secret_ref_env_stays_structural_and_on_the_right_cronjob` and
`test_literal_env_reaches_the_minimal_override_for_both_cronjobs` pin both sides.

### The line numbers

error.txt cites "line 1065 (command)" and "the microservice merge at lines
1370-1451". Neither matches this converter: the command is captured inside
`handle_cronjob` and there is no merge in that range. There is no merge step at
all — the loss was a plain dict assignment. Same correction as folder 13 §1.

## 2. Files changed

**None.** The defect is the single line folder 13 replaced:

```python
ctx.values.setdefault("cronjobs", {})[sub_name] = entry     # silent overwrite
```

now `claim_subresource_key(ctx, "cronjobs", name)`, which gives each raw CronJob
its own key. Because the overwrite replaced the **whole entry**, restoring the
entry restores command, args and env with it — there is no separate command
defect to fix. Folder 13's STATUS §6 recorded this folder as the reason it did
not stop at the schedule.

What this folder adds is the test coverage from the command/env side, which
folder 13 only guarded in passing, plus the refutation above.

## 3. Tests

`CronJob_Merge_Overwrites_Command_And_Env/test/` — 8 tests + `render_check.ps1`.

`fixture/background-jobs/db-migrate.yaml` is derived from
`namespaces-showcase/background-jobs/db-migrate.yaml` — same namespace, same
microservice file name, same Secret, same secretKeyRef-into-a-job pattern. Added:
two CronJobs reducing to the key `backup`, carrying error.txt's verbatim
commands and its named `STAGE` variable. `fixture/background-jobs-prd/` mirrors
the real `background-jobs-prd` layout so the base-sharing layer is exercised.

| raw object | command | args | STAGE | DB_PASSWORD |
|---|---|---|---|---|
| CronJob `db-migrate-backup` | `...backup-1!` | — | `dev` | secretKeyRef |
| CronJob `backup` | `...backup-2!` | `--verbose` | `prod` | — |

`args` and `DB_PASSWORD` are there so a fix that special-cased `command` and
`env` would still fail, and so the test can assert the second CronJob does **not**
inherit the first's secret reference.

### RED — converter reverted to the pre-folder-13 overwrite

Run with `claim_subresource_key` and both assignments restored to their original
form, then restored byte-identically (sha256 verified):

```
=== converter reverted to the pre-folder-13 silent overwrite ===
FFFFFFF.                                                            [100%]

test_both_commands_survive_capture
  AssertionError: CronJob/db-migrate-backup's command is gone;
    got [['/bin/bash', '-c', "echo 'Hello from jenkins-backup-2!'"]]
test_each_cronjob_keeps_the_command_that_belongs_to_it
  KeyError: '0 1 * * *'
test_both_stage_values_survive_capture
  AssertionError: one CronJob's STAGE was lost during the merge; got ['prod']
test_the_secret_ref_env_stays_structural_and_on_the_right_cronjob
  KeyError: '0 1 * * *'
test_literal_env_reaches_the_minimal_override_for_both_cronjobs
  AssertionError: minimal override lost a STAGE; got ['prod']
test_command_moves_out_of_base_when_namespaces_disagree
  AssertionError: base/db-migrate.yaml should hold no command when the namespaces
    disagree; got [None]
test_round_trip_each_rendered_cronjob_has_its_own_command_and_stage
  AssertionError: 2 raw CronJobs, 1 rendered: ['db-migrate-backup']

7 failed, 1 passed in 1.60s

restored: True  sha256 52c6c12395c5ecc6
```

Line 3 is issue 10's exact symptom — *"Values has only one"*. Line 1 is issue 9's.
The last line is the whole thing end to end: one of two CronJobs simply gone.

The one test that passes either way is the fidelity-checker check, which does not
depend on this code path.

### GREEN — current converter

```
8 passed in 1.42s
```

### Accumulated (folders 1–14 + the found P0)

```
139 passed in 43.51s
```

## 4. Validation chain

`render_check.ps1`, real output:

```
== 4. raw vs rendered, per CronJob ==
  OK   raw backup             -> rendered db-migrate-backup-2   STAGE=prod args=['--verbose']
  OK   raw db-migrate-backup  -> rendered db-migrate-backup     STAGE=dev  args=None

== 5. helm lint ==
1 chart(s) linted, 0 chart(s) failed
```

Both CronJobs render, each with its own command, its own args and its own STAGE;
the second does not inherit `DB_PASSWORD`. `db-migrate-backup-2` is the renamed
loser of the key collision, warned about by folder 13's change — see that
folder's STATUS §2 for why this chart cannot render a bare `backup`.

| Step | Result |
|---|---|
| Full showcase converter run | `122 objects matched (28 relocated). No fidelity issues found.` |
| Fidelity before / after | 0 lost / 0 changed, unchanged |
| Generated-output churn | none — no code changed in this folder |
| `helm lint` | 0 failed |
| Chart's own suite | 28 suites, 234 tests passed |

## 5. What could NOT be verified without a live cluster

- **That the two CronJobs run the commands they now render.** Verified only that
  the rendered container `command`, `args` and `env` equal the raw manifests'.
- **That `db-migrate-backup-2` is an acceptable object name on the target
  cluster**, and what becomes of the original `backup` object already deployed
  there. Same open migration question as folder 13 §5.
- **The report's actual objects** (`jenkins-backup` in the metroair project) are
  not in this repo. The mechanism is fixed and covered; those objects are
  unverified.
- **Whether the original report's "neither" was this layering or a third
  mechanism.** I can only prove what this converter does: it drops exactly one,
  and the layering explains the appearance of both being gone. If the original
  `values/` tree is ever available, checking `values/<ns>/` rather than `base/`
  is the first thing to do.

## 6. OBSERVED, DEFERRED

- **`Sidecar_Container_Name_Key_Collision` (folder 4)** is the third member of
  this family — a non-unique string used as a dict key. It was refuted on this
  repo's data, but folder 13's `claim_subresource_key` is the shape a fix would
  take if it resurfaces. `sidecars` and `initContainers` are still keyed by bare
  container name (`_pod-spec.tpl`, `handle_cronjob`), and container names are
  only unique **within one pod** — two CronJobs' sidecars cannot collide, but
  nothing enforces that a values file keeps them separate. Untouched.
- **`ConfigMap_Secret_Identical_Content_Duplicated` (folder 18)** is the same
  question one level up and already has a recorded decision (keep the rename,
  warn louder), which is what folder 13 implemented for CronJobs. Worth reusing
  the wording.
- **The literal-env split is easy to misread as a loss.** `STAGE` is in neither
  `base/` nor the structural values by design. Any future folder auditing env
  fidelity must merge `base/ + values/<ns>/` before comparing, exactly as
  `test_round_trip_each_rendered_cronjob_has_its_own_command_and_stage` does.
- Folders 1–13 deferred items still stand.

## 7. Reproducing

```powershell
python -m pytest universal/gitops-factory/output-showcase/ERRORS_ANALYSIS/CronJob_Merge_Overwrites_Command_And_Env/test/test_cronjob_command_env.py -v
& universal\gitops-factory\output-showcase\ERRORS_ANALYSIS\CronJob_Merge_Overwrites_Command_And_Env\test\render_check.ps1
```
