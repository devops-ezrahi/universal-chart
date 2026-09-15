# STATUS — Route_Governance_Labels_Lost (TIER 0, folder 3/26)

**Result: FIXED.** The largest single error class in the report — 613–705
occurrences, more than every other defect combined. Cause confirmed as a
half-fix: `build_route_entry` carried a comment saying labels were handled and
never made the call. One line restored, one label suppressed on both sides, and
the corpus given its first-ever Route so the standing check can see this class
at all. 19 new tests RED→GREEN, reproduced end-to-end as 7 real fidelity ERRORs
before the fix and 0 after. Chart's 234 tests green, sanitizer's 41 green,
`helm lint` clean.

---

## 1. The disputed decision, settled

`error.txt`: *"RESOLVE THIS FIRST — it determines whether the remaining error
count lands near 80 or near 700, and every other number in the report depends
on it."* The report gives three incompatible instructions:

| Source | Instruction |
|---|---|
| remediation table | **fix** — "Fix custom labels on Routes (~613) \| -613 \| 80 remaining" |
| summary table | **suppress** — "Route labels \| 705 \| Suppress in `_fidelity_ignored_path()` for Routes specifically" |
| technical note | neither — "Route metadata.labels are NOT suppressed by design" |

**Decision: FIX.** Three reasons, in order of weight:

1. **The chart was already waiting for the value.** `templates/route.yaml` and
   `templates/routes.yaml` both render
   `include "universal-chart.objectLabels" (dict "root" $ "own" .labels)`.
   Half the work was done and shipped; only the converter side was missing.
   Suppressing would have meant writing code to hide a defect that was one line
   from being fixed.
2. **The report's own priority table rates it P1** and names the consequence:
   "Breaks ArgoCD sync tracking, team/env filtering".
3. **A functional loss is hiding in the bucket.**
   `opentelemetry.io/enabled` (36 occurrences, 5 namespaces) is not governance
   metadata — losing it disables OpenTelemetry instrumentation on those Routes.
   A blanket suppression would have hidden that permanently.

**One exception, and the report identifies it correctly.**
`argocd.argoproj.io/instance` (92) is marked "Unavoidable — ArgoCD injects at
deploy". A chart cannot know which Application will sync it, and rendering a
guess would fight ArgoCD's own tracking on the next sync. That key alone is
suppressed. *"The other ~520 are not."*

## 2. Root cause — a comment was written, the line was not

`grep -n "raw_labels("` returns 20 hits across 14 handlers. Line 1108 was the
only one that was a comment rather than a call:

```python
def build_route_entry(doc: dict, ctx: MicroserviceContext) -> dict:
    # labels handled at the end of this function via raw_labels(doc)
    ...
    return entry          # raw_labels() never called
```

`git diff` shows that comment being *added* by the same change that added the
real `entry["labels"] = raw_labels(doc)` line to Service, ConfigMap, Secret,
ExternalSecret, PVC, NetworkPolicy, Role, RoleBinding, ServiceAccount,
ServiceMonitor, CronJob, Job, workloads and cluster-scoped objects. Route got
the note.

Nothing contradicted it, because `namespaces-showcase` contained **zero
Routes** — so the fidelity report read 0 for this class whether the code worked
or not.

## 3. Files changed

| File | Lines | Change |
|---|---|---|
| `universal/gitops-factory/convert_to_universal_chart.py` | 1107–1108 | delete the false comment |
| `universal/gitops-factory/convert_to_universal_chart.py` | 1129–1140 (new) | `entry["labels"] = raw_labels(doc)` — the missing line |
| `universal/gitops-factory/convert_to_universal_chart.py` | 1094–1103 | `CHART_OWNED_LABELS` += `argocd.argoproj.io/instance` (stop capturing it) |
| `universal/gitops-factory/convert_to_universal_chart.py` | 2922–2930 | `FIDELITY_HELM_LABELS` += `argocd.argoproj.io/instance` (stop reporting it) |
| `universal/gitops-factory/namespaces-showcase/identity-auth-prd/opa-prd.yaml` | 65–110 (new) | the corpus' first Route, carrying all seven governance labels + the ArgoCD one |
| `universal/gitops-factory/output-showcase/**` | regenerated | consequence |

