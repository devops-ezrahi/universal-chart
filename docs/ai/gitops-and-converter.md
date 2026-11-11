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
  `**/*.yaml` with Argo CD's new globbing), `project`, `destinationServer` /
  `destinationName`, `chartRepoURL` / `chartRevision` / `chartPath`.
- It emits one ApplicationSet with a matrix generator: a git-files generator
  globbing `<folder>/values/<valuesGlob>` (one Application per file, with the
  three `valueFiles` above) times one reading `<folder>/defaults.yaml`
  (`pathParamPrefix: defaults`, so `.path` is still the values file). So each
  Application also sees `defaults.yaml`'s top-level keys. The folder must have a
  `defaults.yaml`, or no Applications are generated.
- `autoSync` (chart default `true`) is the folder's **default** only. The root
  ApplicationSet passes the tree's value as a plain `"true"`/`"false"`
  (the converter's `--auto-sync`, default `false`). `autoSync: true|false` in the
  folder's own `defaults.yaml` overrides it. ms-applicationSet decides this,
  not the root: its `templatePatch` emits `syncPolicy.automated` (prune +
  selfHeal) unless `{{ dig "autoSync" "<chart autoSync>" . | toString | lower }}`
  is `"false"`. `templatePatch` needs Argo CD 2.10 or later. To switch one
  environment between manual and automatic sync, edit that line in its
  `defaults.yaml`. It doesn't affect `cluster-shared-application.yaml`.
- `preserveResourcesOnDeletion: true` (default): deleting an Application
  leaves the workloads running. `false` makes deletes cascade.
- `applicationsSync`: `create-only` / `create-delete` stop the controller from
  updating existing Applications.
- `links.repoWebUrl` / `argocdUrl` / `consoleUrl` / `consoleLink` produce the
  navigation annotations between Argo CD, git and the OpenShift console.
- Values-file keys `links.*` in the main chart are filled from here. Don't set them by hand.
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
- Deliberately not converted: ServiceMonitors (dropped, reported). PDBs and
  NetworkAttachmentDefinitions pass through `extraDeploy`.
- Reports are written to `<out>/report/` (`conflicts_and_warnings.txt`,
  `renames.json`, `fidelity_report.txt`, `conversion_report.json`).
- Tests: `python -m pytest -q`. `tests/converter/regression/<Defect>/` holds one
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
