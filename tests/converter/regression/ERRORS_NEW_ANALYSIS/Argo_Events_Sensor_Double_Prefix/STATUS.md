# STATUS — Argo_Events_Sensor_Double_Prefix (TIER 1, folder 11/26)

**Result: REPORTED DEFECT NOT REPRODUCIBLE HERE — AND A WORSE ONE FOUND AND
FIXED.** Sensors never reach this converter's naming logic, so they cannot
acquire a doubled prefix; `error.txt`'s prediction that folder 10's guard would
resolve this folder does not apply. Its *question* was the productive one: the
generic fallthrough path was broken, differently and more severely — a standard
Argo Events parameter expression made `helm template` **fail outright**. 15 new
tests RED→GREEN, first Argo Events coverage in the corpus, showcase fidelity
0 lost / 0 changed, chart's 234 green.

---

## 1. The reported defect does not exist in this converter

`error.txt` predicts:

> This is the same fullname/prefix machinery that produced the ServiceAccount
> doubling [...] **Fixing the idempotence guard likely resolves both folders at
> once.**

It does not, because the guard is on a code path Sensors never enter.

`Sensor` and `EventSource` are in neither `NS_RAW_NAME_KINDS` nor
`CLUSTER_SCOPED_KINDS`, so `build_microservice`'s pass-1 claiming loop skips
them entirely and `NamespaceRegistry.claim` is never called for them. They land
in the dispatch chain's `else:` branch and are copied verbatim into
`.Values.extraDeploy`:

```
[background-jobs/nightly-report] Unrecognized kind 'EventSource' (nightly-report) passed through via extraDeploy
[background-jobs/nightly-report] Unrecognized kind 'Sensor' (nightly-report) passed through via extraDeploy
```

Confirmed end to end. A Sensor named `nightly-report` in a release named
`nightly-report` — the exact shape that produced `<name>-<name>` for
ServiceAccounts — renders as `nightly-report`, with `eventSourceName`,
`eventName`, dependency name and trigger template name all intact.

`test_sensor_is_not_routed_through_the_naming_registry` pins the structural
reason: if `Sensor` is ever added to `NS_RAW_NAME_KINDS`, the reported defect
becomes possible and the test says so before it ships.

**Where the report's doubling came from is not determinable from here.** The
`rakia-*` converter may have had a different kind list, or the observation may
be the ServiceAccount doubling of folder 10 seen through an Argo Events lens.
What is now provable is that this converter cannot produce it for a Sensor.

## 2. The defect that *was* there

`error.txt`'s question, rather than its prediction:

> Confirm the converter has a real handler for Sensor/EventSource rather than
> **falling through to a generic path** that applies workload naming rules — a
> generic path is the most probable source of the double prefix.

It does fall through to a generic path, and that path was broken.
`templates/extra-deploy.yaml` renders every entry through `tpl`:

```
{{- range .Values.extraDeploy }}
---
{{ tpl . $ }}
{{- end }}
```

That is a deliberate feature for hand-written entries — the template's own
docstring says "inject any raw Kubernetes YAML, **with full Helm templating
support**". It is exactly wrong for entries the converter produces, which are
raw manifests copied from a cluster.

An Argo Events Sensor that passes a parameter to an Argo Workflow carries:

```yaml
value: "{{workflow.parameters.target}}"
```

Ordinary Argo syntax, meaningless to Helm. `tpl` tried to evaluate it:

```
Error: template: super-universal-chart/templates/extra-deploy.yaml:16:3:
  executing "..." at <tpl . $>: error calling tpl: cannot parse template "apiVersio...
```

**Not a mangled reference — a hard render failure of the entire release.**
Strictly worse than the reported defect: a doubled name still deploys.

And extraDeploy is, by definition, the path taken by every kind the chart does
not model — so it is simultaneously the least-tested path and the one most
likely to carry foreign templating syntax.

## 3. Files changed

| File | Lines | Change |
|---|---|---|
| `universal/gitops-factory/convert_to_universal_chart.py` | 1063–1090 (new) | `as_extra_deploy()` — serialise with `{{` escaped |
| `universal/gitops-factory/convert_to_universal_chart.py` | 1745, 2443 | both `extra_deploy.append` sites routed through it |
| `universal/gitops-factory/namespaces-showcase/background-jobs/nightly-report.yaml` | 49–117 (new) | first Argo Events `EventSource` + `Sensor`, including a `{{ }}` expression |
| `universal/gitops-factory/output-showcase/**` | regenerated | consequence |

### Why the fix is in the converter, not the template

Disabling `tpl` in `extra-deploy.yaml` would have been a one-line fix and the
wrong one: it would break every hand-written entry using
`{{ .Release.Name }}-special`, which is the documented usage and appears in the
template's own docstring.

