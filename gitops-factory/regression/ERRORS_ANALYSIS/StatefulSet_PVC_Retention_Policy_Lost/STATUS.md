# STATUS — StatefulSet_PVC_Retention_Policy_Lost (P0, folder 1/35)

**Result: FIXED.** Two-sided defect confirmed by direct grep, fixed on both
sides, 13 new tests RED→GREEN, chart's own 234-test suite still green, fidelity
report unchanged at 0 lost / 0 changed.

---

## 1. Where the repo actually is

`error.txt` cites paths under `C:\repos\metroair-argocdnew\`. That project is not
this repo. The equivalents here, and the paths every command below uses:

| error.txt path | actual path in this repo |
|---|---|
| `convert_to_universal_chart.py` | `universal/gitops-factory/convert_to_universal_chart.py` |
| `Universal-chart/templates/statefulset.yaml` | `universal/Universal-chart/templates/statefulset.yaml` |
| `namespaces/interconn/` | `universal/gitops-factory/namespaces-showcase/` (see §6) |
| `values/report/fidelity_report.txt` | `universal/gitops-factory/output-showcase/report/fidelity_report.txt` |

`ERRORS_ANALYSIS/` is **not** at the repo root — it is at
`universal/gitops-factory/output-showcase/ERRORS_ANALYSIS/`.

## 2. Were the line-number hints accurate?

**Yes, essentially.** error.txt said:

> Locations: convert_to_universal_chart.py handle_workload() field whitelist
> (line 665 onward); Universal-chart/templates/statefulset.yaml (69 lines, zero
> references to the field).

Verified before trusting:

- `def handle_workload(...)` was at **line 663**, not 665 — a 2-line drift. The
  StatefulSet-specific whitelist block (`if kind == "StatefulSet":`) was at
  **lines 676–694**, and it extracted exactly two things: `podManagementPolicy`
  and `volumeClaimTemplates`. `persistentVolumeClaimRetentionPolicy` was absent.
- `statefulset.yaml` was **exactly 69 lines** and contained **zero** references
  to the field. Hint exact.

Confirmed the field appears nowhere at all in the project before the fix:

```
$ grep -ri "persistentVolumeClaimRetentionPolicy" universal/
universal/gitops-factory/output-showcase/ERRORS_ANALYSIS/... (error.txt only)
```

No hit in the converter, the chart, the values, or any raw manifest.

## 3. Files changed

| File | Lines | Change |
|---|---|---|
| `universal/gitops-factory/convert_to_universal_chart.py` | 686–697 (new) | Extract `spec.persistentVolumeClaimRetentionPolicy` inside the `if kind == "StatefulSet":` block (comment 686–694, code 695–697) |
| `universal/Universal-chart/templates/statefulset.yaml` | 20–23 (new) | Render the field under `spec:`, guarded by `{{- with }}` |
| `universal/Universal-chart/values.yaml` | 422–431 (new) | Declare `persistentVolumeClaimRetentionPolicy: {}` (line 429) with the reasoning for the empty default |
| `universal/Universal-chart/values.schema.json` | 118–126 (new) | Type/enum the key: `whenDeleted`/`whenScaled` ∈ {Retain, Delete}, `additionalProperties: false` |
| `universal/gitops-factory/convert_to_universal_chart.py` | 224–231 | `NON_NAMESPACE_OUTPUT_DIRS = {"report", "ERRORS_ANALYSIS"}` (line 231) — see §7, this is cleanup of pollution my own test fixtures caused |

### Design decision inside the fix

The chart default is `{}` (field omitted), **not** `Retain`. Defaulting to
`Retain` would be safer for data but would (a) silently override the intent of
every chart consumer who wants Kubernetes' default and (b) show up as an
invented field on every raw-vs-rendered diff, converting a 0-noise fidelity
report into ~8 permanent false ADDEDs. The converter copies the raw value
verbatim, so `Retain` survives when the source declared it and nothing is
invented when it didn't. Both behaviours are asserted by tests.

Each half is copied independently — a partial policy (`whenDeleted` only) is
legal Kubernetes and the missing half must stay missing rather than being
normalised to `Delete`. Covered by `test_converter_preserves_every_policy_combination`.

## 4. Tests created

All under `ERRORS_ANALYSIS/StatefulSet_PVC_Retention_Policy_Lost/test/`:

- `fixture/platform-data/postgres.yaml` — minimal StatefulSet + headless
  Service, derived from the real
  `namespaces-showcase/platform-data/postgres.yaml`, with the retention policy
  block added (see §6 for why it had to be added).
- `test_pvc_retention.py` — 13 tests in 4 layers:
  1. converter extracts the field (and every partial combination of it)
  2. template renders it; omits it when unset; never emits it on Deployment/DaemonSet
  3. end-to-end: real CLI over the fixture → `helm template` → compare to the raw value
  4. the fidelity checker still *reports* the loss (guards against a future
     "fix" that silences the report instead of fixing the converter)
- `render_check.ps1` — the same chain as a standalone script with a printed
  side-by-side raw-vs-rendered comparison.

### RED — before the fix

```
$ python -m pytest test_pvc_retention.py -v
FAILED test_converter_extracts_retention_policy
FAILED test_converter_preserves_every_policy_combination[policy0]
FAILED test_converter_preserves_every_policy_combination[policy1]
FAILED test_converter_preserves_every_policy_combination[policy2]
FAILED test_converter_preserves_every_policy_combination[policy3]
FAILED test_converter_preserves_every_policy_combination[policy4]
FAILED test_template_renders_retention_policy
FAILED test_round_trip_raw_fixture_to_rendered_statefulset
========================= 8 failed, 5 passed in 2.47s =========================
```

Both sides demonstrably broken, independently:

```
E  AssertionError: handle_workload dropped spec.persistentVolumeClaimRetentionPolicy;
   the rendered StatefulSet will fall back to the Kubernetes default
   whenDeleted/whenScaled: Delete and destroy the PVC on delete/scale
