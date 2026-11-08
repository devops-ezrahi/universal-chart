# Universal Chart + GitOps Factory — Bugfix Report

This document summarizes the bugs that were found in the `Universal-chart`
Helm chart and the `gitops-factory` conversion tooling, what was changed to
fix them, and how the system behaves now. Everything below was verified by
re-running the chart's `helm-unittest` suite, `helm lint`, and by fully
regenerating and re-rendering every microservice in `gitops-factory/output`
and `gitops-factory/output-live-demo` with `helm template` (256 + 8 releases,
zero ownership conflicts).

---

## 1. Naming — resources were getting a `super-universal-chart` suffix

**The bug:** `templates/_helpers.tpl`'s `fullname` helper built resource
names as `<Release.Name>-<Chart.Name>` whenever `nameOverride` wasn't set.
Since every app using this chart shares the same `Chart.Name`
(`super-universal-chart`), and GitOps release names already carry their own
namespace/app plumbing (e.g. `analytics-svc-analytics-svc-billing`),
resources ended up named things like:

- `buildkit-super-universal-chart` instead of `buildkit`
- `jenkins-restore-tst-values-super-universal-chart-jenkins-restore-tst` for
  a CronJob — which **exceeds Kubernetes' 52-character CronJob name limit**
  and fails to apply.

A second, compounding bug was found in the converter: CronJob/Job names in
the raw source manifests already followed a `<microservice>-<suffix>`
convention (e.g. `analytics-svc-batch-1-backup`), and the converter used
that *full* raw name as the map key. The chart then prepended the
microservice's own name again, doubling it:
`analytics-svc-batch-1-analytics-svc-batch-1-backup`.

**The fix:**
- `_helpers.tpl` no longer references `.Chart.Name` **at all**. A resource's
  name is now: `fullnameOverride` if set, else `nameOverride` if set, else
  plain `.Release.Name`. This is fully generic — verified with several
  different release names (`buildkit`, `jenkins-restore`,
  `totally-different-app-name`), none of which ever produce a chart-name
  suffix.
- A dedicated `cronJobName` helper truncates the composite CronJob name to
  52 characters (Kubernetes needs the extra headroom for the timestamp/pod
  suffixes it appends automatically).
- The converter (`convert_to_universal_chart.py`) now sets `nameOverride`
  to the microservice's own name for every generated release, and strips
  the redundant `<microservice>-` prefix from CronJob/Job names before
  using them as map keys (`short_subresource_name`), fixing the doubling.

---

## 2. Selector labels — `commonLabels` could break Pod/ArgoCD sync

**The bug:** The `labels` helper built `metadata.labels` by concatenating
YAML text fragments: `selectorLabels` first, then `commonLabels` appended
after. If a caller's `commonLabels` happened to set
`app.kubernetes.io/name`, the rendered YAML contained **two** `name:` keys
in the same mapping — invalid/ambiguous YAML that can fail to parse or
silently pick the wrong value depending on the tool reading it, causing
ArgoCD sync failures. Because `commonLabels` was never applied to
`selectorLabels` (used for `selector.matchLabels` and the Pod template),
this created two independently-computed identity values that could drift
apart.

**The fix:** `labels` is now built as a real merged map (`merge` in Sprig),
not text concatenation — impossible to produce a duplicate key. The chart's
own identity keys (`name`, `instance`, `chart`, `managed-by`, `version`)
always win over `commonLabels`, so `commonLabels` can only *add* labels,
never override or duplicate an identity one. `selectorLabels` remains fully
static and untouched by `commonLabels`, and is used consistently everywhere
matching is required (Deployment/StatefulSet/DaemonSet selector + Pod
template, Service/PDB/ServiceMonitor selectors).

---

## 3. Volume mount errors + broken env/volume references in sidecars

**The bug, part A (chart):** Nothing validated that a `volumeMounts` entry
had a matching `volumes` entry. A typo or an omitted volume definition
produced a chart that rendered fine but **failed at `kubectl apply` time**
with `Not found: "<volume-name>"` — much harder to debug than a template
error. Running the fix against the real fixtures actually caught a genuine
pre-existing case of this.

**The bug, part B (converter):** When the converter renames a ConfigMap or
Secret to resolve a naming collision, every reference to it (`env`,
`envFrom`, `volumes`) is supposed to be rewritten to match. This rewriting
(`remap_env`/`remap_envfrom`) was only ever applied to the **main
container**. Init containers and sidecars were copied through as-is, so if
one of *their* ConfigMap/Secret references got renamed, they'd be left
pointing at a name that no longer existed — `CrashLoopBackOff` at deploy
time. A related, more severe gap: CronJob/Job conversion didn't extract
sidecars/init containers **at all** — they were silently dropped.

**The fix:**
- `_helpers.tpl`'s `validate` now fails at template time if any
  `volumeMounts` key has no matching `volumes` (or, for StatefulSets,
  `volumeClaimTemplates`) entry.
- A new `remap_container_refs` function in the converter walks init
  container / sidecar `env`/`envFrom` lists directly and applies the same
  ConfigMap/Secret rename table the main container already used. It's now
  wired into the Deployment/StatefulSet/DaemonSet path **and** into
  CronJob/Job conversion, which now also correctly extracts sidecars and
  init containers instead of dropping them.

---

## 4. Other chart bugs found by review/lint

