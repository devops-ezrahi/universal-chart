# STATUS — Zero_Width_Unicode_In_SecretStoreRef (P2, folder 22/35)

**Result: FIXED defensively. The defect does not occur in this repo — zero
invisible characters anywhere — but the repair is cheap, narrow and now
warned about.**

9 tests, GREEN. RED proved by disabling the sanitiser and re-running (§3).

---

## 1. Grep proof: it does not occur here

Scanned every `*.yaml` under `namespaces/`, `namespaces-showcase/` and
`namespaces-live-demo/` for U+200B, U+200C, U+200D, U+2060, U+FEFF and U+00AD:

```
TOTAL zero-width/invisible chars in raw manifests: 0
TOTAL in generated output-showcase:                0
```

The character came from the original project's data. `test_this_repo_has_no_
invisible_characters_of_its_own` re-runs that scan on every test run, so if one
is ever committed the suite says so immediately — which is the only time anyone
would find out, given the character is invisible in a terminal, a diff and a
code review.

## 2. One correction to the report's framing

> "Helm processes the template, it outputs the value as-is (with the char). The
> fidelity checker compares raw vs rendered and they differ."

Whether Helm normalises it is beside the point, and treating this as a
*fidelity-report noise* problem understates it. `Se<U+200C>cretStore` is not
`SecretStore` to the **API server** either. An ExternalSecret whose
`secretStoreRef.kind` carries an invisible character references a kind that does
not exist; it never syncs, and the error message names a string that looks
exactly right. The same applies to a `metadata.name`: every reference to that
object silently misses.

So this is not cosmetic — but it is also **not a conversion defect**. The
character is in the source data. The converter is simply the only place allowed
to repair it, since the campaign's hard constraints forbid editing the raw
manifests.

## 3. The fix, and the line it does not cross

| File | Change |
|---|---|
| `convert_to_universal_chart.py` | new `ZERO_WIDTH_CHARS`, `strip_zero_width()`, `_sanitize_identifiers()` |
| `convert_to_universal_chart.py` | `sanitize_object(doc, notes=None)` calls it and records every repair |
| `convert_to_universal_chart.py` | both call sites in `main()` pass `ns_warnings`, so repairs reach `conflicts_and_warnings.txt` |

**Identifiers only — never payload.** The sanitiser touches `metadata.name`,
`spec.secretStoreRef.name` and `spec.secretStoreRef.kind`. A blanket strip over
the whole document would also rewrite ConfigMap and Secret *data*, where a
zero-width character can be legitimate content — a test fixture, a template, a
translation string — and silently corrupting a payload to tidy up a name is a
worse bug than the one being fixed. Identifiers have no such ambiguity: no
Kubernetes name or kind may contain one, so removing it can only ever repair.
`test_payload_data_is_left_alone` pins that line with a ConfigMap whose value
deliberately contains a ZWNJ.

**And it is never silent.** Every repair is appended to the run's warnings:

```
ExternalSecret/session-store-signing-key: metadata.name contained 1 invisible
character(s) (U+200C) - stripped. Fix the raw manifest at source; until then any
lookup by this name would silently fail.
```

The raw manifest is still wrong; the converter working around it must not hide
that.

## 4. Tests

`Zero_Width_Unicode_In_SecretStoreRef/test/` — 9 tests. Fixture derives from
`namespaces-showcase/identity-auth/session-store.yaml` with three U+200C
characters added: one in `secretStoreRef.kind`, one in the ExternalSecret's
`metadata.name`, and one inside a ConfigMap's data value.

`test_the_fixture_really_contains_invisible_characters` asserts the count is 3.
Without it the whole folder could pass vacuously — nothing else would notice the
characters going missing from the fixture, because they are invisible.

### RED — sanitiser disabled, then restored byte-identically

```
=== zero-width sanitiser disabled ===
...FF..FF                                                            [100%]

AssertionError: assert 'Se\u200ccretStore' == 'SecretStore'
AssertionError: no warning recorded; notes=[]
AssertionError: assert 'session-stor...u200cning-key' == 'session-store-signing-key'
AssertionError: the repair happened silently; it must be visible so the raw
  manifest gets fixed at source.

4 failed, 5 passed in 1.55s
restored: True  sha256 0b609f73b2ca7807
```

### GREEN

```
9 passed in 1.53s
```

## 5. Validation chain

| Step | Result |
|---|---|
| Full showcase converter run | `122 objects matched (28 relocated). No fidelity issues found.` |
| Fidelity before / after | 0 lost / 0 changed, unchanged |
| Generated-output churn | none — 0 occurrences in the real data |
| Warning count | unchanged on the showcase (nothing to repair) |
| `helm lint` | 0 failed |
| Chart's own suite | 234 passed |

## 6. What could NOT be verified without a live cluster

- **That the API server rejects / silently misses the un-sanitised name.** The
  reasoning is from the Kubernetes name grammar, not from an observed failure.
- **That no other invisible character matters.** The list covers the six that
  appear in practice; a homoglyph attack (Cyrillic `с` for Latin `c`) would look
  identical and is not covered — a different problem, deliberately not solved
  here.
- **The report's original object.** Not in this repo.

## 7. OBSERVED, DEFERRED

- **Only three identifier fields are sanitised.** Other references — `envFrom`
  ConfigMap names, `volumes[].secret.secretName`, `serviceAccountName` — would
  have the same failure mode and are not covered. Extending the list is easy;
  choosing to do it should be a deliberate decision rather than a drive-by, and
  no folder covers it.
- **The fidelity checker does not flag invisible characters.** It compares
  strings, so it would report a CHANGED field without ever explaining why two
  identical-looking strings differ. Worth a targeted message; not this folder's.
- Earlier folders' deferred items still stand.

## 8. Reproducing

```powershell
python -m pytest universal/gitops-factory/output-showcase/ERRORS_ANALYSIS/Zero_Width_Unicode_In_SecretStoreRef/test -v
```
