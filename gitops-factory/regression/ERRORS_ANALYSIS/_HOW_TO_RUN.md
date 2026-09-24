# HOW TO RUN THE CONVERTER — verified reference

Every command below was executed on this machine and its output confirmed.
Verified 2026-08-18.

---

## 0. Environment (verified)

| Thing | Value |
|---|---|
| Repo root | `C:\repos\homelab\universal\gitops-factory` |
| Chart | `C:\repos\homelab\universal\Universal-chart` (= `../Universal-chart`, the default) |
| Python | 3.10.11 |
| pytest | 7.1.2 — **only via `python -m pytest`**. The bare `pytest` command is NOT on PATH. |
| helm | v4.1.1 — client-side only, no cluster needed for `template`/`lint` |
| kubectl / cluster | **NONE. On-prem, unreachable. Never attempt a live check.** |

### PATH CORRECTION — READ THIS BEFORE TRUSTING ANY error.txt

The `ADDITIONAL CONTEXT / CLUES` section of every `error.txt` cites paths like
`C:\repos\metroair-argocdnew\...` and namespaces like `interconn`, `rakia-monitor`,
`bluetorch`. **Those do not exist on this machine.** They are the original on-prem
environment captured in the source screenshots.

The equivalents here are the sanitized showcase inputs:

| error.txt says | actually use |
|---|---|
| `C:\repos\metroair-argocdnew\` | `C:\repos\homelab\universal\gitops-factory\` |
| `convert_to_universal_chart.py` | same filename, at the repo root ✓ |
| `Universal-chart\templates\` | `..\Universal-chart\templates\` ✓ |
| `namespaces\interconn\` | `namespaces-showcase\<ns>\` (shop-web, platform-ops, identity-auth, platform-data, background-jobs, + `-prd` variants) |
| `values\report\fidelity_report.txt` | `output-showcase\report\fidelity_report.txt` |
| `values\report\conflicts_and_warnings.txt` | `output-showcase\report\conflicts_and_warnings.txt` |
| release `zookeeper-zbs`, `interconn-kafka-entity-operator` | find the showcase analogue by SHAPE (a StatefulSet with a headless svc, a Deployment with sidecars), not by name |

**Line numbers in error.txt are also unreliable** — they were transcribed from
photographs of a different, older copy of the file. Locate defects by FUNCTION NAME
(`named_list_to_map`, `split_image`, `subtract_defaults`, `claim`, `handle_workload`,
`handle_cronjob`, `build_service_entry`, `build_route_entry`, `build_externalsecret_entry`),
never by line number. Record the real location in STATUS.md.

---

## 1. The one command that does everything

```powershell
cd C:\repos\homelab\universal\gitops-factory
python run_all.py
```

That is the whole interface. It runs four steps in order and stops at the first real
problem:

```
STEP 1  CONVERT      raw manifests -> Helm values + ArgoCD Applications
STEP 2  CHECK NAMES  every metadata.name must survive byte-for-byte
STEP 3  CHECK FIELDS did any field get lost or changed?
STEP 4  SANITIZE     anonymized, phone-screen-sized reports safe to send outside
```

Defaults: `--input namespaces-showcase --output output-showcase --chart ../Universal-chart`.

**Nothing is ever deleted.** Every step only writes new files. Raw manifests are never
touched. `--prune` is off by default, so your `ERRORS_ANALYSIS/` folder inside
`output-showcase/` is safe across re-runs.

### Exit code
`0` = everything passed. `1` = names changed, fidelity issues found, or a step failed.
This is your regression gate.

### Other useful forms (verified flags)
```powershell
python run_all.py --cluster-scoped          # if STEP 1 reports a CLUSTER CONFLICT
python run_all.py --skip-convert            # re-check + re-sanitize existing output only
python run_all.py --input namespaces --output output          # the other input set
python run_all.py --seed 42                 # reproducible anonymizer aliases, for diffing runs
```

---

## 2. Running the converter directly (finer control)

`run_all.py` STEP 1 shells out to exactly this:

```powershell
cd C:\repos\homelab\universal\gitops-factory
python convert_to_universal_chart.py `
    --input  namespaces-showcase `
    --output output-showcase `
    --chart  ..\Universal-chart `
    --verify-fidelity
```

### Flags that matter for this work

| Flag | Effect |
|---|---|
| `--verify-fidelity` | **This is what writes `report/fidelity_report.txt`.** Without it there is no report to check. Opt-in, report-only, never changes output, never fails the run. |
| `--fidelity-strict` | With the above, exit non-zero if anything was lost/altered/invented. **Use this as your pass/fail gate per fix.** A clean conversion reports zero findings, so any finding is real. |
| `--skip-verify` | Skips the `helm template` render-verification step. Faster, but you lose the ownership-conflict proof. Do not use while fixing rendering defects. |
| `--prune` | Deletes stale generated files. **Leave OFF** — it would remove release pointers and make ArgoCD prune live workloads. |
| `--cluster-scoped` | Opt back in to a deduplicated cluster-shared release (there is no tree-root `defaults.yaml` any more). Default is namespace-scoped (fails safe). Relevant to the `_FOUND_Global_Defaults_Subtracted_But_Never_Written` folder. |
| `--external-secrets-api-version` | Pin every SecretStore/ExternalSecret to one apiVersion, e.g. `external-secrets.io/v1beta1`. Needed when the target on-prem ESO only serves v1beta1 and would reject v1. |
| `--chart-repo-url` / `--values-repo-url` / `--values-path` | Only affect the generated ArgoCD Application manifests, not fidelity. Ignore for this work. |

Full flag list: `python convert_to_universal_chart.py --help`

---

## 3. The two checkers, run standalone

```powershell
# STEP 2 — every metadata.name must survive byte-for-byte
python name_fidelity_check.py --input namespaces-showcase --output output-showcase --chart ..\Universal-chart

