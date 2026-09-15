# STATUS — Deployment_MinReadySeconds_Lost (TIER 1, folder 7/26)

**Result: FIXED — preserved, not suppressed.** The report contradicts itself on
this field; the line-by-line pass ("Should be preserved") is right and the bulk
suppression pass is wrong. One occurrence, small fix, real safety payoff — and a
textbook instance of the recurrence pattern behind this whole round. 10 new
tests RED→GREEN, showcase fidelity 0 lost / 0 changed, chart's 234 green, lint
clean.

---

## 1. The contradiction, resolved

| Pass | Says |
|---|---|
| line-by-line | "MEDIUM impact. [...] Losing this means rolling updates will proceed immediately, potentially causing brief downtime. **Should be preserved.**" |
| bulk suppression | "Pod template defaults \| ~12 \| `spec/template/spec/enableServiceLinks`, `shareProcessNamespace`, **`minReadySeconds`** on Deployments" |

**PRESERVE.** And the reason is not a judgement call:

- `enableServiceLinks: false` and `shareProcessNamespace: false` restate what is
  already effectively in force for those workloads — dropping them changes
  nothing observable.
- **`minReadySeconds`' Kubernetes default is 0.** There is no default worth
  losing. An explicit `10` is a deliberate rollout-safety setting, and dropping
  it means a rolling update advances to the next pod the instant the current one
  reports Ready, with no soak. For a gateway — the report's affected object is
  `gateway-tyk-gateway-dev` — that is precisely the workload where a too-fast
  rollout drops in-flight connections.

`error.txt` names the mechanism of the mistake exactly: the suppression list
"groups minReadySeconds with genuinely inert fields purely because they sit near
each other in the pod spec". It does not even sit in the same *spec*:
`enableServiceLinks` and `shareProcessNamespace` are pod-spec fields;
`minReadySeconds` is a Deployment-spec field. Adjacency in a report table is not
adjacency in the API.

## 2. Why it was missing at all

`minReadySeconds` sits **one line** from `progressDeadlineSeconds` in
`handle_workload`. The previous round fixed `progressDeadlineSeconds`
(`ERRORS_ANALYSIS/ProgressDeadlineSeconds_Lost`) and did not touch this one,
because the converter captures fields by explicit enumeration.

This is RC-4 from `README_REGRESSION_ANALYSIS.md` in its purest form: under an
allow-list, *"we fixed field X"* never generalises to field Y. Four folders in
this batch are the same shape (`Service_AllocateLoadBalancerNodePorts_Lost`,
`EnableServiceLinks_Lost`, `ShareProcessNamespace_Lost`,
`CronJob_Parallelism_Lost`).

## 3. Files changed

| File | Lines | Change |
|---|---|---|
| `universal/gitops-factory/convert_to_universal_chart.py` | ~897–909 (new) | capture `spec.minReadySeconds` |
| `universal/Universal-chart/templates/deployment.yaml` | 26–35 (new) | render it under `spec:`, omitted when unset |
| `universal/Universal-chart/values.yaml` | 53–60 (new) | declare `minReadySeconds: ""` with the reasoning |
| `universal/Universal-chart/values.schema.json` | 35 (new) | integer `minimum: 0`, or empty string |
| `universal/gitops-factory/namespaces-showcase/shop-web/api-gateway.yaml` | 8–16 | declare `minReadySeconds: 10` on the gateway |
| `universal/gitops-factory/output-showcase/**` | regenerated | consequence |

### Two details that are not incidental

**Captured with `in`, not truthiness.** `if spec.get("minReadySeconds")` would
drop an explicit `0` — and dropping an explicit zero is the entirety of the
previous round's `Subtract_Defaults_Removes_Explicit_Zero`. Pinned by
`test_an_explicit_zero_is_captured_not_treated_as_absent`.

