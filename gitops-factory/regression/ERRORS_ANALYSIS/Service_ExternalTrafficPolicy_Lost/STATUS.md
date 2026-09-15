# STATUS — Service_ExternalTrafficPolicy_Lost (P3, folder 30/35)

**Result: NO IMPACT on this repo — proved with a grep. A real code gap behind it
is FIXED anyway.**

6 tests RED→GREEN, all other folders still green, chart's own 234 still green,
fidelity unchanged.

---

## 1. Two things wrong with the report, and the grep that settles it

**The semantics are backwards.** The MEDIUM row says losing
`externalTrafficPolicy: Cluster` means *"Source IP not preserved for external LB
traffic"*. It is the other way round: **`Local`** preserves the client source IP;
**`Cluster`** SNATs and load-balances across nodes, which does not. error.txt's
own note already flags this. `Cluster` is also the Kubernetes **default**, so
losing an explicit `Cluster` and falling back to `Cluster` changes nothing.

**And it does not occur here at all:**

```
$ grep -rn "externalTrafficPolicy" namespaces namespaces-showcase namespaces-live-demo | wc -l
0
```

Zero occurrences. `Service/zookeeper-external`, the object the report names, is
not in this repo. `test_this_repo_has_no_external_traffic_policy_at_all` re-runs
that grep as a test and fails the moment one appears.

## 2. The real gap behind it

`build_service_entry` captures the field (line 957) and `service.yaml` renders
it, so the PRIMARY Service is correct. **`templates/services.yaml` had no
`externalTrafficPolicy` block**, so a second LoadBalancer Service dropped it:

```
AssertionError: services.yaml dropped externalTrafficPolicy on the additional Service; got
  {'type': 'LoadBalancer', 'ports': [...], 'selector': {...}}
```

Tested with `Local`, not the reported `Cluster` — `Local` is the value whose loss
would actually change behaviour, so it is the one worth pinning.

## 3. Files changed

| File | Change |
|---|---|
| `Universal-chart/templates/services.yaml` | render `externalTrafficPolicy` |

Converter unchanged. Same missing block as folder 31's `sessionAffinity`; one
edit, documented in both folders.

## 4. Tests

`Service_ExternalTrafficPolicy_Lost/test/` — 6 tests. Fixture derives from
`namespaces-showcase/shop-web/storefront.yaml`, two LoadBalancer Services, both
`externalTrafficPolicy: Local`.

### RED — before

```
2 failed, 4 passed

AssertionError: services.yaml dropped externalTrafficPolicy on the additional Service
AssertionError: raw {'storefront': 'Local', 'storefront-lb': 'Local'}
  != rendered {'storefront': 'Local', 'storefront-lb': None}
```

### GREEN — after

```
6 passed
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

- **That source-IP preservation works.** That is kube-proxy and cloud
  load-balancer behaviour.
- **Whether the report's 2 objects were really affected.** If both were
  `Cluster` on a primary Service, nothing was ever wrong — which is what
  error.txt's own note predicts. Not settleable from here.

## 7. OBSERVED, DEFERRED

- Same `services.yaml` omissions as folder 25 §7 — `loadBalancerIP`,
  `loadBalancerSourceRanges`, `externalIPs`. No folder owns them.
- Earlier folders' deferred items still stand.

## 8. Reproducing

```powershell
python -m pytest universal/gitops-factory/output-showcase/ERRORS_ANALYSIS/Service_ExternalTrafficPolicy_Lost/test -v
```
