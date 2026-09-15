# STATUS — Sanitizer_Drops_Unparsed_Lines (TIER 3, folder 26/26 — LAST)

**VERIFIED_AND_FIXED.**

**Result: the omission was kept, the blind spot was closed.** `error.txt` draws
the line precisely and this folder respects it — unparsed lines still never reach
shareable output. What changed is that the parser now recognises the three line
*shapes* that were being counted as unparsed, all of which turned out to be the
report's own header text. `lines unparsed` went **4 → 0** on this repo's report,
which finally makes a nonzero value mean something.

15 new tests, existing sanitizer suite 41 still passing, showcase 0 lost /
0 changed, `helm unittest` 234 passed, lint clean.

---

## 1. The distinction `error.txt` insists on

> *"THE BEHAVIOUR IS CORRECT BY DESIGN AND SHOULD NOT BE 'FIXED' THE OBVIOUS WAY.
> […] Do not make it emit unparsed lines into shareable output."*

> *"THE ACTUAL DEFECT IS THE BLIND SPOT, not the omission. […] the count is
> surfaced precisely so someone acts on it. Nobody has."*

Both honoured. Nothing about the omission changed — a test asserts an unparsed
line carrying a real identifier still never reaches any shareable file. The work
was the part nobody had done: **reading the lines.**

## 2. The lines, read

Straight from the internal report, never the shareable copy, exactly as
`error.txt` instructs. This repo's run had **four**, not three:

```
'         renamed on collision) — expected, not a loss.'
'         source, or a field the conversion LOST or CHANGED.'
'Fields the render ADDED are counted but not listed: a Helm chart filling in a'
'dangling references (rename lost a reference): 0'
```

Not one is a finding. Three shapes:

| Shape | Why it failed to parse |
|---|---|
| **Wrapped legend sentence** (×2) | the report wraps at ~78 columns, so the second half arrives indented, carrying no keyword of its own and no severity token |
| **Legend sentence with unmatched opening** (×1) | the hint list has `"fields added"` for the summary row; this sentence opens *"Fields the render ADDED are counted but not listed"* and matches neither |
| **Numeric summary counter** (×1) | prose, colon, integer — a shape the hint list never covered |

So for this corpus the honest answer to `error.txt`'s question is *"truly
uninteresting"* — **and that is exactly why they had to be classified rather than
left.** Four lines of permanent noise in the counter is what made the counter
unreadable, and an unreadable counter is the blind spot.

## 3. What changed

| File | Change |
|---|---|
| `universal/gitops-factory/sanitize_fidelity_report.py` | `_SUMMARY_COUNTER` and `_SEVERITY_LEAD` patterns; `_is_preamble` gained an `after_preamble` context flag; `parse` tracks that flag; one hint added for the unmatched legend sentence |

```python
_SUMMARY_COUNTER = re.compile(r"^[a-z][a-z0-9 ,'()/&+-]*:\s*\d+$")
_SEVERITY_LEAD   = re.compile(r"^\s*(ERROR|WARN\w*|INFO)\b")

def _is_preamble(line, after_preamble=False):
    ...
    if _SUMMARY_COUNTER.match(low):
        return True
    if after_preamble and line[:1].isspace() and not _SEVERITY_LEAD.match(line):
        return True
```

**Shape-based, not a keyword list**, for the two general cases. The continuation
rule is the one with teeth, so it is fenced three ways: it needs the previous
line to have been header text, the current line to be indented, and the current
line not to lead with a severity. `parse` also resets the flag after a record, so
an unrecognised line following a *finding* is still reported.

## 4. The failure mode of this fix, tested for

A rule broad enough to swallow everything would read zero forever and be strictly
worse than the blind spot it replaced. Four tests exist only to prevent that:

- a genuinely novel shape (`CRITICAL >>> …`) is **still** unparsed;
- a real record is never swallowed — asserted through `parse`, since the RECORD
  pattern is tried first and that ordering is the actual guarantee;
- the continuation rule does nothing without the context flag;
- an indented prose line following a **record** is still reported;
- the summary-counter rule requires an actual number, so it cannot degrade into
  "any line with a colon".

## 5. Validation

```
python -m pytest output-showcase/ERRORS_NEW_ANALYSIS/Sanitizer_Drops_Unparsed_Lines/test \
                 test_sanitize_fidelity_report.py -q
56 passed in 1.84s

python run_all.py --cluster-scoped --input namespaces-showcase --output output-showcase
    fields LOST     0
    fields CHANGED  0
    render failures 0
  OK - nothing lost, nothing changed.
    parsed 40 record(s); 0 line(s) unparsed        <- was: 4 line(s) unparsed
```

| Check | Result |
|---|---|
| Unparsed lines on this repo's report | **4 → 0** |
| Line accounting invariant (records + preamble + unparsed == all non-blank) | holds |
| Novel shapes still flagged | yes |
| Unparsed lines still excluded from shareable output | yes, asserted with a planted identifier |
| Warning still printed when the count is nonzero | yes; and absent when it is zero |
| Folder's own tests | **15 passed** |
| Pre-existing `test_sanitize_fidelity_report.py` | **41 passed**, unchanged |
| All `ERRORS_NEW_ANALYSIS` + sanitizer tests | **481 passed** |
| `ERRORS_ANALYSIS` (previous round) | 12 failed / 320 passed — same 12 as before, none new |
| `helm unittest` / `helm lint` | 234 passed / 0 failed |

One test failed on first run and the assertion was wrong, not the code: it
checked `_is_preamble` in isolation for a record line. That helper answers `True`
for records, because the hint list has carried a bare `"error "` entry since
before this folder for the legend — unreachable in practice, since `parse` tries
the RECORD pattern first. The test now asserts through `parse`, and the quirk is
documented rather than "fixed".

## 6. What could NOT be verified

- **The original three lines.** They belong to a run this repo does not have, and
  `error.txt` warns that its index *"summarizes a SUBSET, not the full 1,313-line
  report"*. The four found here are this repo's, read from its own internal
  report. Whether the other run's three had the same shapes is unknowable — but
  the counter is now a live test, so if that run were repeated here, any shape
  the parser still cannot read would fail `test_the_repos_own_report_has_NO_unparsed_lines`
  rather than sit in a header.
- **The scale check `error.txt` asks for** (which run, which namespace tree) is
  answered by the run-identity block folder 1 added; a test asserts the report
  still carries it.

## 7. Reproducing

```powershell
cd c:\repos\homelab\universal\gitops-factory
python -m pytest output-showcase/ERRORS_NEW_ANALYSIS/Sanitizer_Drops_Unparsed_Lines/test -v
python run_all.py --cluster-scoped --input namespaces-showcase --output output-showcase
```

---

**VERIFIED_AND_FIXED** — and with this, all 26 folders in `ERRORS_NEW_ANALYSIS`
carry a STATUS.md.
