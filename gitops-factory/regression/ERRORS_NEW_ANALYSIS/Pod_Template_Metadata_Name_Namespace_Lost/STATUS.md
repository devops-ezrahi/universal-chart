# STATUS — Pod_Template_Metadata_Name_Namespace_Lost (TIER 3, folder 22/26)

**VERIFIED_AND_FIXED.**

**Result: BENIGN — removed at the boundary, not suppressed in the checker.** All
three fields (`spec.template.metadata.name`, `.namespace`, `.creationTimestamp`)
are inert, each for its own reason, and `sanitize_object` now strips them from
the raw side so they never reach the values file *or* the comparison.
`ownerReferences` — grouped with them in the same report lines — is deliberately
**not** touched here; its benign verdict carries a dependency the other two do
not, and that is folder 23's question.

RED→GREEN proved against the real pipeline: 3 LOST → 0 LOST. Folder tests 16
passed, all `ERRORS_NEW_ANALYSIS` 362 passed, `helm unittest` 234 passed, lint
clean.

---

## 1. State on entry — the work was present but unproven

Unlike folders 1–21, this folder's three artefacts were **already in the working
tree** when this pass began, left by an earlier session that stopped before
validating or documenting them:

| Artefact | State on entry |
|---|---|
| `sanitize_object` pod-template strip (converter) | present, uncommitted |
| corpus fixture on `Deployment/opa` | present, uncommitted |
| `test/test_pod_template_metadata.py` | present, uncommitted |
| `STATUS.md` | **absent** — folder not closed |

So this pass wrote no new fix. What it did was the part that was missing: prove
the code does what it claims against the real converter, prove it breaks nothing
else, and record the result. Everything below is measurement, not assertion.

## 2. The verdict, field by field

`error.txt` rates all three benign and the reasoning holds independently for
each — which is why the rule names the three keys rather than the metadata block:

| Field | Why it is inert |
|---|---|
| `name` | Kubernetes **ignores it entirely**. Pod names come from the controller name plus a hash; a name on the template has no effect whatsoever. |
| `namespace` | Inherited from the object's own namespace. Setting it is redundant; setting it to a *different* namespace is invalid. |
| `creationTimestamp` | Server-populated, same as the outer object's — which `VOLATILE_METADATA_KEYS` has always stripped. |

**Removed, not suppressed.** This is the same treatment the outer object's
metadata gets, and the same treatment folder 8 gave the embedded
`volumeClaimTemplates`. The pod template is the third embedded object with the
identical problem, and handling it at the boundary keeps one rule in one place —
and keeps the junk out of the generated values, not merely out of the report.

## 3. Where it lives

| File | Location | Change |
|---|---|---|
| `universal/gitops-factory/convert_to_universal_chart.py` | `sanitize_object`, lines 528–545 | strip `name` / `namespace` / `creationTimestamp` from `spec.template.metadata` on `WORKLOAD_KINDS` |
| `universal/gitops-factory/namespaces-showcase/identity-auth-prd/opa-prd.yaml` | pod template metadata | corpus fixture carrying all three |
| `.../ERRORS_NEW_ANALYSIS/Pod_Template_Metadata_Name_Namespace_Lost/test/` | — | 16 tests |

```python
    if kind in WORKLOAD_KINDS and isinstance(spec, dict):
        pod_meta = (spec.get("template") or {}).get("metadata")
        if isinstance(pod_meta, dict):
            for key in ("name", "namespace", "creationTimestamp"):
                pod_meta.pop(key, None)
```

## 4. Proof — RED, by disabling the rule

The corpus must be able to *reproduce* the defect, or a green report proves
nothing. The strip was temporarily neutralised (`for key in ():`) and the full
pipeline re-run:

```
python run_all.py --cluster-scoped --input namespaces-showcase --output <scratch>\out22RED

    fields LOST                                  3
    fields CHANGED                               0
    render failures                              0
    - fidelity issues: {'fields LOST': 3} (STEP 3)

=== identity-auth-prd — 10 raw objects vs 10 rendered ===
  ERROR  Deployment/opa LOST at spec.template.metadata.name: raw had 'opa'
  ERROR  Deployment/opa LOST at spec.template.metadata.namespace: raw had 'identity-auth-prd'
  ERROR  Deployment/opa LOST at spec.template.metadata.creationTimestamp: raw had None
```