### Why the suppression needed both halves

Excluding the ArgoCD label from capture alone would move the noise rather than
remove it: the render would lack the label, and the checker would report it as
LOST on all 92 Routes. Suppressing in the checker alone would let the chart
render a guessed Application name. Both, and only that one key.

### Where the corpus Route went, and why not the obvious place

First attempt appended it to `shop-web/storefront.yaml`, which failed
immediately:

```
helm template failed for storefront: Error: execution error at
(super-universal-chart/templates/statefulset.yaml:1:4):
ingress.enabled and route.enabled cannot both be true — pick one
```

A useful guardrail doing its job. Moved to `identity-auth-prd/opa-prd.yaml`,
which is the one workload in the corpus with no Ingress **and** which exists in
only one namespace — so there is no shared base for "Route in prd, Ingress in
dev" to fight over.

## 4. RED — the defect reproduced end to end

With the corpus Route in place and `entry["labels"] = raw_labels(doc)` removed
from a throwaway copy of the converter:

```
$ python <scratch>/red_route.py --input namespaces-showcase --output <tmp> \
    --chart ../Universal-chart --verify-fidelity --cluster-scoped

  123 objects matched (28 legitimately relocated).
  7 potential fidelity issue(s) written to ...

ERROR BREAKDOWN BY CATEGORY (generated from the lines below):
  field_lost_critical     1
  field_lost_environment  1
  field_lost_io/enabled   1
  field_lost_io/runtime   1
  field_lost_operational  1
  field_lost_purpose      1
  field_lost_team         1

  ERROR  Route/opa-route LOST at metadata.labels.team: raw had 'identity-auth'
  ERROR  Route/opa-route LOST at metadata.labels.environment: raw had 'prd'
  ERROR  Route/opa-route LOST at metadata.labels.critical: raw had 'true'
  ERROR  Route/opa-route LOST at metadata.labels.operational: raw had 'false'
  ERROR  Route/opa-route LOST at metadata.labels.operational.purpose: raw had 'policy-decision-endpoint'
  ERROR  Route/opa-route LOST at metadata.labels.app.openshift.io/runtime: raw had 'golang'
  ERROR  Route/opa-route LOST at metadata.labels.opentelemetry.io/enabled: raw had 'true'
```

These lines match the report's own shape verbatim — compare its line 423:
`Route/midasson-api-yellow-b2c-fac1 LOST at metadata.labels.team: raw had
'rakia_app_a'`. Same defect, same object kind, same field paths.

`argocd.argoproj.io/instance` is present on the raw Route and is **absent from
this list** — the suppression already working, in the RED run, which is what
proves the 7 above are the unsuppressed remainder rather than an artefact.

Unit tests RED in the ordinary way: `test_build_route_entry_captures_governance_labels`
and the 7 parametrised per-label tests all failed on `entry.get("labels") is None`.

## 5. GREEN

```
$ python convert_to_universal_chart.py --input namespaces-showcase --output <tmp> \
    --chart ../Universal-chart --verify-fidelity --cluster-scoped

  123 objects matched (28 legitimately relocated).
  No fidelity issues found.
```

Generated values (`base/opa.yaml`):

```yaml
route:
  annotations:
    haproxy.router.openshift.io/balance: roundrobin
  enabled: true
  host: opa.apps.example.com
  labels:
    app.openshift.io/runtime: golang
    critical: 'true'
    environment: prd
    opentelemetry.io/enabled: 'true'
    operational: 'false'
    operational.purpose: policy-decision-endpoint
    team: identity-auth
  name: opa-route
  serviceName: opa
```

`helm template` of that release:

```yaml
kind: Route
metadata:
  name: opa-route
  labels:
    app.kubernetes.io/instance: opa
    app.kubernetes.io/managed-by: Helm
    app.kubernetes.io/name: opa
    app.kubernetes.io/version: 2.0.0
    app.openshift.io/runtime: golang
    critical: "true"
    environment: prd
    helm.sh/chart: super-universal-chart-1.0.0
    opentelemetry.io/enabled: "true"
    operational: "false"
    operational.purpose: policy-decision-endpoint
    team: identity-auth
```

All seven governance labels present; the chart's own identity labels intact
alongside them; `argocd.argoproj.io/instance` correctly absent.

