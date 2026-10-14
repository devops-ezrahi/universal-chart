# GitOps Factory — Raw Manifests → Universal Helm Chart → ArgoCD ApplicationSets

Converts raw per-namespace/per-microservice Kubernetes/OpenShift YAML dumps
into [Universal Chart](https://github.com/devops-ezrahi/universal-chart) values files and ArgoCD
`ApplicationSet`s, and includes a mock-environment generator used to
stress-test the conversion at scale (5 namespaces x 70 microservices).

This folder lives inside the Universal Chart repo, whose root is the chart
itself — so `--chart` defaults to `..` and nothing else needs cloning.
(It used to be the separate, now archived, `devops-ezrahi/gitops-factory` repo.)

`output/`, `output-showcase/` and `output-live-demo/` are generated and are not
committed; `python3 convert_and_verify.py` rebuilds the showcase from `namespaces-showcase/`.
The large mock dump `namespaces/` is not committed either — generate it with
`python generate_mock_environment.py --output namespaces --seed 42`.

### Converter options added in the fork merge

| Option / behaviour | What it does |
|---|---|
| `--argocd-manifests true` | Also writes the Argo CD wiring: `rootApplicationSet.yaml` + `rootApplication.yaml.txt` at the tree root (plus, under `--cluster-scoped`, `cluster-shared-application.yaml`). Off by default: then only `base/`, `<folder>/defaults.yaml`, `<folder>/values/` (and `cluster-shared/values/` under `--cluster-scoped`) are written, and wiring left by an earlier run is reported stale. A bare `--argocd-manifests` means `true`. The root Application is `.txt` on purpose — you apply it by hand, and no directory apply (Argo CD's included) ever picks it up. Files under the old names `root-application.yaml` / `root-applicationSet.yaml` are reported stale (`--prune` deletes them). |
| `--auto-sync [true\|false]` | Whether the Applications that deploy releases sync themselves. **Off by default**: `rootApplicationSet.yaml` passes `autoSync: false` to the ms-applicationSet chart (whose own default stays `true`, so existing wiring is unchanged), so every microservice Application — and `cluster-shared-application.yaml` — waits for a manual Sync. The root Application and root ApplicationSet still reconcile, so a new namespace folder appears in Argo CD by itself. |
| `--include PATTERN` | The allow-list: only microservice files with an object matching PATTERN (same matching as `--exclude`) are converted, and `--exclude` still wins. `--include payments,orders`; `--include app.kubernetes.io/part-of=payments` matches that exact label. Patterns match object names and label/annotation values, not namespace names; one that matches nothing stops the run before anything is written. With `--verify-fidelity`, what the selection left out is reported once per namespace as INFO, not as lost objects. |
| Sibling namespaces (`--auto-env-suffixes`) | `goldenbell-ui/goldenbell-ui.yaml` + `goldenbell-next/goldenbell-next.yaml` (or `web-ui` / `web-next`) are one app named after its namespace. Without `--env-suffixes`, a namespace's last word becomes an environment word when a file there carries it and a file in another namespace, carrying that namespace's last word, comes down to the same name: one `base/goldenbell.yaml` with the namespace templated (`{{ .Release.Namespace }}`, `{{ .Values.environment }}`), `environment: ui` / `next` in each `defaults.yaml`, and only the real differences (image tag, replicas) in each `values/goldenbell.yaml`. Their `shared.yaml` files are one `shared` release too, even when they name no object alike: one `base/shared.yaml`, each namespace's own objects in its `values/shared.yaml` (not a second `base/goldenbell-ui-shared.yaml`). One report line names the words. `--env-suffixes` replaces the guess; `--auto-env-suffixes false` turns it off. |
| `shared.yaml` under a selection | With `--include` or `--exclude`, `shared` keeps only what **two or more** converted microservices use. Used by one: moved into it. Used only by skipped ones: dropped. Used by none (e.g. `argocd-cm`): kept if `--include` names it (or, with no `--include`, if not excluded), else dropped. Use is followed through mounts/env/pull secrets/claims, a Service's selector, an Ingress/Route's backend, a RoleBinding's ServiceAccount and its Role, HPA/PDB targets, and ExternalSecret → store → SA/CA. One report line per object moved or dropped. |
| `--exclude PATTERN` / `--keep PATTERN` | Skip whole stacks by case-insensitive substring over object names and label/annotation values (never data). `--keep` wins. A pattern starting with `=` matches an object name exactly (`=web` is not `web-worker`), and `key=value` one exact label, in all three flags. Repeatable or comma-separated (`--exclude kafka,zookeeper`). |
| `--env-group NAME=tok[:VALUE],...` | A release whose file name carries one of these words moves into a folder of that name inside its namespace: `dev/ms1-yellow.yaml` → `dev/yellow/values/ms1.yaml` (release `ms1`); files without one stay in `dev/values/`. Values of one release that differ only by these words (plus the namespace name) become ONE templated value in `base/` — `filter.dev.yellow.example.org` → `filter.{{ .Release.Namespace }}.{{ .Values.color }}.example.org` — and each folder's `defaults.yaml` says `color: yellow`. A second group nests (`dev/yellow/eu/`). `--env-group namespace` enables only the namespace axis; `--interactive` asks before each combination. Repeatable. The chart renders every value with `tpl`, so the output renders back byte-for-byte (`--verify-fidelity --fidelity-strict` proves it). |
| Variant folders | `dev/yellow/` and `dev/black/` (made by `--env-group`, or already in the input — detected on their own) both deploy into namespace `dev` and share the one `base/`. Argo CD names gain the folder (`ms1-dev-yellow`); releases of two folders in one namespace are checked against each other. The root ApplicationSet finds every folder through its `defaults.yaml`. `values_editor.py` handles it too (`--namespace dev/yellow`). |
| `values/` sub-folders | `values/group1/ms1.yaml` is only grouping: the release is still `ms1`, reads `base/ms1.yaml`, and a re-run rewrites the file where it is. A file name may appear once per namespace folder (verification fails on duplicates). With Argo CD's new git-file globbing set the ms-applicationSet `valuesGlob` to `**/*.yaml`. |
| A name that merely contains a token | `--env-suffixes` / `--env-group` tokens only fold names that actually differ across files: `pay-dev` + `pay-prd` → `pay`, `ms1-yellow` + `ms1-black` → `ms1`. A file whose stripped name no other file shares — `project-api.yaml` in every namespace while `project` is also a token — keeps its own name and gets no variant folder, with one report line saying so. |
| `--env-suffixes tok,tok` | Strip these words from anywhere in a file name, replacing the default rule (strip a trailing suffix equal to the namespace's). They never make folders: values differing by them become `{{ .Values.environment }}` (`env` is the chart's env-var map), with `environment: dev` in every folder's `defaults.yaml`. A word given to both `--env-suffixes` and `--env-group` counts as a suffix. |
| Group tokens in keys | A map key differing only by group words (a volume `certs-black`/`certs-yellow`, a CronJob, sidecar, init container, ConfigMap, env var name) becomes one templated key in `base/` — `certs-{{ .Values.color }}` — and the chart `tpl`s keys too. A list (args, a sidecar's env) is templated whole the same way. Top-level keys (the chart's own) are never renamed. |
| Labels in `defaults.yaml` | A workload label or pod label every microservice in a namespace carries, with the same value, goes into that namespace's `defaults.yaml` and out of its `values/` files — so `environment: dev` is written once per namespace instead of once per microservice. Only releases with a Deployment/StatefulSet/DaemonSet count, since those labels render nowhere else: a `shared` release (or a CronJob-only one) with no labels no longer keeps them out. `base/` is not changed, as it is shared with other namespaces. |
| `--exact-copy` | For apps that cannot take a restart: renders every migrated pod template byte-for-byte as it runs, so the cutover sync restarts no pods — `portOrder` / `envOrder` / `volumeMountOrder` / `volumeOrder` / `sidecarOrder` in the raw order, and an empty `securityContext: {}` kept because the API server stored it. **Off by default**: the values carry only what the app needs, and the first sync rolls the pods once (the chart renders those lists sorted by name). Orders that change behaviour are written either way: `initContainerOrder` (run order), `envFromOrder` (a later ref wins a shared key) and `envOrder` when a value reads another var through `$(VAR)`. Selector labels and `chartLabels: false` are not part of it: a selector is immutable, so those are needed to adopt a live workload at all. |
| Literal env in base | A literal env var (`value: x`) is treated like any other value: in `base/` when it is the same in every namespace, in `values/` only where it differs. `--env-in-values` restores the old split, where every literal env var was written into every namespace's `values/` file. |
| Root ApplicationSet | One git files generator on `**/defaults.yaml` finds every namespace and variant folder. The values repo, branch and project are YAML anchors (`&repoURL`, `&branch`, `&project`), said once and reused as helm parameters; a commented `exclude` shows how to switch a namespace off. Without `--values-repo-url` it carries the placeholder `<values-repo-url>` to replace by hand. |
| `--root-set-name`, `--argocd-destination-namespace`, `--argocd-cluster` | Name the root ApplicationSet; alias of `--argocd-namespace`; deploy to a registered cluster *name* instead of `--destination-server`'s URL. |
| `{{` in source data | Escaped in every values file (`{{ "{{" }}`) so the chart's `tpl` pass leaves an Alertmanager/Go template in a ConfigMap literal. |
| Shared promotion | ConfigMaps/Secrets/etc. declared byte-identically by several microservices move into the namespace `shared` release under their original name (Services/Routes excluded). Recorded in `report/renames.json`. |
| One placement across namespaces | An object (kind + name, env words aside) is in `shared` only if it is shared in **every** namespace that has it. If it sits in a microservice's file in any namespace, it goes to that microservice (the most common owner) everywhere: out of `shared.yaml` in the namespaces where the importer filed it there, and not promoted where several files declare it identically — the owner keeps its copy, the others reference it by name. That is what lets `base/` see it as one object instead of per-namespace copies. A namespace without that microservice keeps the old placement and says so in the report. Services/Routes are not moved. |
| Cross-reference warnings | Pods referencing a ConfigMap/Secret/SA nobody creates, an SA with no RoleBinding, or a missing NetworkAttachmentDefinition. Report-only. |
| NetworkPolicy / ServiceMonitor | **Not converted.** Dropped at load time with one line per object in `report/conflicts_and_warnings.txt` (`NetworkPolicy/<name>: dropped — ...`); never in values or `extraDeploy`, and the fidelity / name checks do not count them as lost. The chart's own `networkPolicies` / `serviceMonitor` keys still work for hand-written values. `namespace_importer.py` no longer fetches them. |
| Unused ServiceAccount | A ServiceAccount in a microservice file that none of its pods runs as (e.g. a SecretStore's Vault SA) goes to `extraDeploy` instead of becoming the chart's `serviceAccount` — which would move the pods onto it. Pods on `default` stay there (`serviceAccount.create: false`). |
| Other | PDB and NetworkAttachmentDefinition pass through `extraDeploy` verbatim; `initContainerOrder` preserves init container order; a PVC `volumeName` whose PV is not in the dump is dropped; redis/emptyDir/nodePort lint warnings; multi-line strings render as `\|` blocks. |

## Files

| File | Purpose |
|---|---|
| `namespace_importer.py` | Pulls a namespace (or one app / one resource / a list of topology URLs) from a live cluster with `oc` and writes the converter's input: `<output>/<namespace>/<microservice>.yaml` + `shared.yaml`, cleaned of runtime metadata. An ExternalSecret brings its SecretStore/ClusterSecretStore (`spec.secretStoreRef`) along, with the store's Vault CA ConfigMap/Secret (`caProvider`) and ServiceAccount (`auth.kubernetes.serviceAccountRef`); each goes to `shared.yaml` only when more than one microservice file needs it, otherwise into that microservice's file — written once either way. |
| `convert_to_universal_chart.py` | The whole pipeline in one script: raw YAML in, Helm values + the two root ArgoCD manifests out, then (unless `--skip-verify`) every generated release is rendered for real with `helm template` and checked so no two releases claim the same Kubernetes resource. |
| `values_editor.py` | Values-authoring/editing CLI for microservices you're adding or tuning **by hand** (not converted from a raw dump) — defaults.yaml-aware, so it never asks you to duplicate what a lower layer already covers. See [values_editor.py — hand-authoring microservices](#values_editorpy--hand-authoring-microservices). |
| `ui/multi-app-manager.html` | The same idea as `values_editor.py`, as a browser UI — open multiple apps' output trees side by side, browse/edit visually, no install. See [ui/multi-app-manager.html — browser UI](#uimulti-app-managerhtml--browser-ui). |
| `generate_mock_environment.py` | Generates a large, deliberately messy mock raw-dump fixture. |
| `namespaces_to_chart.py` | Namespaces in, values tree out: pulls each `-n` namespace with `namespace_importer.py`, runs the converter over the dump, and passes every other flag to it (`--env-suffixes`, `--env-group`, `--argocd-manifests`, …). `-o` gets only the tree and nothing else is left behind (`--report DIR` / `--dump DIR` keep those). `--oc kubectl` for plain Kubernetes; `--from-yaml` answers the importer from manifest files instead of a cluster. |
| `convert_and_verify.py` | Convert + name check + fidelity check + sanitize, in one command. The default entry point. |
| `regression/` | The pytest regression suite — one folder per fixed defect (`STATUS.md`, `error.txt`, fixtures, a test). `pytest` runs it; see `pytest.ini`. |
| `docs/` | `BUGFIX-REPORT.md` (what was fixed in the chart and the converter) and `README_REGRESSION_ANALYSIS.md` (cross-reference of the two analysis rounds). |

## namespaces_to_chart.py — namespaces in, chart out

The shortest path from a running namespace to a values tree: one command runs
`namespace_importer.py` over every namespace you name, then
`convert_to_universal_chart.py` over what it pulled, and leaves **only the
chart tree** in `-o`. No dump folder, no report, nothing beside it.

```bash
# Live cluster (oc on PATH, logged in)
python namespaces_to_chart.py -n shop-dev -n shop-prd -o out

# Plain Kubernetes
python namespaces_to_chart.py -n shop-dev,shop-prd -o out --oc kubectl --kubeconfig ~/.kube/lab

# No cluster: answer the importer from manifest files or folders
python namespaces_to_chart.py --from-yaml dump/ extra.yaml -o out
```

### What it does

1. **Pull.** Each `-n` namespace goes through the importer's own
   full-namespace mode — the same as `namespace_importer.py -n <ns>`:
   workloads grouped into microservices (`part-of` → `app` → workload name),
   the ConfigMaps/Secrets/SAs/claims they reference, `shared.yaml` for what
   several of them use, runtime metadata stripped. An Ingress travels with the
   microservice whose Service it routes to.
2. **Convert.** The converter runs over that pull. **Every option this script
   does not know is handed to the converter unchanged**, so anything in
   `convert_to_universal_chart.py --help` works here too.
3. **Verify.** Unless you pass `--skip-verify`, every release is rendered with
   `helm template` against the chart this script ships beside, and checked so
   no two releases claim the same object. It works from any directory.
4. **Clean up.** The pull and the converter's report are deleted. `-o` holds
   `base/` and one folder per namespace (plus the ArgoCD wiring, if you asked
   for it).

### Options

| Option | Meaning |
|---|---|
| `-n NS` | Namespace to pull. Repeat it, or comma-separate: `-n a -n b,c`. |
| `-o DIR` | Where the values tree is written. Required. |
| `--oc CLI` | Cluster CLI to call. Default `oc`; `kubectl` works for plain Kubernetes (OpenShift-only kinds like Routes are then just not found). |
| `--kubeconfig FILE` | Passed to every `oc`/`kubectl` call. |
| `--from-yaml PATH…` | Read manifests from files/folders instead of a cluster (`kubectl get -o yaml` output, a `List`, several documents per file — anything). Documents are grouped by their own `metadata.namespace`; with `-n`, only those namespaces are kept. |
| `--default-namespace NS` | With `--from-yaml`: the namespace for documents that name none (what `helm template` renders). Without it, such a document is an error. |
| `--dump DIR` | Keep the importer's pull here instead of a temp dir. Must be empty or new. Useful to hand-edit the pull and then run the converter on it yourself. |
| `--report DIR` | Keep the converter's report (`conflicts_and_warnings.txt`, `conversion_report.json`, `renames.json`) here. |
| anything else | Passed to the converter: `--env-suffixes`, `--env-group`, `--argocd-manifests`, `--values-repo-url`, `--chart-revision`, `--skip-verify`, … |

`--input` and `--output` are the script's own business and are refused.

### Common recipes

```bash
# Environment suffixes stripped from names, colour variants grouped,
# ArgoCD wiring written, report kept for review
python namespaces_to_chart.py -n pay-dev,pay-stg,pay-prd -o values \
    --env-suffixes dev,stg,prd --env-group color=black,yellow \
    --argocd-manifests true --values-repo-url https://git.example.com/gitops/pay-values.git \
    --report values-report

# A packaged chart, rendered first, converted like any other manifests
helm template shop ./shop-chart -n shop-dev > shop.yaml
python namespaces_to_chart.py --from-yaml shop.yaml --default-namespace shop-dev -o out
```

### What it prints

- `==> pulling namespace <ns>` per namespace, then the importer's own fetch
  summary.
- `warning: nothing was pulled from '<ns>'` for a namespace with no workloads
  — it is left out of the tree rather than failing the run.
- `==> N referenced object(s) could not be fetched` — a Secret, ConfigMap or
  store some workload references that the cluster (or the YAML) does not have.
  The tree is still written; the reference is left as it was.
- The converter's summary, and — without `--report` — a note that the report
  path it names is gone.

The exit code is the converter's: `0` means converted **and** verified.

### What it does not carry

- **Cluster-scoped objects nothing references.** The importer pulls a
  namespace, so a ClusterRole, ClusterRoleBinding or StorageClass that no
  namespaced object points at is not pulled. Add those to the converter's
  input by hand: `--dump pulled/`, drop them in, then run
  `convert_to_universal_chart.py --input pulled …` yourself.
- **Variant folders.** A cluster has namespaces, not `prd/yellow`; variant
  folders come from the converter's `--env-group`, or from a hand-arranged
  `--dump`.

The devops-portal's **Convert** runs the same importer over pasted YAML
(its `SPLIT_DUMP` adapter is what `--from-yaml` mirrors), so a tree from this
script and one converted in the portal come out the same.

## Input contract

```
namespaces/
  <namespace>/
    shared.yaml              # optional — resources shared by multiple microservices
    <microservice-a>.yaml    # multi-document YAML, one file per microservice
    <microservice-b>.yaml
```

## Output layout

```
output/
  base/                                # comprehensive, environment-agnostic values — FLAT, one file
                                        # per release-slug, SHARED across every namespace that runs it
    shared.yaml                        # owns everything from shared.yaml (per namespace it first appears in)
    <release-slug>.yaml                # usually just the microservice's own name — see release-slug below
  <namespace>/                         # one directory per namespace — its EXISTENCE is what deploys it
    defaults.yaml                      # NAMESPACE defaults — group values + labels identical across every release in this
                                        # namespace, the 'shared' release included, plus group values
                                        # (environment: dev) from --env-suffixes / --env-group
    values/                            # image + whatever differs from base in this namespace ONLY —
                                        # hand-editable, one file per release-slug. Its existence is what
                                        # deploys the release: this directory IS the fan-out.
      <release-slug>.yaml
      <group>/<release-slug>.yaml      # optional grouping sub-folder; the release is still <release-slug>
    <variant>/                         # optional variant folder (dev/yellow, nested dev/yellow/eu) —
      defaults.yaml                    # same shape, deploys into <namespace>, shares base/; its
      values/<release-slug>.yaml       # defaults.yaml carries the group value (color: yellow)
  rootApplication.yaml.txt             # --argocd-manifests true: the ONE object you apply by hand (.txt: never picked up by a directory apply)
  rootApplicationSet.yaml              # --argocd-manifests true: one Application per namespace (and variant) folder
  cluster-shared/                      # only with --cluster-scoped
    values/
      cluster-shared-values.yaml       # ClusterRole/ClusterRoleBinding/StorageClass/PV/SCC/ClusterSecretStore, deduped globally
  cluster-shared-application.yaml      # only with --cluster-scoped
  report/
    conversion_report.json
    conflicts_and_warnings.txt         # every auto-resolved naming conflict, explained
    render_conflicts.txt               # only written if the helm-template verification step finds a real conflict
```

## The ArgoCD wiring: two files and one chart

The entire deployment wiring is the two files at the tree root, plus the
[`ms-applicationSet`](https://github.com/devops-ezrahi/universal-chart/tree/main/ms-applicationSet)
chart that lives in the Universal Chart repo:

```
kubectl apply -f output/rootApplication.yaml.txt     # once, by hand
  └─ deploys rootApplicationSet.yaml                 # git files generator on '**/defaults.yaml'
       └─ one Application per <namespace>/ folder, each deploying ms-applicationSet
            └─ one ApplicationSet per folder          # git files generator over <folder>/values/<valuesGlob>
                 └─ one Application per release, rendering the Universal Chart
```

`kubectl apply -f` reads a file it is given by name whatever its extension; it
filters on `.yaml`/`.yml`/`.json` only while walking a directory, so
`kubectl apply -f output/` skips it (checked with kubectl 1.36). Argo CD's
directory source applies the same filter,
which is why the root Application is `.txt`: it can sit in the repo it
bootstraps without Argo CD ever deploying or managing it.

The root ApplicationSet passes the `ms-applicationSet` chart `namespace`,
`folder` (the variant folder path, e.g. `prd/yellow`; empty for a plain
namespace), `originRepoURL`/`originBranch`/`originPath` and, with
`--argocd-cluster`, `destinationName`. `valuesGlob` (default `*.yaml`) is the
chart's own knob for Argo CD's new git-file globbing (`**/*.yaml`).

Three consequences worth stating plainly:

* **Adding a namespace is adding a directory. Adding a microservice is adding a
  file.** Neither needs a new manifest, and neither needs another `kubectl apply`.
* **The layering rules live in the chart, not in this output.** One place,
  versioned alongside the chart that applies them, instead of a copy baked into
  every values repo this converter ever writes.
* **`rootApplication.yaml.txt` names the files it deploys outright** and does not
  recurse. ArgoCD matches directory globs against paths relative to `source.path`
  and a single `*` spans directory levels
  ([argo-cd#7319](https://github.com/argoproj/argo-cd/issues/7319)), so a pattern
  like `*application.yaml` would also match a microservice legitimately named
  `web-application` down in `<ns>/values/` — and ArgoCD would try to apply a
  values file as a manifest. Naming the files outright makes that unreachable,
  whatever a namespace or microservice is called.

Each generated Application is named `<release>-<last "-" token of the
namespace>` — `jenkins` in `devops-team-dev` becomes Application `jenkins-dev`.
Two namespaces ending in the same token that run a release of the same name
would produce one Application name for two deployments; the converter detects
that and reports it in `report/conflicts_and_warnings.txt` rather than
generating a tree that silently fights itself.

**base/ is shared across namespaces.** A microservice's comprehensive,
environment-agnostic file is written **once**, under its own name
(`base/<microservice>.yaml`), and every namespace that runs a microservice by
that name reuses it — that's the whole point: the same "buildkit" or
"jenkins-restore" utility deployed into five namespaces gets ONE base file,
not five duplicates. Each namespace still gets its own flat `values/` tree
holding only that namespace's image tag + whatever differs from base there.

**release-slug**: normally the microservice's own name. It only becomes
`<namespace>-<microservice>` when two namespaces declare a microservice by
the same name but with genuinely *different* base content — the converter
detects this (by content hash, not just name) and falls back to a
namespace-qualified slug for the conflicting one instead of silently
overwriting or incorrectly sharing, the same claim/rename-on-conflict
philosophy used everywhere else in this script (see "Conflict-free by
construction" in the converter's own docstring). Check
`report/conflicts_and_warnings.txt` for any `base content differs` lines —
each one names the namespace that got a `<namespace>-<name>.yaml` fallback
instead of sharing the plain `<name>.yaml`.

The image tag and any literal-value (`value:`, not `valueFrom:`) env vars live
**exclusively** in `<namespace>/values/<slug>.yaml` — the comprehensive
`base/<slug>.yaml` omits them rather than duplicating them, so a day-2 image
bump or config tweak only ever touches the per-namespace minimal file, never
the shared base. `image.repository`, `pullPolicy`, and any `valueFrom`-based
env (ConfigMap/Secret/field refs — structural wiring, not day-2 tuning) stay
in the comprehensive `base/` file, since the minimal file has nowhere else to
apply them from. A per-namespace `defaults.yaml` is deliberately never
subtracted from `base/`, since a delta specific to one namespace's release mix
may not hold for every other namespace reusing the same base file.

The `base/` and `values/` filenames for one release are always the **same
name**. That is what keeps the chart's chain self-consistent: it builds
`base/{{ .path.filename }}` and `<namespace>/values/{{ .path.filename }}` from
one filename, so a release whose slug had to be disambiguated still resolves
both halves correctly.

`shared` is an ordinary release like any other — `base/shared.yaml` plus
`<namespace>/values/shared.yaml`, discovered by the same fan-out. Namespaces
whose shared release declares the same objects (`shop-dev`, `shop-tst`,
`shop-prd` each holding `ConfigMap/shop-common`) are one release, split like a
microservice: what every copy agrees on goes in `base/shared.yaml`, and each
`values/shared.yaml` holds only what differs there, e.g. `ENV: prd`. With
`--env-suffixes` or `--env-group` even that becomes a template in `base/` and
`values/shared.yaml` is `{}`. Every namespace still has its own
`values/shared.yaml`, since that file is what deploys the release. A namespace
whose shared objects have nothing in common with the first group's (another
app's namespaces in the same run), or that ends in the same `-<token>` as one
already in it, gets its own name: `pay-shared` for `pay-dev` + `pay-prd`, or
`<namespace>-shared` for a namespace on its own. Before this, a shared release
that differed in a single env word was forked into one
`<namespace>-shared.yaml` per namespace.

## Cutting a live app over from its old ArgoCD apps

The aim is that the cutover restarts no pods. The only difference should be
`argocd.argoproj.io/instance`, which ArgoCD stamps on each object's own
metadata. That label never reaches the pod template, so changing it doesn't
restart anything.

Pods restart only when `spec.template` or `spec.selector` changes. The selector
is immutable, so changing it forces ArgoCD to delete and recreate the workload,
which kills every pod at once. To keep both unchanged, the converter carries
the following over from the source manifest:

- The **existing selector**, as `selectorLabels`. The pod template keeps every
  raw label, including identity labels such as `app`.
- The **list order** of env, ports, volumeMounts, volumes, envFrom and sidecars
  (`envOrder`, `portOrder`, ...). Helm would otherwise render them sorted.
- An explicit **empty container `securityContext: {}`**.
- **No** `serviceAccountName` or `automountServiceAccountToken` the pod didn't
  have. A pod running as `default` gets `serviceAccount.create: false`.
- **No** `kubectl.kubernetes.io/restartedAt` or
  `openshift.openshift.io/restartedAt`. These are stamps from a manual
  `oc rollout restart`, not configuration.

The chart also no longer defaults a Route's `insecureEdgeTerminationPolicy` to
`Redirect`.

Order of operations, per namespace:

1. Remove the old tree from ArgoCD **without cascading**. Every resource stays
   running:
   `scripts/argo-orphan-tree.sh <old-root-app> -n <argocd-ns>`
   This is a dry run; it prints the tree and every command. Add `--apply` to
   execute it.
   - The script turns off auto-sync parents-first and deletes ApplicationSets
     with `--cascade=orphan`.
   - It then removes each Application's finalizers before deleting it, deepest
     first, in parallel.
   - It refuses to run if the tree reaches any app made by `ms-applicationSet`.
2. Apply the new ApplicationSet. If you want to review the diff first, disable
   auto-sync on it.
3. `argocd app diff <app>` should show nothing under `spec.template` or
   `spec.selector`. After the sync, `oc get rs` should show no new ReplicaSet.

## defaults.yaml — factoring out what's common

This is the same "each layer only has what's different" idea already
documented in [`examples/gitops/STRUCTURE.md`](https://github.com/devops-ezrahi/universal-chart/blob/main/examples/gitops/STRUCTURE.md)
(`_base/defaults.yaml`) — except computed automatically from the converted
values instead of hand-written, which matters once you have real scale (say
100 similar microservices in one namespace): every one of those files would
otherwise repeat the same `resources`/`probes`/`securityContext` boilerplate
in full, making it hard to see what actually makes one microservice different
from another.

**The algorithm** (`common_subtree` in the converter): across every release in
one namespace — the `shared` release included — recursively find the key/value
pairs that are byte-identical in literally every one of them. Of those, only
the labels (`commonLabels`, `podLabels` — `DEFAULTS_KEYS`) are kept, plus the
folder's group values (`environment: dev`, `color: yellow`); that becomes
`output/<namespace>/defaults.yaml`. Everything else common is already in
`base/` or in every `values/` file — neither is subtracted against this layer —
so copying it here only made a second source of truth that editors read as
"set once for the namespace" when it was not.

**Why this is safe to do automatically**: a key is only ever promoted to a
defaults file if it is *present, with the exact same value*, in **every**
release being compared — never a majority, never "most of them." Helm merges
maps, so if defaults.yaml supplied a value that some release never actually
had, that release would silently inherit it. Requiring 100% agreement makes
that impossible: nothing a release didn't already effectively have gets added
by the defaults layer. Comparison is recursive per-key, not
whole-block — `resources.requests.cpu` can be promoted even if
`resources.limits` differs per release, and lists/scalars must match exactly
(never partially merged).

Layering order, built by the `ms-applicationSet` chart (lowest → highest
precedence): `base/<release-slug>.yaml` → `<folder>/defaults.yaml` →
`<folder>/values/<release-slug>.yaml`. Every release goes through that one
chain. (It used to be defaults → base → values; defaults now sits above the
shared `base/`, so a namespace's common value wins over the cross-namespace
base.)

Every value is rendered through Helm's `tpl` by the chart, which is what lets
`base/` hold `{{ .Values.environment }}` / `{{ .Values.color }}` /
`{{ .Release.Namespace }}` and each folder's `defaults.yaml` fill them in. The
flip side: a literal `{{` coming from the source (an Alertmanager template in a
ConfigMap) is written escaped as `{{ "{{" }}` in the values files, and anything
you add by hand needs the same escaping.

### Why `shared` is in the defaults pool

`shared.yaml` is meant to be a namespace-wide resource pool — the natural
place to put a Route/Ingress/PVC/ServiceAccount that several microservices
rely on, not just ConfigMaps. That only works if `shared`
renders from *exactly* what it declares, nothing else.

Because there is now one fixed chain for every release, `shared` reads
`<namespace>/defaults.yaml` like everything else, and ArgoCD's templating has
no way to vary the chain per matched file. Excluding `shared` from the pool
that file is *computed* from would therefore be unsafe: a setting two
unrelated sibling microservices simply happen to agree on (say, both have
`route.enabled: true` with identical TLS settings) would be promoted into that
namespace's defaults and silently switch on a Route on `shared` too — one it
never declared and, having no workload of its own, no Service to back. The
chart's own validation catches that at render time ("route.enabled is true but
route.targetPort is unset and service.ports is empty"), which is correct
behavior, but the point is it should never come up.

So `shared` is in the pool. The 100%-agreement rule then does the work: a key
reaches `<namespace>/defaults.yaml` only if `shared` declares it identically
too, which makes "the defaults layer added something to `shared`" unreachable
by construction. The cost is a smaller defaults layer in any namespace that
has a `shared.yaml` — the right trade, since a defaults file is an
optimisation and rendering objects nobody asked for is a defect.

There is no global (tree-root) `defaults.yaml`. The chart layers three files —
`base/<file>` → `<folder>/defaults.yaml` → `<folder>/values/<file>` —
and none of them is a tree-root one, so such a file would be applied by
nothing; and computing one safely would mean intersecting an entire run's
microservices with every namespace's `shared` release, which leaves
essentially nothing. Whatever is genuinely common across namespaces is
repeated in each `<namespace>/defaults.yaml` instead — N small copies rather
than one, which is what the values-repo layouts this targets do by hand.

The defaults files are regenerated fresh on every converter run — like the
values files, treat them as generated output, not something to hand-edit.

## values_editor.py — hand-authoring microservices

`convert_to_universal_chart.py` is for converting an *existing* raw manifest
dump. `values_editor.py` is for the other case: a microservice you're adding
or tuning **by hand**, directly into an app's already-converted output tree
(the `<app-dir>` in the commands below is exactly what one converter run's
`--output` produces — see [Output layout](#output-layout)). It's
defaults.yaml-aware, so it shows you what the namespace layer already
supplies.

```bash
# See what's already in an app: namespaces, microservices, how many keys
# each defaults.yaml already covers.
python values_editor.py list --app-dir output/payments-app

# Print the fully-merged EFFECTIVE values for one microservice — global
# base/ -> namespace defaults -> this namespace's values/,
# exactly what would actually deploy.
python values_editor.py show --app-dir output/payments-app \
    --namespace dev --name checkout

# Scaffold a brand-new microservice. Anything you set here that's already
# identical to what defaults.yaml supplies is reported and OMITTED from the
# file it writes — you only ever get the real delta on disk. If base/checkout.yaml
# already exists (another namespace already runs a "checkout"), the
# structural flags below are ignored and that shared base is reused as-is —
# only this namespace's values/checkout.yaml gets written.
python values_editor.py new --app-dir output/payments-app \
    --namespace dev --name checkout \
    --image my-registry/checkout:1.0.0 \
    --port http:8080 \
    --service-port http:80:http \
    --route-host checkout-dev.apps.example.com \
    --cpu-request 100m --mem-request 128Mi --cpu-limit 500m --mem-limit 256Mi \
    --env LOG_LEVEL=debug

# Patch a single key on an existing microservice. image.tag and literal
# (value:, not valueFrom:) env vars are routed to this namespace's minimal
# file automatically, matching the same split convert_to_universal_chart.py
# itself uses — everything else goes to the shared base/ file (and so
# affects every other namespace reusing it).
python values_editor.py set --app-dir output/payments-app \
    --namespace dev --name checkout \
    --key resources.limits.memory --value 512Mi

# Validate every microservice's merged values against the chart's schema,
# and (with --chart) render each one for real to catch what the schema
# can't.
python values_editor.py validate --app-dir output/payments-app \
    --schema ../values.schema.json --chart ..
```

`new` and `set` both use `--set`/generic `--key`/`--value` flags for a
dotted path (e.g. `resources.limits.memory`, `podSecurityContext.runAsUser`)
for anything without a dedicated flag — the value is parsed as YAML, so
`80` becomes an int, `true` a bool, `[a,b]`/`{a: 1}` a real list/dict, and
anything else stays a plain string.

## ui/multi-app-manager.html — browser UI

It reads the converter's tree: the shared `base/`, and every folder —
namespace (`dev/`) or variant (`dev/yellow/`) — with its `defaults.yaml` and
`values/` (group sub-folders included). **Base (shared)** edits
`base/<release>.yaml`, which every folder running that release reads;
**Values (this folder)** edits the folder's own file where it sits. A new
microservice whose `base/` file already exists reuses it and only writes the
folder's values file, like `values_editor.py new`.

The same idea as `values_editor.py`, as a single self-contained HTML file —
open it directly in **Chrome or Edge** (no server, no install, no build
step). It reads and writes real files on your machine via the browser's
[File System Access API](https://developer.mozilla.org/en-US/docs/Web/API/File_System_API)
— nothing is uploaded anywhere, and it works fully offline (the YAML parser
it depends on, [js-yaml](https://github.com/nodeca/js-yaml), is vendored
inline in the file, not fetched from a CDN).

**"+ Open App Folder"** picks a local directory — the same `<app-dir>` shape
`values_editor.py` operates on — and adds it as a tab. Open **multiple** app
folders at once (the multi-app-repo architecture this whole project is built
around) and switch between them with the tabs at the top.

Within an app: the sidebar lists namespaces (with how many keys each
`defaults.yaml` covers) and, once expanded, its microservices plus a
**"+ New microservice"** entry. Selecting a microservice gives you four
views:

- **Effective (merged)** — read-only, meant to match `values_editor.py show`
  (base → namespace defaults → values, the same chain the chart builds).
- **Values** / **Minimal** — the raw YAML for that file, editable directly,
  saved straight back to disk.
- **Form editor** — structured fields mirroring `values_editor.py new`'s
  flags (image, ports, service ports, route host, resources, replicas,
  literal env vars). Pre-filled from the effective values when editing an
  existing microservice. On save, anything you entered that's identical to
  what `defaults.yaml` already supplies is reported and **left out** of the
  file that gets written — the same no-duplication guarantee as the CLI,
  just visual.

Browser support note: File System Access API is Chromium-only (Chrome,
Edge) as of this writing — Firefox and Safari aren't supported. The page
detects this and tells you plainly rather than failing silently.

### Testing this file

Since it can't be driven by the CLI-oriented tools elsewhere in this repo,
it has its own test setup:

```bash
npm install jsdom          # dev-only, not a runtime dependency of the page itself
node ui/core-logic.test.js # pure-JS unit tests for the deepMerge/subtractDefaults
                            # logic embedded in the page — mirrors
                            # convert_to_universal_chart.py's deep_merge /
                            # common_subtree / subtract_defaults; keep these in
                            # sync if either side changes
node ui/e2e-test.js <path-to-an-app-dir-copy>
                            # loads the REAL html/js into jsdom, wraps a real
                            # directory on disk behind a minimal File System
                            # Access API-compatible adapter, and drives it
                            # exactly like a browser would (opens the app,
                            # views effective values, edits an existing
                            # microservice via both the form and the raw YAML
                            # tab, creates a new one) - point it at a
                            # disposable COPY of an app-dir, since it writes
                            # real files
```

## Why it's conflict-free

Several Universal Chart resource kinds render with the **raw values map-key**
as the Kubernetes resource name (no `<release>-` prefix): ConfigMap, Secret,
PVC, Role, RoleBinding, SecretStore, ExternalSecret, and (cluster-scoped) ClusterRole,
ClusterRoleBinding, StorageClass, PersistentVolume, SecurityContextConstraints.
Those are exactly the ones that collide once every microservice becomes its
own Helm release. The converter resolves this deterministically:

1. **shared.yaml owns the canonical name.** Anything declared there becomes
   the namespace's `shared` release. If a microservice's own file also
   happens to contain that same object (a common artifact of raw cluster
   dumps that capture shared objects once per consuming workload), the
   duplicate is dropped and the microservice just references the shared
   name — no double ownership.
2. **Private name collisions are auto-renamed.** If two microservices
   independently declare a same-named ConfigMap/Secret/etc. that ISN'T from
   shared.yaml, the second one is renamed to `<microservice>-<name>`, and
   every reference to it inside that microservice's own values (`env`,
   `envFrom`, `volumes`, RBAC `roleRef`/`subjects`) is rewritten to match.
   Logged either way — silent when content is identical, still logged
   because two releases can never jointly own one object regardless.
3. **Cluster-scoped kinds are deduped globally**, across every namespace in
   the run, not renamed (a StorageClass/SCC name is referenced by exact
   string elsewhere, so renaming it would silently break those references).
   If two namespaces define the same cluster-scoped name with *different*
   content, the first wins and it's flagged in the report for a human to
   resolve — this is the one case that can't be auto-resolved safely.

See `conflicts_and_warnings.txt` after a run for the full, human-readable log
of every decision the converter made.

A microservice can also **consume** a ServiceAccount/Role/etc. it doesn't own
(e.g. a pod running as the namespace-shared ServiceAccount, or a RoleBinding
whose Role got renamed by the collision logic above). In that case its
values only get a *reference* — `serviceAccount.create: false` for a
consumed SA, remapped `roleRef`/`subjects`/`env`/`volumes` names for anything
renamed — never a duplicate ownership entry. This is order-independent
(resolved in a name-claiming pass before any resource is built) so it's
correct regardless of which order objects appear in the source multi-doc
file.

## Avoiding ArgoCD "OutOfSync every few seconds" from OpenShift admission

Two real drift sources are handled up front rather than patched after the
fact:

1. **Live Pod objects are never converted.** SCC/admission mutates the
   *Pod* (injecting `runAsUser`/`fsGroup`/`seLinuxOptions` from the
   namespace's allocated UID range), not the Deployment/StatefulSet/DaemonSet
   that owns it. The converter drops `Pod` and `ReplicaSet` objects entirely
   (`TRANSIENT_KINDS`) specifically so those admission-injected values are
   never captured into `podSecurityContext`/`securityContext` in the first
   place — the classic way teams accidentally bake a UID range from one
   namespace into a values file that then fights every other namespace's SCC.
2. **Every generated ApplicationSet still carries a defensive
   `ignoreDifferences`** (`common_ignore_differences()` in the converter) for
   the handful of fields OpenShift/Kubernetes mutate on the *live* object
   after admission regardless: `securityContext.{runAsUser,runAsGroup,
   fsGroup,seLinuxOptions}` on Deployment/StatefulSet/DaemonSet pod
   templates, container-level `runAsUser`/`capabilities`, and
   ServiceAccount's controller-populated `secrets`/`imagePullSecrets`.
   StatefulSet `volumeClaimTemplates` is included too — it's immutable after
   creation, so a diff there can never be applied and would otherwise loop
   forever. HPA-managed `replicas` needs no such entry: the chart's
   `deployment.yaml`/`statefulset.yaml` omit the `replicas` key from the
   rendered manifest entirely whenever `hpa.enabled: true`, so there's
   nothing for Argo to diff against in the first place.

## Chart changes made to support this (the chart at `..`)

- `workload.type: none` — new option (alongside `deployment`/`statefulset`/
  `daemonset`) for microservices with no compute workload at all (just
  ServiceAccounts/RBAC/ConfigMaps). Guarded in `_helpers.tpl` so `hpa`/`vpa`/
  `pdb` correctly refuse to enable with no pods to target.
- `routes: {}` / `services: {}` — new map-based, plural counterparts to the
  existing singular `route:`/`service:`, for microservices that legitimately
  need more than one OpenShift Route or Service object. The singular keys are
  untouched and remain the common-case path.
- `secretStores: {}` / `clusterSecretStores: {}` / `externalSecrets: {}` —
  [External Secrets Operator](https://external-secrets.io) support. The
  converter maps `SecretStore`/`ExternalSecret`/`ClusterSecretStore` objects
  from a raw dump into these directly (they're first-class `NS_RAW_NAME_KINDS`
  / `CLUSTER_SCOPED_KINDS` now, same dedup/collision handling as ConfigMap/
  Secret/StorageClass) — they no longer fall through to `extraDeploy`.

Everything else in the chart was already exactly what this conversion
needed (map-based values throughout, `extraDeploy` as a raw-YAML escape
hatch for anything unrecognized).

## Running the full end-to-end verification

```bash
cd gitops-factory        # inside the universal-chart repo
pip install pyyaml

# 1. Generate the mock raw-dump fixture: 5 namespaces x 70 microservices,
#    with deliberately planted naming conflicts and edge cases.
python generate_mock_environment.py --output namespaces --seed 42

# 2. Convert it AND verify it in one call. This does everything:
#      - builds Helm values + the root ArgoCD manifests from the raw manifests
#      - resolves/renames naming conflicts deterministically (logged)
#      - renders every generated release for real with `helm template` and
#        checks no two releases claim the same Kubernetes object
python convert_to_universal_chart.py --input namespaces --output output \
    --chart .. --argocd-manifests true \
    --chart-repo-url  https://github.com/devops-ezrahi/universal-chart.git \
    --values-repo-url https://git.example.com/gitops/microservices-values.git
```

`--chart` defaults to `..`, the root of this repo, which is the chart itself, so
it can usually be omitted. Pass `--skip-verify` to skip the `helm template`
render step (e.g. `helm` isn't installed, or you just want a fast dry run of
the conversion) — you still get the values/root manifests/report either way.

Expected output:

```
Converted 350 microservices across 5 namespaces.
Auto-resolved conflicts / warnings: 55 (see output/report/conflicts_and_warnings.txt)
Base (shared) values written under: output/base/
Minimal values written under:       output/<namespace>/values/
ArgoCD wiring:                      output/rootApplication.yaml.txt + output/rootApplicationSet.yaml
Bootstrap with:                     kubectl apply -f output/rootApplication.yaml.txt

Verifying against .. — rendering every release with `helm template`...
Rendered 356 releases.
Namespaced resource claims checked: 2081
Cluster-scoped resource claims checked: 3

No duplicate resource ownership detected across any of the 356 rendered releases. Conflict-free.
```

Check `output/report/conflicts_and_warnings.txt` — every entry there is a
conflict the converter resolved automatically (or, for the one deliberately
irreconcilable cluster-scoped fixture, flagged for manual review). If the
render-verification step ever finds a REAL conflict (it shouldn't, given the
static analysis above — this step exists to catch template bugs, not
converter bugs), it's written to `output/report/render_conflicts.txt` and the
script exits non-zero.

### Wiring it into a real ArgoCD

1. The chart is its own repo — https://github.com/devops-ezrahi/universal-chart,
   which is what `--chart-repo-url` defaults to. Its root *is* the chart, so
   `--chart-path` defaults to `.`; override it only if you vendored the chart
   into a subdirectory of some other repo. That repo also holds the
   `ms-applicationSet` chart the wiring deploys per namespace
   (`--appset-chart-path`, default `ms-applicationSet`) — the same repo and
   revision, so the two never drift apart.
2. Push everything under `output/` — `base/`, the per-namespace directories
   (`<namespace>/defaults.yaml` + `<namespace>/values/`), the two root
   manifests, and (under `--cluster-scoped`) `cluster-shared/` plus
   `cluster-shared-application.yaml` — to the Git repo passed as
   `--values-repo-url`, preserving the layout exactly as generated. The
   directory names ARE the namespace names; the root ApplicationSet discovers
   them by listing the repo.
3. `kubectl apply -f output/rootApplication.yaml.txt` against your ArgoCD
   namespace (name the file explicitly — see above). That is the whole
   bootstrap. It deploys `rootApplicationSet.yaml`, which creates one
   Application per namespace (and variant) folder, each rendering the
   `ms-applicationSet` chart, which fans out over that folder's `values/`. Nothing else is applied by hand,
   ever — adding a namespace or a microservice afterwards is a git commit.
4. Set `--argocd-project`, `--argocd-namespace` and `--destination-server` to
   match your ArgoCD install. They default to `default`, `argocd` and the
   in-cluster API URL.
5. Re-run the converter whenever the raw dump changes and commit the diff —
   values files are regenerated deterministically from the source manifests,
   so treat them as generated artifacts, not something to hand-edit (the
   per-namespace `values/<slug>.yaml` files are the one exception: they're
   explicitly the safe, human-editable day-2 override layer).

## Regenerating from scratch

Both scripts are idempotent given the same `--seed`/input. To start over:

```bash
rm -rf namespaces output
python generate_mock_environment.py --output namespaces --seed 42
python convert_to_universal_chart.py --input namespaces --output output
```

## Onboarding a real application/namespace

The mock fixture above exists to stress-test the converter at scale; onboarding a
real app follows the exact same input contract, just with real data:

> Steps 1 and 3 in one command, with verification and nothing left beside
> the tree: [`namespaces_to_chart.py`](#namespaces_to_chartpy--namespaces-in-chart-out).

1. **Dump the namespace's desired state.** The easy way is
   `namespace_importer.py`, which splits the namespace into per-microservice
   files and a `shared.yaml` for you (and brings each ExternalSecret's
   SecretStore, Vault CA and ServiceAccount along):
   ```bash
   python namespace_importer.py -n <namespace> -o namespaces [--kubeconfig <file>]
   ```
   By hand, for each namespace you're onboarding:
   ```bash
   oc get deployment,statefulset,daemonset,service,route,configmap,secret,pvc,\
   serviceaccount,role,rolebinding,hpa,pdb,ingress,secretstore,externalsecret,\
   cronjob,job -n <namespace> -o yaml > namespaces/<namespace>/<microservice>.yaml
   ```
   (NetworkPolicies and ServiceMonitors are not converted; leave them out.)
   Do this **per microservice** if the namespace hosts several distinct apps (one
   file per deployable unit), using label selectors (`-l app=<name>`) to split a
   namespace-wide dump into per-microservice files. Anything genuinely shared by
   every microservice in the namespace (a CA-bundle ConfigMap, a shared
   SecretStore, a namespace-wide RoleBinding) goes in `namespaces/<namespace>/shared.yaml`
   instead — don't duplicate it into every microservice file, the converter
   already handles de-duplication FROM `shared.yaml` but can't guess which
   raw-dump copy was "the shared one" if you don't tell it via that filename.
   Cluster-scoped objects (`ClusterRole`, `ClusterRoleBinding`, `StorageClass`,
   `PersistentVolume`, `SecurityContextConstraints`) are fine left wherever they
   naturally show up in the dump — the converter finds and deduplicates them
   globally regardless of which file they're in.

2. **Sanity-check the raw dump before converting.** Raw `oc get -o yaml` output
   carries live cluster noise (`status`, `resourceVersion`, `Pod`/`ReplicaSet`
   instances, auto-generated `ServiceAccount` token Secrets, SCC-injected
   `runAsUser`/`fsGroup`). You don't need to strip any of this by hand — steps
   `sanitize_object()`/`filter_transient()` in the converter do it automatically
   — but it's worth a quick `grep -c "^kind:"` per file to make sure the dump
   actually captured everything you expect (an empty `service.yaml` selector
   because `oc get` was run with the wrong label selector is a common mistake,
   and it's much easier to catch before converting than after).

3. **Run the converter** exactly as in the mock walkthrough above, pointing
   `--input` at your real `namespaces/` tree:
   ```bash
   python convert_to_universal_chart.py --input namespaces --output output \
       --chart .. \
       --chart-repo-url  https://<your-git-host>/gitops/universal-chart.git \
       --values-repo-url https://<your-git-host>/gitops/microservices-values.git
   ```

4. **Read `output/report/conflicts_and_warnings.txt` before doing anything else.**
   For a real app, pay special attention to:
   - `renamed` entries — confirm the auto-rename (`<microservice>-<name>`) is
     actually what you want, not a sign that two microservices should have been
     sharing that ConfigMap/Secret/Role via `shared.yaml` instead.
   - `base content differs` entries — two namespaces declared a microservice
     by the same name but with genuinely different specs, so the later one
     got its own `base/<namespace>-<name>.yaml` instead of sharing
     `base/<name>.yaml`. Confirm that's actually intentional (different
     environments legitimately needing different specs) and not drift that
     should be reconciled so they CAN share one base file.
   - `promoted to namespace-shared` entries — a microservice privately declared
     something that also got promoted to shared; verify that microservice's
     generated values now correctly *reference* the shared copy rather than
     still owning its own.
   - `[cluster-scoped]` conflicts with DIFFERING content — these are never
     auto-resolved and need a human decision about which definition is correct
     before you deploy either namespace.
   - `chart supports one managed ServiceAccount per release` / `no pod in this
     release runs as it` — extra ServiceAccounts land in `extraDeploy`; double
     check nothing downstream (a RoleBinding subject, a pod's
     `serviceAccountName`) still expects the chart to manage it directly.
   - `NetworkPolicy/<name>: dropped` / `ServiceMonitor/<name>: dropped` — not
     converted by design; recreate them on the target platform if needed.

5. **Let the render-verification step do its job** (it runs automatically unless
   you passed `--skip-verify`) — if it exits non-zero, do not proceed to step 6
   until `output/report/render_conflicts.txt` is empty.

6. **Review the generated values by hand once, for the first onboarding of any
   given app.** In particular:
   - `base/<slug>.yaml` is auto-generated / do-not-edit — treat it as such
     and re-run the converter instead of hand-patching it.
   - `<folder>/values/<slug>.yaml` is explicitly the safe, hand-editable
     override layer (image tag + what differs per namespace) — this is where day-2
     changes belong (e.g. a config value a developer needs to tweak without
     re-running the whole conversion).
   - `<folder>/defaults.yaml` is also auto-generated / do-not-edit — see
     [defaults.yaml — factoring out what's common](#defaultsyaml--factoring-out-whats-common).
     If a value you expected in a microservice's own file isn't there, check
     it (and `base/`) before assuming it's missing.
   - If the app needs something the converter has no first-class handling for
     (a CRD, an unusual resource kind), it'll have been passed through as raw
     YAML under `extraDeploy` with a warning — confirm that's actually fine
     rather than a sign the converter needs a new per-kind handler.

7. **Push and wire into ArgoCD** exactly as described below — same chart repo,
   same values repo layout, same two root manifests.

8. **Day 2**: whenever the source manifests change (a new microservice added to
   the namespace, a ConfigMap key changed upstream), re-run the converter and
   commit the diff. The values files are deterministic output — the git diff on
   `output/` is your review surface for "what actually changed", the same way
   you'd review a `terraform plan`.