E  assert 'persistentVolumeClaimRetentionPolicy' in {'image': ..., 'ports': ...,
   'volumeClaimTemplates': {'data': {...}}, 'volumeMounts': {...}, ...}
```

```
E  AssertionError: statefulset.yaml did not render persistentVolumeClaimRetentionPolicy;
   rendered spec keys = ['podManagementPolicy', 'replicas', 'revisionHistoryLimit',
                         'selector', 'serviceName', 'template', 'volumeClaimTemplates']
E  assert None == {'whenDeleted': 'Retain', 'whenScaled': 'Retain'}
```

The 5 tests that passed while RED are the negative/guard tests (don't invent a
policy, don't emit on Deployment, checker still reports the loss). They are
expected to pass on both sides of the fix — they exist to pin behaviour the fix
must not break, and one of them (`test_template_omits_the_field_when_values_do_not_set_it`)
would have caught a naive `Retain` default.

### GREEN — after the fix

```
$ python -m pytest test_pvc_retention.py -q
.............                                                            [100%]
13 passed in 2.14s
```

## 5. Validation chain

**1. Converter re-run** (full showcase, in place, original invocation reconstructed
from the committed ApplicationSets — repo URL, `checker` revision, values path):

```
$ python convert_to_universal_chart.py --input namespaces-showcase --output output-showcase \
    --chart ../Universal-chart --verify-fidelity \
    --values-repo-url https://github.com/devops-ezrahi/homelab.git \
    --values-revision checker --values-path universal/gitops-factory/output-showcase

Converted 20 microservices across 10 namespaces.
Rendered 24 releases.
No duplicate resource ownership detected across any of the 24 rendered releases. Conflict-free.
  122 objects matched (28 legitimately relocated).
  No fidelity issues found.