Three errors, exactly the three fields, on exactly the shape `error.txt`
describes. The converter was then restored byte-for-byte from a backup taken
before the edit (`diff -q` → identical).

## 5. Proof — GREEN, with the rule restored

```
python run_all.py --cluster-scoped --input namespaces-showcase --output <scratch>\out22GREEN

    fields LOST                                  0
    fields CHANGED                               0
    objects in raw but rendered by nothing       0
    objects rendered with no raw source          0
    render failures                              0
  OK - nothing lost, nothing changed.

=== identity-auth-prd — 10 raw objects vs 10 rendered ===
  (no ERROR lines)
```

## 6. The folder's own tests

```
python -m pytest output-showcase/ERRORS_NEW_ANALYSIS/Pod_Template_Metadata_Name_Namespace_Lost/test/ -q
................                                                         [100%]
16 passed in 0.46s
```

Four groups: the three fields stripped individually and together, across all
three workload kinds; the load-bearing neighbours that must **survive** (pod
template labels and annotations — folder 5's governance labels and the
sidecar-injection annotations live in the same block, and a rule written against
`spec.template.metadata` rather than the three keys would take them with it); the
object's own `name` untouched and a non-workload kind (`Sensor`) untouched; and
`ownerReferences` explicitly still present, so folder 23 inherited an undisturbed
question.

> **Superseded by folder 23.** That last test asserted `ownerReferences` was
> still *present* on the pod template. Folder 23 decided the question and
> stripped it, so the test is now inverted and re-documented in place —
> the deferred-verdict mechanism firing, not a regression. The rule this folder
> introduced now carries four keys, not three.

## 7. Regression attribution — the one thing worth reading twice

`ERRORS_ANALYSIS` (the previous round) currently has **12 failing tests**. They
are pre-existing and **none is attributable to this folder.** Proven by running
that suite in both states:

| Converter state | ERRORS_ANALYSIS result |
|---|---|
| strip **enabled** (shipped) | **12 failed**, 319 passed |
| strip **disabled** | **13 failed**, 319 passed |

The failure lists are identical except for one entry that fails **only when the
strip is off**:

```
Missing_Objects_Not_Rendered_By_Any_Release/test/test_missing_and_extra_objects.py
    ::test_showcase_has_no_orphaned_or_invented_objects
```

So this folder's rule is not merely harmless to the previous round — a
previous-round test now depends on it.

The other 12 are inherited from folders 1–21 and are out of scope here (Zero
Multitasking). They are flagged in §10, not fixed.

## 8. Validation chain

| Check | Result |
|---|---|
| Corpus reproduces the defect (strip off) | yes — 3 LOST on `Deployment/opa` |
| Showcase fidelity (strip on) | **0 lost / 0 changed / 0 render failures** |
| Folder's own tests | **16 passed** |
| All `ERRORS_NEW_ANALYSIS` tests | **362 passed** (377 after folder 23 landed) |
| `ERRORS_ANALYSIS` regressions caused by this folder | **0** (§7) |
| `helm unittest Universal-chart` | **234 passed**, 28 suites |
| `helm lint Universal-chart` | 0 failed |
| Converter restored after RED experiment | `diff -q` identical to pre-edit backup |

STEP 2 (name check) reports one INVENTED name,
`background-jobs: ConfigMap/nightly-report-job-defaults`. That is the
**deliberate** rename decided in folder 13
(`Secret_ConfigMap_Rename_Reference_Inconsistency`) to break a cross-release
ConfigMap collision. Present identically before and after this folder's work,
and unrelated to it.

## 9. Scope gap, measured not guessed

The rule is scoped to `WORKLOAD_KINDS = {Deployment, StatefulSet, DaemonSet}`.
`Job` and `CronJob` carry pod templates with the same inert fields and are **not**
covered:

```
Job     pod-template metadata after sanitize: {'name': 'j', 'namespace': 'ns'}
CronJob pod-template metadata after sanitize: {'name': 'c', 'namespace': 'ns'}
WORKLOAD_KINDS = ['DaemonSet', 'Deployment', 'StatefulSet']
```

Left alone deliberately. `error.txt` scopes this defect to Deployments (Thanos),
no batch object in the report or the corpus carries these fields, and widening
the rule would reach into the CronJob/Job handling that folders 4 and 5 own.
Recorded as a known state rather than an oversight — if a future report shows a
Job losing pod-template `name`, the fix is adding those two kinds to this exact
loop.

