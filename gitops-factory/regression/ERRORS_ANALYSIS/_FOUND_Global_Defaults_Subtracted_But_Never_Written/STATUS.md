# STATUS — Global defaults subtracted but never written  (FOUND, no error.txt)

**This folder has no `error.txt`.** The defect was found while verifying folders
8/9 and does not correspond to any of the 35 listed folders. It is a strong
candidate for one of the two missing report issues (28/29) — see `_INDEX.txt`
"KNOWN GAP IN THE SOURCE MATERIAL".

**Severity: P0 — silent, total loss of values, by default.**
**Result: FIXED, in two rounds.** 9 tests RED→GREEN; showcase output corrected.

The first round (below, §1–§8) fixed the regular-microservice case, found while
verifying folders 8/9. A **second instance of the same defect** was found later,
while probing folder 15, and is written up in §10 — it survived the first fix
because that guard asked whether the defaults file was *written*, when the
question that matters is whether the release *applies* it.

---

## 1. The defect

`convert_to_universal_chart.py`, `main()`, Pass 2 (was lines 3049–3063):

```python
if not NAMESPACE_SCOPED_ONLY:
    (output_dir / DEFAULTS_FILE_NAME).write_text(... global_defaults ...)
for ms_list in per_namespace_ms.values():
    for _, ctx in ms_list:
        ctx.values = subtract_defaults(ctx.values, global_defaults)   # <-- unconditional
for shared_ctx in per_namespace_shared.values():
    shared_ctx.values = subtract_defaults(shared_ctx.values, global_defaults)
```

The tree-root `defaults.yaml` is written **only** in cluster-scoped mode. The
subtraction of `global_defaults` from every microservice's values ran in **both**
modes. Namespace-scoped is the **default** (`--cluster-scoped` opts out), so by
default every value identical across all microservices in a run was removed from
the output and written nowhere.

The subtraction and the write are one operation: a value may only be removed
because a layer underneath supplies it again. Here the layer was never created.

### Why the code's own justification is wrong

The comment read:

> Safe to omit rather than needing to be folded in anywhere: namespace_defaults
> below is common_subtree over THAT namespace's microservices, and this global
> layer is common_subtree over ALL of them — a subset of the same computation,
> so whatever global holds is already present in every namespace's own
> defaults.yaml. Dropping it removes a layer, never a value.

The set-theory is right; the **ordering** defeats it. `namespace_defaults` is
computed in **Pass 3** (line 3148) from `ctx.values` — *after* this subtraction
has already deleted those keys. Anything taken here can never reappear there.

### Why it stayed hidden

`global_defaults` is `common_subtree` over **every** microservice in the run, and
the showcase's 20 microservices span Deployment, StatefulSet, DaemonSet, Job and
CronJob. They share almost nothing, so the set was nearly empty. **Two similar
Deployments in one namespace is enough to trigger it.**

## 2. Reproduction

Two near-identical Deployments (`storefront`, `catalog`) in one namespace:

```
storefront: these values were subtracted into the un-written global defaults
layer and are absent from every generated file:
  {'workload.type': 'deployment', 'containerName': 'nginx',
   'ports.http.containerPort': 8080, 'service.enabled': True,
   'service.ports.http.port': 80}
```

The whole generated base file was:

```yaml
ingress: {...}
nameOverride: storefront
service:
  name: storefront-service
```

No `workload`, no `image.repository`, no `ports`, no `service.ports`, no
`containerName`. Round-trip fidelity on that input: **2 fields LOST, 4 CHANGED**,
including `Service spec.ports: raw=[{...}] render=None` — a Service rendered
with **no ports at all**.

The same fixture with **one** microservice is perfectly clean (0/0/0/0), which
isolates the trigger to homogeneity, not to any individual manifest.

## 3. It was actively eating a real showcase value

Not hypothetical. After folder 7's fix, `ingress.serviceName` was captured for
`dex`, `api-gateway` and `storefront`. Regenerating the showcase then produced
**no change** to those three values files — because this bug swallowed the new
value. Only after fixing it did they appear:

```diff
--- a/universal/gitops-factory/output-showcase/identity-auth/values/dex.yaml
+++ b/universal/gitops-factory/output-showcase/identity-auth/values/dex.yaml
       portName: http
+  serviceName: dex
 replicaCount: 2
```

Same for `shop-web/values/api-gateway.yaml` (`serviceName: api-gateway`) and
`shop-web/values/storefront.yaml` (`serviceName: storefront`).

