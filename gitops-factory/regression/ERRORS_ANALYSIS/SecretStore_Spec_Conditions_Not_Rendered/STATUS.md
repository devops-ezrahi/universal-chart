# STATUS — SecretStore_Spec_Conditions_Not_Rendered (P2, folder 24/35)

**Result: the labels half FIXED by folder 21; the conditions half REFUTED —
it already worked, before and after.**

7 tests GREEN, two of them guards that were green before the fix.

---

## 1. The two halves have different answers

> "Why (labels): build_secretstore_entry never captures labels.
>  Why (conditions): secretstore.yaml template doesn't render registration or
>  conditions from values."

**Labels — TRUE.** `build_secretstore_entry` read four spec keys and no
metadata at all.

**Conditions — FALSE.** `conditions` was one of those four keys, and
`secretstore.yaml` already had:

```gotemplate
{{- with $spec.conditions }}
conditions:
  {{- toYaml . | nindent 4 }}
{{- end }}
```

Both sides worked. `test_conditions_were_already_captured` and
`test_conditions_were_already_rendered` passed before the fix and after it —
which is what makes them proof rather than a claim.

**`ownerReferences` — REFUTED**, same as folder 21: it is stripped from every
object on purpose, because a source-cluster `uid` would make the target's
garbage collector delete the object. See folder 21's STATUS §1.

## 2. What was actually fragile

`conditions` survived only because somebody had thought to list it. The four-key
whitelist dropped everything else, so a SecretStore's `controller` field — or
whatever the operator adds next — would have vanished with no warning.
`test_unlisted_spec_fields_survive` pins the general case rather than the one
field the report happened to name.

`spec.conditions` is worth the care: it controls which namespaces may reference
the store. Losing it either widens access or breaks ExternalSecrets that
legitimately reached the store from another namespace.

## 3. Files changed

**None of its own.** Folder 21's `_eso_spec()` + `_eso_metadata()` and the
`secretstore.yaml` metadata block cover it.

## 4. Tests

`SecretStore_Spec_Conditions_Not_Rendered/test/` — 7 tests, sharing folder 21's
fixture.

```
7 passed
```

RED evidence is folder 21's: `AssertionError: entry keys = ['conditions',
'provider', 'refreshInterval']` — note `conditions` present, `labels` absent.
That single line is both halves of this folder's answer at once.

## 5. Validation chain

Shared with folder 21: showcase converter run clean, fidelity 0 lost / 0 changed,
no generated-output churn, `helm lint` 0 failed, chart's 234 green.

## 6. What could NOT be verified without a live cluster

- **That `conditions` actually gates cross-namespace access.** ESO controller
  behaviour.
- **That the target operator serves the same SecretStore schema.** The
  pass-through carries the source's fields verbatim.

## 7. OBSERVED, DEFERRED

- The report's note that "the labels half is a manifestation of the global label
  loss documented in `Workload_Metadata_Labels_Lost`" is correct, and that folder
  is still open. This fix covers the ESO objects only; workloads, ConfigMaps,
  Secrets and the rest are still that folder's.
- Earlier folders' deferred items still stand.

## 8. Reproducing

```powershell
python -m pytest universal/gitops-factory/output-showcase/ERRORS_ANALYSIS/SecretStore_Spec_Conditions_Not_Rendered/test -v
```
