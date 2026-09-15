# STATUS — UTF8_Encoding_Corruption_In_ConfigMap_Data (TIER 1, folder 4/26)

**Result: FIXED.** The only genuine data-corruption defect in this batch. Root
cause is one missing keyword on one call: `helm_template()` decoded Helm's
UTF-8 stdout with the Windows ANSI codepage. Reproduced end-to-end as a real
fidelity CHANGED error, fixed, and now covered by the corpus — which previously
contained no non-ASCII byte at all. 15 new tests RED→GREEN, showcase fidelity
0 lost / 0 changed, chart's 234 green, sanitizer's 41 green.

---

## 1. Why this one is different from everything else in the batch

`error.txt` draws the line itself:

> THIS IS A REAL CORRUPTION, NOT A FORMATTING DIFF. Unlike the CA-bundle reflow
> and grafana.ini items in this same report, the rendered content genuinely
> differs from the source and the mangled bytes would be deployed to the
> cluster. A corrupted name inside navlogback.xml ships as corrupted; a
> corrupted character inside init-config.sh is executed as a shell script.

## 2. Root cause — one call, one missing keyword

`helm_template()`:

```python
result = subprocess.run(cmd, capture_output=True, text=True)   # no encoding=
```

`text=True` with no `encoding=` decodes the child's stdout using
`locale.getpreferredencoding()` — on Windows, the ANSI codepage. Helm always
emits UTF-8. Decoding UTF-8 bytes with a single-byte codepage is textbook
mojibake.

**This is the only place the pipeline crosses a process boundary.** Every
`read_text` / `write_text` in the converter already passes `encoding="utf-8"`,
which is exactly why the corruption had one narrow, hard-to-spot home.

`error.txt` reached the same hypothesis unaided — "On Windows, open() with no
encoding= argument defaults to the system ANSI codepage (cp1252 here), which is
almost certainly the culprit" — and named the fix pattern already in this repo:
`run_all.py` calls `subprocess.run(..., encoding="utf-8", errors="replace")`.

## 3. Files changed

| File | Lines | Change |
|---|---|---|
| `universal/gitops-factory/convert_to_universal_chart.py` | 2725–2739 | `helm_template`: `encoding="utf-8", errors="replace"` |
| `universal/gitops-factory/convert_to_universal_chart.py` | 291–316 | `dump_yaml`: `allow_unicode=True` |
| `universal/gitops-factory/namespaces-showcase/platform-data/redis.yaml` | 69–81 (new) | ConfigMap key `init-config.sh` carrying Ľ, š, á and an em-dash |
| `universal/gitops-factory/output-showcase/**` | regenerated | consequence |

### Why `errors="replace"` and not the default

A byte sequence that is genuinely not UTF-8 should surface as U+FFFD in the
diff, where the fidelity checker reports it as a CHANGED field. The default
(`strict`) would raise and abort a 25-release verification run on one bad byte
— turning a visible, localised data problem into an opaque crash. Matches
`run_all.py`.

### The second half — `dump_yaml(allow_unicode=True)`

