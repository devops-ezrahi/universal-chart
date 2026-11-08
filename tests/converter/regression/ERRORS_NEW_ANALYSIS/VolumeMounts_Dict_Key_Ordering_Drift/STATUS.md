# STATUS — VolumeMounts_Dict_Key_Ordering_Drift (TIER 2, folder 13/26)

**Result: FIXED in the checker — and the report's stated root cause was wrong.**
The checker was already order-insensitive for both things the report names. The
real trigger is narrower: volumeMounts sharing a name and differing only by
`subPath`, where `_by_name` bailed to positional comparison and a pure reorder
read as a wholesale CHANGED. Third instance in this batch of converter and
checker disagreeing about object identity. 13 new tests RED→GREEN, first
`subPath` coverage in the corpus, showcase fidelity 0 lost / 0 changed, chart's
234 green.

---

## 1. The decision

`error.txt` offers two options and recommends one:

> **A. SUPPRESS / FIX THE COMPARISON** — teach the fidelity checker to compare
> mappings order-insensitively. **RECOMMENDED.** If the checker is currently
> doing a string or ordered comparison on dicts, that is itself the defect, and
> fixing it silences an entire class of future false positives rather than this
> one instance.
> B. NORMALIZE THE OUTPUT — [...] treats the symptom.

**Option A**, and the recommendation was right. The diagnosis attached to it was
not.

## 2. The report's stated cause is not what was happening

Measured directly against `_fidelity_diff`:

```
dict key order differs            -> NONE
list entry order differs          -> NONE
duplicate names, reordered        -> CHANGED (whole list dumped)
GENUINE change (mountPath)        -> CHANGED at ...[0].mountPath
GENUINE loss (mount dropped)      -> LOST at ...volumeMounts[b]
```

The report says: *"Root cause: Dictionary serialization ordering — Helm/YAML
outputs sorted keys vs Python insertion order."* Python compares dicts
order-insensitively, and `_by_name` already paired list entries by name — so
neither of those produced a diff, before this folder or after.

Recorded explicitly, because the next reader will otherwise go looking for a bug
that was never there. Two tests pin those non-defects so the distinction
survives.

## 3. The actual cause

`_by_name` returned `None` — falling back to exact ordered comparison — the
moment any name repeated:

```python
if any(n is None for n in names) or len(set(names)) != len(names):
    return None  # unnamed, or duplicate names — positional is safer
```

Duplicate names are not a malformed manifest here. Mounting one volume several
times under different `subPath`s is the standard way to project individual
ConfigMap keys onto individual file paths, and every such mount carries the same
`name`. So a container doing something completely ordinary got its entire
volumeMounts list compared positionally, and the chart's sorted-key render order
was reported as a wholesale change.

The symptom identifies the cause exactly: the report shows
`raw=[...] render=[...]` — a dump of the whole list — rather than a per-field
diff. A positional fallback cannot say *which* entry differs, because it never
paired them.

**`named_list_to_map` already solved this on the converter side**, keying these
`'<name>:<subPath or mountPath>'`, and its docstring explains why. The checker
was using a coarser identity than the converter.

That is the third time in this batch:

| Folder | Converter identity | Checker identity | Result |
|---|---|---|---|
| 6 | `port-<number>` | `#port:<number>/<protocol>` | a port silently lost |
| 12 | remapped by `name_map` | name resolves anywhere | a rename could point at the wrong object |
| 13 | `<name>:<subPath>` | bare `<name>` | a reorder reported as a change |

## 4. Files changed

| File | Lines | Change |
|---|---|---|
| `universal/gitops-factory/convert_to_universal_chart.py` | `_by_name` | composite `<name>:<subPath\|mountPath>` identity when plain names repeat |
| `universal/gitops-factory/namespaces-showcase/identity-auth/dex.yaml` | 30–58 | three subPath mounts on one volume, deliberately unsorted |
| `universal/gitops-factory/output-showcase/**` | regenerated | consequence |

**Falls back to positional if the composite is also ambiguous.** Two mounts with
the same name and no discriminator cannot be paired without guessing, and a
coarse CHANGED is better than a confident wrong answer. Pinned by
`test_duplicate_names_with_no_discriminator_fall_back_to_positional`.

**The corpus mounts are deliberately not in alphabetical order.** The chart
renders them back in sorted-key order, so a raw order that was already sorted
would never exercise the reordering and the coverage would be inert. Pinned by
`test_the_corpus_mounts_are_not_already_in_sorted_order`.

