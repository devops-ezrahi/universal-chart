# GitOps layout, ms-applicationSet and the converter

Condensed from `README.md` (GitOps section) and `gitops-factory/README.md`.
Read those for full option tables. This file is the map.

## The values repo (separate git repo, not this one)

```
<values repo>/
  base/<release>.yaml            # full, environment-agnostic values, shared by every namespace
  base/shared.yaml               # objects shared within a namespace (the "shared" release)
  <namespace>/
    defaults.yaml                # what every release in this namespace has in common
                                 # (e.g. environment: dev, common labels, cluster: {...},
                                 #  autoSync: true|false to override the tree's default sync mode)
    values/<release>.yaml        # image tag + whatever differs from base here
    values/<group>/<release>.yaml   # optional grouping folder; release name unchanged
    configMaps/<name>.yaml       # optional: ONE ConfigMap named <name>, the file starts at `data:`;
                                 #   every file is deployed by cm-applicationSet, one Application each (<name>-cm-<ns>)
    <variant>/                   # optional, e.g. prd/yellow: deploys INTO <namespace>,
      defaults.yaml              #   shares base/, carries its group value (color: yellow)
      values/<release>.yaml
  cluster-shared/values/cluster-shared-values.yaml   # with --cluster-scoped
  rootApplication.yaml.txt       # applied once by hand (kubectl apply -f); .txt so no tool picks it up
  rootApplicationSet.yaml        # one Application per folder that has a defaults.yaml
```

- **Adding a microservice = adding `values/<release>.yaml`** (plus `base/` if new).
- **Adding a namespace = adding a folder with `defaults.yaml`.**
- Layering per release, lowest to highest: `base/<file>` → `<folder>/defaults.yaml` → `<folder>/values/<file>`.
- **ConfigMaps folder:** a namespace's shared ConfigMaps. Every
  `<folder>/configMaps/<name>.yaml` is deployed; there is no list file. The
  file starts at `data:` (and/or `binaryData:`, optional `commonLabels` /
  `commonAnnotations`), with nothing before it: the file name is the ConfigMap's
  name. The **cm-applicationSet** chart (below) deploys them: each file is its
  own Application, `<name>-cm-<ns suffix>`, in the `<ns>-configmaps`
  ApplicationSet, rendering only
  that file with `fullnameOverride=<name>` and `workload.type=none`: no `base/`,
  no `defaults.yaml` (its `autoSync` still applies), no pods. Microservices
  consume them by name (`envFrom`, `volumes`). Hand-written; the converter
  keeps them, verifies them and never prunes them. Render one like Argo CD:
  `helm template env1-cm-dev . -n dev -f dev/configMaps/env1.yaml --set workload.type=none --set-string fullnameOverride=env1`.
  The old shape (`values/configMaps.yaml` listing `configMapFiles`, each file
  wrapped in `configMaps: <name>:`) is migrated in place by re-running the
  converter on the tree (report: `report/config_maps_migration.txt`). A wrapped
  file still renders meanwhile. A leftover `values/configMaps.yaml` keeps the
  old `configmaps-<ns>` Application (ms-applicationSet) but empties it: it renders nothing, never
  syncs itself (so it never prunes), and is labelled `universal-chart/obsolete`.
  Deleting the file deletes that Application. With
  `preserveResourcesOnDeletion: "false"` that delete would take the ConfigMaps
  with it, so in that case the converter keeps the file: delete it by hand once
  the new `<name>-cm` Applications have synced.
- **Instances:** several releases sharing one base. Each values file sets
  `BaseFile: x` (Argo CD layers `base/x.yaml`) and `instance: alpha`
  (`base/x.yaml` uses `{{ .Values.instance }}`).

Find a working tree in `examples/converted/flat/output/` (plain) and
`examples/converted/grouped/output/` (with variant folders). Render one with:

```bash
o=examples/converted/flat/output
helm template ms1 . -n dev -f $o/base/ms1.yaml -f $o/dev/defaults.yaml -f $o/dev/values/ms1.yaml
```

## ms-applicationSet (`ms-applicationSet/`)

