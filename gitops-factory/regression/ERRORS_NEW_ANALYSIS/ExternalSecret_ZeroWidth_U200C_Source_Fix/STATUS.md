# STATUS — ExternalSecret_ZeroWidth_U200C_Source_Fix (TIER 2, folder 15/26)

**Result: BOTH, as `error.txt` recommends — with the wider sweep it asks for
actually done.** The converter's defensive strip already existed and already
warned; what did not exist was coverage beyond three fields, or any visibility
into invisible characters sitting in *payload*. Identifier-valued keys are now
stripped anywhere in a document, and anything else carrying one is reported
without being touched. 22 new tests RED→GREEN, one deliberately-broken raw
manifest added as standing coverage, showcase fidelity 0 lost / 0 changed,
chart's 234 green.

---

## 1. The disagreement between rounds

| Round | Position |
|---|---|
| previous | "Do not modify raw manifests; fix Helm templates/values or the converter script" → sanitize in the converter |
| this report | "Fix the raw source manifests in the cluster/upstream. [...] The chart's auto-stripping works but is **a workaround, not a fix**." |

`error.txt` resolves it: *"The strongest answer is BOTH — repair the upstream
manifest so the source of truth is clean, AND keep a defensive strip in the
converter so a future paste cannot reintroduce it silently."*

Agreed, with one refinement that turned out to matter: **a defensive strip is
only a workaround while it is silent.** It was not. `_sanitize_identifiers`
already recorded every removal in `conflicts_and_warnings.txt`, ending with the
words "Fix the raw manifest at source". So the "workaround" complaint was
already half-answered before this folder started.

**The upstream-repair half is not applicable to this repo.** The `rakia-*`
manifests are not here; there is nothing to repair. What this repo can
contribute is making the next occurrence impossible to miss.

## 2. What was actually missing

`error.txt`'s other instruction, which had not been carried out:

> **SEARCH WIDER THAN THIS ONE FIELD**: grep every raw manifest for the whole
> zero-width family — U+200B, U+200C, U+200D, U+FEFF. Only `secretStoreRef.kind`
> surfaced because that is where a mismatch happened to be detectable; the same
> paste could have landed anywhere.

`_sanitize_identifiers` covered exactly three fields: `metadata.name`,
`spec.secretStoreRef.name`, `spec.secretStoreRef.kind`. A stray character in
`serviceAccountName`, a volume's `claimName`, a `configMap.name` or a Route's
backing Service name passed through untouched *and unreported*.

The character family itself was already complete — all six, including the BOM
and the soft hyphen. It was the *coverage of positions* that was narrow.

## 3. Files changed

| File | Lines | Change |
|---|---|---|
| `universal/gitops-factory/convert_to_universal_chart.py` | ~366–381 (new) | `IDENTIFIER_KEYS`, `NON_IDENTIFIER_PARENTS` |
| `universal/gitops-factory/convert_to_universal_chart.py` | ~384–404 (new) | `find_zero_width()` — read-only whole-document scan |
| `universal/gitops-factory/convert_to_universal_chart.py` | ~407–429 (new) | `_strip_identifiers_deep()` |
| `universal/gitops-factory/convert_to_universal_chart.py` | ~470–495 | wire both into `_sanitize_identifiers`, plus the payload report |
| `universal/gitops-factory/convert_to_universal_chart.py` | ~462 | warning names the object, not the repaired value |
| `universal/gitops-factory/namespaces-showcase/identity-auth/session-store.yaml` | 42–54 | a real U+200C in `spec.secretStoreRef.kind` |

### The line this fix must not cross

**Identifiers are stripped; payload is reported and left alone.**

ConfigMap/Secret `data` may legitimately contain a zero-width character — a test
fixture, a translation string, a template — and silently rewriting payload to
tidy up a name would be a worse bug than the one being fixed. So `find_zero_width`
scans the whole document but mutates nothing, and anything found under `data.`
or `stringData.` produces:

```
... contains invisible character(s) (U+200C) in CONTENT, not in an identifier -
deliberately NOT stripped, since payload may legitimately contain them. Confirm
it is intentional; if it is a stray paste, fix the raw manifest at source.
```

Labels are treated the same way, for a subtler reason: labels are *compared*,
not *resolved*, so rewriting one changes what a selector matches. Reporting is
right; repairing is not.

`NON_IDENTIFIER_PARENTS` exists because key names are reused — `data.name` in a
ConfigMap is content, and a container's `env[].name` is a variable name that
Kubernetes does permit to be odd. Three tests pin each of these.

### A message bug fixed along the way

The existing warning interpolated the *cleaned value* where a reader expects the
object's name, so repairing a `kind` field printed:

```
ExternalSecret/SecretStore: spec.secretStoreRef.kind contained 1 invisible character(s)...
```

