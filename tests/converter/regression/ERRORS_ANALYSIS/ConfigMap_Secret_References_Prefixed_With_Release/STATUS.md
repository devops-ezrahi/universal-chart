# STATUS — ConfigMap_Secret_References_Prefixed_With_Release (P1, folder 19/35)

**Result: VERIFIED — every reference resolves. The check error.txt asks for now
runs over the whole showcase on every test run.** 5 tests, 1 RED→GREEN (my own
first version of the check was wrong; see §2).

---

## 1. The concern, and the check that settles it

Reported: `secretKeyRef.name` (10) and `volumes[*].configMap.name` (7) come back
prefixed with the release name, so they "no longer match the objects that exist
in the cluster".

The report answers half of this itself — *"The volume references are updated
consistently ... Both match, so it works"* — and error.txt names the check for
the rest:

> "run `helm template` per release and confirm every secretKeyRef.name and
> volumes[*].configMap.name resolves to an object that release actually creates."

Done, and widened: the test renders **every release the converter produces from
the real showcase manifests** (24 of them), walks every ConfigMap/Secret
reference in the rendered output — `volumes[].configMap`, `volumes[].secret`,
`envFrom.configMapRef`, `envFrom.secretRef`, `configMapKeyRef`, `secretKeyRef` —
and resolves each against what actually gets created.

**Result: zero dangling references**, in the showcase and in the collision
fixture where the renaming actually fires.

## 2. My first version of the check was wrong, and the failure was informative

I resolved references **per release**. Six failed:

```
platform-ops/log-agent:  envFrom.configMapRef.name -> ConfigMap/platform-ops-alert-thresholds
shop-web/storefront:     envFrom.configMapRef.name -> ConfigMap/shared-ca-bundle
...
```

Neither is broken. Both are declared in their namespace's `shared.yaml`, and
`claim()`'s `reference_shared` action is exactly the mechanism that lets a
microservice point at an object the **shared** release creates. A ConfigMap is
namespace-scoped, so per-release resolution asks a question the cluster never
asks.

The check now resolves per **namespace**, which is what the cluster sees. It
still catches the thing that matters — a reference that resolves *nowhere* in its
namespace, which is what a bad collision rename would produce — and it no longer
flags the design working correctly.

## 3. The residual risk error.txt names

> "zookeeper release's volume points to zookeeper-zookeeper-config, but that
> ConfigMap may be rendered by the wrong release or not at all"

`test_collision_renames_keep_every_reference_resolvable` is that test, against
the three-way `zookeeper-config` collision. It passes: each release renders its
own renamed copy and its own volume points at it.

Two guards stop it passing vacuously —
`test_the_rename_actually_happened` asserts a rename occurred at all, and
`test_each_release_keeps_its_own_copy_of_the_colliding_object` asserts one object
per release rather than a reference with no creator.

## 4. Files changed

**None.** This folder is a verification, and the mechanism it verifies —
`ctx.name_map`, which renames the object and rewrites every reference from the
same map — was already correct. `test_the_name_map_is_what_keeps_references_consistent`
pins the reason they cannot drift apart.

## 5. Validation chain

| Step | Result |
|---|---|
| Reference integrity, real showcase | **0 dangling** across 24 releases |
| Reference integrity, collision fixture | **0 dangling** |
| Full showcase converter run | `122 objects matched (28 relocated). No fidelity issues found.` |
| `helm lint` | 0 failed |
| Chart's own suite | 234 passed |

## 6. What could NOT be verified without a live cluster

- **That nothing OUTSIDE the conversion referenced the original names.** This is
  the real residual risk and it is unanswerable from here: a manifest that was
  never part of this migration, a hard-coded name in application config, or an
  operator watching for `zookeeper-config` would all break. The converter warns
  on every rename (folder 18) precisely because only a human can check this.
- **That the cluster applies these in an order where the reference resolves.**
  Kubernetes tolerates a pod referencing a not-yet-created ConfigMap by leaving
  it pending, so ordering is not fatal — but that is documented behaviour, not
  something observed.

## 7. OBSERVED, DEFERRED

- **Cross-NAMESPACE references would not be caught by this check** and are not
  expressible in Kubernetes anyway (ConfigMaps and Secrets are namespace-scoped),
  so a raw manifest that had one was already broken. Same conclusion folder 2
  reached about a cross-namespace `serviceName`.
- **The check is now a standing sweep**, like the two in folders 5 and 7. It runs
  against the real manifests on every test run, so a future change that breaks
  reference integrity fails here rather than on a cluster.
- Earlier folders' deferred items still stand.

## 8. Reproducing

```powershell
python -m pytest universal/gitops-factory/output-showcase/ERRORS_ANALYSIS/ConfigMap_Secret_References_Prefixed_With_Release/test -v
```