```
$ python -m pytest .../Route_Governance_Labels_Lost/test/test_route_labels.py -q
...................                                                      [100%]
19 passed in 1.60s
```

## 6. Tests created

`ERRORS_NEW_ANALYSIS/Route_Governance_Labels_Lost/test/test_route_labels.py`
— 19 tests in 4 groups:

1. **Converter** (11) — captures the label set; each of the seven labels
   parametrised individually, because the report's nine per-label triage files
   are nine views of this one defect and a future filter dropping one key should
   name it; extra Routes (`.Values.routes`) carry labels too, not just the
   primary; an unlabelled Route gets no `labels` key rather than an empty one.
2. **The suppression, scoped** (4) — the ArgoCD label is neither captured nor
   reported; a genuinely lost governance label still reports; and
   `app.kubernetes.io/applicationSet` is **not** suppressed despite the
   ArgoCD-ish name — the report's field table calls it "ArgoCD-managed", but it
   is set by the source org's ApplicationSet template, i.e. custom metadata the
   chart must carry. Suppressing it on name resemblance would silently drop 75
   real labels.
3. **Template** (3) — both Route templates render the labels via `helm
   template`, and custom labels merge with rather than clobber the chart's
   identity labels.
4. **Corpus coverage** (2) — a Route exists in `namespaces-showcase` at all,
   and it still carries the label set the report names. These fail if someone
   removes the coverage, which is exactly how this defect survived a round.

## 7. Validation chain

| Check | Result |
|---|---|
| Converter re-run, showcase | 123 objects matched, **no fidelity issues** |
| `helm template` of the affected release | 7/7 governance labels rendered |
| Folder's own tests | 19 passed |
| All `ERRORS_NEW_ANALYSIS` tests | 69 passed |
| `sanitize_fidelity_report` suite | 41 passed |
| `helm unittest Universal-chart` | 234 passed, 28 suites |
| `helm lint Universal-chart` | 0 failed |

**Regeneration churn is fully attributable.** Diffed the regenerated
`output-showcase/` against folder 2's run: the only content differences are
`base/opa.yaml` (the new `route:` block) and `report/fidelity_report.txt`
(object count 122 → 123, plus the run id, which moves by design because the
converter and the corpus both changed). Every other generated file is
byte-identical.

## 8. What this does NOT fix

- **The 613–705 occurrences in the `rakia-*` report.** Those manifests are not
  in this repo. What is proven is that the code path they take now carries
  labels, for every label the report names, through converter and chart.
- **`app.kubernetes.io/part-of` (80) and `app.kubernetes.io/applicationSet`
  (75)** are not separately tested by name — they are ordinary custom labels
  and are covered by the general capture. Only `applicationSet` gets its own
  test, because its name invites a wrong suppression.
- **Nothing about CronJob pod-template labels.** The report's per-label triage
  files split each label's occurrences into resource-level (this folder) and
  CronJob pod-template-level. The latter is folder 5 and was not touched — see
  §9.

## 9. OBSERVED, DEFERRED

- **`CronJob_Pod_Template_Labels_Lost` (folder 5)** — confirmed present:
  `handle_cronjob` calls `raw_labels(doc)` for the CronJob's own labels but
  never reads `spec.jobTemplate.spec.template.metadata.labels`. Same root class
  (RC-4, per-handler fixes), different handler. Not touched.
- **`Service_AllocateLoadBalancerNodePorts_Lost` (folder 9)** and
  **`Deployment_MinReadySeconds_Lost` (folder 7)** — still confirmed absent from
  their whitelists. Not touched.
- **The chart's ingress/route mutual exclusion** is a real constraint on where
  corpus Routes can live (§3). Worth knowing before adding more Route coverage
  in later folders.

## 10. Reproducing

```powershell
python -m pytest universal/gitops-factory/output-showcase/ERRORS_NEW_ANALYSIS/Route_Governance_Labels_Lost/test/test_route_labels.py -v

cd universal/gitops-factory
python convert_to_universal_chart.py --input namespaces-showcase --output <tmp> `
  --chart ../Universal-chart --verify-fidelity --cluster-scoped
Select-String <tmp>/base/opa.yaml -Pattern "labels" -Context 0,8
```
