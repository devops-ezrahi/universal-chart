# STATUS — Service_SessionAffinity_Lost (P3, folder 31/35)

**Result: the reported loss is COSMETIC on this repo's data — proved with a
grep, not asserted. But a REAL code gap sits behind it and is now FIXED.**

6 tests RED→GREEN, all other folders still green, chart's own 234 still green,
fidelity unchanged.

---

## 1. The severity question, settled from the data

The report's two sources disagree, and error.txt says so:

- MEDIUM table: *"Lost sessionAffinity on services | Count 29 | Load balancer
  session affinity broken"*
- `conflicts_and_warnings.txt`: *"sessionAffinity: None | 150 | K8s default, safe
  to drop"*

Both can be true, and the number that decides it — how many Services carry a
**non-None** value — is stated nowhere in the report. So I counted:

```
$ grep -rho "sessionAffinity: .*" namespaces namespaces-showcase namespaces-live-demo \
    | sort | uniq -c
    300 sessionAffinity: None

$ grep -rn "sessionAffinity:" namespaces* | grep -v "sessionAffinity: None" | wc -l
0

$ grep -rn "sessionAffinityConfig" namespaces* | wc -l
0
```

**300 of 300 are `None`, which IS the Kubernetes default.** Zero non-None values,
zero `sessionAffinityConfig`. Dropping them changes nothing, so
`conflicts_and_warnings.txt` is right and the MEDIUM row overstates it.

`test_every_session_affinity_in_this_repo_is_the_kubernetes_default` re-runs that
count as a test, so the downgrade is not a one-time claim: the moment a non-None
value lands in the raw manifests, the suite fails and the severity gets looked at
again.

## 2. Where the downgrade stops — a real gap the report did not find

`build_service_entry` deliberately keeps a non-None value and drops `None`
(line 959), and `service.yaml` renders it. That path is correct.

**`templates/services.yaml` has no `sessionAffinity` block at all.** Every
Service beyond the primary one renders through that template, so a second,
sticky Service loses its affinity outright — a genuine break, unexercised by
this repo's data but real in the code:

```
AssertionError: services.yaml dropped sessionAffinity on the additional Service; got
  {'type': 'ClusterIP', 'ports': [...], 'selector': {...}}
```

## 3. Files changed

| File | Change |
|---|---|
| `Universal-chart/templates/services.yaml` | render `sessionAffinity` and `sessionAffinityConfig` |

Converter unchanged — it was already correct. The same edit also carries
`externalTrafficPolicy`, which is folder 30's; both folders needed the same
missing block and it is documented in both.

## 4. Tests

`Service_SessionAffinity_Lost/test/` — 6 tests. Fixture derives from
`namespaces-showcase/shop-web/storefront.yaml` with **two** Services, both
carrying `sessionAffinity: ClientIP` plus a `sessionAffinityConfig` timeout —
the non-None case the report never counted.

### RED — before

```
2 failed, 4 passed

AssertionError: services.yaml dropped sessionAffinity on the additional Service
AssertionError: raw {'storefront': ('ClientIP', {...10800}),
  'storefront-sticky': ('ClientIP', {...3600})}
  != rendered {'storefront': ('ClientIP', {...10800}), 'storefront-sticky': (None, None)}
```

The primary Service was right both before and after; only the additional one
moved. That contrast is the evidence for where the defect actually was.

### GREEN — after

```
6 passed
```

## 5. Validation chain

| Step | Result |
|---|---|
| Full showcase converter run | `122 objects matched (28 relocated). No fidelity issues found.` |
| Fidelity before / after | 0 lost / 0 changed, unchanged |
| Generated-output churn | none new — 0 non-None values in the data |
| `helm lint` | 0 failed |
| Chart's own suite | 234 passed |

## 6. What could NOT be verified without a live cluster

- **That kube-proxy actually pins a client to a backend.** Verified only that the
  rendered Service carries the raw value.
- **The report's 29 Services.** Not in this repo. If any of them carried
  `ClientIP` *and* were not the primary Service of their release, they were
  really broken — and are fixed now. If they were all `None`, nothing was ever
  wrong. That distinction cannot be settled from here.

## 7. OBSERVED, DEFERRED

- `services.yaml` still omits `loadBalancerIP`, `loadBalancerSourceRanges` and
  `externalIPs`, none of which `build_service_entry` captures either. See folder
  25's STATUS §7 — same finding, no folder owns it.
- Earlier folders' deferred items still stand.

## 8. Reproducing

```powershell
python -m pytest universal/gitops-factory/output-showcase/ERRORS_ANALYSIS/Service_SessionAffinity_Lost/test -v
```
