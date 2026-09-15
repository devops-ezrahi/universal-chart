# STATUS — Workload_Metadata_Labels_Lost (P2, folder 20/35)

**Result: FIXED, both halves.** The single highest-count defect in the report —
~634 label fields / 3,087 labels.

15 tests RED→GREEN (RED proved by reverting both halves and restoring
byte-identically), all other folders still green, chart's own 234 still green,
fidelity unchanged at 0 lost / 0 changed.

---

## 1. The report is right about the mechanism, and wrong about two things

**Right:** two independent defects, both real.

| half | before |
|---|---|
| A — converter handlers never read `metadata.labels` | only **5** of ~25 handlers captured them |
| B — chart templates render only the identity helper | **19 of the chart's templates** had no own-labels block at all |

**Correction 1 — "LOW / cosmetic-metadata-only" understates it.**
`argocd.argoproj.io/instance` sits in the same bucket as `team` and
`environment`, and losing it makes ArgoCD stop managing the object while the
object stays on the cluster. The report's own MEDIUM #12 says exactly that about
ExternalSecrets (*"ArgoCD can't track/manage"*) — it just did not connect the two
rows. Ownership labels are cosmetic; tracking labels are not, and they were in
the same loss.

**Correction 2 — issue 33 says `service.yaml` has no `$spec.labels` block.**
It had one. The problem was its shape: a SECOND `labels:` entry under the same
key —

```gotemplate
labels:
  {{- include "universal-chart.labels" . | nindent 4 }}
  {{- with .Values.service.labels }}
  {{- toYaml . | nindent 4 }}
  {{- end }}
```

— which is a duplicate YAML key. Some parsers reject it outright; Helm keeps the
last one, so a values file could overwrite `app.kubernetes.io/name` and drift an
object's metadata away from what its selector matches. Six templates had this
shape. It is worse than the missing block, not better.

## 2. Files changed

### The chart — one helper, twenty templates

New `universal-chart.objectLabels` in `_helpers.tpl` builds **one merged map**:

```gotemplate
{{- define "universal-chart.objectLabels" -}}
{{- $core := fromYaml (include "universal-chart.labels" .root) }}
{{- toYaml (merge $core (.own | default dict)) }}
{{- end }}
```

`merge` leaves the destination intact, so the chart's identity labels **win** and
an object's own labels can only ADD — the same rule `universal-chart.labels`
already applies to `commonLabels`, and what keeps metadata permanently
consistent with `selectorLabels`.

Applied to every template: deployment, statefulset, daemonset, service,
services, serviceaccount, ingress, hpa, pdb, vpa, servicemonitor, cronjob, job,
configmap, secret, route, routes, pvc, pv, networkpolicy, storageclass, scc,
rbac (4 blocks), clustersecretstore, externalsecret, secretstore.

### The converter — one helper, every handler

New `raw_labels(doc)` returns the object's own labels minus `CHART_OWNED_LABELS`
(`app`, the four `app.kubernetes.io/*` identity labels, `helm.sh/chart`,
`pod-template-hash`). Excluding rather than carrying them is the same rule
`handle_workload` already applied to `podLabels`: the chart computes them from
`Release.Name`, and carrying the raw copies would either be redundant or
contradict what the selector matches on.

Wired into `handle_workload`, `handle_pvc`, `handle_networkpolicy`,
`handle_role`, `handle_serviceaccount`, `handle_cronjob`, `handle_job`, and the
five handlers that already captured labels now filter through the same helper so
the rule is consistent.

## 3. Tests

`Workload_Metadata_Labels_Lost/test/` — 15 tests. Fixture derives from
`namespaces-showcase/shop-web/api-gateway.yaml` with real custom labels on eight
object kinds: `team`, `environment`, `app.kubernetes.io/part-of` (135
occurrences in the source report) and `argocd.argoproj.io/instance`.

**Two of the tests are structural rather than behavioural**, and they are the
ones that make this fix stick:

