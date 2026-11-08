# STATUS — Route_And_Service_Names_Replaced_By_Fullname (P1, folder 7/35)

**Result: FIXED.** 10 new tests RED→GREEN, folders 1–6 still green, chart's own
234 tests still green, fidelity unchanged at 0 lost / 0 changed.

---

## 1. Hint verification

| Hint | Reality |
|---|---|
| `service.yaml` line **5** uses fullname | **Exact.** |
| `route.yaml` line **6** (own name) and the `to:` block use fullname | **Exact** at 6; `spec.to.name` was line **22**, not 53 (53 is `routes.yaml`'s territory). |
| `routes.yaml` line **53** uses `$spec.to.name` | Wrong line — it is **28**, and it already read `$spec.serviceName \| default fullname`. |
| `build_route_entry` line **330**: `spec_to_name = release_fullname` | **No such assignment.** `build_route_entry` (line 861) already captured the raw target into `entry["serviceName"]`, correctly remapped through `name_map`. The loss happened one function later, in `handle_route`, which **deliberately popped it** for the primary Route. |
| `build_service_entry` line **313** stores the raw name but it is unused | Roughly right in effect, wrong in detail: `build_service_entry` (line 902) never had the name at all — `handle_service` received it as `final_name` and discarded it. |

The correction that mattered: the converter was not failing to *compute* the
right target, it was throwing it away with an explicit `entry.pop("serviceName")`
and a comment asserting the primary Route "always targets this release's own
fullname Service". That assumption is the defect.

## 2. Files changed

| File | Line | Change |
|---|---|---|
| `Universal-chart/templates/_helpers.tpl` | 107–124 (new) | New `universal-chart.primaryServiceName` helper |
| `Universal-chart/templates/service.yaml` | 5 | Use the helper |
| `Universal-chart/templates/route.yaml` | 6 | `.name \| default fullname` |
| `Universal-chart/templates/route.yaml` | 22 | `.serviceName \| default primaryServiceName` |
| `Universal-chart/templates/routes.yaml` | 28 | default is now `primaryServiceName`, not fullname |
| `Universal-chart/templates/ingress.yaml` | 30 | `.Values.ingress.serviceName \| default primaryServiceName` |
| `convert_to_universal_chart.py` | 942–958 | `handle_service`: record the primary Service's own name |
| `convert_to_universal_chart.py` | 886–899 | `handle_route`: record the Route's own name; **stop popping** `serviceName` |
| `convert_to_universal_chart.py` | 1216–1247 | `handle_ingress`: capture the backend Service name, remapped |

### Why a helper rather than editing four call sites

Every reference to the primary Service must resolve through one place. Renaming
the Service in `service.yaml` alone would have left `route.yaml`, `routes.yaml`
and `ingress.yaml` pointing at the old fullname — turning a naming defect into a
**dangling-reference** defect, which is strictly worse. The helper makes that
structural: the docstring states the rule, and
`test_ingress_backend_follows_the_renamed_service` /
`test_extra_routes_default_to_the_renamed_primary_service` enforce it.

The Ingress backend is not in error.txt. It is in scope because the Service
rename *requires* it — not as an extra.

## 3. Tests created

`ERRORS_ANALYSIS/Route_And_Service_Names_Replaced_By_Fullname/test/`

- `fixture/shop-web/control-station-client.yaml` — error.txt's own worked
  example: Deployment `control-station-client`, Service
  `control-station-client-service`, Route `control-station-client-route`.
- `fixture/shop-web/storefront.yaml` — the Ingress half. **Split into a second
  microservice on purpose:** the chart's `universal-chart.validate` refuses to
  render `ingress.enabled` and `route.enabled` together
  (*"ingress.enabled and route.enabled cannot both be true — pick one"*). My
  first fixture had both and failed to render; that is a genuine chart
  constraint, not a defect, so the fixture was reshaped rather than the chart.
- `test_route_service_names.py` — 10 tests.
- `compare_all_object_names.py` — whole-showcase sweep of every rendered
  Service/Route/Ingress name **and** every reference, against the raw manifests.
- `render_check.ps1` — fixture chain (Part A) + the sweep (Part B).

### RED — before the fix

```
FAILED test_converter_records_primary_service_name
FAILED test_converter_records_primary_route_name_and_target
FAILED test_converter_records_ingress_backend_service_name
FAILED test_converter_remaps_a_renamed_target_service
FAILED test_templates_render_the_raw_service_and_route_names
FAILED test_ingress_backend_follows_the_renamed_service
FAILED test_extra_routes_default_to_the_renamed_primary_service
FAILED test_round_trip_object_names_match_the_raw_manifests
========================= 8 failed, 2 passed in 3.21s =========================
```

```
AssertionError: handle_service discarded the Service's own name; the chart will render
  it as the release fullname 'control-station-client' and
  'control-station-client-service' will not exist
AssertionError: Route name lost; got keys ['enabled', 'host', 'targetPort']
AssertionError: Ingress backend service name lost; got keys ['className','enabled','hosts']
AssertionError: Ingress still points at the release fullname; got 'storefront'.
  Renaming the Service without this leaves the Ingress dangling.
```

### GREEN — after the fix

```
10 passed in 2.92s
```

### Accumulated regression suite (folders 1–7)

```
69 passed in 13.25s
```

## 4. Validation chain

**1. Converter re-run:** `122 objects matched (28 relocated). No fidelity issues found.`

**2–3. Fixture, raw vs rendered:**

```
   Service  control-station-client-service   -> -                                OK
   Route    control-station-client-route     -> control-station-client-service   OK
   Service  storefront-service               -> -                                OK
   Ingress  storefront                       -> storefront-service               OK
```

Before the fix all four rendered as the bare release name
(`control-station-client` / `storefront`), so the Route and Service collided on
one name and the Ingress pointed at a Service that did not exist.

**Whole showcase — 22 objects, 0 problems.** Every rendered Service, Route and
Ingress corresponds to a raw object of that exact name, and every reference
matches the raw reference.

**4. Fidelity report before vs after: byte-identical**, 0 lost / 0 changed, no
count moved. Notable because this fix *renames rendered objects* — had any
showcase Service been renamed by it, the object-matching pass would have flagged
it immediately.

**5. Generated-output diff:** each release's base file gains an explicit
`service.name` (`api-gateway`, `storefront`, `dex`, `opa`, `postgres`,
`redis-cache`, `metrics-agent`, `platform-ops-shared-placeholder`). All of these
already equalled the fullname, which is exactly why the showcase fidelity count
never moved — and why the fixture was necessary.

**6. `helm lint`:** passes. **7. Chart's own suite:** `28 suites, 234 tests
passed`, including `service_test.yaml`, `route_test.yaml`, `routes_test.yaml`
and `ingress_test.yaml` — all of which assert the fullname defaults, preserved
by the `| default` fallbacks.

## 5. One scare worth recording

`base/shared.yaml` gained `service.name: platform-ops-shared-placeholder`, and
that base file is named after the microservice (`shared`), which exists in both
`platform-ops` and `shop-web`. If both shared releases loaded it, `shop-web`
would render a Service named after platform-ops.

They do not. The converter disambiguates base slugs on cross-namespace content
conflict, and the generated Applications prove it:

```
shop-web-shared-application.yaml   -> base/shop-web-shared.yaml
platform-ops-shared-application.yaml -> base/shared.yaml
```

No cross-contamination. Recorded because the diff looks alarming and is not.

## 6. A sweep bug worth recording (mine, not the converter's)

My first whole-showcase sweep reported an Ingress named `shared` in `shop-web`
with no raw source. That was the sweep layering `defaults.yaml` onto the shared
release. The namespace `shared` release gets a **dedicated** Application that
deliberately skips the defaults layers (`build_shared_application`), so
`ingress.enabled`, hoisted into `defaults.yaml` from api-gateway/storefront, was
being applied to a release that never sees it in reality.

Fixed in `compare_all_object_names.py`, which now mirrors the real valueFiles
chain. Worth knowing for later folders: **any script that renders releases must
special-case the shared release**, or it will manufacture phantom objects — a
trap folders 8 and 9 (missing/extra objects) are especially exposed to.

## 7. What could NOT be verified without a live cluster

- **That in-cluster DNS resolves `<app>-service`.** The rendered Service carries
  the raw name; whether CoreDNS then serves it is cluster behaviour.
- **That external references recover.** error.txt's real risk is config *outside*
  these manifests hardcoding `control-station-client-service`. Nothing in a
  rendered manifest can confirm those consumers.
- **The 33 Services/Routes error.txt counts** — none of those manifests are here.
- **Behaviour on a cluster that already has the old objects.** If a previous
  deployment created `control-station-client` (fullname), this change makes the
  chart create `control-station-client-service` instead and **orphan** the old
  object rather than rename it. With `prune: false` on the generated
  ApplicationSets, the stale Service will linger. That is a migration concern the
  rendered output cannot show.

## 8. OBSERVED, DEFERRED

- **Ingress `metadata.name` is still `fullname`** (`ingress.yaml:5`). Not raised
  by error.txt and not required by this fix — the showcase Ingresses already
  match. Same class of defect; left alone deliberately.
- **`hpa.yaml`, `pdb.yaml`, `vpa.yaml`, `servicemonitor.yaml` all name
  themselves `fullname`**, and `hpa.yaml:16` / `vpa.yaml:16` use it as
  `scaleTargetRef.name`. The scaleTargetRef is correct (it must point at the
  workload, which *is* the fullname), but the object names have the same
  raw-name-loss exposure. Not raised by error.txt. Not touched.
- **`Missing_Objects_Not_Rendered_By_Any_Release` (folder 8) and
  `Extra_Objects_Rendered_With_No_Raw_Source` (folder 9)** — error.txt predicts
  this fix produces both halves of those pairs. In this repo both counts were
  already 0 and stayed 0, so there is nothing here for them to cancel. The sweep
  bug in §6 is directly relevant to how those two must be measured.
- Folders 1–6 deferred items still stand.

## 9. Reproducing

```powershell
python -m pytest universal/gitops-factory/output-showcase/ERRORS_ANALYSIS/Route_And_Service_Names_Replaced_By_Fullname/test/test_route_service_names.py -v
& universal/gitops-factory/output-showcase/ERRORS_ANALYSIS/Route_And_Service_Names_Replaced_By_Fullname/test/render_check.ps1

# standing whole-showcase check (run from universal/gitops-factory):
python output-showcase/ERRORS_ANALYSIS/Route_And_Service_Names_Replaced_By_Fullname/test/compare_all_object_names.py
```