`SecretStore` is not an object name. In a warning whose entire purpose is to
point at one line of one manifest, naming the wrong thing wastes exactly the
time the warning was meant to save. Now:

```
ExternalSecret/session-store-signing-key: spec.secretStoreRef.kind contained 1 invisible character(s) (U+200C) - stripped. Fix the raw manifest at source; until then any lookup by this name would silently fail.
```

## 4. The corpus manifest that is knowingly wrong

`namespaces-showcase/identity-auth/session-store.yaml` now contains a real
U+200C between `Secret` and `Store` in `spec.secretStoreRef.kind` — the exact
field and exact character the report describes.

This is **the only place in this repo where a raw manifest is deliberately
broken**, and the file says so at the point of the defect, ending with: *"Do not
'clean' this line — deleting the character removes the only standing coverage
for the strip."* `test_the_corpus_still_carries_the_zero_width_character` fails
if someone does.

The tension with "do not modify raw manifests" is real and resolved in the
opposite direction from the rest of this batch: everywhere else I added
manifests representing *correct* input the corpus lacked. Here the corpus needs
an *incorrect* input, because the code under test is a repair.

## 5. Validation

```
$ python convert_to_universal_chart.py --input namespaces-showcase --output <tmp> \
    --chart ../Universal-chart --verify-fidelity --cluster-scoped
  128 objects matched (30 legitimately relocated).
  No fidelity issues found.

conflicts_and_warnings.txt:
[identity-auth] ExternalSecret/session-store-signing-key: spec.secretStoreRef.kind
contained 1 invisible character(s) (U+200C) - stripped. Fix the raw manifest at
source; until then any lookup by this name would silently fail.
```

Fidelity stays at 0 **because** the strip repairs the reference — which is the
point `error.txt` makes about auto-stripping masking the problem. The warning is
what stops it being masked.

| Check | Result |
|---|---|
| Folder's own tests | 22 passed |
| All `ERRORS_NEW_ANALYSIS` tests | 249 passed |
| `helm unittest Universal-chart` | 234 passed, 28 suites |
| Corpus-wide sweep | one known instance, nothing else |

## 6. Tests created

22 tests in 5 groups:

1. **The whole family** (8) — all six characters parametrised by name and
   codepoint; non-strings pass through; clean strings unchanged.
2. **The reported field and the wider sweep** (5) — the exact field is repaired
   and warned about; the warning names the object; `serviceAccountName` and
   `priorityClassName` are now covered; a nested `configMap.name` is stripped.
3. **What must not be stripped** (3) — ConfigMap data is reported but never
   rewritten; a key literally named `name` inside `data` is not an identifier;
   label values are left alone.
4. **The sweep itself** (3) — reports path and codepoint, walks lists, mutates
   nothing.
5. **Corpus** (3) — the character is still there, it is where the report says,
   and no *other* manifest carries one.

Group 3 is the group that matters. Everything else here makes the strip do more;
those three bound it, and without them "strip identifiers anywhere" would
gradually become "strip everything".

## 7. What could NOT be verified

- **The upstream repair.** The affected manifests are in a cluster this repo
  cannot see. `error.txt`'s "fix at source" half is recorded as
  not-applicable-here rather than done.
- **Whether the report's two occurrences were only in `secretStoreRef.kind`.**
  Its own text says that is simply where a mismatch happened to be *detectable* —
  which is the reason for the wider sweep, and the reason the answer cannot be
  known from the report alone.
- **Whether a zero-width character in payload anywhere in `rakia-*` is
  intentional.** Now reported rather than decided, which is the correct division
  of labour: the converter cannot know, and the person who pasted it can.

## 8. OBSERVED, DEFERRED

- **`UTF8_Encoding_Corruption_In_ConfigMap_Data` (folder 4)** is the adjacent
  encoding defect and was fixed there. This folder deliberately did not touch
  it; `test_every_corpus_file_is_valid_utf8` there and the sweep here now cover
  the two halves of "is the source text clean".
- **`find_zero_width` is not called on the RENDERED side.** A character
  introduced by the chart is not currently possible (templates are ASCII), but
  the scan would be cheap to add if that changes. Not done — no defect observed.

## 9. Reproducing

```powershell
python -m pytest universal/gitops-factory/output-showcase/ERRORS_NEW_ANALYSIS/ExternalSecret_ZeroWidth_U200C_Source_Fix/test/test_zero_width.py -v

cd universal/gitops-factory
python convert_to_universal_chart.py --input namespaces-showcase --output <tmp> `
  --chart ../Universal-chart --verify-fidelity --cluster-scoped
Select-String <tmp>/report/conflicts_and_warnings.txt -Pattern invisible
```
