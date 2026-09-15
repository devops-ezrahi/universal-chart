# STATUS — ShareProcessNamespace_Lost (TIER 3, folder 21/26)

**Result: BENIGN — suppressed, after trying the more faithful option and finding
it costs more than it is worth.** The report's rating is correct here (unlike
its neighbour in folder 20): the Kubernetes default for `shareProcessNamespace`
IS `false`, so losing an explicit `false` changes nothing. The one value that
matters — `true` — survives and always did. 16 new tests RED→GREEN, showcase
fidelity 0 lost / 0 changed, chart's 234 green, lint clean.

---

## 1. The condition, checked

`error.txt` attaches one:

> **THE ONE CASE THAT WOULD MATTER**: `shareProcessNamespace: TRUE` lets
> containers in a pod see each other's processes — used by debugging sidecars and
> by some service meshes. If any workload sets it to true and that is being
> dropped, containers lose the ability to signal or inspect each other and the
> sidecar silently stops working. The report only ever cites False. **Grep the
> raw manifests for `shareProcessNamespace: true` before accepting this as
> closed.**

Checked on both sides:

| | Result |
|---|---|
| converter captures `true` | yes — it was already in `handle_workload`'s whitelist |
| chart renders `true` | yes — `{{- if .Values.shareProcessNamespace }}` passes exactly that value |
| any corpus workload sets `true` | no — so the tests exercise it synthetically, and one test asserts the corpus still doesn't, so the verdict is re-examined if that changes |

The dangerous direction was never at risk. The benign verdict holds on its own
terms.

## 2. I tried to render it instead, and was wrong

Rendering looked strictly better than suppressing. `handle_workload` **already
captures** the value — it was reaching the values file and being discarded by
the template, which is a worse thing for a template to do than simply not
supporting a field. A one-line `hasKey` guard, identical to folder 20's, would
have preserved it exactly and needed no "is this default benign?" judgement.

That change was made. `test_absent_stays_absent` failed immediately:

```
AssertionError: assert 'shareProcessNamespace' not in
  {..., 'shareProcessNamespace': False, ...}
```

**`values.yaml` declares `shareProcessNamespace: false` as a chart default.** So
`hasKey .Values "shareProcessNamespace"` is *always* true, and the guard would
stamp the field onto every pod the chart renders — inventing it on the many
workloads whose source never mentioned it. Making the guard mean what it says
would require changing the chart's public values API for a field whose loss is
genuinely benign.

Reverted, and suppressed instead — which is what the report recommended in the
first place.

Its neighbour `enableServiceLinks` has **no** declared default, so the same
guard there means what it says; and its Kubernetes default is `true`, so an
explicit `false` genuinely matters. Two adjacent fields, opposite treatments,
for two independent reasons. `test_the_declared_default_is_what_makes_hasKey_useless_here`
pins the values.yaml fact the decision rests on — and also asserts
`enableServiceLinks` has *not* gained a default, since that would silently break
folder 20's guard.

## 3. Files changed

| File | Lines | Change |
|---|---|---|
| `universal/gitops-factory/convert_to_universal_chart.py` | `_is_benign_default_loss` | suppress `shareProcessNamespace == False` on workload kinds |
| `universal/Universal-chart/templates/_pod-spec.tpl` | 34–44 | comment recording why this renders only-when-true while its neighbour uses `hasKey` |
| `universal/gitops-factory/output-showcase/ERRORS_NEW_ANALYSIS/K8s_Runtime_Defaults_Benign_Suppress/test/` | — | folder 17's deferred-verdict test updated, see §5 |

The corpus manifest carrying `shareProcessNamespace: false` was added in folder
20, alongside `enableServiceLinks: false`, specifically so the two verdicts
would be visible side by side.

**Scoped to `False`.** `true` is rendered by the chart and can therefore never
reach the suppression — but the rule checks the value anyway, because a
path-only rule would swallow it, and that is the one outcome this folder exists
to prevent.

## 4. Tests created

16 tests in 3 groups:

1. **The condition** (3) — an explicit `true` survives the converter and the
   template; no corpus workload sets `true`, asserted so the verdict is revisited
   if one ever does.
2. **The false case** (7) — captured by the converter; the diff is suppressed;
   `true` is never suppressed; the rule is scoped to workload kinds and to the
   pod-spec path; the chart does not stamp the field onto every pod; and the
   values.yaml default that forced this decision is pinned.
3. **The neighbours** (6) — `hostNetwork` / `hostIPC` / `hostPID` still render
   only when true (parametrised both ways), and `enableServiceLinks` renders
   while this one does not.

Group 3's first test records something that is otherwise invisible: those three
booleans have the **identical latent issue** — captured by the converter,
rendered only when true — and were deliberately left alone. No defect was
observed for them, no folder covers them, and for all three the dangerous
direction is `true`, which works. Recorded so the asymmetry is a known state
rather than an oversight.

## 5. This folder changed another folder's test, on purpose

Folder 17 deliberately refused to decide `enableServiceLinks` and
`shareProcessNamespace` from inside a bulk rule, and left a test asserting both
verdicts were still open — with the note *"If a later folder decides to suppress
one, it updates this test along with the rule."*

That test failed when this folder made its decision, which is the mechanism
working. It is now split into two named tests recording what each folder decided
and why they went opposite ways. That is a better artefact than the original
placeholder: the next reader sees the two verdicts and their reasons in the file
that lists them.

## 6. Validation chain

| Check | Result |
|---|---|
| `true` survives converter + chart | yes |
| `false` suppressed, `true` not | yes |
| Rendering attempt | reverted — would invent the field on every pod |
| Converter re-run, showcase | 128 objects matched, **no fidelity issues** |
| Folder's own tests | 16 passed |
| All `ERRORS_NEW_ANALYSIS` tests | 346 passed |
| `helm unittest Universal-chart` | 234 passed, 28 suites |
| `helm lint Universal-chart` | 0 failed |

## 7. What could NOT be verified

- **`Deployment/observability-otel-collector`.** Not in this repo. The field's
  handling is now covered in both directions.
- **Whether any `rakia-*` workload sets `true`.** `error.txt` asks for the grep;
  it needs those manifests. The suppression is scoped to `False`, so the answer
  cannot change the outcome — which is the point of scoping it that way.

## 8. OBSERVED, DEFERRED

- **`hostNetwork` / `hostIPC` / `hostPID`** share the exact latent issue and are
  untouched (§4). If a future report shows one of them lost, the fix is this
  folder's analysis applied again — and the values.yaml declared-default problem
  applies to all three equally.
- **`Pod_Template_Metadata_Name_Namespace_Lost` (folder 22)** is the last
  deferred verdict from folder 17's list. Next.

## 9. Reproducing

```powershell
python -m pytest universal/gitops-factory/output-showcase/ERRORS_NEW_ANALYSIS/ShareProcessNamespace_Lost/test/test_share_process_namespace.py -v
```
