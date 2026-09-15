# STATUS — Extra_And_Relocated_Objects (TIER 2, folder 14/26)

**Result: RECONCILED — and a regression I introduced two folders ago, found and
fixed.** The 0-vs-90 contradiction is a two-runs artefact, already made
impossible by folder 1's run id. The durable deliverable is the test `error.txt`
specifies: "rendered by NO release" is now a conclusion reached by searching for
the object under another name, not a claim. That search also caught folder 10
breaking the checker's rename mirror. 11 new tests RED→GREEN, showcase fidelity
0 lost / 0 changed, chart's 234 green.

---

## 1. The contradiction, reconciled

`error.txt`:

> One table shows "Objects rendered by nothing: 0" and "Objects w/ no raw
> source: 0" [...] The cross-check table shows "Route/Service rendered by NO
> release | 90 | 90 | Exact". **A count of 0 and a count of 90 for the same
> phenomenon cannot both describe the current run.**

They do not. They describe **different runs** — the 0 is a post-fix run on a
small corpus, the 90 is `rakia-*`. This is the same root cause as folders 1 and
2 (RC-5), and it is now structurally prevented: every fidelity report carries a
content-derived `run id`, so two reports can state whether they are comparable
before anyone compares them.

The counts themselves cannot be re-derived — `rakia-*` is not in this repo — and
that is exactly why the mechanism matters more than the number.

## 2. The 16 extra + 15 relocated

`error.txt` reads these as benign and gives the reason:

> An object that moved into the shared release, or that was renamed by the
> collision resolver, legitimately appears to have "no raw source" under its new
> name while its old name appears to be "rendered by nothing". **These are two
> halves of one rename, double-counted.**

That double-counting is now impossible rather than merely explained: matching
the raw side by content also marks the rendered object as matched, so it can no
longer be reported as extra. One rename produces one INFO line, not one ERROR
plus one ERROR.

## 3. What was built — the test error.txt asked for

> **THE TEST THAT SEPARATES THEM:** for each object rendered by nothing, search
> the rendered output for an object of the same KIND with matching content under
> a different name. If found, it is a rename and benign. If not found, it is
> genuinely lost and is a P0.

Implemented in `verify_raw_fidelity`, in two places:

**A. Before declaring an object lost.** If no rendered object carries the name,
scan the namespace for one of the same kind with matching content. Found →
INFO, naming the new name and telling the reader to check for outside
references. Not found → ERROR, and the message now says *why* it is a
conclusion:

```
ERROR  ConfigMap/dex-config (from dex.yaml): rendered by NO release, and no
       object of this kind in the namespace has matching content under any
       other name — genuinely lost, not renamed
```

**B. When the raw name is ambiguous.** When several raw files declare the same
(kind, name) — a collision — the plain name lookup is ambiguous *by
construction*: it returns whichever rendered object kept the name, which for
every declarer but one is somebody else's object. Content matching is used
there instead.

**`_identity_free_hash()`** makes both possible: `content_hash` deliberately
keeps `metadata.name` (it answers "are these two objects interchangeable", which
the collision registry depends on), so it can never match a renamed object. The
new helper drops the name and reuses everything else. It is a separate function
rather than a flag, and `test_content_hash_itself_is_unchanged_and_still_name_sensitive`
pins that the original behaviour is untouched.

## 4. The regression this caught — mine, from folder 10

Building the probe corpus surfaced a failure that had nothing to do with the
corpus:

```
INFO   ConfigMap/shared-conf (from shared-conf.yaml): relocated to release 'alpha' (expected)
ERROR  ConfigMap/shared-conf CHANGED at data.OWNER: raw='shared-conf' render='alpha'
ERROR  ConfigMap/shared-conf-2: rendered by 'shared-conf' but present in NO raw manifest
```

One correctly-converted object, reported as a content mismatch **and** a ghost.

The checker mirrors the converter's rename rule to pair a renamed object with
its raw source, and says so in a comment: *"Applying the converter's own rename
rule rather than scanning for any name ending in '-<name>'."* It reconstructs
`<owner>-<name>`. **Folder 10 changed that rule** — the idempotence guard now
emits `<name>-2` where a prefix would be redundant — so the mirror went stale
the moment I shipped it, and the showcase did not catch it because it contained
no collision of that shape.

That is the general hazard with a mirrored rule: it has to be updated in two
places, and nothing enforces it. Content matching needs no mirror, which is why
it is the right answer here and not merely the requested one.

After the fix, the same corpus:

```
INFO   ConfigMap/shared-conf (from shared-conf.yaml): rendered as 'shared-conf-2'
       by release 'shared-conf' (expected — collision rename or the chart's naming for this kind)

  4 objects matched (3 legitimately relocated).
  No fidelity issues found.
```

## 5. Files changed

| File | Lines | Change |
|---|---|---|
| `universal/gitops-factory/convert_to_universal_chart.py` | ~3251–3269 (new) | `_identity_free_hash()` |
| `universal/gitops-factory/convert_to_universal_chart.py` | ~3700–3722 (new) | content matching when the raw name is ambiguous |
| `universal/gitops-factory/convert_to_universal_chart.py` | ~3723–3752 (new) | content search before declaring an object lost |

No corpus change: the probe corpus that exposed the regression lives in the
scratch directory. Adding a second collision to `namespaces-showcase` would have
duplicated folder 12's without testing anything folder 12 does not — and the
`-2` shape it needs requires the contrived "microservice named after its own
object" manifest that folder 10 explains was deliberately kept out.

## 6. RED → GREEN, both branches

| Branch | Evidence |
|---|---|
| rename found by content | probe corpus: `shared-conf` → `shared-conf-2`, INFO, 0 issues |
| genuinely lost | `handle_configmap` stubbed to drop every ConfigMap → 39 issues, each reading *"genuinely lost, not renamed"* |
| stale mirror (the regression) | pre-fix probe: phantom CHANGED + phantom extra object |

## 7. Tests created

11 tests in 4 groups:

1. **The identity-free hash** (5) — same object under a different name hashes
   equal; different content does not; chart labels and annotations do not defeat
   the match; `content_hash` itself stays name-sensitive; a missing metadata
   block does not crash it.
2. **The reconciliation** (2) — extra-and-relocated are two halves of one
   rename; a genuinely lost object has no twin.
3. **The stale mirror** (2) — the collision resolver really does emit a shape
   the name rules do not predict, and the search excludes already-matched
   objects so two raw copies cannot claim one render.
4. **Scope** (2) — the search is scoped to the same kind (a ConfigMap must never
   be "found" as a Secret with identical data), and content matching only runs
   where the name is ambiguous.

Group 4 is the honesty group. "Matched by content" is a way of *not* reporting
something, so the tests that matter most are the ones bounding when it can fire.

## 8. Validation chain

| Check | Result |
|---|---|
| Probe corpus, pre-fix | phantom CHANGED + phantom extra object |
| Probe corpus, post-fix | 1 INFO, **no fidelity issues** |
| RED (ConfigMaps dropped) | 39 errors, all *"genuinely lost, not renamed"* |
| Converter re-run, showcase | 128 objects matched, **no fidelity issues** |
| Folder's own tests | 11 passed |
| All `ERRORS_NEW_ANALYSIS` tests | 227 passed |
| `sanitize_fidelity_report` suite | 41 passed |
| `helm unittest Universal-chart` | 234 passed, 28 suites |

## 9. What could NOT be verified

- **The 90 orphaned Routes/Services.** Not in this repo. What now exists is the
  machinery to classify them: the next run over that input will say, per object,
  whether it was renamed or genuinely lost, instead of producing a count that
  two documents disagree about.
- **"131 raw objects vs 162 rendered".** `error.txt` asks to reconcile the
  per-namespace object counts. That needs the input; the showcase equivalent
  reconciles exactly (128 matched, 0 missing, 0 extra).
- **Whether anything outside the releases referenced an object by its old name.**
  Reported as a caution in the INFO message, and it is genuinely outside what
  this converter can see. Same limit recorded in folder 12.

## 10. OBSERVED, DEFERRED

- **The content search is O(rendered) per ambiguous raw object.** Negligible at
  this corpus size and bounded to the ambiguous path, but worth knowing before
  running against 131-object namespaces.
- **`Sanitizer_Drops_Unparsed_Lines` (folder 26)** — the new INFO wording is
  longer than the old; the sanitizer parses it as a record without complaint
  (41 tests still green, unparsed count unchanged), but that is worth
  re-checking in folder 26 rather than assumed.

## 11. Reproducing

```powershell
python -m pytest universal/gitops-factory/output-showcase/ERRORS_NEW_ANALYSIS/Extra_And_Relocated_Objects/test/test_object_reconciliation.py -v

# the two-microservice collision that produces a `-2` rename
python convert_to_universal_chart.py --input <scratch>/f14probe --output <tmp> `
  --chart ../Universal-chart --verify-fidelity --cluster-scoped
```