- `test_no_template_renders_the_bare_identity_helper_for_object_metadata`
  greps every template. A new template that copy-pastes the old
  `{{- include "universal-chart.labels" $ }}` line reintroduces the defect for
  its own kind, and nothing else in the suite would notice.
- `test_no_template_emits_two_labels_blocks_under_one_key` catches the duplicate
  key coming back.

### RED — both halves reverted, then restored byte-identically

```
=== both halves of the label fix reverted ===

AssertionError: Service/api-gateway: team label lost, got {}
AssertionError: ConfigMap/api-gateway-config: team label lost, got {}
AssertionError: ServiceAccount/api-gateway: no labels at values path
  serviceAccount.labels; got ['create', 'name']
AssertionError: PersistentVolumeClaim/api-gateway-cache: no labels at values path
  pvc.api-gateway-cache.labels; got ['accessModes', 'size']
AssertionError: NetworkPolicy/api-gateway-allow: no labels at values path
  networkPolicies.api-gateway-allow.labels; got ['podSelector', 'policyTypes']
AssertionError: Role/api-gateway-reader: no labels at values path
  rbac.roles.api-gateway-reader.labels; got ['rules']
AssertionError: CronJob/api-gateway-rotate: no labels at values path
  cronjobs.rotate.labels; got ['jobTemplate', 'schedule']

10 failed, 5 passed in 1.66s
restored: True  sha256 c9eda5d9c692 6f75a30bc41b
```

Every object kind the report names, failing for its own reason.

### GREEN

```
15 passed in 1.37s
```

## 4. Validation chain

| Step | Result |
|---|---|
| Full showcase converter run | `122 objects matched (28 relocated). No fidelity issues found.` |
| Fidelity before / after | 0 lost / 0 changed, unchanged |
| Generated-output churn | **none** — see below |
| `helm lint` | 0 failed |
| Chart's own suite | 28 suites, 234 tests passed |

**Why a 3,087-label defect changes nothing in the generated output:** the
showcase's raw manifests carry exactly one object-level label, `app: <name>`,
which is chart-owned and correctly filtered out. So the defect was invisible
here for the same reason it was invisible in the fidelity report — the data does
not exercise it. That is why the fixture, not the count, is the evidence.

## 5. What could NOT be verified without a live cluster

- **That ArgoCD resumes managing objects** once `argocd.argoproj.io/instance` is
  present. Verified only that the label reaches the rendered manifest.
- **That no admission controller or policy engine rejects the added labels.** A
  label the source cluster accepted should be fine, but the target's policies
  are not visible here.
- **The migration effect on existing objects.** Adding labels to an object
  already on the cluster is a normal server-side-apply update, but
  `app.kubernetes.io/*` labels are immutable inside a Deployment's
  `selector.matchLabels` — this fix does not touch selectors, and
  `test_identity_labels_win_over_declared_ones` exists to keep it that way.
- **The report's 3,087 labels.** Those manifests are not in this repo.

## 6. OBSERVED, DEFERRED

- **`Annotations_Lost` is the same shape one field over** and is still open.
  Every template already has an annotations block (24 of them), so that folder is
  mostly a converter-capture question plus the triage error.txt asks for —
  `argocd.argoproj.io/*`, `prometheus.io/*` and `checksum/config` are the ones
  that would not be cosmetic.
- **`commonLabels` remains the release-wide mechanism** and is untouched. It is
  applied to every object; `labels` is one object's own. Conflating them was the
  report's suggested fix (*"commonLabels stays {} empty"*) and would have put one
  workload's `team` label onto every ConfigMap in the release.
- **`clean_annotations` has no label equivalent.** Annotations get a filter for
  server-generated noise (`kubectl.kubernetes.io/last-applied-configuration`
  and friends); labels get only the identity exclusion list. If a source cluster
  stamps generated labels, they will now be carried. None appear in this repo.
- Earlier folders' deferred items still stand.

## 7. Reproducing

```powershell
python -m pytest universal/gitops-factory/output-showcase/ERRORS_ANALYSIS/Workload_Metadata_Labels_Lost/test -v
```
