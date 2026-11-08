# STATUS — Service_AllocateLoadBalancerNodePorts_Lost (TIER 1, folder 9/26)

**Result: FIXED — carried for every Service, nothing suppressed.** The report
proposed suppressing 62 benign occurrences and fixing 3 real ones; carrying the
field unconditionally does both with one rule and no per-instance judgement
encoded in code. The load-bearing detail is an existence check rather than a
truthiness check, because the Kubernetes default is `true` and it is an explicit
`false` that changes behaviour. 13 new tests RED→GREEN, showcase fidelity
0 lost / 0 changed, chart's 234 green, lint clean.

---

## 1. The decision

`error.txt` splits the field:

> 1. **THE 62 ON REDIS "ANNOUNCE" SERVICES** — marked "Suppressible or
>    explicitly set". Redis announce Services are headless/internal; the field
>    is irrelevant there.
> 2. **THE 3 REAL ONES** — `Service/data-receiver-paamon` and
>    `Service/zookeeper-external` [...] FIX THESE.
>
> Suppressing the whole field path silences the 62 AND the 3 together. Scope the
> suppression to the announce Services, **or fix the field properly and suppress
> nothing.**

**Chose the second.** The report's own field table already offers it —
"Suppressible **or explicitly set**" — and it is the better half:

- **One rule.** Scoping by "which Service this is" would encode a per-instance
  judgement in code, and the maintenance cost of that is visible three folders
  back: the Route-labels question became a fix/suppress/neither deadlock
  precisely because the rule was expected to know which objects were special.
- **Nothing to re-litigate.** A suppression is a claim that has to stay true;
  carrying the value is byte-accurate regardless of what the Service turns into.
- **The cost is one rendered line per inert Service.** 62 of them. That is the
  entire downside, and it buys a report line count of 0 rather than 62-suppressed.

## 2. The part that actually matters — `false`, not `true`

`error.txt` asks for this check by name:

> CHECK FIRST: does any affected Service set it to FALSE? Losing an explicit
> false while the default is true would silently flip behaviour and IS a
> production break. The report only ever cites true — confirm that holds.

**The Kubernetes default is `true`.** So:

| Raw value | Lost | Consequence |
|---|---|---|
| `true` | yes | none — the default reinstates it |
| `false` | yes | **a node port is opened on every node** that the Service was configured not to open |

A truthiness check (`if spec.get(...)`) preserves exactly the harmless case and
drops the dangerous one. That trap exists on both sides and both were avoided:

- converter: `if spec.get("allocateLoadBalancerNodePorts") is not None`
- template: `{{- if hasKey .Values.service "allocateLoadBalancerNodePorts" }}`,
  not `{{- with }}` — a `with` block treats `false` as empty and skips it.

`test_the_template_uses_hasKey_not_with` asserts this through behaviour (render
`false`, expect `false`) rather than by reading the template text.

The corpus now carries an explicit `false` on a real LoadBalancer, which is the
case the report never observed. A `true` would have proved nothing: its loss is
undetectable from behaviour.

## 3. Files changed

| File | Lines | Change |
|---|---|---|
| `universal/gitops-factory/convert_to_universal_chart.py` | ~1210–1226 (new) | `build_service_entry`: capture the field with `is not None` |
| `universal/Universal-chart/templates/service.yaml` | 40–46 (new) | render on the primary Service via `hasKey` |
| `universal/Universal-chart/templates/services.yaml` | 46–54 (new) | render on extra Services via `hasKey` |
| `universal/Universal-chart/values.schema.json` | 88 | type it `boolean` |
| `universal/gitops-factory/namespaces-showcase/platform-data/redis.yaml` | 79–105 (new) | a LoadBalancer with an explicit `false` |
| `universal/gitops-factory/output-showcase/**` | regenerated | consequence |

**Both Service templates, not just the primary.** The two real LoadBalancers the
report names — a data-receiver and an externally exposed ZooKeeper — are the
shape that lands in `services.yaml` as a second Service beside the primary.
Fixing only `service.yaml` would have missed exactly the objects the folder is
about.

**Typed `boolean` in the schema** so `"false"` (a string, truthy in every Go
template) fails at lint time rather than rendering. Pinned by
`test_schema_rejects_a_stringified_boolean`, which asserts a real
`helm template --set-string` invocation fails.

