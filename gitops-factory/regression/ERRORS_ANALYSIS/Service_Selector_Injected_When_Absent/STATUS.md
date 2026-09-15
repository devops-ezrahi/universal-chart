# STATUS — Service_Selector_Injected_When_Absent (P3, folder 32/35)

**Result: the checker classification is CONFIRMED — it was already correct
before this campaign. But re-reading the template as error.txt demanded turned
up two things the report missed, and both are now fixed.** 13 tests, 2 RED→GREEN.

---

## 1. The template, re-read

error.txt flags its own analysis as unreliable — it quotes
`{{- if or $spec.selector $spec.labels }}`, then corrects itself mid-sentence
with *"Wait, line 21 says `{{- if not $spec.selector }}`"* — and says the
conclusion depends on which is really there.

**Neither is there.** `service.yaml` renders:

```gotemplate
{{- if ne .Values.service.type "ExternalName" }}
selector:
  {{- include "universal-chart.selectorLabels" . | nindent 4 }}
{{- end }}
```

There is no condition on `$spec.selector` at all. The selector is injected
**unconditionally** for every non-ExternalName Service, whether or not the raw
manifest had one. `services.yaml` does the same with a `with/else` fallback.
`test_neither_quoted_condition_exists_in_the_template` pins the actual text so
the next reader does not have to re-derive it.

## 2. Finding one: the checker was already right

`_fidelity_ignored_path` has suppressed `Service.spec.selector` since before
this campaign, with the reason recorded in its docstring — the selector comes
from `universal-chart.selectorLabels`, which is computed from `Release.Name` and
never reads `.Values`, so the converter cannot influence it. The report's own
recommendation ("the fidelity check should account for this") was already
implemented.

**But the suppression was asymmetric**, and that is finding 1a.
`_fidelity_ignored_path` gated only the recursive descent, so:

- raw HAS a selector, render differs → suppressed ✔
- raw has NO selector, render adds one → **reported as `ADDED spec.selector`** ✘

Exactly the case this folder is named for. Fixed by consulting the same rule in
the ADDED branch. Asymmetric suppression is worse than none — it makes the rule
look unreliable to anyone reading the report.

Measured on the real showcase: **`fields ADDED` 275 → 273**, the two
selector-less Services.

## 3. Finding two: the behavioural risk is real and was unguarded

error.txt is right to insist on this:

> "a selector-less Service in Kubernetes is normally paired with a manually-
> managed Endpoints object ... Injecting a selector into such a Service would
> make it start matching pods and silently override those manual Endpoints —
> that would be a REAL and serious break, not a false positive."

The check it asks for:

```
selector-less Services: 2
   shared.yaml     / platform-ops-shared-placeholder  (ClusterIP)
   shared-prd.yaml / platform-ops-shared-placeholder  (ClusterIP)
Endpoints/EndpointSlice objects anywhere: none
```

Two selector-less Services, **zero** Endpoints objects. So nothing in this repo
breaks, and the false-positive classification holds — for this data.

It does not hold in general, and nothing said so. The chart has no way to render
a Service without a selector, the converter dropped the fact silently, and the
fidelity checker deliberately ignores that exact path. Three layers, each
correct on its own, adding up to a silent behaviour change.

`handle_service` now warns:

```
[platform-ops/shared] Service/platform-ops-shared-placeholder: the raw manifest declares
NO selector, but the chart renders one from the release's own labels. If this Service is
backed by a hand-managed Endpoints object, the injected selector will override it.
```

Warning rather than failing: on this data it is harmless, and a conversion that
refused to proceed would be wrong more often than right. But it is no longer
invisible.

## 4. Files changed

| File | Change |
|---|---|
| `convert_to_universal_chart.py` `_fidelity_diff` | consult `_fidelity_ignored_path` in the ADDED branch too |
| `convert_to_universal_chart.py` `handle_service` | warn when the raw Service declares no selector |

No template change. error.txt says explicitly *"the fix belongs in the fidelity
checker, not in the converter or the chart. Do not 'fix' the template here"* —
and that is right: injecting the release's selector is the chart's intended
behaviour and what makes a normal Service work.

## 5. Tests

`Service_Selector_Injected_When_Absent/test/` — 13 tests (shared file with the
blob-fidelity run). Fixture derives from
`namespaces-showcase/platform-ops/shared.yaml`, which really does contain a
selector-less Service, reshaped as a microservice so both Service templates are
exercised.

### RED — before

```
2 failed, 11 passed in 29.39s

AssertionError: the injected selector is reported after all;
  diffs=[('ADDED', 'spec.selector', "render added {'app.kubernetes.io/name': 'log-agent'}")]
AssertionError: a selector-less Service was converted with no warning; warnings=[]
```

### GREEN — after

```
13 passed
```

## 6. Validation chain

| Step | Result |
|---|---|
| Full showcase converter run | `122 objects matched (28 relocated). No fidelity issues found.` |
| `fields ADDED` before / after | **275 → 273** — the two selector-less Services |
| Warnings before / after | **19 → 21** — the two new selector warnings |
| Fidelity lost / changed | 0 / 0, unchanged |
| `helm lint` | 0 failed |
| Chart's own suite | 234 passed |

This is one of the few folders whose fix changes the real report, and both
numbers moved by exactly the expected 2.

## 7. What could NOT be verified without a live cluster

- **That the injected selector would actually override hand-managed Endpoints.**
  That is the endpoints controller's behaviour: once a Service has a selector it
  takes ownership of the Endpoints object. Documented, not observed here.
- **Whether `platform-ops-shared-placeholder` is meant to have Endpoints.** It
  is a placeholder with no backend in this repo, so probably not — but that is
  an inference about intent, not a verification.

## 8. OBSERVED, DEFERRED

- **`ExternalName` Services are the one shape that renders no selector**, which
  is a real if narrow escape hatch for a selector-less Service. Not offered as a
  workaround because it changes the Service type and therefore its DNS
  behaviour.
- **The chart cannot express a selector-less Service at all.** Adding
  `service.selector: none` would be the honest fix and is a chart feature, not a
  defect — no folder covers it.
- Earlier folders' deferred items still stand.

## 9. Reproducing

```powershell
python -m pytest universal/gitops-factory/output-showcase/ERRORS_ANALYSIS/Service_Selector_Injected_When_Absent/test -v
```
