# STATUS — ContainerRestartPolicy_Not_Captured (P1, folder 16/35)

**Result: FIXED — two-sided, converter and chart.**
10 tests RED→GREEN, folders 1–15 still green, chart's own 234 still green,
fidelity unchanged at 0 lost / 0 changed.

---

## 1. What error.txt gets right and wrong

**Right:** the field is never captured and never rendered. Confirmed.

**Wrong, twice.** error.txt says the chart holds a bad default at
`_pod-spec.tpl` line 244:

```gotemplate
containerRestartPolicy: {{ .containerRestartPolicy | default "OnFailure" }}
```

No such line exists. `_pod-spec.tpl` never mentions `containerRestartPolicy` at
all, and its line 234 — `{{ .job.restartPolicy | default "OnFailure" }}` — is the
**pod-level** field, which already round-trips correctly. So root-cause row F
("chart template has wrong defaults") does not apply here: the field was absent
from both sides, not defaulted to the wrong thing. Nothing replaced `Always`;
nothing rendered anything.

**The two fields are easy to conflate and are not the same:**

| path | meaning | before |
|---|---|---|
| `spec.template.spec.restartPolicy` | pod level | already worked |
| `spec.template.spec.containers[*].restartPolicy` | container level | **the gap** |
| `spec.template.spec.initContainers[*].restartPolicy: Always` | native sidecar | already worked (pass-through) |

The init-container case passing through is worth stating: init containers are
carried verbatim by `named_list_to_map_split_image`, so the native-sidecar
marker survived. `test_init_container_restart_policy_passes_through_untouched`
passed before the fix and is pinned so a future "tidy up init container fields"
change cannot break it.

## 2. Files changed

| File | Change |
|---|---|
| `convert_to_universal_chart.py` `handle_main_container` | capture `containerRestartPolicy` from `c["restartPolicy"]` |
| `convert_to_universal_chart.py` `handle_cronjob` / `handle_job` | same, per job |
| `_pod-spec.tpl` `mainContainer` | render `restartPolicy` from `.Values.containerRestartPolicy` |
| `_pod-spec.tpl` `jobPodSpec` | render it from `.job.containerRestartPolicy` |
| `values.yaml` | declare `containerRestartPolicy: ""` |
| `values.schema.json` | string, enum `"" / Always / OnFailure / Never` |

**Why the key is `containerRestartPolicy` and not `restartPolicy`:**
`.Values.restartPolicy` is already the pod-level field, rendered by `podSpec`.
Reusing the name would have made the two silently overwrite each other. This is
error.txt's own name for it, so the report and the values agree.

## 3. Why it matters

`restartPolicy: Always` on an initContainer is how Kubernetes 1.28+ declares a
**native sidecar**: it starts before the main container and keeps running
alongside it. Drop the field and the sidecar degrades into an ordinary init
container the workload waits to **complete** — so a long-running log shipper
hangs the Job forever rather than failing it. On a regular container (1.33+) the
same value restarts that container in place instead of failing the pod.

## 4. Tests

`ContainerRestartPolicy_Not_Captured/test/` — 10 tests.

Fixtures derive from `namespaces-showcase/background-jobs/db-migrate.yaml` (Job
+ Secret + credentials mount, plus a CronJob sibling) and
`shop-web/api-gateway.yaml` (Deployment). Each carries the field on a regular
container **and** on an initContainer.

### RED — before

```
6 failed, 4 passed in 2.43s

AssertionError: the main container's restartPolicy was dropped;
  values keys = ['containerName', 'image', 'initContainers', 'ports', 'workload']
AssertionError: the Job container's restartPolicy was dropped; entry keys =
  ['args','backoffLimit','command','containerName','image','initContainers',
   'restartPolicy','volumeMounts','volumes']
AssertionError: Deployment container restartPolicy not rendered; got None
```

Note the second line: `restartPolicy` IS in the entry — the pod-level one. That
is exactly the confusion this folder is about.

### GREEN — after

```
10 passed in 2.34s
```

Accumulated across folders 1–16 plus 25/30/31: `183 passed`.

## 5. Validation chain

| Step | Result |
|---|---|
| Full showcase converter run | `122 objects matched (28 relocated). No fidelity issues found.` |
| Fidelity before / after | 0 lost / 0 changed, unchanged |
| Generated-output churn | none new — all four raw `restartPolicy` occurrences in this repo are pod-level |
| `helm lint` | 0 failed |
| Chart's own suite | 234 passed |

Grep proof that the data does not exercise it: every `restartPolicy:` in
`namespaces-showcase/` is at pod level (4 occurrences, all `Never`). Zero
container-level ones anywhere. Fixture-driven, like folders 10–12.

## 6. What could NOT be verified without a live cluster

- **That the sidecar actually stays running** and that the Job completes rather
  than hanging. That is kubelet behaviour.
- **That the target cluster is new enough.** Container-level `restartPolicy` on
  a *regular* container needs Kubernetes 1.33+; on an initContainer, 1.28+. An
  older API server will reject or strip it. The chart now renders whatever the
  raw manifest said, which is correct for a round-trip, but the target version is
  not something this repo can check.
- **The report's counted objects** are not in this repo.

## 7. OBSERVED, DEFERRED

- **`containerRestartRules`** (alpha, K8s 1.35+) is captured and rendered for the
  main container only, not for jobs — the same job-path asymmetry as folders 6,
  10, 11 and 12. No folder covers it and it is a different field. Untouched.
- Folders 1–15 deferred items still stand.

## 8. Reproducing

```powershell
python -m pytest universal/gitops-factory/output-showcase/ERRORS_ANALYSIS/ContainerRestartPolicy_Not_Captured/test -v
```
