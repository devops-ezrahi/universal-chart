# STATUS — Summary_Vs_Report_Data_Mismatch (TIER 0, folder 1/26)

**Result: FIXED.** The defect is a process defect — `error.txt` says so itself
("THIS IS A PROCESS DEFECT, NOT A CODE DEFECT") — so the fix is structural: the
fidelity report now states which run it describes and generates its own
category breakdown, making both halves of the failure impossible to repeat.
20 new tests RED→GREEN. Sanitizer's own 41 tests green, chart's 234 green,
showcase fidelity still 0 lost / 0 changed, and the sanitizer's unparsed-line
count is unchanged at 3 (the same 3 lines, byte-for-byte, as before).

The substantive question `error.txt` raised and could not answer — its "TWO
POSSIBLE READINGS" of the eight missing error types — is **settled as
Reading 1** (they were genuinely fixed) with a test per type.

---

## 1. What the defect actually is

Two documents were used interchangeably to set priorities while describing
different converter runs:

- a hand-maintained error summary, and
- the generated `values/report/fidelity_report.txt`.

Eight error types the summary rated significant — including its own #1 HIGH
(`containers[0].resources`, 41) and its highest MEDIUM
(`progressDeadlineSeconds`, 68) — appear **zero times** in the report. Matched
categories disagree by up to 4.5x (`Labels/annotations lost | 644 | 2,928`).

Neither document said which run it came from. That is the whole defect: the
contradiction was undetectable, so every count in every other folder inherited
it. `error.txt`'s own instruction is *"re-run the converter on a single known
input set, regenerate the report, and rebuild the summary from it. Do not
reconcile the two documents by hand."*

Fixing it by re-running once would fix today's confusion and leave the
mechanism intact. Two changes remove the mechanism.

## 2. Files changed

| File | Lines | Change |
|---|---|---|
| `universal/gitops-factory/convert_to_universal_chart.py` | 432–461 (new) | `run_fingerprint()` — content-derived run identity |
| `universal/gitops-factory/convert_to_universal_chart.py` | 3216–3221 (new) | `_FIDELITY_FIELD_ERROR` regex |
| `universal/gitops-factory/convert_to_universal_chart.py` | 3222–3262 (new) | `fidelity_error_breakdown()` |
| `universal/gitops-factory/convert_to_universal_chart.py` | 3967, 3971–3983 | stamp the RUN IDENTITY block into the report header |
| `universal/gitops-factory/convert_to_universal_chart.py` | 4003 | append the generated breakdown to the summary block |
| `universal/gitops-factory/sanitize_fidelity_report.py` | 302–318 | teach `_PREAMBLE_HINTS` / `_BREAKDOWN_ROW` about the new header |

### Design decisions inside the fix

**The run id is content-derived, not a timestamp.** A timestamp distinguishes
two runs that are identical and fails to distinguish the case that actually
matters — the same input read by a converter that changed underneath it.
`run_fingerprint` hashes the converter's own source and every input manifest, so
the id moves if and only if a count could legitimately move. An unchanged id is
therefore a real guarantee that two reports are about the same thing, which is
exactly the guarantee that was missing.

**Both halves are printed separately** (`converter:` and `input:`), because a
reader facing two differing ids has to know *which* half moved. Same converter +
different input is "more data". Different converter + same input is a code
change. One combined hash would leave the reader exactly where this defect left
them.

**The path is hashed alongside the bytes.** A manifest that only *moved* is a
different input — it can land in another namespace and change which release owns
it. Pinned by `test_run_fingerprint_changes_when_a_manifest_only_MOVES`.

**The breakdown splits field errors by the LEAF of the field path**, matching
`sanitize_fidelity_report.categorise()` rather than inventing a second
convention — the two are read side by side. This is also the direct answer to
the report's own worst category, `Labels/annotations lost | 644 | 2,928`: one
bucket hiding many defects. Under the leaf rule those become
`field_lost_team`, `field_lost_critical`, `field_lost_environment` and so on,
separately countable.

**An unrecognised ERROR shape is counted under `other_error`, never folded into
a neighbouring bucket.** A shape the parser does not know is the one most likely
to be genuinely new; absorbing it is how a novel defect becomes invisible. Same
reasoning the sanitizer already applies to unparsed lines.

**A clean run still prints the block** (`... : none`) rather than omitting it.
An absent breakdown reads as "not generated" and invites someone to write one by
hand — which is the defect.

## 3. One bug found and fixed inside my own fix

The first version of `_FIDELITY_FIELD_ERROR` matched the field path as
`[^:]+` — up to the first colon. `_dot()` renders an unnamed container port as
the synthetic `ports[#port:9125/TCP]`, so a legitimate field path can contain a
colon, and the regex truncated it to `ports[#port`. Every unnamed port would
have become its own category of one, reproducing in the breakdown the exact
unreadability the breakdown exists to remove.