# STEP 4 — anonymize the report for sharing
python sanitize_fidelity_report.py -i output-showcase\report\fidelity_report.txt -o output-showcase\report\shareable
python sanitize_fidelity_report.py -i output-showcase\report\fidelity_report.txt -o output-showcase\report\shareable --seed 42
```

`name_fidelity_check.py` requires all three args — there are no defaults.

The sanitizer **refuses to write** if a real identifier would leak; that hard failure is
correct behaviour, not a bug to fix.

---

## 4. Reading the result

STEP 3 parses these counters out of the report. These are the numbers your fix must move:

```
fields LOST
fields CHANGED
objects in raw but rendered by nothing
objects rendered with no raw source
render failures
```

Report locations:
```
output-showcase\report\fidelity_report.txt          <- full, INTERNAL ONLY
output-showcase\report\conflicts_and_warnings.txt   <- collision/rename decisions
output-showcase\report\shareable\                   <- anonymized, safe to send
```

Legend used inside the report:
```
INFO   an object that legitimately moved (shared release, cluster-shared, renamed on
       collision) — expected, not a loss.
ERROR  an object rendered by no release at all, an object with no raw source, or a
       field the conversion LOST or CHANGED.
```

---

## 5. Rendering one release by hand

To prove a specific field reaches the rendered YAML — this is the check that catches
"the converter captured it but the template never renders it", which is the failure
mode in roughly half of these defects:

```powershell
cd C:\repos\homelab\universal\gitops-factory

helm template testrelease ..\Universal-chart `
    -f output-showcase\base\<microservice>.yaml `
    -f output-showcase\<namespace>\defaults.yaml `
    -f output-showcase\<namespace>\values\<microservice>.yaml `
    > $env:TEMP\rendered.yaml

Select-String -Path $env:TEMP\rendered.yaml -Pattern "persistentVolumeClaimRetentionPolicy" -Context 0,3
```

That is the chain ArgoCD assembles, lowest to highest: `base/<file>` ->
`<folder>/defaults.yaml` -> `<folder>/values/<file>` (a folder may be a variant
folder such as `dev\yellow`, and `values\` may hold grouping sub-folders).
A single `-f` does not reproduce it. To see which files exist:

```powershell
Get-ChildItem output-showcase -Recurse -Filter *.yaml | Select-Object FullName
```

Lint:
```powershell
helm lint ..\Universal-chart -f output-showcase\<namespace>\values\<microservice>.yaml
```

Schema-validate without an API server:
```powershell
Get-Content $env:TEMP\rendered.yaml | kubectl apply --dry-run=client -f -
```
(only if kubectl is installed — it is not required and not currently present)

---

## 6. Per-fix loop (what to actually do in each folder)

```powershell
cd C:\repos\homelab\universal\gitops-factory

# 1. RED — prove the test fails before the fix
python -m pytest output-showcase\ERRORS_ANALYSIS\<Folder>\test\ -v

# 2. apply the fix to convert_to_universal_chart.py and/or ..\Universal-chart\templates\

# 3. GREEN — same test, now passing
python -m pytest output-showcase\ERRORS_ANALYSIS\<Folder>\test\ -v

# 4. no regressions across every folder finished so far
python -m pytest output-showcase\ERRORS_ANALYSIS\ -v

# 5. full pipeline; exit code 0 is the gate
python run_all.py
echo "exit=$LASTEXITCODE"

# 6. strict fidelity gate for this specific defect
python convert_to_universal_chart.py --input namespaces-showcase --output output-showcase --chart ..\Universal-chart --verify-fidelity --fidelity-strict
echo "exit=$LASTEXITCODE"
```

Remember: `python -m pytest`, never bare `pytest`.

Run a single test file:
```powershell
python -m pytest output-showcase\ERRORS_ANALYSIS\<Folder>\test\test_<name>.py -v
```

---

## 7. Isolated fixture runs (recommended for unit-level defects)

Rather than converting all of `namespaces-showcase` on every iteration, point the
converter at a minimal fixture holding only the manifest that reproduces the defect.
Fast, and the fidelity report contains nothing but your defect:

```powershell
python convert_to_universal_chart.py `
    --input  output-showcase\ERRORS_ANALYSIS\<Folder>\test\fixture `
    --output $env:TEMP\fixture-out `
    --chart  ..\Universal-chart `
    --verify-fidelity

Get-Content $env:TEMP\fixture-out\report\fidelity_report.txt
```

The fixture must mirror the real input layout: `fixture\<namespace>\<microservice>.yaml`.
The existing folders already follow this — e.g.
`Annotations_Lost\test\fixture\shop-web\api...yaml`.

Write to `$env:TEMP`, never over `output-showcase`, so a fixture run cannot disturb the
real output tree or the ERRORS_ANALYSIS work in progress.

---

## 8. Never do these

- `kubectl apply` / `helm install` / `helm upgrade` — no cluster, on-prem, unreachable.
- Edit anything under `namespaces*\` — source of truth. Fixture files are COPIES.
- Hand-edit generated values under `output-showcase\<ns>\values\` — fix the generator and re-run.
- Pass `--prune` — it makes ArgoCD prune live workloads.
- Trust a line number from `error.txt` without opening the file and confirming.
- Report a fix as done on `helm template` merely succeeding — it succeeded before the fix too.