The outcome was accidentally survivable only because `ingress.yaml`'s fallback is
`primaryServiceName`, which happened to equal the lost value. Had the raw Service
been named `dex-service`, the Ingress would have routed to nothing.

## 4. Fix

`convert_to_universal_chart.py` lines 3049–3075: the two subtraction loops moved
**inside** the `if not NAMESPACE_SCOPED_ONLY:` block, so the layer is subtracted
only when it is also written. In namespace-scoped mode `ctx.values` keeps
everything, `base/<slug>.yaml` carries it, and Pass 3's `namespace_defaults`
picks up genuinely-common values into `<ns>/defaults.yaml`, which the generated
ApplicationSet **does** reference.

The old comment is replaced with one recording why the two operations are
inseparable.

## 5. Tests

`_FOUND_Global_Defaults_Subtracted_But_Never_Written/test/`

- `fixture/shop-web/storefront.yaml` + `fixture/shop-web/catalog.yaml` — two
  deliberately similar Deployments.
- `test_global_defaults_layer.py` — 5 tests:
  - every common value survives into the effective (ApplicationSet-ordered)
    values, for both releases;
  - no ApplicationSet `valueFiles` entry references a file that was never written;
  - round-trip fidelity is clean for homogeneous input;
  - **cluster-scoped mode still writes the layer it subtracts** — the other half
    of the invariant, and the guard that keeps the fix honest rather than just
    disabling a feature.

### RED → GREEN

```
FAILED test_namespace_scoped_output_keeps_every_common_value[storefront]
FAILED test_namespace_scoped_output_keeps_every_common_value[catalog]
FAILED test_round_trip_fidelity_is_clean_for_similar_microservices
      -> {'fields LOST': 2, 'fields CHANGED': 4}
3 failed, 2 passed
```

The two that passed while RED are the guards — notably
`test_cluster_scoped_mode_still_writes_the_layer_it_subtracts`, which passed
before and after and pins that the fix did not simply delete the feature.

```
5 passed in 3.33s
```

## 6. Validation chain

| Step | Result |
|---|---|
| Converter re-run (showcase) | `122 objects matched. No fidelity issues found.` |
| Fidelity report before/after | **unchanged** — 0 lost / 0 changed, no count moved |
| Generated-output diff | 3 values files **gained** the `ingress.serviceName` they had been losing (§3); base files gained folders 5/7 keys |
| Standing sweep: container names | `19 workloads compared, 0 mismatches` |
| Standing sweep: object names | `22 objects compared, 0 problems` |
| `helm lint` | 0 failed |
| Chart's own suite | 28 suites, 234 tests passed |
| Accumulated suite (folders 1–7 + this) | **74 passed** |

## 7. What could NOT be verified without a live cluster

- **That the previously-broken output would actually have failed to deploy.** A
  Service with no ports and a Deployment with no container ports are accepted by
  the API server (they are structurally valid) but route no traffic. Confirming
  the traffic failure needs a cluster.
- **Whether any already-deployed release is running degraded** because it was
  generated by the buggy converter. Anyone who ran this on homogeneous input
  should regenerate and diff before trusting the live state.

## 8. OBSERVED, DEFERRED

- **`<ns>/defaults.yaml` is now duplicative rather than load-bearing.** Base
  files are written from the un-subtracted `ctx.values` (Pass 2.5, line 3105),
  and `base/` sits *above* `<ns>/defaults.yaml` in the ApplicationSet chain, so
  the namespace layer only ever restates values base already supplies. Harmless
  and correct, but the layering no longer earns its keep in namespace-scoped
  mode. Not touched — it is a design question, not a defect.
- Folders 1–7 deferred items still stand.

## 9. Reproducing

```powershell
python -m pytest universal/gitops-factory/output-showcase/ERRORS_ANALYSIS/_FOUND_Global_Defaults_Subtracted_But_Never_Written/test/test_global_defaults_layer.py -v
```


---

## 10. SECOND ROUND — the `shared` release (found while probing folder 15)

**Severity: P0 again — phantom objects on a release that declared none.**
**Result: FIXED.** 3 more tests RED→GREEN.

### The defect

The first fix paired the subtraction with the write:

```python
if not NAMESPACE_SCOPED_ONLY:
    (output_dir / DEFAULTS_FILE_NAME).write_text(...)
    for ms_list in per_namespace_ms.values():
        for _, ctx in ms_list:
            ctx.values = subtract_defaults(ctx.values, global_defaults)
    for shared_ctx in per_namespace_shared.values():
        shared_ctx.values = subtract_defaults(shared_ctx.values, global_defaults)   # <-- still wrong
```

For every regular microservice that is correct: the ApplicationSet's fixed
valueFiles chain applies the root `defaults.yaml`, so what is subtracted comes
back. **`shared` is the one release that does not read it.**
`build_shared_application` gives it a dedicated Application whose chain is
`base/<slug>.yaml` + `<ns>/values/<slug>.yaml` and nothing else — deliberately,
so that two sibling microservices agreeing on `route.enabled` cannot switch on a
Route the shared release never declared.

So the guard was checking the wrong condition. *Written* is not the same as
*applied*, and for this one release the two diverge by design.

### What it cost

A config-only shared release loses `workload.type: none` and
`service.enabled: false` to `global_defaults`, then falls back to the chart's own
defaults. RED, before the fix:

```
AssertionError: the shared release rendered
  ['ConfigMap', 'Deployment', 'Service', 'ServiceAccount'];
  it declared only a ConfigMap

AssertionError: [cluster-scoped] the shared release lost workload.type; it will
  render the chart's default workload instead. effective values: ['configMaps', 'nameOverride']

AssertionError: these keys are in the root defaults.yaml but missing from the
  shared release, which never applies that file: ['service', 'workload']
```

A Deployment and a Service materialising on a release whose manifests declare a
ConfigMap — the exact phantom-object failure `build_shared_application` exists to
prevent, arriving through the back door. It is also the
`Extra_Objects_Rendered_With_No_Raw_Source` failure mode (folder 9) reappearing
through a different route.

### The fix

Delete the shared subtraction outright. `shared` is never subtracted against
`global_defaults` in either mode, because there is no mode in which it applies
that layer. The comment in the converter now states the invariant as *applies*,
not *written*, so the next person to add a layer has the right test to apply.

### Tests

Second fixture, `test/fixture-shared/` — two namespaces (`platform-ops`,
`platform-data`), each with a `shared.yaml` holding one ConfigMap and one
config-only microservice. Config-only is what makes `global_defaults` hold
`workload.type: none` and `service.enabled: false`, which is exactly what the
shared releases also carry.

| test | asserts |
|---|---|
| `test_shared_release_keeps_what_it_declared_in_both_modes` | parametrised over both modes; the values survive |
| `test_shared_release_renders_no_phantom_workload_or_service` | end to end through `helm template` |
| `test_the_shared_release_is_never_subtracted_against_an_unapplied_layer` | the invariant itself — nothing may be in the root defaults and missing from shared |

The effective-values helper reads the generated shared **Application** rather than
assuming a path, because `base_registry` forks the slug to
`base/<ns>-shared.yaml` when two namespaces' shared content differs. Asserting
against the file the Application actually names is the only way this test stays
honest.

```
3 failed, 6 passed        (before)
9 passed                  (after)
153 passed in 59.18s      (whole accumulated suite, folders 1–15)
```

Showcase re-run: `122 objects matched (28 legitimately relocated). No fidelity
issues found.` The showcase's `shared` releases declare ServiceAccounts, Roles
and NetworkPolicies rather than being config-only, and its microservices are
heterogeneous enough that `global_defaults` is nearly empty — so its generated
output is unchanged, and the fixture was required.

### OBSERVED while fixing this

- **A release that declares no ServiceAccount still renders one.** The chart's
  `serviceAccount.create` defaults to `true`, and the converter only writes a
  `serviceAccount` block when a pod references one — so `db-migrate` and
  `nightly-report` each render a ServiceAccount with no raw source. The fidelity
  checker already sees this and classifies it deliberately:
  `INFO ServiceAccount/db-migrate: created by the chart for release 'db-migrate'
  (serviceAccount.create) — no raw source expected`. So it is a known, recorded
  choice rather than a blind spot — but it is still an object appearing on the
  cluster that no manifest asked for, and it belongs with folder 9's question
  rather than this one. Not touched.
- **The invariant deserves a permanent home.** Every "subtract X from a release"
  in this converter needs the matching question *"does that release apply the
  layer X was written to?"* — asked twice now, wrong twice. The third place to
  check if it recurs is `subtract_defaults(ctx.values, shared_base)` in Pass 2.5,
  which is sound today because `base/` genuinely is in every chain.