Caught by `test_breakdown_strips_list_subscripts_from_the_leaf`, which failed
on the first run. Fixed by matching non-greedily up to a colon *followed by
whitespace*.

## 4. Tests created

`ERRORS_NEW_ANALYSIS/Summary_Vs_Report_Data_Mismatch/test/test_run_identity.py`
— 20 tests in 4 groups:

1. **Run identity** (4) — stable for identical input; moves when content
   changes; moves when a manifest only relocates; both halves separately
   attributable.
2. **Generated breakdown** (7) — splits by leaf not verb; counts every ERROR
   shape `verify_raw_fidelity` emits; strips list subscripts; ignores INFO and
   namespace headers; never absorbs an unrecognised shape; totals agree with the
   lines summarised; prints `none` on a clean run.
3. **Header stays machine-readable** (1) — the sanitizer parses all 15 new
   header lines as preamble, with zero unparsed and zero mistaken for records.
4. **Settling the eight** (8) — one guard test per disputed error type.

### RED — before the fix

```
$ python -m pytest test_run_identity.py -q
TypeError: ... module has no attribute 'run_fingerprint'      (x4)
AttributeError: ... no attribute 'fidelity_error_breakdown'   (x7)
...
```

`run_fingerprint` and `fidelity_error_breakdown` did not exist, so groups 1–3
could not run at all. After they existed but before the regex fix:

```
FAILED test_breakdown_strips_list_subscripts_from_the_leaf
FAILED test_breakdown_counts_every_error_shape_the_checker_emits
8 failed, 12 passed in 1.01s
```

### GREEN — after the fix

```
$ python -m pytest output-showcase/ERRORS_NEW_ANALYSIS/Summary_Vs_Report_Data_Mismatch/test/test_run_identity.py -q
....................                                                     [100%]
20 passed in 0.25s
```

## 5. Validation chain

**1. The header is produced, on a real run:**

```
$ python convert_to_universal_chart.py --input namespaces-showcase --output <tmp> \
    --chart ../Universal-chart --verify-fidelity --cluster-scoped

Round-trip fidelity report — raw input vs `helm template` of the converted output.

RUN IDENTITY — counts below describe THIS run and no other.
  run id:         c919e0fb0308-e1073701d07c
  converter:      c919e0fb0308  (convert_to_universal_chart.py)
  input:          e1073701d07c  (namespaces-showcase)
  output:         ...\f01
  Compare two fidelity reports ONLY when their run ids match. A differing
  run id means the converter, the input, or both changed — so a count that
  moved between them may be more data rather than a regression, and the two
  numbers are not evidence about each other. Do not maintain a summary of
  this report by hand; the breakdown below is generated from these same
  lines, in this same run, and is the only summary that cannot drift.
```

**2. The breakdown populates end-to-end** — the showcase is clean, so it was
run against a 3-object temporary corpus built for this check:

```
objects matched:            3
fields LOST (raw -> render): 5

ERROR BREAKDOWN BY CATEGORY (generated from the lines below):
  field_lost_allocateLoadBalancerNodePorts  1
  field_lost_critical                       1
  field_lost_environment                    1
  field_lost_minReadySeconds                1
  field_lost_team                           1
```

Worth recording that this incidentally reproduces, in live converter output,
three of the defects Phase 1 predicted from code reading — Route governance
labels (folder 3), `minReadySeconds` (folder 7) and
`allocateLoadBalancerNodePorts` (folder 9). **None of them was touched here.**
The temporary corpus lives in the scratch directory, not in the repo.

**3. Showcase fidelity unchanged:**

```
122 objects matched (28 legitimately relocated).
No fidelity issues found.
```

**4. The report file is valid UTF-8** (checked because folder 4 is about
encoding, and a header is a new write path):

```
file decodes as UTF-8 OK, bytes= 5955
em-dash U+2014 present: True
```

The em-dashes render as `ג€"` in the PowerShell console only — that is the
console's codepage, not the file. The bytes are correct.

**5. The sanitizer gained ZERO unparsed lines** — the regression that mattered
most, since 15 header lines the parser did not recognise would have raised
`lines unparsed` from 3 to 18 and quietly degraded every shareable artifact:

```
new-format report:        unparsed: 3
committed (old) report:   unparsed: 3

both, identically:
  '         renamed on collision) — expected, not a loss.'
  '         source, or a field the conversion LOST or CHANGED.'
  'Fields the render ADDED are counted but not listed: a Helm chart filling in a'
```

Same three lines, byte-for-byte. They are pre-existing and are precisely what
folder 26 (`Sanitizer_Drops_Unparsed_Lines`) is about — identified here as a
by-product, **not acted on**.

**6. Sanitizer's own regression suite:**

```
$ python -m pytest test_sanitize_fidelity_report.py -q
41 passed in 2.12s
```

**7. Chart's own suite** (untouched by this folder, run as the standing gate):

```
$ helm unittest Universal-chart
Test Suites: 28 passed, 28 total
Tests:       234 passed, 234 total
```