| Bug | Root cause | Fix |
|---|---|---|
| **Service `targetPort` silently drops traffic** | Defaulted to the *Service's own port map-key* instead of matching real Kubernetes behavior, where an unset `targetPort` defaults to the port **number**. | `targetPort` now defaults to `.port` (the numeric service port), matching Kubernetes' own default exactly. |
| **Ingress/Route target an empty port** | `firstKey` on an empty `service.ports` map silently returns `""`, producing an Ingress/Route with a blank backend port. | `validate` now fails fast if Ingress/Route is enabled with no explicit port and `service.ports` is empty. |
| **`mapToList` name collision** | If a map value (e.g. a port spec) itself contained a `name` key, the rendered list item had two `name:` keys — same class of bug as #2. | The value's own `name` key is stripped (`omit`) before being merged into the list item. |
| **Digest-pinned images silently corrupted** | `image.tag \| default .Chart.AppVersion` was unconditionally appended, so `myrepo@sha256:...` became the invalid `myrepo@sha256:...:2.0.0`. | Added `image.digest` field; the image helper now recognizes digest-pinned repositories (`image.digest` set, or `@` already in `repository`) and never appends a tag to them. |

---

## 5. GitOps folder architecture — DRY `base/` + `values/<namespace>/`

**The problem:** Every microservice's comprehensive configuration was
duplicated per namespace, even when the *same* microservice (e.g. a
"buildkit" utility pod) ran in multiple namespaces with near-identical
specs. There was also no strict separation ensuring environment-agnostic
config never carried an image tag or env var.

**What changed:** The output layout is now:

```
output/
  defaults.yaml                 # global — identical across every microservice
  base/
    <microservice>.yaml         # comprehensive, environment-agnostic — NEVER
                                 # an image tag or env var. Written ONCE and
                                 # reused by every namespace that runs it.
  <namespace>/
    defaults.yaml                       # per-namespace common values
    values/<microservice>.yaml          # image tag + literal env + minor
                                         # per-namespace tweaks ONLY
    releases/<microservice>.yaml        # tiny pointer file for ArgoCD
  applicationsets/<namespace>-applicationset.yaml
```

- A microservice's `base/<name>.yaml` is shared across every namespace that
  runs it, by design.
- If two namespaces declare a microservice with the **same name but
  genuinely different content**, the converter detects it (by content hash)
  and automatically falls back to a namespace-qualified file
  (`base/<namespace>-<name>.yaml`) instead of silently overwriting or
  incorrectly sharing — logged in
  `report/conflicts_and_warnings.txt` as a `base content differs` entry.
  This was verified live: the per-namespace `shared` pseudo-release (RBAC/
  ConfigMaps local to each namespace) correctly triggers this fallback in
  4 of 5 real namespaces.
- The ArgoCD `ApplicationSet` template's `valueFiles` chain was updated to
  match: `defaults.yaml` → `<namespace>/defaults.yaml` →
  `base/{{release}}.yaml` → `<namespace>/values/{{release}}.yaml`.
- `values_editor.py` (the hand-authoring CLI) was updated to match: `new`
  reuses an existing shared `base/` file instead of re-scaffolding it, and
  `set` warns when a structural edit will affect every namespace reusing
  that base file.

**Known follow-up:** `ui/multi-app-manager.html` (the browser UI) still
targets the old per-namespace layout and has **not** been updated yet — use
`values_editor.py` for hand-authoring in the meantime.

---

## What the tooling does now

**`Universal-chart`** — a generic Helm chart for deploying any application:
- Resource names are always just the app's own name (via `nameOverride`/
  `fullnameOverride`, or the release name) — never the chart's own name,
  regardless of what app or release uses it.
- Labels and selectors are guaranteed internally consistent; `commonLabels`
  can only add labels, never corrupt identity ones.
- Fails fast (at `helm template`/`helm install --dry-run` time) on
  misconfigurations that used to only surface as a cluster-side apply
  error: mismatched volume mounts, empty ingress/route ports, invalid
  workload-type combinations.
- Correctly handles digest-pinned images, and CronJob names that respect
  Kubernetes' stricter 52-character limit.

**`convert_to_universal_chart.py`** — converts raw per-namespace Kubernetes
manifest dumps into this chart's values files and ArgoCD `ApplicationSet`s:
- Every generated resource reference (env, envFrom, volumes) is correctly
  rewritten for **every** container in a pod — main, sidecars, and init
  containers alike — when a naming collision forces a rename.
- CronJobs and Jobs now carry their sidecars/init containers through
  instead of silently dropping them.
- Produces a DRY, two-layer values structure: one shared `base/` file per
  microservice (reused across namespaces), and a minimal per-namespace
  override holding only the image tag, env vars, and small tweaks —
  detecting and safely handling the case where the same microservice name
  means genuinely different things in different namespaces.
- Still self-verifies its own output end-to-end: every generated release is
  rendered for real with `helm template` and checked so no two releases
  ever claim the same Kubernetes object.

**`values_editor.py`** — a CLI for hand-authoring/tuning microservices
outside of a raw-manifest conversion, aware of the same `base/` +
per-namespace `values/` split and the defaults.yaml layering, so it never
asks you to duplicate what a lower layer already supplies.

---

## Verification performed

- `helm lint` — clean.
- `helm-unittest` — 231/234 tests passing (the 3 remaining failures are
  pre-existing, unrelated to any of the above: a `Statefulset`/`StatefulSet`
  casing typo in two test fixtures, and a stale `appVersion` expectation).
- Full regeneration of `gitops-factory/output` (250 microservices, 5
  namespaces) and `gitops-factory/output-live-demo` (7 microservices, 4
  namespaces): 264 releases rendered end-to-end through the real chart,
  zero duplicate resource ownership.