## 4. RED

```
$ python <scratch>/red9.py --input namespaces-showcase --output <tmp> \
    --chart ../Universal-chart --verify-fidelity --cluster-scoped
  1 potential fidelity issue(s)

  field_lost_allocateLoadBalancerNodePorts  1

  ERROR  Service/redis-cache-external LOST at spec.allocateLoadBalancerNodePorts: raw had False
```

## 5. GREEN

```
$ python convert_to_universal_chart.py --input namespaces-showcase --output <tmp> \
    --chart ../Universal-chart --verify-fidelity --cluster-scoped
  124 objects matched (28 legitimately relocated).
  No fidelity issues found.
```

```yaml
  name: redis-cache-external
  type: LoadBalancer
  allocateLoadBalancerNodePorts: false
```

```
$ python -m pytest .../Service_AllocateLoadBalancerNodePorts_Lost/test/test_allocate_lb_nodeports.py -q
.............                                                            [100%]
13 passed in 2.43s
```

## 6. Tests created

13 tests in 3 groups:

**Converter** (5) — explicit `false` captured (the one that matters); explicit
`true` captured; absent stays absent; carried regardless of Service type; and
the neighbouring fields (`appProtocol`, `sessionAffinity`,
`externalTrafficPolicy`, `publishNotReadyAddresses`) did not regress — that
whitelist is where each of those was added by a previous folder, which is the
same one-field-at-a-time pattern that left this one behind.

**Template** (7) — both values render on the primary Service and on extra
Services (parametrised); omitted when unset; `hasKey` semantics proven by
behaviour; the schema rejects a stringified boolean.

**Corpus** (1) — an explicit `false` exists, and it is on a `LoadBalancer`,
where the field actually applies.

## 7. One test bug of mine, corrected

`test_extra_services_template_renders_both_values` failed with `StopIteration` —
no Service rendered. `templates/services.yaml` gates every extra Service on
`{{- if $spec.enabled }}` and my values omitted it. A test-harness mistake, not
a code defect; fixed by setting `enabled: true`, which is what the converter
emits anyway.

## 8. Validation chain

| Check | Result |
|---|---|
| RED | `LOST at spec.allocateLoadBalancerNodePorts: raw had False` |
| Converter re-run, showcase | 124 objects matched, **no fidelity issues** |
| `helm template` | `allocateLoadBalancerNodePorts: false` on the LoadBalancer |
| Folder's own tests | 13 passed |
| All `ERRORS_NEW_ANALYSIS` tests | 156 passed |
| `helm unittest Universal-chart` | 234 passed, 28 suites |
| `helm lint Universal-chart` | 0 failed |

## 9. What could NOT be verified

- **`Service/data-receiver-paamon` and `Service/zookeeper-external`.** Not in
  this repo. The report says both lost `true`, which is the harmless direction —
  so the 3 "real" ones may in practice have been harmless too. The fix does not
  depend on that: it is the `false` case that needed the care, and it is now
  impossible to lose.
- **Whether the 62 announce Services really are inert.** Carrying the field
  makes the question moot, which is a further argument for carrying over
  suppressing.
- **That a cloud LB controller honours `false`.** Cluster behaviour. Verified
  only that the value reaches the rendered Service.

## 10. OBSERVED, DEFERRED

- **`K8s_Runtime_Defaults_Benign_Suppress` (folder 17)** lists
  `allocateLoadBalancerNodePorts (62)` for suppression. That entry should now be
  **removed entirely** — not narrowed. `error.txt` already flags it as one of the
  three that "do not belong" in the bulk list. Recorded for that folder; not
  edited here.
- **`Service_SessionAffinity_Lost` (previous round)** — `sessionAffinity: None`
  is still dropped by `build_service_entry` (`!= "None"`), which folder 17 will
  need to reconcile with its own "suppress sessionAffinity None (199)" line.
  Not touched.

## 11. Reproducing

```powershell
python -m pytest universal/gitops-factory/output-showcase/ERRORS_NEW_ANALYSIS/Service_AllocateLoadBalancerNodePorts_Lost/test/test_allocate_lb_nodeports.py -v
```