**Schema `minimum: 0`, not `minimum: 1`.** The neighbouring
`progressDeadlineSeconds` uses `minimum: 1`, and copying that would have made a
legal, meaningful value fail `helm lint`. Pinned by `test_the_schema_accepts_zero`,
which runs a real `helm template --set minReadySeconds=0` — Helm validates values
against the schema, so the test exercises the schema rather than asserting its
text.

## 4. RED

```
$ python <scratch>/red7.py --input namespaces-showcase --output <tmp> \
    --chart ../Universal-chart --verify-fidelity --cluster-scoped
  1 potential fidelity issue(s)

  field_lost_minReadySeconds  1

  ERROR  Deployment/api-gateway LOST at spec.minReadySeconds: raw had 10
```

Matches the report's field-table row `spec.minReadySeconds | 1 |
gateway-tyk-gateway-dev`.

## 5. GREEN

```
$ python convert_to_universal_chart.py --input namespaces-showcase --output <tmp> \
    --chart ../Universal-chart --verify-fidelity --cluster-scoped
  123 objects matched (28 legitimately relocated).
  No fidelity issues found.
```

`helm template` of the affected release:

```yaml
kind: Deployment
  revisionHistoryLimit: 3
  minReadySeconds: 10
```

```
$ python -m pytest .../Deployment_MinReadySeconds_Lost/test/test_min_ready_seconds.py -q
..........                                                               [100%]
10 passed in 1.91s
```

## 6. Tests created

`ERRORS_NEW_ANALYSIS/Deployment_MinReadySeconds_Lost/test/test_min_ready_seconds.py`
— 10 tests:

**Converter** (4) — captured; an explicit `0` survives; absent stays absent (so
the chart never invents a field); and it is not swept away when a genuinely
inert neighbour (`shareProcessNamespace: false`) is present on the same object —
the specific mistake `error.txt` identifies.

**Template** (5) — rendered when set; omitted when unset; omitted for the
`""` default that `values.yaml` ships; the schema accepts `0`; and it is **not**
rendered on a StatefulSet, where `minReadySeconds` is a different, beta-gated
field the converter deliberately does not read.

**Corpus** (1) — a Deployment declares it, with a **non-zero** value. A zero
would prove nothing: losing a 0 is invisible to the round-trip check, so the
coverage has to carry a value whose loss is detectable.

## 7. Validation chain

| Check | Result |
|---|---|
| RED | 1 error — `LOST at spec.minReadySeconds: raw had 10` |
| Converter re-run, showcase | 123 objects matched, **no fidelity issues** |
| `helm template` | `minReadySeconds: 10` present |
| Folder's own tests | 10 passed |
| All `ERRORS_NEW_ANALYSIS` tests | 126 passed |
| `helm unittest Universal-chart` | 234 passed, 28 suites |
| `helm lint Universal-chart` | 0 failed |

## 8. What could NOT be verified

- **`Deployment/tyk-yuval` / `gateway-tyk-gateway-dev`.** Not in this repo. The
  code path is now covered on both sides.
- **That Kubernetes honours the soak.** Cluster behaviour, not chart behaviour.
  Verified only that the field reaches the rendered manifest with the raw value.
- **Whether 10s is the right value.** Not this converter's business — it carries
  what the source declared, which is the entire point of the fix.

## 9. OBSERVED, DEFERRED

- **`EnableServiceLinks_Lost` (folder 20)** and **`ShareProcessNamespace_Lost`
  (folder 21)** are the two fields this one was wrongly bucketed with.
  `shareProcessNamespace` **is** already captured by `handle_workload`;
  `enableServiceLinks` is not. Both left alone — their verdicts (probably
  suppress) are genuinely different from this one's, which is the whole reason
  the bucket was wrong.
- **`K8s_Runtime_Defaults_Benign_Suppress` (folder 17)** must now drop
  `minReadySeconds (1)` from its suppression list. Recorded for that folder; not
  edited here.

## 10. Reproducing

```powershell
python -m pytest universal/gitops-factory/output-showcase/ERRORS_NEW_ANALYSIS/Deployment_MinReadySeconds_Lost/test/test_min_ready_seconds.py -v
```
