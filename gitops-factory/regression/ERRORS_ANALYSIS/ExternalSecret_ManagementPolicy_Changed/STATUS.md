# STATUS — ExternalSecret_ManagementPolicy_Changed (P1, folder 23/35)

**Result: FIXED by folder 21's whitelist removal. Every clause of the reported
mechanism is refuted, each with its own test.**

6 tests, GREEN; RED evidence is folder 21's (`entry keys = ['data',
'refreshInterval', 'secretStoreRef', 'target']` — no `managementPolicy`).

---

## 1. The reported mechanism, clause by clause

> "build_externalsecret_entry captures it at line 489, but subtract_defaults
> removes 'Default' as it equals the default value. Chart defaults to 'Create'."

| Clause | Verdict | Test |
|---|---|---|
| the converter captures it | **false** — it copied a 5-key whitelist and dropped the rest | `test_the_converter_carries_management_policy` (RED before) |
| `subtract_defaults` removes `"Default"` because it *is* the default | **false** — it compares values, not names | `test_subtract_defaults_does_not_treat_the_string_Default_as_a_default` |
| the chart defaults to `"Create"` | **false** — no `Create` appears in the template at all | `test_the_chart_has_no_Create_default` |

`subtract_defaults` treats `"Default"` as an ordinary string. It removes the key
only when the layer underneath holds the same string — and that layer then
supplies it again at render time, which folder 15 proves end to end. The literal
word "Default" carries no meaning to it.

The chart's only ESO default of that shape is
`creationPolicy | default "Owner"`, a different field entirely.

**So nothing turned `Default` into `Create`.** The field was dropped by the
whitelist and the rendered manifest had no `managementPolicy` line at all. What
the report saw as a changed value was an absent one.

This is the third folder in the campaign whose stated cause was
`subtract_defaults` and whose real cause was somewhere else — see folder 11
(imagePullPolicy, never captured) and folder 15 (the chart's `| default`).
`subtract_defaults` has now been accused three times and been innocent three
times.

## 2. Files changed

**None of its own.** The fix is folder 21's `_eso_spec()` — carry every spec key
rather than a whitelist. See that folder's STATUS §2.

## 3. Tests

`ExternalSecret_ManagementPolicy_Changed/test/` — 6 tests, sharing folder 21's
fixture rather than duplicating it: one raw manifest, one defect, two reports.

Beyond the three refutations, `test_the_chart_renders_the_declared_policy_verbatim`
loops over `Default`, `Merge` and `Create` so the value is carried rather than
mapped, and `test_nothing_is_invented_when_the_field_is_absent` pins that an
ExternalSecret without the field still renders without it.

```
6 passed
```

## 4. Validation chain

Shared with folder 21: showcase converter run clean, fidelity 0 lost / 0 changed,
no generated-output churn, `helm lint` 0 failed, chart's 234 green.

## 5. What could NOT be verified without a live cluster

- **What `managementPolicy` actually does to Secret ownership.** That is ESO
  controller behaviour.
- **Whether the target operator version defines the field at all.** The
  pass-through carries whatever the raw manifest declared; a CRD that does not
  know the field would reject it at apply time. Not checkable offline.
- **The report's original object.** Not in this repo.

## 6. OBSERVED, DEFERRED

- Root-cause row F in the report (*"chart template has wrong defaults (6, 3,
  OnFailure, Create)"*) is now fully accounted for: `6`/`3` were folder 15
  (real, fixed), `OnFailure` was folder 16 (the line does not exist), `Create` is
  this folder (does not exist either). **The row is two-thirds fiction.**
- Earlier folders' deferred items still stand.

## 7. Reproducing

```powershell
python -m pytest universal/gitops-factory/output-showcase/ERRORS_ANALYSIS/ExternalSecret_ManagementPolicy_Changed/test -v
```
