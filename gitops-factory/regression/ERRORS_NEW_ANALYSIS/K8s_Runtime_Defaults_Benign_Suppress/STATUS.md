# STATUS — K8s_Runtime_Defaults_Benign_Suppress (TIER 3, folder 17/26)

**Result: LADDER RE-DERIVED AND MOSTLY ALREADY DONE — three entries suppressed
here, four struck off as fixed, six deferred to their own folders.** The
highest-leverage item in the batch (~930 of 1,314 errors) and the most dangerous,
because it is a list of things to stop looking at. Measured rather than applied:
most of it was already handled, and what remained splits three ways. 18 new tests
RED→GREEN, showcase fidelity 0 lost / 0 changed, chart's 234 green.

---

## 1. Measure first

The report presents a 14-step ladder from 1,314 errors to ~45. Rather than
applying it, a probe manifest was built carrying **every field on its benign
list** — `creationTimestamp: null`, `checksum/*` annotations, Thanos
`ownerReferences`, pod-template `name`/`namespace`, `enableServiceLinks: false`,
`shareProcessNamespace: false`, `dnsPolicy`, `schedulerName`,
`terminationGracePeriodSeconds`, `terminationMessagePath`/`Policy`,
`resources: {}`, `sessionAffinity: None`, SA `secrets`/`imagePullSecrets`,
`immutable: false` — and run through the converter.

**Already suppressed, before this folder touched anything: 9 of the 14 field
groups.**

| Ladder item | Where it was already handled |
|---|---|
| `creationTimestamp` (166) | `sanitize_object` strips it from the raw doc |
| CronJob/pod defaults (44) — `dnsPolicy`, `schedulerName`, `terminationGracePeriodSeconds`, `terminationMessagePath`/`Policy` | carried by the converter, so nothing is lost |
| checksum annotations (10) | `pod_template_annotations` (folder 5) |
| VCT runtime fields (17) | `sanitize_object` (folder 8) |
| ArgoCD labels (92) | `CHART_OWNED_LABELS` + `FIDELITY_HELM_LABELS` (folder 3) |
| Thanos `ownerReferences` | `VOLATILE_METADATA_KEYS` |
| `resources: {}` | the `{} == absent` rule |

Nine field losses remained. They split three ways.

## 2. Four entries struck off — fixed, not suppressed

`error.txt`'s central warning:

> **THREE ENTRIES IN THE SUPPRESSION LIST DO NOT BELONG AND MUST BE PULLED OUT
> BEFORE IMPLEMENTING.** The bulk list was assembled by grouping adjacent field
> paths, and it swept in real losses.

All three, plus a fourth with the same problem, are **fixed** earlier in this
batch:

| Ladder entry | Verdict | Folder |
|---|---|---|
| `minReadySeconds (1)` | real loss — K8s default is **0**, not 10 | 7 |
| `VCT labels (1)` | real loss — PVCs lose quota/cost-allocation labels | 8 |
| `allocateLoadBalancerNodePorts (62)` | real loss — an explicit `false` flips behaviour | 9 |
| `parallelism (2)` | real loss at any non-default value | 16 |

The fields are now carried, so there is nothing left to suppress. Four
parametrised tests assert `_is_benign_default_loss` returns False for each, so a
future bulk rule cannot quietly re-hide them.

## 3. Three entries suppressed here

`_is_benign_default_loss(kind, path, value)` — keyed on kind **and** exact path
**and the value**.

| Rule | Condition, verified |
|---|---|
| `Service.spec.sessionAffinity == "None"` | literally the Kubernetes default. `ClientIP` is a real setting that `build_service_entry` carries, and is **not** suppressed — the previous round rated its loss MEDIUM ("Load balancer session affinity broken") |
| `ServiceAccount.secrets` | Kubernetes creates and rotates SA token Secrets itself |
| `ServiceAccount.imagePullSecrets` | OpenShift's `<sa>-dockercfg-XXXXX` names an object that does not exist on a vanilla target; `handle_serviceaccount` carries a hand-added credential and drops only these |

**Value-keying is the whole discipline.** The report's list was assembled by
grouping fields that sit near each other *in the pod spec*, which is precisely
how it swept up three real losses. Grouping by *"does losing this do anything"*
cannot make that mistake — and a path-only rule for `sessionAffinity` would have
hidden a lost `ClientIP`, reintroducing a defect the previous round fixed.