```

**2. `helm template` for the named release** (platform-data / postgres):

```
kind: StatefulSet
metadata:
  name: postgres
spec:
  serviceName: postgres
  replicas: 1
  revisionHistoryLimit: 3
  podManagementPolicy: OrderedReady
  ...
-- retention policy present in this render? --
NO - correct: raw platform-data/postgres.yaml declares no policy, so none is rendered
```

**3. Side-by-side raw vs rendered, actual values** (fixture, which *does* declare
the policy — from `render_check.ps1`):

```
== 5. side-by-side: raw vs rendered
   RAW      (fixture/platform-data/postgres.yaml):
     persistentVolumeClaimRetentionPolicy:
     whenDeleted: Retain
     whenScaled: Retain
   RENDERED (helm template):
     persistentVolumeClaimRetentionPolicy:
     whenDeleted: Retain
     whenScaled: Retain
== 6. exact value assertion
PASS: raw whenDeleted/whenScaled: Retain survived converter + helm template.
EXITCODE=0
```

Before the fix the RENDERED half of that block was `<ABSENT>`.

**4. Fidelity report, before vs after:**

```
$ Compare-Object (committed fidelity_report.txt) (post-fix fidelity_report.txt)
IDENTICAL - no line differs

objects matched:            122
  of those, relocated:      28
fields LOST (raw -> render): 0
fields CHANGED:             0
fields ADDED (chart default, informational): 275
objects in raw but rendered by nothing:      0
objects rendered with no raw source:         0
render failures:            0
```

This defect's count was **0 before and 0 after** — see §6, no showcase manifest
exercises it. Critically, **no other count went up**: the report is byte-identical,
and every generated values file is byte-identical to what was committed
(verified by per-file hash comparison across the whole `output-showcase` tree).

**5. `helm lint`:**

```
$ helm lint Universal-chart
[INFO] Chart.yaml: icon is recommended
1 chart(s) linted, 0 chart(s) failed

$ helm lint Universal-chart --set workload.type=statefulset --set image.repository=postgres \
    --set persistentVolumeClaimRetentionPolicy.whenDeleted=Retain \
    --set persistentVolumeClaimRetentionPolicy.whenScaled=Retain
