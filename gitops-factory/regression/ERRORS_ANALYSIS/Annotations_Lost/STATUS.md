# STATUS — Annotations_Lost (P2, folder 28/35)

**Result: FIXED, both sides. The LOW rating holds for this repo — but only by
luck, and the triage the report never did is what shows why.**

14 tests RED→GREEN, all other folders still green (273 total), chart's own 234
still green, fidelity unchanged at 0 lost / 0 changed.

---

## 1. The triage the report skipped

error.txt flags the report's own wording as the warning sign: the pattern table
says *"metadata.annotations | 135 | **Mixed**: revisions, OpenShift-specific"*,
and **"Mixed" means the bucket was never triaged**. It lists what must be
checked before LOW is acceptable — `argocd.argoproj.io/*`, ingress/route
behaviour annotations, `prometheus.io/scrape`, `checksum/config`.

Done. Every annotation key across all three manifest trees:

```
  2405  kubectl.kubernetes.io/last-applied-configuration   (server-generated, safe)
     3  traefik.ingress.kubernetes.io/router.entrypoints   <-- NOT SAFE TO DROP
     3  traefik.ingress.kubernetes.io/router.tls           <-- NOT SAFE TO DROP

total distinct keys: 3     total occurrences: 2411
```

Zero `argocd.argoproj.io/*`. Zero `prometheus.io/*`. Zero `checksum/*`. So
**2405 of 2411 are the one key that was already filtered**, and the LOW rating
holds.

**But it holds by accident**, and that is the finding. The six that are NOT safe
— they change which Traefik entrypoint serves the route and whether TLS is on —
all sit on **Ingress**, which happens to be one of the few kinds whose handler
already captured annotations. Eight other handlers captured none at all, and two
templates had no annotations block. The same six annotations on a NetworkPolicy
or an HPA would have vanished silently.

**Whether an annotation survived depended on which kind it was attached to, and
nothing said so.** That asymmetry is the defect, not the count.

## 2. Files changed

### Converter — capture

| handler | before |
|---|---|
| `handle_workload` | **no annotations at all** — Deployment/StatefulSet/DaemonSet |
| `handle_networkpolicy`, `handle_role`, `handle_rolebinding` | none |
| `handle_hpa`, `handle_pdb`, `handle_servicemonitor`, `handle_vpa` | none |

All now read `clean_annotations(metadata.annotations)`. `handle_rolebinding` was
also missing `labels` — folder 20 built its entry as a bare dict literal and the
capture had nowhere to go; both are added there now.

One key added to `VOLATILE_ANNOTATION_PREFIXES`:
**`deployment.kubernetes.io/revision`**. It is a server-maintained rollout
counter — meaningless on the target cluster, different on every source, and
carried through it produces a permanent non-empty ArgoCD diff. error.txt names
it explicitly as safe to drop.

### Chart — render

| template | before |
|---|---|
| `rbac.yaml` (4 blocks), `servicemonitor.yaml` | **no annotations block at all** |
| `deployment.yaml`, `daemonset.yaml`, `statefulset.yaml`, `hpa.yaml`, `pdb.yaml`, `vpa.yaml` | rendered `.Values.commonAnnotations` **only** |

That second row is the subtler half and it is why the round-trip test failed
while every capture test passed: the annotations reached values.yaml and then
rendered nowhere. Those six now merge the object's own annotations over the
release-wide ones, the same shape the other templates already used.

## 3. Tests

`Annotations_Lost/test/` — 14 tests. Fixture derives from
`namespaces-showcase/shop-web/api-gateway.yaml`, giving seven object kinds one
annotation each from the NOT-SAFE list, plus both server-generated keys so the
filter is tested from both directions at once.

The Ingress needs its backing Service in the same fixture: the chart refuses to
render an Ingress whose backend port it cannot resolve
(*"ingress.hosts[...].paths has no portName and service.ports is empty"*). A
real chart constraint, so the pair travels together rather than the chart being
loosened.

### RED

```
AssertionError: annotations lost end to end:
  Deployment: argocd.argoproj.io/sync-wave='2' -> None
  Deployment: prometheus.io/scrape='true' -> None
  Deployment: prometheus.io/port='8080' -> None
  Deployment: checksum/config='9f2c1e4a' -> None
  HorizontalPodAutoscaler: argocd.argoproj.io/sync-wave='3' -> None
  PodDisruptionBudget: argocd.argoproj.io/sync-wave='3' -> None
```

and earlier, before the handlers were wired:

```
AssertionError: Deployment: no annotations at values path annotations; got
  ['containerName', 'image', 'labels', 'ports', 'workload']
```

### GREEN

```
14 passed in 14.03s
```

**Two structural tests**, the same shape as folder 20's:
`test_every_template_has_an_annotations_block` greps every template, and
`test_the_not_safe_annotations_in_this_repo_are_only_the_six_traefik_ones`
re-runs the triage on every test run — so an `argocd.argoproj.io/sync-wave`
landing in the raw manifests fails the suite, which is exactly when the LOW
rating stops being safe.

## 4. Validation chain

| Step | Result |
|---|---|
| Full showcase converter run | `122 objects matched (28 relocated). No fidelity issues found.` |
| Fidelity before / after | 0 lost / 0 changed, unchanged |
| Generated-output churn | none — 2405 of 2411 raw annotations were already filtered |
| `helm lint` | 0 failed |
| Chart's own suite | 28 suites, 234 tests passed |
| Accumulated | 273 passed |

## 5. What could NOT be verified without a live cluster

- **That a restored `argocd.argoproj.io/sync-wave` actually orders the
  deployment.** That is ArgoCD's behaviour; verified only that the annotation
  reaches the rendered manifest.
- **That `prometheus.io/scrape` resumes collection**, and that
  `checksum/config` triggers a restart. Both depend on controllers not present
  here.
- **That the six Traefik router annotations behave the same on the target
  ingress controller.** They are Traefik-specific; a cluster running a different
  controller ignores them entirely.
- **That dropping `deployment.kubernetes.io/revision` is harmless.** It is
  server-maintained, so Kubernetes recreates it — but that is documented
  behaviour, not something observed here.

## 6. OBSERVED, DEFERRED

- **`clean_annotations` filters by prefix, and the list is hand-maintained.**
  Anything a future source cluster generates that is not on it will be carried
  through and show up as noise. The inverse of the labels situation (folder 20),
  where the filter is an identity exclusion list. Neither is wrong; both are
  lists someone has to remember to update.
- **`extra-deploy.yaml` renders no annotations block and does not need one** —
  it passes whole objects through verbatim, so their metadata travels with them.
  Excluded from the structural test for that reason.
- **The report's 135 occurrences** are not in this repo; the 2,411 here are its
  own data.
- Earlier folders' deferred items still stand.

## 7. Reproducing

```powershell
python -m pytest universal/gitops-factory/output-showcase/ERRORS_ANALYSIS/Annotations_Lost/test -v
```
