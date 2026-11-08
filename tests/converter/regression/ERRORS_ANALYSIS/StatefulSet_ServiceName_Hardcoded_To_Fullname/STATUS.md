# STATUS — StatefulSet_ServiceName_Hardcoded_To_Fullname (P0, folder 2/35)

**Result: FIXED** (sub-case (a); sub-cases (b) and (c) are not reproducible in
this repo — see §6). 9 new tests RED→GREEN, folder 1 still green, chart's own
234 tests still green, fidelity unchanged at 0 lost / 0 changed.

---

## 1. Were the error.txt hints accurate?

**Yes — all three, exactly.** Verified before trusting:

| Hint | Reality |
|---|---|
| `statefulset.yaml` line 14 hardcodes `serviceName: {{ include "universal-chart.fullname" . }}` | **Exact.** Line 14, character for character. |
| `handle_workload()` around lines 678–696 is the fix site | **Correct**, after folder 1 shifted it; the `if kind == "StatefulSet":` block is where it belongs. |
| Extraction table: `serviceName | No | -` | **Correct.** The converter read `podManagementPolicy` and `volumeClaimTemplates` and nothing else. |

The proposed fix in error.txt is also essentially right, and the template half I
adopted verbatim. I deviated on the converter half — see §3.

Worth noting: the same `X | default (include "universal-chart.fullname" $)`
pattern already existed in this chart at
[routes.yaml:28](../../../../Universal-chart/templates/routes.yaml#L28) for a
Route's backing service, so the fix follows an established local idiom rather
than inventing one.

## 2. Files changed

| File | Line | Change |
|---|---|---|
| `universal/gitops-factory/convert_to_universal_chart.py` | 686–702 (new) | Capture `spec.serviceName` in the StatefulSet block (comment 686–700, code 701–702) |
| `universal/Universal-chart/templates/statefulset.yaml` | 14 | `serviceName: {{ .Values.serviceName \| default (include "universal-chart.fullname" .) }}` |
| `universal/Universal-chart/values.yaml` | 42–49 (new) | Declare `serviceName: ""` (line 49) with the headless-Service rationale |
| `universal/Universal-chart/values.schema.json` | 34 (new) | `"serviceName": { "type": "string" }` |

## 3. One deliberate deviation from error.txt's proposed fix

error.txt proposes capturing **only on mismatch**:

```python
if svc_name and svc_name != ss_name:
    v['serviceName'] = svc_name
```

I capture **unconditionally**:

```python
if spec.get("serviceName"):
    v["serviceName"] = spec["serviceName"]
```

Reasoning. The conditional form is correct *today*, but only because of an
invariant that lives somewhere else in the file: `build_*` pins
`fullnameOverride` to the raw workload name whenever it differs from the
filename ([convert_to_universal_chart.py:1505](../../../convert_to_universal_chart.py#L1505)),
so `fullname` happens to resolve back to the raw StatefulSet name. Capturing on
mismatch alone silently depends on that. This entire P0 exists because an
unstated "the names will match" assumption turned out to be false — re-encoding
the same class of assumption inside its own fix is how it comes back. Pinning
the real value retires the assumption.

Cost: two extra lines in generated values (§5), and no behavioural difference
today. `test_converter_captures_service_name_even_when_it_equals_the_workload_name`
pins this choice so it cannot be quietly "optimised" back.

## 4. Tests created

`ERRORS_ANALYSIS/StatefulSet_ServiceName_Hardcoded_To_Fullname/test/`

- `fixture/platform-data/redis.yaml` — derived from the real
  `namespaces-showcase/platform-data/redis.yaml`, with one change: the governing
  Service is a dedicated `redis-cache-headless` alongside the regular ClusterIP
  `redis-cache`. That is the pattern error.txt describes for jaeger-es-master
  and zookeeper.
- `test_serviceName.py` — 9 tests across 4 layers (converter capture, template
  render + fallback, end-to-end CLI, fidelity checker still sees it).
- `render_check.ps1` — converter → `helm template` → side-by-side, **plus** a
  check that the named Service actually exists and is headless.

### RED — before the fix

```
FAILED test_converter_captures_headless_service_name
FAILED test_converter_captures_service_name_even_when_it_equals_the_workload_name
FAILED test_template_renders_explicit_service_name
FAILED test_round_trip_service_name_points_at_the_headless_service
========================= 4 failed, 5 passed in 2.60s =========================
```

Both sides broken independently:

```
test_serviceName.py:98:  AssertionError: handle_workload dropped spec.serviceName; the template
   will fall back to the release fullname and the StatefulSet will govern the regular ClusterIP
   Service instead of the headless one -> no per-pod DNS -> no cluster
test_serviceName.py:146: AssertionError: statefulset.yaml ignored .Values.serviceName and used
   the release fullname; rendered serviceName = 'redis'
```

That `'redis'` is the defect in one value: the raw manifest said
`redis-cache-headless`, the chart rendered the release name.

The 5 that passed while RED are guards (fallback-to-fullname must survive the
fix; no serviceName on Deployment; checker still reports the change).

### GREEN — after the fix

```
$ python -m pytest test_serviceName.py -q
.........                                                                [100%]
9 passed in 2.33s
```

### Accumulated regression suite (folders 1–2)

```
$ python -m pytest universal/gitops-factory/output-showcase/ERRORS_ANALYSIS -q
......................                                                   [100%]
22 passed in 4.68s
```

Folder 1 did not regress — relevant here because both fixes edit the same
`if kind == "StatefulSet":` block and the same template.

## 5. Validation chain

**1. Converter re-run:** `122 objects matched (28 legitimately relocated). No fidelity issues found.`

**2–3. `helm template` + side-by-side, every StatefulSet in the showcase:**

```
RELEASE                     RAW serviceName    RENDERED serviceName   MATCH
---------------------------------------------------------------------------
platform-data/postgres      postgres           postgres               OK
platform-data/redis         redis-cache        redis-cache            OK
platform-data-prd/postgres  postgres           postgres               OK
platform-data-prd/redis     redis-cache        redis-cache            OK
```

`platform-data/redis` is the interesting row: the raw StatefulSet is
`redis-cache` in a file called `redis.yaml`, so the release is `redis` and only
the pinned `fullnameOverride` kept the rendered name right. It now also carries
an explicit `serviceName`, so it no longer depends on that.

And on the fixture, which does use the headless split (`render_check.ps1`):

```
   RAW      spec.serviceName = redis-cache-headless
   RENDERED spec.serviceName = redis-cache-headless
   Service/redis-cache-headless rendered
   clusterIP: None confirmed - per-pod DNS records will be published
PASS
```

Before the fix, RENDERED read `redis`.

**4. Fidelity report before vs after: unchanged** — `git diff` on
`output-showcase/report/` is empty. This defect class was 0 before and 0 after
(§6), and no other count moved.

**5. Generated-output diff** — exactly two lines, both pinning a real value:

```
--- a/universal/gitops-factory/output-showcase/base/postgres.yaml
+serviceName: postgres
--- b/universal/gitops-factory/output-showcase/base/redis.yaml
+serviceName: redis-cache
```

**6. `helm lint`:** `1 chart(s) linted, 0 chart(s) failed`.

**7. Chart's own suite:** `Test Suites: 28 passed | Tests: 234 passed`. Notably
`statefulset_test.yaml:20` ("should set serviceName to fullname") still passes —
the `| default` fallback preserves it.

## 6. Sub-cases (b) and (c): not fixable here, and why

error.txt splits this defect into three, with different fixes. Only (a) is
addressed:

- **(a) headless Service exists but is not referenced** — the 6–10 StatefulSets
  in the count. **Fixed and tested above.**
- **(b) `prometheus-prometheus` → `prometheus-operated`** — that Service is
  created by the prometheus-operator at runtime and exists in no manifest, raw
  or rendered. Not a converter defect: there is nothing to carry across. The
  converter now faithfully renders `serviceName: prometheus-operated`, which is
  correct *if* the operator is installed and wrong in exactly the same way the
  raw manifest was. **No code change is possible without inventing a Service the
  source never declared.**
- **(c) `interconn-opr-postgres` → `postgres-try-postgresql-hl` in another
  namespace** — `serviceName` is a bare name resolved in the StatefulSet's own
  namespace; a cross-namespace reference is not expressible in the Kubernetes
  API. error.txt says it plainly: *"this one is broken in raw too."* The
  converter now reproduces the broken reference faithfully, which is the correct
  behaviour for a fidelity tool. **Flagging it is a migration-planning task, not
  a code fix.**

Neither (b) nor (c) has a manifest in this repo to test against.

## 7. Same caveat as folder 1 — showcase data does not exercise this

All four showcase StatefulSets use `serviceName == StatefulSet name`, so they
round-tripped correctly by coincidence and the committed fidelity report shows
**0 before and 0 after**. Proof the defect was real is the code (line 14
hardcoded, converter never read the field) plus the RED output above.

The fixture is derived from the real `redis.yaml` with the headless split added.
Raw manifests were **not** modified.

## 8. What could NOT be verified without a live cluster

- **That pod DNS actually resolves.** `<pod>.<serviceName>.<ns>.svc` producing
  per-pod A records is kube-dns/CoreDNS behaviour. Per the brief, I tested the
  closest observable proxy — rendered `spec.serviceName` equals the raw headless
  Service name — and went one step further by asserting that Service is rendered
  *and* carries `clusterIP: None`, which is the property that makes per-pod
  records exist. Whether CoreDNS then serves them is untestable here.
- **That Elasticsearch/ZooKeeper/Redis actually form a cluster.** Application
  behaviour, needs running pods.
- **That the API server accepts the manifests** — no reachable API server;
  `kubectl --dry-run=client` needs one for its OpenAPI schema.
- **Sub-cases (b) and (c)** — no manifests in this repo (§6).

## 9. OBSERVED, DEFERRED

- **`Route_And_Service_Names_Replaced_By_Fullname` (folder 7)** — I expected the
  extra Service to be renamed and it is **not**:
  [services.yaml:12](../../../../Universal-chart/templates/services.yaml#L12)
  renders `name: {{ $name }}`, the raw map key, unprefixed. So the *extra*
  Services in `.Values.services` already keep their raw names. Whatever folder 7
  is about, it is the **primary** Service (`.Values.service`, rendered by
  `service.yaml`) and/or Routes — not this template. Useful scoping for that
  folder. Not touched.
- **Converter nit, no folder:** `base/redis.yaml` sets both
  `nameOverride: redis` and `fullnameOverride: redis-cache`. Correct, but it
  means release identity and object identity diverge for that release. Working
  as designed and warned about in the converter's own output. Not touched.
- Folder 1's deferred items still stand.

## 10. Reproducing

```powershell
python -m pytest universal/gitops-factory/output-showcase/ERRORS_ANALYSIS/StatefulSet_ServiceName_Hardcoded_To_Fullname/test/test_serviceName.py -v
& universal/gitops-factory/output-showcase/ERRORS_ANALYSIS/StatefulSet_ServiceName_Hardcoded_To_Fullname/test/render_check.ps1
```