## 5. RED → GREEN

RED restores `_by_name`'s original body:

```
$ python <scratch>/red13.py --input namespaces-showcase --output <tmp> \
    --chart ../Universal-chart --verify-fidelity --cluster-scoped
  1 potential fidelity issue(s)

  field_changed_volumeMounts  1

  ERROR  Deployment/dex CHANGED at spec.template.spec.containers[0].volumeMounts:
         raw=[{'name': 'config', 'mountPath': '/etc/dex/theme.css', 'subPath': 'theme.css'}, ...
```

The whole-list dump, exactly as the report shows it.

GREEN:

```
  128 objects matched (30 legitimately relocated).
  No fidelity issues found.
```

Generated values confirm the composite keys:

```yaml
config:config.yaml:      {mountPath: /etc/dex/config.yaml,     name: config, subPath: config.yaml}
config:connectors.yaml:  {mountPath: /etc/dex/connectors.yaml, name: config, subPath: connectors.yaml}
config:theme.css:        {mountPath: /etc/dex/theme.css,       name: config, subPath: theme.css}
```

## 6. Tests created

13 tests in 4 groups:

1. **The non-defects** (2) — dict key order and unique-name list order were
   never the problem. Recorded so the wrong diagnosis does not get re-derived.
2. **The actual defect** (5) — reordered duplicate-name mounts are not a change;
   a real difference now reports **per field and names the specific mount**; a
   dropped mount is still LOST; an added one is still ADDED; converter and
   checker identities are asserted equal.
3. **The fallback stays conservative** (4) — duplicates with no discriminator
   and unnamed entries still bail; `args` (where order *is* the meaning) is
   untouched; containers and ports still pair by their own identity, including
   folder 6's `#port:9125/TCP`.
4. **Corpus** (2) — duplicate-name subPath mounts exist, and they are not
   pre-sorted.

Group 2's `test_a_dropped_duplicate_name_mount_is_still_reported` is the
severity guard. `error.txt` is explicit that these two volumeMount defects must
not be conflated — the previous round's **lost data**, this one loses nothing —
and a checker that paired mounts too eagerly would hide the dangerous one.

## 7. Validation chain

| Check | Result |
|---|---|
| Probe, pre-fix | reorder of duplicate-name mounts → whole-list CHANGED |
| Probe, post-fix | NONE; genuine change and genuine loss still caught |
| RED, full corpus | 1 `field_changed_volumeMounts`, whole-list dump |
| Converter re-run, showcase | 128 objects matched, **no fidelity issues** |
| Folder's own tests | 13 passed |
| All `ERRORS_NEW_ANALYSIS` tests | 216 passed |
| `helm unittest Universal-chart` | 234 passed, 28 suites |

## 8. A carryover this incidentally covers

`error.txt` asks: *"Worth confirming while here: that the earlier subPath fix is
still holding on these two deployments, since both defects live in the same
volumeMounts serialization path."*

Confirmed, and better than confirmed. The corpus contained **no `subPath`
anywhere** before this folder — so the previous round's
`VolumeMount_SubPath_Lost_Duplicate_Name_Collision` fix had no standing coverage
either. It does now: `test_the_corpus_contains_duplicate_name_subpath_mounts`
plus the round-trip check, which would report a LOST mount if the converter ever
dropped one again.

## 9. What could NOT be verified

- **`Deployment/midasson-blue-fac1` and `midasson-yellow-tst`.** Not in this
  repo. The mechanism is reproduced and fixed; whether those two have exactly
  this shape is inferable from the report's whole-list dump (which only a
  positional fallback produces) but not provable.
- **That the report's 2 errors were only these.** Its own count varies and the
  input is unavailable.

## 10. OBSERVED, DEFERRED

- **`Number_Serialization_Drift` (folder 24)** is the remaining "checker sees a
  difference that isn't one" item. Different mechanism (scalar representation,
  not list identity). Not touched.
- **`_by_name` now has three identity strategies** (name, `#port:n/proto`,
  `name:subPath`). If a fourth is ever needed, it belongs in `_item_name`
  alongside the port rule rather than as another branch here — noted, not
  refactored, since two call sites do not justify it yet.

## 11. Reproducing

```powershell
python -m pytest universal/gitops-factory/output-showcase/ERRORS_NEW_ANALYSIS/VolumeMounts_Dict_Key_Ordering_Drift/test/test_volumemount_ordering.py -v
python <scratch>/probe_order.py
```
