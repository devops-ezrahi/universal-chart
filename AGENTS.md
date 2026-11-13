# AGENTS.md — universal-chart

Guide for AI coding agents (OpenCode, Claude Code, ...) working in this repo.
Read this whole file first. Deeper material:

- `docs/ai/values-guide.md`: how to write values correctly (rules, every
  collection's shape, pitfalls).
- `docs/ai/gitops-and-converter.md`: the values repo layout, `ms-applicationSet`,
  the converter and `values_editor.py`.
- `docs/ai/examples/*.yaml`: values files verified to render. Copy from these.

## MANDATORY: keep the AI docs current

These files are what every AI agent knows about this chart, in this repo and
in every values repo (they read them at the chart revision they deploy). A
change that makes them wrong breaks those agents silently. So **a task is not
done until the docs match the code, in the same change (commit/PR)**:

| If you changed... | Update |
|---|---|
| a template, helper, default or validation (`templates/`, `values.yaml`, `values.schema.json`) | `docs/ai/values-guide.md`, and the golden rules below if a rule changed |
| a values key (added, renamed, removed, new default) | `docs/ai/values-guide.md`, and `docs/ai/examples/*.yaml` if they use it |
| `ms-applicationSet/` (parameters, naming, layering) | `docs/ai/gitops-and-converter.md`. A new ApplicationSet-level setting goes in `rootApplicationSet.yaml` (a fallback in the template, a parameter written by `build_root_applicationset`), never as a key in `ms-applicationSet/values.yaml` |
| converter options or output layout (`converter/`, `gitops-factory/`) | `docs/ai/gitops-and-converter.md`, `gitops-factory/README.md` |
| the values-repo layout, or how agents should find the chart | `converter/values_repo_agents.md` (the AGENTS.md written into values repos) |
| a breaking change for existing values | all of the above that apply, plus a clear `feat!:`/`fix!:` commit |
| anything that makes a statement in this file false | this file |

Before finishing:
1. Re-read the doc sections touching what you changed and fix every statement
   that is no longer true. Don't append contradicting notes; rewrite.
2. Render every example: `for f in docs/ai/examples/*.yaml; do helm template t . -f "$f" >/dev/null || echo "FAIL $f"; done`.
3. In your final message, list the doc files you updated, or say why none needed it.

If you find a doc that is already wrong, even unrelated to your task, fix it
or tell the user.

## What this repo is

Three things live here. Keep them apart in your head:

| Part | Path | What it is |
|---|---|---|
| **The chart** | repo root (`Chart.yaml`, `values.yaml`, `values.schema.json`, `templates/`, `tests/`) | `super-universal-chart` v4: ONE generic Helm chart that deploys any app from values alone. No app-specific templates, ever. |
| **ms-applicationSet** | `ms-applicationSet/` | A small separate chart: one Argo CD ApplicationSet per namespace folder of a values repo. It fans out over `values/*.yaml` and deploys each file with the chart (`values/configMaps.yaml` is the exception: one Application for the namespace's `configMaps/` folder). |
| **The converter** | `gitops-factory/convert_to_universal_chart.py`, `converter/`, `tools/`, `tests/converter/` | Python. It converts running namespaces or manifest dumps into a values repo (`base/` + `<ns>/defaults.yaml` + `<ns>/values/`) and verifies it by rendering every release. |

`.helmignore` excludes everything except the chart itself (Argo CD renders the
chart from the repo root).

## How a release is rendered (the mental model)

1. Argo CD (via ms-applicationSet) or a person runs `helm template` with value
   files layered lowest to highest:
   `base/<release>.yaml` → `<ns>/defaults.yaml` → `<ns>/values/<release>.yaml`.
2. Helm deep-merges them. **Maps merge key by key; lists are replaced whole.**
   That is why nearly every collection in this chart is a map keyed by name.
3. Every template first calls `universal-chart.renderValues`
   (`templates/_helpers.tpl`). It runs every string value **and map key**
   through `tpl` once, so values can say `{{ .Release.Namespace }}` or
   `{{ .Values.environment }}`. `extraDeploy` is excluded (it `tpl`s itself).
4. Templates turn maps into Kubernetes lists (`universal-chart.orderedMapToList`,
   sorted by key unless an `*Order` list says otherwise). The shared pod spec is
   `templates/_pod-spec.tpl`, used by deployment/statefulset/daemonset and,
   via `jobPodSpec`, by jobs/cronjobs.
5. `universal-chart.validate` fails the render on invalid combinations. So do
   `values.schema.json` and mount checks (`validateVolumeMounts`).

## Golden rules (most mistakes break one of these)

1. **Collections are MAPS keyed by name, not lists:** `env`, `ports`, `envFrom`,
   `volumes`, `initContainers`, `sidecars`, `configMaps`, `secrets`, `pvc`,
   `cronjobs`, `jobs`, `services`, `routes`, `networkPolicies`, `service.ports`,
   `rbac.*`, `externalSecrets`, `secretStores`, `volumeClaimTemplates`, ...
   The exception is **`volumeMounts`, which is a LIST** (rendered in written order).
2. **The map key IS the object's `metadata.name`, verbatim.** No release prefix.
   `cronjobs.nightly-cleanup` → `CronJob/nightly-cleanup`.
3. **Object names come from `fullnameOverride`** (else `nameOverride`, else the
   release name). The chart name never appears in object names.
4. **Most features are OFF by default:** `service.enabled`,
   `serviceAccount.create`, `ingress`, `route`, `hpa`, `pdb`, `vpa`,
   `checksums`, `chartLabels`. Turn on only what is needed.
5. **Every value goes through `tpl`.** A literal `{{` (e.g. a Go or Alertmanager
   template in a ConfigMap) must be escaped: `'{{ "{{" }} define ... }}'`.
   Rendered values come out as strings.
6. **`env` entries say what they are:** `NAME: {value: ...}` or
   `NAME: {valueFrom: {...}}`. A later file deletes a var with `NAME: null`. If
   it switches between `value` and `valueFrom`, it must null the old key, or
   the render fails.
7. **Order matters only where Kubernetes cares:** use `initContainerOrder`,
   `envFromOrder`, `envOrder` (only for `$(VAR)` references), and
   `portOrder`/`volumeOrder`/`sidecarOrder` (to avoid pod restarts on migration).
8. **Never invent keys.** If a key is not in `values.yaml`, `values.schema.json`
   or a template, it does nothing or fails the schema. Grep before you claim.

## Trust order when sources disagree

1. `templates/*.yaml` + `templates/_helpers.tpl` + `templates/_pod-spec.tpl`: the truth.
2. `tests/*_test.yaml` (helm-unittest): executable, current examples of every feature.
3. `values.yaml` comments and `values.schema.json`.
4. `docs/ai/*`, `README.md`, `gitops-factory/README.md`.
5. **`COOKBOOK.md` last.** Its sections 4, 5, 8, 10, 18, 19, 27 and the pvc
   examples still use the OLD list format and fail the schema (it says so at the
   top). Use it for concepts only and convert its YAML to the map format.

**Do not copy** `examples/simple-webapp.yaml`, `app1-fullstack-webapp.yaml`,
`app2-postgres-statefulset.yaml` or `full-example.yaml`. They are stale and fail
to render. Good real examples: `docs/ai/examples/`, `examples/converted/*/output/`,
`examples/gitops/`, and the `set:` blocks in `tests/*_test.yaml`.

## Verify, don't guess

Before answering "what does X render to", render it. After editing values or
templates, render and lint.

```bash
# one values file
helm template myrel . -n myns -f path/to/values.yaml

# a release from a values repo, layered like Argo CD does
helm template ms1 . -n dev -f <repo>/base/ms1.yaml -f <repo>/dev/defaults.yaml -f <repo>/dev/values/ms1.yaml

# narrow to one kind
helm template myrel . -f v.yaml --show-only templates/deployment.yaml

helm lint .
helm lint ms-applicationSet
helm unittest .                    # needs the helm-unittest plugin
helm unittest ms-applicationSet
python -m pytest -q                # converter suite (pip install -r requirements.txt)
```

## Changing the chart itself

- Template change → add or adjust a test in `tests/<kind>_test.yaml`.
- New values key → document it in `values.yaml` with a comment, and add it to
  `values.schema.json`. Top-level keys are open, but some nested objects set
  `additionalProperties: false`.
- Preserve the "render only when set" style: use `hasKey` where `0`/`false` are
  meaningful (`with`/`default` swallow them). Many comments in the templates
  explain past bugs of exactly this kind. Read them before "simplifying".
- Labels and annotations sit on object metadata; anything touching
  `spec.template` restarts pods. Be explicit when a change does that.
- The converter (`converter/engine.py`) emits values for this chart. A chart
  change can require a converter change and vice versa. Run both test suites.
- Versioning is by semantic-release (`.releaserc.json`). Use conventional
  commits: `feat:`, `fix:`, `chore:`.
- `docs/ai/` is what agents working in values repos read, **at the chart
  revision the values repo deploys**. Values repos don't copy it; their
  `AGENTS.md` (written by the converter's `--agents-md`, text in
  `converter/values_repo_agents.md`) points here. Keep them current: see
  "MANDATORY: keep the AI docs current" above.

## Arriving from a values repo

You were sent here by a values repo's `AGENTS.md` and should be checked out
at the revision it deploys. Read this file, then `docs/ai/values-guide.md`, then
`docs/ai/gitops-and-converter.md`. Render with this checkout as the chart path:
`helm template <release> <this-folder> -n <ns> -f <values-repo>/base/<release>.yaml -f <values-repo>/<ns>/defaults.yaml -f <values-repo>/<ns>/values/<release>.yaml`.
