# STATUS — Extra_Objects_Rendered_With_No_Raw_Source (P1, folder 9/35)

**Result: ALREADY FIXED by folder 7.** No further code change. The count went
**3 → 0** on a reproduction fixture, in lockstep with folder 8.

**This folder's error.txt was right about itself.** It states plainly:

> *"This is NOT an independent bug ... the same rename is counted twice — once as
> a raw object that disappeared, once as a rendered object that appeared from
> nowhere. Fixing the naming defect should eliminate BOTH counts simultaneously."*

That prediction was tested rather than assumed, and it held exactly.

---

## 1. Evidence (shared with folder 8)

The full experiment, the object-by-object pairing table, the pre-fix/post-fix
counts and the test suite are in
[`../Missing_Objects_Not_Rendered_By_Any_Release/STATUS.md`](../Missing_Objects_Not_Rendered_By_Any_Release/STATUS.md).

Tests live in that folder too —
`Missing_Objects_Not_Rendered_By_Any_Release/test/test_missing_and_extra_objects.py`
— deliberately in **one** module covering both folders, so the two halves of a
single defect cannot drift apart in future work. Duplicating them here would
invite exactly the divergence the brief warned about.

Summary of the measurement:

```
BEFORE (git HEAD converter + chart, folder-7 fixture)
  objects in raw but rendered by nothing:      3
  objects rendered with no raw source:         3

    Route/control-station-client      rendered by 'control-station-client' but present in NO raw manifest
    Service/control-station-client    rendered by 'control-station-client' but present in NO raw manifest
    Service/storefront                rendered by 'storefront' but present in NO raw manifest

AFTER (current code)
  objects in raw but rendered by nothing:      0
  objects rendered with no raw source:         0
```

Each "extra" object is the fullname-renamed twin of a "missing" one:

| rendered from nothing | was really this raw object |
|---|---|
| `Route/control-station-client` | `Route/control-station-client-route` |
| `Service/control-station-client` | `Service/control-station-client-service` |
| `Service/storefront` | `Service/storefront-service` |

**Both counts moved together.** Per the brief's test — *"if only one count drops,
they were not the same objects"* — they are confirmed to be the same objects.
`test_the_two_counts_are_the_same_objects` keeps asserting `missing == extra` so
any future divergence fails loudly.

## 2. Files changed

**None.** The fix is folder 7's: `service.yaml` and `route.yaml` now render the
raw object names (`.Values.service.name` / `.Values.route.name`), so the chart no
longer invents release-named objects that match nothing in the cluster.

## 3. On the raw-vs-rendered object surplus

error.txt cites the header lines as evidence of the surplus:

```
=== interconn — 208 raw objects vs 238 rendered ===
=== interconn-opr — 133 raw objects vs 146 rendered ===
```

A surplus is **not** by itself a defect — the chart legitimately creates objects
with no raw source, and the fidelity report classifies those as `INFO`, not
`ERROR`. The showcase shows exactly this today:

```
=== background-jobs — 3 raw objects vs 5 rendered ===
  INFO   ServiceAccount/db-migrate: created by the chart for release 'db-migrate'
         (serviceAccount.create) — no raw source expected
```

5 rendered from 3 raw, and zero errors. What made interconn's surplus a defect
was that the extra objects were *renamed duplicates* of raw objects the chart had
simultaneously failed to render — which is precisely why the two counts were
equal. The count to watch is `objects rendered with no raw source`, not the
header arithmetic.

## 4. Validation chain

| Step | Result |
|---|---|
| Converter re-run (showcase) | `122 objects matched (28 relocated). No fidelity issues found.` |
| Fidelity before/after | unchanged: extra 0 |
| Fixture, pre-fix vs post-fix | **3 → 0**, together with folder 8 |
| `helm lint` | 0 failed |
| Chart's own suite | 28 suites, 234 tests passed |
| Accumulated suite | **78 passed** |

## 5. What could NOT be verified without a live cluster

- **That the ~28 real objects are gone.** Those manifests are not in this repo.
- **That no orphan is left behind on an existing cluster.** Objects already
  created under the old fullname will not be renamed by this change; with
  `prune: false` they persist. Cleaning them up is a migration step, and the
  rendered output cannot reveal which ones exist.

## 6. OBSERVED, DEFERRED

- Folder 8's §6 records a **separate P0 found during this verification**
  (`_FOUND_Global_Defaults_Subtracted_But_Never_Written`) — global defaults
  subtracted but never written. Unrelated to this folder, fixed and documented
  separately.
- Folders 1–7 deferred items still stand.

## 7. Reproducing

```powershell
python -m pytest universal/gitops-factory/output-showcase/ERRORS_ANALYSIS/Missing_Objects_Not_Rendered_By_Any_Release/test/test_missing_and_extra_objects.py -v
```
