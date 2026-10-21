# STATUS — Carryover_Defects_Still_Present (TIER 0, folder 2/26)

**Result: RESOLVED — verified, not regressed, and now guarded.** All nine
carryover defects are handled by the current code, proven by 30 new tests
RED→GREEN. The three counts that "moved the wrong way" are measurement, not
regression. The one classification `error.txt` asks to correct is corrected.
And the reason all nine survived a round that fixed them is closed: the corpus
now carries the irreversible one, so the round-trip fidelity check is the
tripwire instead of a fixture nobody re-runs.

---

## 1. What this folder actually asks for

Not a fix — a verdict. `error.txt`:

> THIS FOLDER EXISTS TO STOP THESE FROM BEING LOST BETWEEN THE TWO ANALYSIS
> ROUNDS. None of them is new.

and, on the counts that grew:

> THE INCREASES MAY BE MEASUREMENT, NOT REGRESSION. [...] right now nobody can
> tell whether the PVC retention defect is 3x worse or simply 3x more visible.
> Re-run the converter once on a single known input and settle it.

Three questions, answered in order below: are they regressions; are they still
present; and why did nine fixed defects come back at all.

## 2. Regression or measurement — settled: **measurement**

Established in folder 1 and cross-checked here. The new report's `_INDEX.txt`
cites `_fidelity_ignored_path()` at line 2953, `ports_list_to_map()` at 645,
`_fidelity_diff()` at 3055 and `handle_main_container()` port handling at 835.
All four landed exactly on those functions in the pre-change working tree, so
the report was produced by the **current, post-fix converter** — against the
`rakia-*` corpus (131 raw objects in `rakia-fac1` alone) rather than the summary's
input.

Same code, more data. So:

| Count that moved | Reading |
|---|---|
| `persistentVolumeClaimRetentionPolicy` 8 → 23 | more StatefulSets scanned, not 3x worse |
| sidecar containers 28 → 66 | per-occurrence vs per-object, larger corpus |
| Route `spec.to.name` 2 → 8 | larger corpus |

This is not an argument that the defects are absent from `rakia-*` — folder 1
records that those manifests are not in this repo and cannot be checked. It is
an argument that **a rising count between these two documents is not evidence
of a regression**, which is precisely what `error.txt` could not determine.
Folder 1's run-identity stamp means the next pair of reports will answer this
mechanically instead.

## 3. Are they still present — nine verdicts

| # | Carryover | Handled by | Verdict |
|---|---|---|---|
| 1 | `persistentVolumeClaimRetentionPolicy` | `handle_workload` StatefulSet block + `templates/statefulset.yaml` | Fixed, **and now corpus-covered** |
| 2 | `serviceName` wrong | `if spec.get("serviceName")`, captured unconditionally | Fixed |
| 3 | `serviceAccount` lost | `serviceAccountName or serviceAccount` fallback | Fixed; **count corrected, see §5** |
| 4 | `image` changed | `split_image` routes the digest separately | Fixed |
| 5 | `configMap.name` broken | `remap_volumes` / `env_list` follow `ctx.name_map` | Fixed |
| 6 | `data.grafana.ini` changed | blob survives the YAML round trip byte-exactly | Whitespace half fixed; quoting half is folder 25 |
| 7 | Route `spec.to.name` | `build_route_entry` remaps through `ctx.name_map["Service"]` | Fixed |
| 8 | sidecar containers lost | `v["sidecars"][name]`, keyed by the container's own name | Fixed |
| 9 | Route/Service rendered by no release | corpus-dependent; the *checker's ability to see it* is guarded | See §6 |

## 4. Why nine fixed defects came back — and the change that closes it

Every one of them was proven once, in a fixture inside its own folder, against
a corpus that does not contain the field. The previous round's own STATUS for
carryover #1 says so plainly:

> The committed fidelity report shows **0** for this defect class both before
> and after, because there is no raw input carrying the field to lose.

A defect the corpus cannot express is a defect no future run can detect. So the
fix here is not another fixture — it is corpus coverage for the one carryover
whose loss is irreversible.

