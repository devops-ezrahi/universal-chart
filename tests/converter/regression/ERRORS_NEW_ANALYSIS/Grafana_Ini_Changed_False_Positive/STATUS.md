# STATUS — Grafana_Ini_Changed_False_Positive (TIER 3, folder 25/26)

**VERIFIED_AND_FIXED** (verdict: **false positive — demonstrated, not assumed**).

**Result: no converter change, no chart change, and deliberately no suppression
either.** The report offers this item under two contradictory labels — *"Fix
grafana.ini colon bug"* in the remediation table and *"likely false positive …
visually identical"* in the issue list. Neither was accepted. The colon
hypothesis was tested directly, as bytes, through both halves of the round trip.
**Not one colon shape drifts.** 23 tests.

---

## 1. What `error.txt` demanded, and what was done

> *"ACTION FOR THE NEXT AGENT: do not accept 'visually identical' — diff the two
> byte strings directly and print repr() of both. If they are byte-identical, the
> checker has a comparison bug worth fixing generally. If they differ by quoting
> or structure around a colon, this is a REAL bug."*

Every comparison in this folder is `==` on the exact string, with `repr()` of
both sides in the failure message, through **`dump_yaml`** and through a real
**`helm template`**. Nothing here rests on how the text looks.

> *"[test_blob_fidelity.py] proves whitespace is safe; it does NOT prove
> colon/quoting handling is safe. Extend it rather than assuming it already
> covers this."*

Done. The previous round's test covers trailing newlines, PEM blobs, scripts and
padded lines. This folder is the **colon half** of the same guarantee, and it is
a separate file so neither can be deleted without the loss being obvious.

## 2. The colon shapes tested

A realistic `grafana.ini` carrying every colon the format actually produces — a
URL scheme, a `host:port`, an interpolated `%(http_port)s`, a section header, a
`; comment: with a colon`, and a bare `22:30` — plus 17 isolated hazards:

| Shape | Example | Why it could bite |
|---|---|---|
| `key: value` in a scalar | `datasource: prometheus` | a bare plain scalar containing `": "` re-parses as a **nested mapping** |
| sexagesimal | `12:30`, `12:30:45` | under YAML **1.1** this is the integer **750** (base 60), and PyYAML resolves it that way |
| URL / host:port | `http://prometheus.platform-ops:9090` | colon not followed by a space |
| Windows path | `C:\ProgramData\grafana` | colon in position 2 |
| leading / trailing / bare colon | `:30`, `section:`, `:` | the degenerate cases |
| colon + hash | `note: a value # with a hash` | comment-start hazard on the same line |
| quoting neighbours | `no`, `~`, `""`, `*glob`, `@reboot`, `%(protocol)s` | same emitter path, all classic YAML traps |
| folder 24's scalar | `1e+09` | inside a blob rather than as a whole value |

**All byte-exact, both halves.**

## 3. Why it passes — and why the test still earns its place

YAML's two real colon traps are both handled, by opposite mechanisms:

- **`key: value`** — PyYAML's emitter quotes any scalar containing `": "`,
  because leaving it bare would change the document's structure. Asserted
  directly, not just via the round trip.
- **Sexagesimal** — PyYAML resolves `12:30` as an integer under YAML 1.1, so its
  emitter quotes the *string* `"12:30"` to keep it a string. Go's yaml.v3
  (YAML 1.2) has no sexagesimal at all.

That second one is **the same 1.1-vs-1.2 fault line folder 24 found a live bug
on, approached from the other side.** There, PyYAML *under*-quoted (`1e+09` is a
number to Go but not to PyYAML) and manifests broke. Here PyYAML *over*-quotes,
and over-quoting is always safe. One fault line, two directions, one real defect
and one false positive — which is exactly why "visually identical" was not an
acceptable standard for closing this.

A test pins the hazard itself (`yaml.safe_load("v: 12:30")["v"] == 750`) so the
quoting assertions cannot quietly become vacuous.

## 4. No suppression was added, on purpose

The report lists this under **both** "fix" and "suppress". The answer is neither.
A false positive that **cannot be reproduced** needs no suppression rule, and
adding one would blind the checker to real drift the day the YAML pipeline
changes — which, as folder 24 proved on this very emitter, is not hypothetical.
A test asserts `_fidelity_ignored_path` contains no `grafana` or `.ini` rule, so
a future "just suppress it" fails loudly.

## 5. Validation

```
python -m pytest output-showcase/ERRORS_NEW_ANALYSIS/Grafana_Ini_Changed_False_Positive/test -q
.......................                                                  [100%]
23 passed in 0.65s
```

| Check | Result |
|---|---|
| Colon shapes through `dump_yaml` | **18/18 byte-exact** |
| Colon shapes through `helm template` | **byte-exact, all keys** |
| The reported blob end-to-end | identical as `str` **and** as UTF-8 bytes |
| `key: value` and sexagesimal actually quoted | yes — mechanism asserted, not just outcome |
| Suppression rule added | **none**, asserted |
| Converter / chart changed | **nothing** |
| All `ERRORS_NEW_ANALYSIS` tests | 402 → 425 with this folder's 23 |

One test failed on first run: `test_no_suppression_was_added_for_this` searched
the whole converter for the word "grafana" and matched a **comment folder 24 had
written**. The assertion was too broad, not the code — it now inspects
`_fidelity_ignored_path` specifically.

## 6. What could NOT be verified

- **The original 1 CHANGED error.** It came from a converter run this repo does
  not have, against the real Grafana ConfigMap, which is not in this corpus. What
  is established is that the pipeline in this repo does not produce that error on
  any colon shape the format can contain — including a full, realistic
  `grafana.ini`. If it recurs, the diff is now the first thing to print, and
  these 23 tests narrow where it can possibly come from.
- **Which of the report's two labels was meant.** Unknowable from here, and moot:
  both were tested and neither survived.

## 7. Reproducing

```powershell
cd c:\repos\homelab\universal\gitops-factory
python -m pytest output-showcase/ERRORS_NEW_ANALYSIS/Grafana_Ini_Changed_False_Positive/test -v
```

---

**VERIFIED_AND_FIXED**