Result on the probe: **9 → 6 reported losses**, with the correct six left.

## 4. Six deferred — deliberately not decided here

| Field | Folder |
|---|---|
| `enableServiceLinks: false` | 20 |
| `shareProcessNamespace: false` | 21 |
| pod-template `metadata.name` | 22 |
| pod-template `metadata.namespace` | 22 |
| pod-template `metadata.creationTimestamp` | 22 |
| `Secret.immutable: false` | "remaining noise" |

All six are on the report's benign list and all are plausibly suppressible. Each
has its own folder with its own stated condition to verify — and `error.txt`
flags at least one where the report's *reasoning* is factually wrong even if its
verdict is right (`enableServiceLinks`: the Kubernetes default is **true**, so
an explicit `false` is a deliberate opt-out, not a redundant restatement).

Deciding them from inside a bulk rule is exactly the mistake that produced the
three bad entries in §2. Five tests assert each is still open, and they are the
tests a later folder updates when it makes its call.

## 5. Files changed

| File | Lines | Change |
|---|---|---|
| `universal/gitops-factory/convert_to_universal_chart.py` | ~3455–3492 (new) | `_is_benign_default_loss()` |
| `universal/gitops-factory/convert_to_universal_chart.py` | ~3552 | consulted in `_fidelity_diff`'s LOST branch |

No corpus change and no chart change. The probe manifest lives in the scratch
directory: it exists to *measure* the ladder, and adding a workload whose only
purpose is to carry fourteen inert fields would make the corpus less
representative of real input, which is the property that gives it its value.

## 6. Validation

```
probe manifest, before:  9 field losses reported
probe manifest, after:   6 field losses reported   (the six deferred, §4)

sessionAffinity None      -> suppressed
sessionAffinity ClientIP  -> NOT suppressed
SA secrets                -> suppressed
Deployment sessionAffinity-> NOT suppressed (wrong kind)
minReadySeconds           -> NOT suppressed (real loss, folder 7)
```

| Check | Result |
|---|---|
| Converter re-run, showcase | 128 objects matched, **no fidelity issues** |
| Folder's own tests | 18 passed |
| All `ERRORS_NEW_ANALYSIS` tests | 286 passed |
| `helm unittest Universal-chart` | 234 passed, 28 suites |

## 7. What the ladder actually looks like now

The report's headline claim was: *"After suppressing all noise, ~45 errors
remain"*, reached largely by suppression and by one big fix (Route labels, 613).

On the evidence of this batch the shape is different. Of the ladder's ~1,269
suppressed errors, the great majority were **already** not reported by this
converter; of the rest, four groups turned out to be real losses that are now
**fixed** rather than hidden; and only three needed a suppression rule.

That is a better outcome than the ladder predicted, and it is worth saying why:
the ladder was computed from a report, and a report cannot distinguish "this
field is inert" from "this field was lost and nobody checked what it does". Only
running the code can, which is what folder 1's run-identity work makes
repeatable.

## 8. What could NOT be verified

- **The counts.** Every number in the ladder is from the `rakia-*` run. The
  probe reproduces each field *class*, not the volume.
- **`sessionAffinity: ClientIP` in the real input.** `error.txt` asks to confirm
  none is set before suppressing globally. Unanswerable from here — which is why
  the rule is scoped to the value instead, making the answer unnecessary.
- **Whether the six deferred fields matter in the target.** That is each folder's
  question, and three of them need the cluster.

## 9. OBSERVED, DEFERRED

- **`_is_benign_default_loss` is the right home for folders 20–22's verdicts** if
  they land on "suppress". The function is written to be extended one verified
  condition at a time, and its docstring says so.
- **`Grafana_Ini_Changed_False_Positive` (folder 25)** appears in the same
  suppression list ("grafana.ini false positive (1)"). Not touched — `error.txt`
  is explicit that "visually identical" is not a standard and the bytes must be
  diffed first.

## 10. Reproducing

```powershell
python -m pytest universal/gitops-factory/output-showcase/ERRORS_NEW_ANALYSIS/K8s_Runtime_Defaults_Benign_Suppress/test/test_benign_suppression.py -v

# the probe manifest carrying every field on the report's benign list
python convert_to_universal_chart.py --input <scratch>/probe_suppress --output <tmp> `
  --chart ../Universal-chart --verify-fidelity --cluster-scoped
```