- The root ApplicationSet renders it **once per namespace or variant folder**.
- Required values: `namespace`, `originRepoURL`, `originBranch`.
- Optional values: `originPath`, `folder`, `valuesGlob` (default `*.yaml`;
  `**/*.yaml` with Argo CD's new globbing), `project`, `destinationName`, `chartPath`.
- **Rule: ApplicationSet-level settings live in each values repo's
  `rootApplicationSet.yaml` (helm parameters), never in
  `ms-applicationSet/values.yaml`.** These keys are deliberately absent from
  `values.yaml`; the template falls back to the default in brackets for a root
  file that leaves one out: `destinationServer` [`https://kubernetes.default.svc`],
  `chartRepoURL` [`https://github.com/devops-ezrahi/universal-chart.git`],
  `chartRevision` [`main`], `autoSync` [`"false"`], `preserveResourcesOnDeletion`
  [`"true"`]. The converter always writes all of them, as ONE parameter list
  shared by both sources of the namespace's root Application (YAML anchor
  `&params` / `*params`), so ms-applicationSet and cm-applicationSet never
  drift apart. `configMaps.dir` is in that list too; ms-applicationSet ignores it. A new setting
  follows the same rule: a default in the template, a parameter written by
  `build_root_applicationset`, a comment in `values.yaml`, no key there.
- It emits one ApplicationSet with a matrix generator: a git-files generator
  globbing `<folder>/values/<valuesGlob>` (one Application per file, with the
  three `valueFiles` above) times one reading `<folder>/defaults.yaml`
  (`pathParamPrefix: defaults`, so `.path` is still the values file). So each
  Application also sees `defaults.yaml`'s top-level keys. The folder must have a
  `defaults.yaml`, or no Applications are generated. It never deploys the
  ConfigMaps folder: that is cm-applicationSet (below).