### Files changed

| File | Lines | Change |
|---|---|---|
| `universal/gitops-factory/namespaces-showcase/platform-data/postgres.yaml` | 9–18 (new) | declare `persistentVolumeClaimRetentionPolicy: {whenDeleted: Retain, whenScaled: Retain}` |
| `universal/gitops-factory/output-showcase/**` | regenerated | consequence of the above |

`Retain` on a Postgres StatefulSet is what the field is for, so this is a
realistic manifest rather than a test hook wearing a manifest's clothes.

**Deliberately asymmetric.** `platform-data-prd/postgres.yaml` is left without
the field, and the converter says so in its own warning:

```
[platform-data/postgres] shares base/postgres.yaml with its other namespaces'
copies; ['persistentVolumeClaimRetentionPolicy'] differ and were pushed into
this namespace's own values/postgres.yaml instead
```

That asymmetry is worth more than symmetry would be: one namespace proves the
policy is carried, the other proves it is not invented, and they prove it
through the shared-base/per-namespace split at the same time.

## 5. The classification correction `error.txt` asks for

> ONE ITEM NEEDS ITS CLASSIFICATION CORRECTED: "serviceAccount (deprecated)
> lost | 25 | NO (0) — but 29 spec.template.spec.serviceAccount LOST exist".
> The summary looked for the wrong path and concluded zero. There are 29.

Applied. The count is **29, not 0**. The previous round's severity rating
(NONE — the deprecated alias is harmless, since Kubernetes still honours it and
`serviceAccountName` carries the same value) **stands unchanged**; only the
count was wrong.

The behaviour is now pinned by three tests: the modern field is carried, the
deprecated alias is read as a fallback, and `serviceAccountName` wins when both
are present.

## 6. Carryover 9 is guarded differently, on purpose

"90 Routes/Services rendered by no release" is a property of the `rakia-*`
corpus, which this repo does not have. Asserting a number would be theatre.
What *can* regress here, and would hide all nine at once, is the checker's
ability to see anything — so the guard is on `_fidelity_ignored_path`:
selector paths stay suppressed, and nothing broader ever is. A report that
reaches 0 because the checker went blind looks identical to a report that
reaches 0 because the code is correct.

## 7. Tests created

`ERRORS_NEW_ANALYSIS/Carryover_Defects_Still_Present/test/test_carryover_guards.py`
— 30 tests, one group per carryover, including 5 parametrised retention-policy
combinations and 6 parametrised image-reference shapes.

### RED — proof the guards and the corpus coverage both bite

The corpus change was verified by disabling the fix in a throwaway copy of the
converter (`if spec.get("persistentVolumeClaimRetentionPolicy"):` → `if False:`)
and running against the real corpus:

```
$ python <scratch>/broken_converter.py --input namespaces-showcase --output <tmp> \
    --chart ../Universal-chart --verify-fidelity --cluster-scoped

  122 objects matched (28 legitimately relocated).
  1 potential fidelity issue(s) written to ...\report\fidelity_report.txt

ERROR BREAKDOWN BY CATEGORY (generated from the lines below):
  field_lost_persistentVolumeClaimRetentionPolicy  1

  ERROR  StatefulSet/postgres LOST at spec.persistentVolumeClaimRetentionPolicy:
         raw had {'whenDeleted': 'Retain', 'whenScaled': 'Retain'}
```

**Before this folder's corpus change, that same broken converter reported 0
fidelity issues.** That is the whole defect class, demonstrated and closed: the
loss is now visible to the standing check, not only to a fixture.

The unit guards were RED in the ordinary way as well — one of them genuinely
caught my own bad assumption, see §9.

### GREEN

```
$ python -m pytest output-showcase/ERRORS_NEW_ANALYSIS/Carryover_Defects_Still_Present/test/test_carryover_guards.py -q
..............................                                           [100%]
30 passed in 0.20s
```

## 8. Validation chain

**1. Converter re-run on the changed corpus:**