The distinction is provenance, and only the converter knows it: **its** entries
are verbatim copies that must not be templated; a **human's** entries are
templates that must be. So the converter escapes what it emits, and the template
keeps working as documented. `test_hand_written_extraDeploy_entries_are_still_templated`
pins that half.

**Only the opening delimiter is escaped.** Go's parser treats a bare `}}` as
ordinary text, and `{{ "{{" }}` round-trips to a literal `{{` — so the rendered
manifest is byte-identical to the raw one, which the fidelity check confirms at
0 changed.

## 4. RED → GREEN

```
BEFORE:  helm template ... -> Error: error calling tpl: cannot parse template
AFTER:   value: '{{workflow.parameters.target}}'      (rendered, intact)
```

`test_unescaped_content_still_fails_so_the_test_above_means_something` guards
against a false pass: it renders the *unescaped* doc and asserts it still fails.
Without that, the escape test would pass if `tpl` ever stopped evaluating
extraDeploy for an unrelated reason, and the protection would rot invisibly.

## 5. Tests created

`ERRORS_NEW_ANALYSIS/Argo_Events_Sensor_Double_Prefix/test/test_sensor_naming.py`
— 15 tests in 3 groups:

1. **The reported defect** (4) — Sensor/EventSource are in neither kind list; a
   Sensor named after its release keeps its name; its internal references
   (`eventSourceName`, `eventName`, dependency name, trigger template name)
   survive; the doc round-trips unmodified.
2. **The `tpl` hazard** (9) — an Argo parameter expression no longer breaks the
   render; it renders back byte-identically; unescaped content still fails (the
   anti-false-pass guard); five brace shapes parametrised, including
   `{{{{ doubled }}}}` and a string that looks like a Helm value reference; and
   hand-written entries are still templated.
3. **Corpus** (2) — a Sensor and its EventSource exist and the Sensor's
   dependencies point at an EventSource that is actually present; and the corpus
   Sensor carries a `{{ }}` expression so the hazard stays covered by the
   standing run.

The first corpus test is worth more than it looks: it asserts the *referential*
property `error.txt` warns about — "A correct Sensor name with a mangled
internal reference is the more likely and more dangerous variant" — against
real manifests, so a future change that rewrote `eventSourceName` would fail
even though the Sensor still rendered.

## 6. Validation chain

| Check | Result |
|---|---|
| Pre-fix render, Sensor with `{{ }}` | **hard failure**, `cannot parse template` |
| Post-fix render | `value: '{{workflow.parameters.target}}'` intact |
| Sensor name with release name identical | `nightly-report`, no doubling |
| Converter re-run, showcase | 126 objects matched, **no fidelity issues** |
| Folder's own tests | 15 passed |
| All `ERRORS_NEW_ANALYSIS` tests | 186 passed |
| `sanitize_fidelity_report` suite | 41 passed |
| `helm unittest Universal-chart` | 234 passed, 28 suites |

## 7. What could NOT be verified

- **Where the report's doubled Sensor name came from.** Not reproducible against
  this converter, and the `rakia-*` manifests are not here. Recorded as
  not-applicable-with-reason rather than as fixed, because claiming a fix for a
  defect that cannot be reproduced would be worse than saying so.
- **Whether the report's Sensors carried `{{ }}` expressions.** If they did,
  their conversion would have failed loudly rather than producing a doubled
  name — which is mild evidence that the reported doubling and the defect fixed
  here are genuinely different findings.
- **That Argo Events itself accepts the rendered Sensor.** Needs a cluster with
  the Argo Events controller. Verified only that the YAML is byte-identical to
  the source.

## 8. OBSERVED, DEFERRED

- **`extraDeploy` has no other coverage.** Before this folder the corpus
  exercised it only through the `ServiceAccount` overflow path
  (`platform-ops/shared`, `shop-web/shared`). It is now covered for a CRD too.
  Broader extraDeploy hardening — e.g. whether `tpl` should apply at all to
  converter-emitted entries as a policy rather than per-entry — is a design
  question, not a defect, and was not attempted.
- **`Helm_Naming_Prefix_Collisions` (folder 10)** is unaffected by this finding
  and its guard stands on its own merits; the two folders turned out to be
  independent despite `error.txt` expecting one fix for both.

## 9. Reproducing

```powershell
python -m pytest universal/gitops-factory/output-showcase/ERRORS_NEW_ANALYSIS/Argo_Events_Sensor_Double_Prefix/test/test_sensor_naming.py -v
python <scratch>/probe_tpl.py    # shows the pre-fix hard failure directly
```
