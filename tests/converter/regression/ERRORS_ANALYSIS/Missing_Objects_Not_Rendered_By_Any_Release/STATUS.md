# STATUS — Missing_Objects_Not_Rendered_By_Any_Release (P1, folder 8/35)

**Result: ALREADY FIXED by folder 7.** No further code change. Verified by
reproducing the defect against the pre-fix code and watching the count go
**3 → 0**, in lockstep with folder 9.

Tests for folders 8 and 9 live together in this folder — see §2.

---

## 1. The verification the brief asked for

> *"verify 8 and 9 cancel out; if only one count drops, they were not the same
> objects"*

I built the pre-fix tree (`git show HEAD:` for the converter and every chart file
I had touched) and ran both versions over the **same** fixture — folder 7's
`-service` / `-route` suffixed manifests.

### BEFORE (git HEAD converter + chart)

```
objects in raw but rendered by nothing:      3
objects rendered with no raw source:         3

  ERROR  Route/control-station-client-route (from control-station-client.yaml): rendered by NO release
  ERROR  Service/control-station-client-service (from control-station-client.yaml): rendered by NO release
  ERROR  Service/storefront-service (from storefront.yaml): rendered by NO release
  ERROR  Route/control-station-client: rendered by 'control-station-client' but present in NO raw manifest
  ERROR  Service/control-station-client: rendered by 'control-station-client' but present in NO raw manifest
  ERROR  Service/storefront: rendered by 'storefront' but present in NO raw manifest
```

The pairing is exact and visible object-by-object:

| raw object (counted MISSING) | rendered object (counted EXTRA) |
|---|---|
| `Route/control-station-client-route` | `Route/control-station-client` |
| `Service/control-station-client-service` | `Service/control-station-client` |
| `Service/storefront-service` | `Service/storefront` |

### AFTER (current code)

```
objects in raw but rendered by nothing:      0
objects rendered with no raw source:         0
```

**Both counts dropped together, 3 → 0.** They were the same three renames counted
twice, exactly as folder 9's error.txt predicted. Confirmed.

## 2. Files changed

**None.** Folder 7 (`Route_And_Service_Names_Replaced_By_Fullname`) is the fix:
the primary Service and Route now render under their raw names, so there is
nothing left to orphan and nothing left to invent.

Tests added under `Missing_Objects_Not_Rendered_By_Any_Release/test/`:

- `test_missing_and_extra_objects.py` — 4 tests covering **both** folders,
  deliberately in one module so the two can never drift apart. It reuses folder
  7's fixture rather than copying it, for the same reason.

## 3. Tests

| test | asserts |
|---|---|
| `test_suffixed_names_produce_no_missing_and_no_extra_objects` | both counts are 0 on the suffixed-name fixture |
| `test_the_two_counts_are_the_same_objects` | `missing == extra` — the brief's explicit question, as a standing assertion |
| `test_showcase_has_no_orphaned_or_invented_objects` | the real showcase stays at 0 for missing, extra, lost and changed |
| `test_over_length_names_are_reported_not_silently_skipped` | error.txt's second sub-cause (§4) |

```
4 passed in 17.98s
```

These are written as end-state assertions, so they double as regression cover for
folder 7 from the opposite direction: reintroduce the rename and both counts
return.

## 4. error.txt's SECOND sub-cause — different answer

error.txt bundles two causes under one count:

1. **Secondary Routes/Services never extracted** — the 33-ish objects. That is
   the rename, fixed in folder 7 and verified above.
2. **"1 Deployment + 1 PVC from the skipped long-named release"** —
   *"one release whose name exceeds 53 characters (prometheus-thanos-...store)
   that was SKIPPED entirely, taking its Deployment and PVC with it"*.

Sub-cause 2 **does not exist in this converter**. There is no silent skip. Names
are validated at conversion time (`validate_resource_names`, line 2423,
`K8S_NAME_MAX = 63` / `CRONJOB_NAME_MAX = 52`) and an over-length name **aborts
the whole run before anything is written**:

```
N resource name(s) Kubernetes will not accept — nothing was written:
  - ...
Shorten them in the source manifests and re-run.
```

`test_over_length_names_are_reported_not_silently_skipped` pins that loud
behaviour, so a regression to silent-skip would fail. This matches the repo's own
history (`02a11ab converter: reject over-length resource names at conversion,
not at apply`) — the sub-cause was fixed before this campaign started.

## 5. Validation chain

| Step | Result |
|---|---|
| Converter re-run (showcase) | `122 objects matched (28 relocated). No fidelity issues found.` |
| Fidelity before/after | unchanged: missing 0, extra 0 |
| Fixture, pre-fix vs post-fix | **3 → 0 missing, 3 → 0 extra** (§1) |
| `helm lint` | 0 failed |
| Chart's own suite | 28 suites, 234 tests passed |
| Accumulated suite | **78 passed** |

## 6. A serious defect found while doing this

Verifying folders 8/9 surfaced an unrelated P0 that belongs to **no** folder:
`global_defaults` was subtracted from every microservice's values but written
only in cluster-scoped mode, so in the default namespace-scoped mode every value
common to all microservices was silently deleted from the output.

Written up in full, with its own tests, at
`ERRORS_ANALYSIS/_FOUND_Global_Defaults_Subtracted_But_Never_Written/STATUS.md`.
Strong candidate for one of the two missing report issues (28/29).

It surfaced here because the post-fix fidelity run on the two-microservice
fixture reported `fields LOST: 2, fields CHANGED: 4` — errors that had previously
been **masked**: while the objects did not match by name at all, the checker
never got as far as comparing their fields. Fixing the naming defect is what made
the field-level losses visible.

## 7. What could NOT be verified without a live cluster

- **That the 36 real objects are now managed.** Those manifests
  (`Route/tyk-gavish-next`, `Service/redis-exp-vip-keepalived-clone`, …) are not
  in this repo. The mechanism is fixed and covered; those specific objects are
  unverified.
- **What happens to objects already deployed under the old (fullname) names.**
  With `prune: false` on the generated ApplicationSets, a previously-deployed
  `control-station-client` Service will linger alongside the newly-correct
  `control-station-client-service`. That is a migration concern no rendered
  manifest can show, and it is worth planning for explicitly.

## 8. OBSERVED, DEFERRED

- Any script that renders releases must special-case the namespace's `shared`
  release (it skips the `defaults.yaml` layers). Getting this wrong manufactures
  phantom "extra" objects — see folder 7 STATUS §6. The tests here avoid it by
  using the converter's own `--verify-fidelity` rather than re-implementing the
  layering.
- Folders 1–7 deferred items still stand.

## 9. Reproducing

```powershell
python -m pytest universal/gitops-factory/output-showcase/ERRORS_ANALYSIS/Missing_Objects_Not_Rendered_By_Any_Release/test/test_missing_and_extra_objects.py -v
```