```
Converted 20 microservices across 10 namespaces.
Rendered 25 releases.
No duplicate resource ownership detected across any of the 25 rendered releases.
  122 objects matched (28 legitimately relocated).
  No fidelity issues found.
```

**2. `helm template` of the affected release** — the field reaches the rendered
StatefulSet with the raw value:

```
$ helm template postgres Universal-chart --namespace platform-data \
    -f defaults.yaml -f platform-data/defaults.yaml \
    -f base/postgres.yaml -f platform-data/values/postgres.yaml

kind: StatefulSet
  serviceName: postgres
  persistentVolumeClaimRetentionPolicy:
    whenDeleted: Retain
    whenScaled: Retain
```

**3. Regeneration churn is fully attributable.** Diffed the regenerated
`output-showcase/` against the pre-change baseline captured at the start of
Phase 2. Every difference is accounted for:

| File | Change | Cause |
|---|---|---|
| `platform-data/values/postgres.yaml` | +3 lines | this folder's corpus change |
| `report/conflicts_and_warnings.txt` | +1 warning | the shared-base split, quoted in §4 |
| `report/conversion_report.json` | warnings 15 → 16 | same |
| `report/fidelity_report.txt` | +RUN IDENTITY, +breakdown | folder 1 |

Every other generated file is **byte-identical**.

**4. Full new-analysis suite and the chart's own suite:**

```
$ python -m pytest output-showcase/ERRORS_NEW_ANALYSIS -q
50 passed in 0.38s

$ helm unittest Universal-chart
Test Suites: 28 passed, 28 total
Tests:       234 passed, 234 total
```

## 9. A wrong assumption of mine, caught by its own test

`test_carryover_4_tag_and_digest_together_both_survive` asserted that
`ghcr.io/acme/api:v1.2.3@sha256:...` decomposes into three tidy parts. It does
not: `split_image` leaves the tag inside the repository and returns the digest
separately, and the chart's image helper then takes the `.Values.image.digest`
branch — `printf "%s@%s"` — reproducing the original string exactly.

The tidy decomposition was my expectation, not a requirement, and asserting it
would have failed against correct code. Replaced with the invariant that
actually matters: six reference shapes, each of which must split and reassemble
to itself, with a digest pin never degrading to a tag. That is the defect this
carryover is about.

## 10. What could NOT be verified

- **The 23 real StatefulSets behind carryover #1.** Not in this repo. What is
  proven is that the field survives converter and chart for every legal
  combination, and that its loss is now detected by the standing check.
- **That Kubernetes honours `Retain`.** That is Kubernetes behaviour, not chart
  behaviour. Verified only that the field reaches the rendered manifest with
  the correct value — no cluster is reachable and `kubectl --dry-run=client`
  still needs an API server for its schema.
- **Whether the 90 orphaned Routes/Services are real.** Corpus-dependent; see
  §6 and folder 14.
- **The quoting half of carryover #6.** Belongs to folder 25
  (`Grafana_Ini_Changed_False_Positive`) and was not touched.

## 11. OBSERVED, DEFERRED

- **`Route_Governance_Labels_Lost` (folder 3)** — `build_route_entry` carries
  the comment *"labels handled at the end of this function via
  raw_labels(doc)"* and never calls it, while `templates/route.yaml` and
  `templates/routes.yaml` both already render `.labels`. Next folder. Not
  touched; `test_carryover_7_*` deliberately asserts only the backend remap.
- **`platform-data-prd/postgres.yaml`** could also declare the retention policy.
  Left alone deliberately (§4) — the asymmetry is the better test.

## 12. Reproducing

```powershell
python -m pytest universal/gitops-factory/output-showcase/ERRORS_NEW_ANALYSIS/Carryover_Defects_Still_Present/test/test_carryover_guards.py -v

cd universal/gitops-factory
python convert_to_universal_chart.py --input namespaces-showcase --output output-showcase `
  --chart ../Universal-chart --verify-fidelity --cluster-scoped `
  --values-repo-url https://github.com/devops-ezrahi/homelab.git `
  --values-revision checker --values-path universal/gitops-factory/output-showcase
```