## 10. OBSERVED, DEFERRED

- **`ownerReferences` on the pod template → folder 23. RESOLVED THERE.** Deferred
  from here because its benign verdict rested entirely on *"PROVIDED the operator
  is actually running in the target cluster"*. Folder 23 decided it on stronger
  ground that needs no operator — an ownerReference is a source-cluster `uid`, so
  carrying it makes the target's garbage collector delete the object — and found
  that the pod-template half was still leaking while the outer half had always
  been stripped via `VOLATILE_METADATA_KEYS`. It is now the fourth key in this
  folder's rule.
- **`Job` / `CronJob` pod templates** — §9.
- **12 pre-existing `ERRORS_ANALYSIS` failures.** Not caused here (§7), not
  fixed here. Mostly corpus-drift assertions of the form *"this repo has no X at
  all"*, which the new round's added fixtures falsified. They need a pass of
  their own.
- **The 98-vs-17 count discrepancy** `error.txt` flags is recorded, not
  resolved — both numbers come from converter runs this repo does not have. This
  is one instance of folder 1's summary-vs-report mismatch; folder 1's run id is
  what stops the next pair being compared by accident.

## 11. Reproducing

```powershell
cd c:\repos\homelab\universal\gitops-factory
python -m pytest output-showcase/ERRORS_NEW_ANALYSIS/Pod_Template_Metadata_Name_Namespace_Lost/test/ -v
python run_all.py --cluster-scoped --input namespaces-showcase --output <scratch>\out22
```

---

**VERIFIED_AND_FIXED**

---

## ADDENDUM — the deferred scope gap is now closed

§9 recorded `Job` / `CronJob` pod templates as outside the rule and left them
alone for want of an observed defect. They are now covered, because the on-prem
manifests this analysis actually came from are not in this corpus and "no
observed defect" only ever meant "not observed *here*".

`_pod_template_metadatas(kind, spec)` returns every pod-template `metadata` an
object can hold — `spec.template` for Deployment/StatefulSet/DaemonSet/**Job**,
and `spec.jobTemplate.spec.template` for a **CronJob**. All four inert keys are
stripped from each.

The CronJob nesting is the part that fails silently: a rule written against the
shallow `spec.template` path runs, reports nothing, and fixes nothing. Measured
before and after:

```
before   Job     pod-template metadata after sanitize: {'name': 'j', 'namespace': 'ns'}
         CronJob pod-template metadata after sanitize: {'name': 'c', 'namespace': 'ns'}
after    Job     pod-template metadata after sanitize: {}
         CronJob pod-template metadata after sanitize: {}
```

Two tests here, one in folder 23 for the `ownerReferences` half.

## ADDENDUM 2 — the 12 previous-round failures in §7 are fixed

§7 measured them and correctly attributed none of them to this folder. They have
since been worked through. All 12 were **stale assertions superseded by later,
deliberate decisions** — no converter defect among them. Each was traced before
being changed:

| Tests | Was | Why it changed |
|---|---|---|
| 3 | "this repo has no X at all" tripwires | later rounds added X **as a deliberate fixture** (an OpenShift Route with a `haproxy.router` annotation; `externalTrafficPolicy: Local`; one zero-width character as standing coverage). Each was traced raw → values → render first: all three are carried, so the tripwires were narrowed to the known fixture rather than deleted |
| 4 | `argocd.argoproj.io/instance` must be carried | moved into `CHART_OWNED_LABELS`: ArgoCD stamps it at sync time and a chart rendering a guessed Application name would fight its tracking. A stale docstring in `_eso_metadata` still argued the opposite and was corrected too |
| 3 | `zookeeper-zookeeper-config` | the doubled prefix **was** the bug; `Helm_Naming_Prefix_Collisions` replaced it with `zookeeper-config-2` |
| 1 | port key `port-9000` | keys gained a `-<protocol>` suffix, which is the fix for the silently dropped `9125/UDP` |
| 1 | round-trip label check | same instance-label decision as above |

And the reason a round could sit at 12 failures unnoticed: **the full suite could
not be run in one command.** Both rounds carry a `test_zero_width.py` and a
`test_reference_integrity.py`, and pytest's default import mode aborts collection
on the basename clash. A `pytest.ini` setting `--import-mode=importlib` fixes it.

```
python -m pytest -q          # from universal/gitops-factory
817 passed
```
