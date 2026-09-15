# STATUS — Service_AppProtocol_Lost (P2, folder 25/35)

**Result: FIXED — two-sided, and the template half needed fixing in TWO places.**
8 tests RED→GREEN, all other folders still green, chart's own 234 still green,
fidelity unchanged.

---

## 1. The defect, and the part error.txt misses

`spec.ports[*].appProtocol` was neither captured by `build_service_entry` nor
rendered by any Service template. A mesh reads `appProtocol` to pick an L7
handler; `protocol` (TCP/UDP/SCTP) is L4 and says nothing about it. So a port
that declared `grpc` came back as opaque TCP and gRPC-aware routing, retries and
telemetry silently degraded.

**error.txt names one template, `service.yaml`. There are two.** The primary
Service renders through `templates/service.yaml`; every ADDITIONAL Service
renders through `templates/services.yaml`, which is a separate copy of the same
port loop. Fixing only the first leaves any second Service still dropping the
field. This is the Service-template equivalent of the CronJob/Job pattern that
has now caught six folders in this campaign: **there is always a second call
site.**

## 2. Files changed

| File | Change |
|---|---|
| `convert_to_universal_chart.py` `build_service_entry` | capture `appProtocol` per port |
| `Universal-chart/templates/service.yaml` | render it on the primary Service's ports |
| `Universal-chart/templates/services.yaml` | render it on every additional Service's ports |

Omitted when unset rather than defaulted — there is no sensible guess for an L7
protocol, and inventing one would mislabel the port.

## 3. Grep proof: this repo's data does not exercise it

```
$ grep -rh "appProtocol" namespaces namespaces-showcase namespaces-live-demo | wc -l
0
```

Zero occurrences. The code gap is real; the fixture is what proves it, and the
generated showcase output is unchanged. `Service/observability-otel-collector`,
the object the report names, is not in this repo.

## 4. Tests

`Service_AppProtocol_Lost/test/` — 8 tests. Fixture derives from
`namespaces-showcase/shop-web/api-gateway.yaml` (same namespace, same file name,
same Deployment + Service pair) with `appProtocol` added and a **second**
Service so both templates are covered.

### RED — before

```
4 failed, 4 passed in 1.84s

AssertionError: appProtocol dropped at capture; ports =
  {'http': {'port': 80, 'targetPort': 8080, 'protocol': 'TCP'}, ...}
AssertionError: service.yaml dropped appProtocol; rendered
  {'api-gateway': {'http': None, 'otlp-grpc': None}, 'api-gateway-admin': {'admin-grpc': None}}
AssertionError: raw {'api-gateway': {'http': 'http', 'otlp-grpc': 'grpc'},
  'api-gateway-admin': {'admin-grpc': 'grpc'}} != rendered {... None ...}
```

### GREEN — after

```
8 passed in 2.06s
```

## 5. Validation chain

| Step | Result |
|---|---|
| Full showcase converter run | `122 objects matched (28 relocated). No fidelity issues found.` |
| Fidelity before / after | 0 lost / 0 changed, unchanged |
| Generated-output churn | none new (0 raw occurrences) |
| `helm lint` | 0 failed |
| Chart's own suite | 234 passed |

## 6. What could NOT be verified without a live cluster

- **That Istio routes gRPC correctly** once the field is present. That is mesh
  behaviour and needs a running mesh. Verified only that the rendered Service
  carries the raw value.
- **That the target cluster's Kubernetes version serves `appProtocol`** (GA since
  1.20, so almost certainly yes, but unverified here).
- **The 8 reported occurrences** are not in this repo.

## 7. OBSERVED, DEFERRED

- **`services.yaml` is missing more than `appProtocol`.** It also omits
  `loadBalancerIP`, `loadBalancerSourceRanges` and `externalIPs`, which
  `build_service_entry` never captures either — absent from both sides, so a
  complete round-trip gap that no folder in the index covers. Recorded, not
  fixed: it is a different set of fields from this folder's. (`sessionAffinity`
  and `externalTrafficPolicy`, also missing there, ARE covered — folders 31 and
  30.)
- Earlier folders' deferred items still stand.

## 8. Reproducing

```powershell
python -m pytest universal/gitops-factory/output-showcase/ERRORS_ANALYSIS/Service_AppProtocol_Lost/test -v
```