- `autoSync` (fallback `false`: manual sync) is the folder's **default** only. The root
  ApplicationSet passes the tree's value as a plain `"true"`/`"false"`
  (the converter's `--auto-sync`, default `false`). `autoSync: true|false` in the
  folder's own `defaults.yaml` overrides it. ms-applicationSet decides this,
  not the root: its `templatePatch` emits `syncPolicy.automated` (prune +
  selfHeal) unless `{{ dig "autoSync" "<root autoSync>" . | toString | lower }}`
  is `"false"`. `templatePatch` needs Argo CD 2.10 or later. To switch one
  environment between manual and automatic sync, edit that line in its
  `defaults.yaml`. It doesn't affect `cluster-shared-application.yaml`.
- `preserveResourcesOnDeletion` (fallback `"true"`, converter
  `--preserve-resources-on-deletion`, default true): deleting an Application
  leaves the workloads running. `false` makes deletes cascade.
- A leftover `values/configMaps.yaml` (the pre-5.0 ConfigMaps list) gets a
  guard: it is named `configmaps-<ns>` (never the invalid `configMaps-<ns>`),
  renders nothing and never syncs itself (see the ConfigMaps folder bullet).
- `applicationsSync`: `create-only` / `create-delete` stop the controller from
  updating existing Applications.
- `links.repoWebUrl` / `argocdUrl` / `consoleUrl` / `consoleLink` produce the
  navigation annotations between Argo CD, git and the OpenShift console.
- Values-file keys `links.*` in the main chart are filled from here. Don't set them by hand.

## cm-applicationSet (`cm-applicationSet/`)

- The namespace's ConfigMaps folder. A sibling chart of ms-applicationSet:
  the root ApplicationSet's per-namespace Application has two sources, this
  chart and ms-applicationSet, with the same parameters. So a namespace folder
  brings both ApplicationSets, `<ns>` and `<ns>-configmaps`.
- Values: the same required/optional ones as ms-applicationSet (no
  `valuesGlob`), and the same root-file settings with the same fallbacks,
  plus `configMaps.dir` [`configMaps`].
- One ApplicationSet, `<ns><variant suffix>-configmaps`, with a matrix
  generator: git files `<folder>/<configMaps.dir>/*.yaml` times
  `<folder>/defaults.yaml` (for `autoSync`). Each file is one Application,
  `<file>-cm-<ns suffix><variant suffix>`, labelled `configmaps: <folder>`,
  rendering the chart with that file alone, `workload.type=none` and
  `fullnameOverride=<file>`, so ConfigMap/`<file>` from its top-level `data:`.
  No `base/`, no `defaults.yaml` layered. An empty or missing folder generates
  nothing.
- `autoSync`, `preserveResourcesOnDeletion`, `applicationsSync` and `links.*`
  behave exactly as in ms-applicationSet. Deleting a file deletes its
  Application (and, with `preserveResourcesOnDeletion: "false"`, the ConfigMap).
- Converter: `--config-maps` (default true) writes the second source,
  `--config-maps false` leaves it out (and records `configMaps.enabled:
  "false"`), `--config-maps-dir` sets `configMaps.dir`,
  `--cm-appset-chart-path` (default `cm-applicationSet`) the chart's path.
- Tests: `helm unittest cm-applicationSet`. Template: `cm-applicationSet/templates/applicationSet.yaml`.
- Tests: `helm unittest ms-applicationSet`. Template: `ms-applicationSet/templates/applicationSet.yaml`.

## The converter (`gitops-factory/convert_to_universal_chart.py`)

Run it from the repo root. It turns live namespaces or manifest dumps into the
values repo above, then proves the result by rendering every release with
`helm template` and checking that no two releases claim the same object.

```bash
python gitops-factory/convert_to_universal_chart.py -n shop-dev -n shop-prd -o out   # live, via oc
python gitops-factory/convert_to_universal_chart.py --input dumps/ -o out            # dump layout
python gitops-factory/convert_to_universal_chart.py --help                           # all options
```

- Input dump layout: `<dump>/<namespace>/<microservice>.yaml` (multi-doc) + optional `shared.yaml`.
- Code map: `converter/importer.py` (pulls through `oc`, cleans runtime
  metadata), `converter/sources.py` (input kinds), `converter/engine.py` (the
  conversion, ~7.6k lines: factoring into base/defaults/values, renames,
  verification, fidelity), `converter/upgrade.py` (`--upgrade` an existing tree).
- Options people ask about most:
  - `--env-suffixes`: strip env words from names and template values to
    `{{ .Values.environment }}`.
  - `--env-group NAME=tok`: variant folders and `{{ .Values.<NAME> }}`.
  - `--prevent-restart`: render the live pod template byte-for-byte so cutover
    restarts nothing. It is on by default for StatefulSets.
  - `--exact-copy`: everything one-to-one with what is live, no links.
  - `--argocd-manifests true`: also write the root Application and ApplicationSet.
  - `--agents-md`: also write an `AGENTS.md` that points AI agents at the chart's docs
    at the deployed chart revision. It never overwrites a hand-written one.
  - `--auto-sync`: off by default. `--namespace-auto-sync NS=true|false` overrides it
    per namespace (written as `autoSync:` in that namespace's `defaults.yaml`, which
    ms-applicationSet reads; the root ApplicationSet passes only `--auto-sync`).
  - `--include`, `--exclude`, `--keep`: select stacks.
  - `--cluster-scoped`: cluster objects go to the `cluster-shared` release.
  - `--instances x`: an instance layout.
  - `--verify-fidelity [--fidelity-strict]`: prove the rendered output equals the source.
  - `--repair-mojibake [true|false]` (default true): ConfigMap/Secret text that
    is UTF-8 read with a Windows console or ANSI codepage (Hebrew showing as
    `╫É╫û╫ò` for cp437, `׳©׳` for cp1255, `×©` for cp1252) is written
    repaired when it can be restored exactly, and every one is reported as
    `[P1]` in `conflicts_and_warnings.txt`. Text that lost characters is only
    reported. The fidelity check lists a repair as INFO, not as a change.
- Deliberately not converted: ServiceMonitors (dropped, reported). PDBs and
  NetworkAttachmentDefinitions pass through `extraDeploy`.
- Reports are written to `<out>/report/` (`conflicts_and_warnings.txt`,
  `renames.json`, `fidelity_report.txt`, `conversion_report.json`).
- Tests: `python tools/convert_and_verify.py --cluster-scoped`, then
  `cd tests/converter && python -m pytest -q` (as CI does: some tests read the
  generated reports, and that folder's `pytest.ini` sets importlib mode and lists
  `testpaths`, so add a new top-level test file there). `tests/converter/regression/<Defect>/` holds one
  folder per fixed bug (`error.txt`, `STATUS.md`, fixture, test). Read the
  matching folder before touching related engine code.
- Showcase: `python tools/convert_and_verify.py` rebuilds `.generated/output-showcase/`
  from `examples/showcase/`.

## values_editor.py (hand-authoring in a values repo)

```bash
python tools/values_editor.py list     --app-dir out
python tools/values_editor.py show     --app-dir out --namespace dev --name ms1   # fully merged effective values
python tools/values_editor.py new      --app-dir out --namespace dev --name ms3 [--base-file x --instance delta]
python tools/values_editor.py set      --app-dir out --namespace dev --name ms1 --key image.tag --value 1.2.3
python tools/values_editor.py validate --app-dir out [--chart .]                  # schema (+ real render) per release
```

`new` writes only the delta vs `defaults.yaml`. `set` routes `image.tag` and
literal env vars to `values/`, and everything else to `base/`, which affects
**every namespace** using that base. Variant folders: `--namespace dev/yellow`.