## 6. The eight disputed error types — settled

`error.txt` gives two readings and says "the difference matters enormously":

> 1. THE SUMMARY IS OLDER, describing a pre-fix run. Then those 8 error types
>    were genuinely FIXED between runs [...]
> 2. THE SUMMARY IS FROM A DIFFERENT INPUT SET [...] they are still live.

**Reading 1 is correct.** Two independent lines of evidence:

- **The report was produced by the current converter.** `_INDEX.txt` cites
  `_fidelity_ignored_path()` line 2953, `ports_list_to_map()` line 645,
  `_fidelity_diff()` line 3055 and `handle_main_container()` port handling line
  835. All four landed exactly on the named functions in the pre-change working
  tree. (They have since shifted, because this folder added code above them —
  which is itself why the run id now exists.)
- **Each of the eight is captured by code today**, one test each:

| # | Error type | Test | Verdict |
|---|---|---|---|
| 1 | `imagePullSecrets` lost (24) | `test_eight_1_...` | captured |
| 2 | ServiceAccount `secrets` lost (24) | `test_eight_2_...` | **resolved by decision**, see below |
| 3 | `containers[0].resources` lost (41) | `test_eight_3_...` | captured |
| 4 | `progressDeadlineSeconds` lost (68) | `test_eight_4_...` | captured |
| 5 | `securityContext` lost (66) | `test_eight_5_...` | captured |
| 6 | `sessionAffinity` lost (44) | `test_eight_6_...` | captured |
| 7 | Job/CronJob misc config (60) | `test_eight_7_...` | captured |
| 8 | `volumeMounts` subPath lost (4) | `test_eight_8_...` | captured |

**#2 is not a "capture" and my first test asserted the wrong thing.** I wrote it
expecting `secrets:` to be carried; it is deliberately not.
`ERRORS_ANALYSIS/ServiceAccount_Secrets_And_ImagePullSecrets_Lost` split the
report's single row in two: a hand-added registry credential must survive
(without it every pod on that SA fails ImagePullBackOff), while `secrets:` and
OpenShift's auto-stamped `<sa>-dockercfg-XXXXX` must not — Kubernetes creates
and rotates those itself, and on a vanilla cluster the referenced Secret never
exists. This round's `K8s_Runtime_Defaults_Benign_Suppress` independently
reaches the same verdict. The test now pins that contract instead.

**One classification correction, as `Carryover_Defects_Still_Present` requests:**
`serviceAccount (deprecated) lost` is **29, not 0**. The summary looked for the
wrong path. The previous round's severity rating (NONE) is unchanged; only the
count was wrong.

## 7. What could NOT be verified

- **The `rakia-*` counts themselves.** Those manifests are not in this repo.
  The run-identity mechanism is proven to work; it cannot retroactively stamp an
  id on a report generated before it existed. The 1,314-line report will always
  be unattributable — that is why this folder is Tier 0 and why the *next*
  report will not be.
- **That the eight are fixed for the `rakia-*` input specifically.** The tests
  prove the code paths capture the fields. Whether those particular manifests
  exercise a further edge case cannot be shown without them.
- **Byte-stability of the report across runs.** It is now deliberately *not*
  byte-stable across different inputs or converter versions — that is the
  feature. It remains byte-stable for a fixed converter + fixed input, which is
  the property the previous round's `Compare-Object` checks actually relied on.

## 8. OBSERVED, DEFERRED

Noticed while working; code left untouched, belongs to other folders:

- **`Route_Governance_Labels_Lost` (folder 3)** — confirmed live in converter
  output: `field_lost_team`, `field_lost_critical`, `field_lost_environment` on
  a Route, in §5 step 2. `build_route_entry` carries the comment *"labels
  handled at the end of this function via raw_labels(doc)"* and never calls it.
  Not touched.
- **`Deployment_MinReadySeconds_Lost` (folder 7)** — confirmed live,
  `field_lost_minReadySeconds  1`. Not touched.
- **`Service_AllocateLoadBalancerNodePorts_Lost` (folder 9)** — confirmed live,
  `field_lost_allocateLoadBalancerNodePorts  1`. Not touched.
- **`Sanitizer_Drops_Unparsed_Lines` (folder 26)** — the three unparsed lines
  are now identified concretely (§5 step 5). All three are wrapped continuation
  lines of the report's own legend, i.e. benign, but the parser gap is real.
  Not touched.

## 9. Reproducing

```powershell
python -m pytest universal/gitops-factory/output-showcase/ERRORS_NEW_ANALYSIS/Summary_Vs_Report_Data_Mismatch/test/test_run_identity.py -v

cd universal/gitops-factory
python convert_to_universal_chart.py --input namespaces-showcase --output <tmp> `
  --chart ../Universal-chart --verify-fidelity --cluster-scoped
Get-Content <tmp>/report/fidelity_report.txt -TotalCount 20
```
