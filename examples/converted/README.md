# Converted examples

Published as GitHub repos (converter output only, `report/` left out):
[universal-chart-example-flat](https://github.com/devops-ezrahi/universal-chart-example-flat) and
[universal-chart-example-grouped](https://github.com/devops-ezrahi/universal-chart-example-grouped).

Two values trees written by `gitops-factory/convert_to_universal_chart.py` from the
plain manifests next to them. Same two microservices (`ms1`, `ms2`) in two
namespaces (`dev`, `prd`); the only difference is the colour variants.

| | `flat/` | `grouped/` |
|---|---|---|
| Input | `dev/ms1.yaml`, `dev/ms2.yaml`, same in `prd/` | `ms1-yellow`, `ms1-black` and `ms2` in each namespace |
| Flags | `--env-suffixes dev,prd` | `--env-suffixes dev,prd --env-group color=black,yellow` |
| Folders | `dev/`, `prd/` | `dev/`, `dev/yellow/`, `dev/black/`, same under `prd/` |
| Base | `{{ .Values.environment }}` | `{{ .Values.environment }}` and `{{ .Values.color }}` |

Every Argo CD Application renders `base/<ms>.yaml`, then `<folder>/defaults.yaml`,
then `<folder>/values/<ms>.yaml`, and the last one wins. A folder's `defaults.yaml`
carries its group values (`environment: dev`, `color: yellow`), which the `{{ }}`
in base resolve against. A variant folder deploys into its namespace
(`dev/yellow` goes to `dev`), and its folder name is appended to each
Application name.

```
grouped/output/
  base/ms1.yaml                  shared by all four ms1 folders
  dev/defaults.yaml              environment: dev
  dev/values/ms2.yaml            ms2 has no colour, so it stays in the namespace folder
  dev/yellow/defaults.yaml       color: yellow, environment: dev
  dev/yellow/values/ms1.yaml
  dev/black/...
  prd/...
  rootApplicationSet.yaml        --argocd-manifests true
  rootApplication.yaml.txt       applied by hand: kubectl apply -f rootApplication.yaml.txt
```

Regenerate (from `gitops-factory/`):

```bash
python convert_to_universal_chart.py --input ../examples/converted/flat/input --output ../examples/converted/flat/output --chart .. --env-suffixes dev,prd --argocd-manifests true --values-repo-url https://github.com/devops-ezrahi/universal-chart-example-flat.git --root-set-name example-flat --verify-fidelity --fidelity-strict
```

```bash
python convert_to_universal_chart.py --input ../examples/converted/grouped/input --output ../examples/converted/grouped/output --chart .. --env-suffixes dev,prd --env-group color=black,yellow --argocd-manifests true --values-repo-url https://github.com/devops-ezrahi/universal-chart-example-grouped.git --root-set-name example-grouped --verify-fidelity --fidelity-strict
```

Both render back to their input manifests with no differences (`report/fidelity_report.txt`).
The devops-portal ArgoCD module imports and rebuilds both trees unchanged; its
test copies live in `src/client/modules/argocd/__tests__/converted/`.