1 chart(s) linted, 0 chart(s) failed
```

The second form also exercises the new `values.schema.json` block (helm validates
values against the schema during lint), proving the enum accepts `Retain`.

**6. Chart's own regression suite** — `helm unittest`, unchanged by this fix:

```
Charts:      1 passed, 1 total
Test Suites: 28 passed, 28 total
Tests:       234 passed, 234 total
```

## 6. Important caveat — the showcase data does not exercise this defect

`error.txt` names 6–8 StatefulSets (`jaeger-es-master`, `zookeeper`,
`prometheus-prometheus`, `rfr-httpproxy-redis-redis-dba`,
`postgres-try-postgresql`, `interconn-opr-postgres`) from the `interconn`
namespace of the original metroair project. **None of those manifests are in
this repo.** A grep proves no raw manifest here declares the field at all:

```
$ grep -ril "persistentVolumeClaimRetentionPolicy" universal/gitops-factory/namespaces*/
(no matches)
```

Consequences, stated plainly:

- The committed fidelity report shows **0** for this defect class both before and
  after, because there is no raw input carrying the field to lose. The report
  cannot be used as before/after evidence here.
- The evidence that the defect was real is therefore **the code itself** (grep:
  zero references in converter and template) plus the **RED test output** above,
  which reproduces the loss on a fixture.
- The fixture is derived from the real
  `namespaces-showcase/platform-data/postgres.yaml` with the retention block
  added, matching the shape error.txt quotes verbatim. This is the one place I
  did not copy a raw manifest unmodified — because no raw manifest here contains
  the field. The raw manifests themselves were **not** modified.

## 7. Side effect I caused, and cleaned up

Adding `test/fixture/platform-data/postgres.yaml` under
`output-showcase/ERRORS_ANALYSIS/` made the converter's stale-output scanner
read `ERRORS_ANALYSIS` as a namespace directory that had vanished from the input,
and report every fixture under it as a stale generated file:

```
+ERRORS_ANALYSIS\StatefulSet_PVC_Retention_Policy_Lost\test\fixture\platform-data\postgres.yaml
```

With 35 folders of fixtures to come, that report would become useless. Fixed at
the existing seam: `NON_NAMESPACE_OUTPUT_DIRS` (which already excluded `report/`)
now also excludes `ERRORS_ANALYSIS`. This is not scope creep into another defect
— it is repairing pollution introduced by this folder's own test artifacts.

`report/stale_output.txt` was restored to its committed content afterwards, so
the working tree carries no incidental diff.

## 8. What could NOT be verified without a live cluster

Explicitly, and these are the whole point of the defect:

- **That Kubernetes actually retains the PVC.** That `whenDeleted: Retain`
  causes the StatefulSet controller to leave the PVC in place on delete is
  Kubernetes behaviour, not chart behaviour. I verified only that the field
  reaches the rendered manifest with the correct value.
- **That the PVC survives an ArgoCD sync/recreate cycle**, the scenario error.txt
  describes as the actual destruction path. Requires a cluster and ArgoCD.
- **That the API server accepts the rendered StatefulSet.** `kubectl apply
  --dry-run=client` still fetches the OpenAPI schema from the API server, so it
  cannot run offline — it is attempted by `render_check.ps1` step 7 and reported
  as SKIP, never as a pass. The structural guarantee I do have is that
  `helm template` output parses as YAML and the field sits at `spec.` with
  schema-validated enum values.
- **Whether the 8 real StatefulSets named in error.txt are fixed**, since those
  manifests are not in this repo (§6). The code path they would take is now
  covered by tests.

## 9. OBSERVED, DEFERRED

Noticed while working; code left untouched, belongs to other folders or is out
of scope:

- **`StatefulSet_ServiceName_Hardcoded_To_Fullname` (folder 2)** — confirmed
  present at `templates/statefulset.yaml:14`:
  `serviceName: {{ include "universal-chart.fullname" . }}`. The raw
  `platform-data/postgres.yaml` declares `serviceName: postgres` and the
  converter never reads it. Not touched.
- **`Main_Container_Name_Forced_To_Release_Name` (folder 5)** — the fidelity
  checker already suppresses this at
  `convert_to_universal_chart.py:_fidelity_ignored_path` (the
  `("containers", "[0]", "name")` tail match). Relevant to that folder's open
  decision: the checker side has *already* been made to accept the rename, so
  "fix the checker" is partly done and the real question is only whether to
  preserve the original name. Not touched.
- **`Service_Selector_Injected_When_Absent` (folder 34)** —
  `_fidelity_ignored_path` ignores `Service.spec.selector` wholesale, so the
  checker cannot currently see an injected selector at all. Worth knowing when
  that folder is reached. Not touched.
- **Converter nit, no folder:** `find_stale_output` does not clear or rewrite
  `report/stale_output.txt` when the stale set becomes empty, so the file
  persists claiming files that no longer exist. The committed copy is an example:
  it lists `defaults.yaml` and two `cluster-shared/` files that are absent from
  the committed tree (verified `Test-Path` = False for both). Harmless but
  misleading. Not touched.

## 10. Reproducing

```powershell
# unit + render + round-trip tests
python -m pytest universal/gitops-factory/output-showcase/ERRORS_ANALYSIS/StatefulSet_PVC_Retention_Policy_Lost/test/test_pvc_retention.py -v

# standalone chain with printed side-by-side comparison
& universal/gitops-factory/output-showcase/ERRORS_ANALYSIS/StatefulSet_PVC_Retention_Policy_Lost/test/render_check.ps1
```