`error.txt` names this as part of the same investigation ("dump_yaml() — confirm
it passes allow_unicode=True"). Without it PyYAML escapes every non-ASCII
character as `\uXXXX`, and because a string containing an escape cannot be a
literal block, it collapses the whole value into one double-quoted scalar:

```yaml
init-config.sh: "#!/bin/sh\n# Maintainer: Ľuboš Nov\xE1k — cache platform team\n..."
```

That **round-trips correctly** — Go's yaml.v3 reads the escapes back, and
fidelity was 0 even before this change — so it is *not* corruption and is
recorded here as a readability fix, not a data fix. It still matters: the
converter's own docs call these files "the hand-editable, per-environment file",
and a shell script arriving as one escaped scalar is not hand-editable in any
useful sense. After:

```yaml
init-config.sh: '#!/bin/sh
  # Maintainer: Ľuboš Novák — cache platform team
  echo "redis cache — warming up"'
```

## 4. RED — the corruption reproduced end to end

With the non-ASCII ConfigMap key added to the corpus and the old
`subprocess.run(...)` in place:

```
$ python convert_to_universal_chart.py --input namespaces-showcase --output <tmp> \
    --chart ../Universal-chart --verify-fidelity --cluster-scoped

  123 objects matched (28 legitimately relocated).
  1 potential fidelity issue(s) written to ...

ERROR BREAKDOWN BY CATEGORY (generated from the lines below):
  field_changed_sh  1

  ERROR  ConfigMap/redis-cache-conf CHANGED at data.init-config.sh:
    raw='#!/bin/sh\n# Maintainer: Ľuboš Novák — cache platform team\n
         echo "redis cache — warming up"\n'
    render='#!/bin/sh\n# Maintainer: ִ½uboֵ¡ Novֳ¡k ג€” cache platform team\n
            echo "redis cache ג€” warming up"\n'
```

Same object kind, same field shape and same failure as the report's
`ConfigMap/rakia-redis-eight-testing-config - data.init-config.sh`.

The mangled characters print as `ִ½uboֵ¡ ... ג€”` rather than the report's
`Ä¾uboÅ¡ ... â€"` only because this console's codepage is not cp1252 — the byte
mechanism is identical, and §6 pins the arithmetic independently of any
console.

## 5. GREEN

```
$ python convert_to_universal_chart.py --input namespaces-showcase --output <tmp> \
    --chart ../Universal-chart --verify-fidelity --cluster-scoped

  123 objects matched (28 legitimately relocated).
  No fidelity issues found.
```

```
$ python -m pytest .../UTF8_Encoding_Corruption_In_ConfigMap_Data/test/test_utf8_encoding.py -q
...............                                                          [100%]
15 passed in 0.76s
```

## 6. A discrepancy in the report's own evidence, recorded not smoothed over

The first version of `test_the_mojibake_signatures_*` asserted the report's
transcription and **failed**. The byte arithmetic:

| char | codepoint | UTF-8 | read as cp1252 | report says |
|---|---|---|---|---|
| `Ľ` | U+013D | `C4 BD` | `Ä½` | `Ä¾` ✗ |
| `ľ` | U+013E | `C4 BE` | `Ä¾` | — (this is what the report's string implies) |
| `š` | U+0161 | `C5 A1` | `Å¡` | `Å¡` ✓ |
| `—` | U+2014 | `E2 80 94` | `â€”` | `â€"` ≈ (0x94 is U+201D `”`, which OCR flattens to `"`) |

So the report's *mangled* half corresponds to the lowercase `ľ` while its
*clean* half is written with the uppercase `Ľ`. The report was transcribed from
screenshots, so this is a one-character reading error in the evidence, **not a
different bug** — the mechanism and the fix are identical for both codepoints.

The test now asserts the arithmetic and documents the divergence, rather than
asserting a transcription that would make a correct fix look wrong.

## 7. Tests created

`ERRORS_NEW_ANALYSIS/UTF8_Encoding_Corruption_In_ConfigMap_Data/test/test_utf8_encoding.py`
— 15 tests in 4 groups:

1. **The diagnosis** (1) — the mojibake signatures are UTF-8 bytes read as a
   single-byte codepage. This tests the report's evidence, not our code: if it
   failed, `encoding=` would be the wrong fix.
2. **helm boundary** (3) — Ľ/š/á/em-dash survive a real `helm template`; no
   mojibake signature (including U+FFFD) appears in the render; and a
   monkeypatched guard asserting `encoding="utf-8"` and `errors="replace"` are
   still pinned. That last one exists because tidying the call back to
   `subprocess.run(cmd, capture_output=True, text=True)` reintroduces the
   corruption silently, on Windows only, for non-ASCII content only.
3. **dump_yaml** (8) — emits real characters not escapes; keeps a multiline
   script as a block scalar; and round-trips six string shapes byte-exactly, so
   readability cannot cost fidelity.
4. **Corpus coverage** (3) — the corpus contains non-ASCII at all; it includes
   the codepoints the report names; and every raw manifest is valid UTF-8 at
   rest.

The second test in group 2 exists because the first would also pass if *both*
sides were equally corrupted — a positive-only assertion is not enough for an
encoding bug.

## 8. Blast radius

`error.txt`: *"the bug affects 100% of non-ASCII content, so the true blast
radius equals however much of it there is."* Before this folder,
`namespaces-showcase` contained **zero** non-ASCII bytes across every manifest —
so the measured blast radius was zero and the round-trip check was structurally
incapable of detecting the corruption. It now carries one deliberate instance,
and `test_every_corpus_file_is_valid_utf8` guards the at-rest half.

## 9. Validation chain

| Check | Result |
|---|---|
| Converter re-run, showcase | 123 objects matched, **no fidelity issues** |
| RED (pre-fix, same corpus) | 1 CHANGED — `data.init-config.sh` mojibake |
| Folder's own tests | 15 passed |
| All `ERRORS_NEW_ANALYSIS` tests | 84 passed |
| `sanitize_fidelity_report` suite | 41 passed |
| `helm unittest Universal-chart` | 234 passed, 28 suites |

## 10. What could NOT be verified

- **The four real occurrences** (`ConfigMap/rakia-zk-config` ×3,
  `ConfigMap/rakia-redis-eight-testing-config` ×1). Those manifests are not in
  this repo. What is proven is that the code path they take no longer corrupts
  non-ASCII, for the exact codepoints involved.
- **Behaviour on a non-Windows host.** On Linux `locale.getpreferredencoding()`
  is normally UTF-8 already, so the bug would not reproduce there — which is
  likely why it survived. The fix pins the encoding on every platform rather
  than relying on the locale.
- **Whether any raw manifest elsewhere carries genuinely non-UTF-8 bytes.**
  Guarded for this corpus by `test_every_corpus_file_is_valid_utf8`; cannot be
  checked for manifests not present here.

## 11. OBSERVED, DEFERRED

- **`ExternalSecret_ZeroWidth_U200C_Source_Fix` (folder 15)** asks for a wider
  sweep of the zero-width family (U+200B/C/D, U+FEFF) across raw manifests.
  Adjacent to this folder's "grep for non-ASCII" and deliberately **not** done
  here — the converter already strips zero-width characters
  (`strip_zero_width`), and the source-vs-converter decision belongs to that
  folder. Not touched.
- **`CA_Bundle_Certificate_Line_Reflow` (folder 18)** is the other half of the
  PyYAML ↔ Go yaml.v3 boundary named in this folder's analysis: block-scalar
  semantics rather than character encoding. `dump_yaml`'s `width=100` is
  relevant there. Not touched.

## 12. Reproducing

```powershell
python -m pytest universal/gitops-factory/output-showcase/ERRORS_NEW_ANALYSIS/UTF8_Encoding_Corruption_In_ConfigMap_Data/test/test_utf8_encoding.py -v

# see the readable, block-scalar form in the generated values
cd universal/gitops-factory
Select-String output-showcase/base/redis.yaml -Pattern "Maintainer"
```
