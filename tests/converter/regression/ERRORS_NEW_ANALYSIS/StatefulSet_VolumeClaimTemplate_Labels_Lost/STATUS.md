# STATUS — StatefulSet_VolumeClaimTemplate_Labels_Lost (TIER 1, folder 8/26)

**Result: FIXED — split into its two opposite verdicts.** One report row
containing a real loss and benign runtime noise, which is exactly why the same
field path appears in both the "suppress" and "fix" columns. Labels are now
carried; `status` / `kind` / `apiVersion` / `creationTimestamp` are now
stripped; and the strip is written against those specific keys so it cannot
swallow the labels. 17 new tests RED→GREEN, both halves reproduced
independently, showcase fidelity 0 lost / 0 changed, chart's 234 green.

---

## 1. The split — the whole point of this folder

`error.txt` is unusually direct about the trap:

> THIS BUCKET CONTAINS TWO DIFFERENT THINGS AND THEY MUST BE SPLIT [...]
> Suppressing the whole path would silently bury the labels defect inside the
> boilerplate suppression. Write the suppression rule against the specific
> runtime keys, **never against the whole volumeClaimTemplates subtree**.

| Inside `volumeClaimTemplates[].` | Verdict | Why |
|---|---|---|
| `metadata.labels` | **FIX — carry** | The PVCs a StatefulSet creates inherit the template's labels, and nothing else gives them any. Quota enforcement and cost allocation select on these |
| `metadata.annotations` | **FIX — carry** | Same inheritance |
| `status` | **strip** | Server-populated (`{phase: Pending}`); must never be in a chart |
| `kind`, `apiVersion` | **strip** | Redundant on an embedded template |
| `metadata.creationTimestamp` | **strip** | Server-populated |

The previous round rated the labels LOW
(`ERRORS_ANALYSIS/Workload_Metadata_Labels_Lost` #20: "ZooKeeper PVCs lose team,
environment labels"). `error.txt` says that verdict is superseded and gives the
reason — quota and cost allocation. It is right, and there is a sharper point
underneath: **a PVC is the one object here that outlives its workload.** An
unlabelled volume left behind after a StatefulSet is deleted is not traceable to
a team at all.

## 2. Root causes — two, in different places

**The labels** were a converter-only gap. `templates/statefulset.yaml` already
renders `$spec.labels` and `$spec.annotations` on each volumeClaimTemplate; the
VCT entry builder in `handle_workload` captured only `accessModes`, `size`,
`storageClassName` and `volumeMode`. Another instance of RC-4 — the whitelist
never grew a `labels` key.

**The runtime noise** escaped `sanitize_object`. That function strips top-level
`status` and the volatile metadata keys, but volumeClaimTemplates are *embedded*
objects and the strip only reached the outer document. So a dump scraped from a
live cluster arrived with `status: {phase: Pending}` and `creationTimestamp:
null` sitting one level down.

## 3. Files changed

| File | Lines | Change |
|---|---|---|
| `universal/gitops-factory/convert_to_universal_chart.py` | ~410–434 (new) | `sanitize_object`: strip embedded VCT runtime fields |
| `universal/gitops-factory/convert_to_universal_chart.py` | ~963–978 (new) | `handle_workload`: capture VCT `labels` / `annotations` |
| `universal/gitops-factory/namespaces-showcase/platform-data/postgres.yaml` | 71–98 | VCT carrying governance labels **and** the runtime noise |
| `universal/gitops-factory/output-showcase/**` | regenerated | consequence |

### Why the strip went in `sanitize_object` and not `_fidelity_ignored_path`

`error.txt` says "suppress", and the checker would have been the obvious place.
Two reasons it is the wrong one:

1. **`sanitize_object` already does exactly this job four lines up** — it strips
   the outer object's `status` and volatile metadata. These are the same fields
   one level down. Putting the rule anywhere else would split one concept across
   two functions.
2. **A checker-side rule would have had to name the subtree.**
   `_fidelity_ignored_path` matches on paths, and the natural expression is
   "ignore under `spec.volumeClaimTemplates`" — precisely the blunt instrument
   `error.txt` warns against, because the labels live under that same path.

Stripping at the source also means the junk never reaches the generated values
file, not just that nobody compares it.

**Scoped to `kind == "StatefulSet"`**, so it fires on the kind rather than on
any object that happens to carry a `volumeClaimTemplates` key. Pinned by
`test_other_kinds_are_untouched_by_the_statefulset_rule`.

## 4. RED — each half, independently

**RED A — labels not captured** (sanitizer still stripping):

```
  ERROR  StatefulSet/postgres LOST at spec.volumeClaimTemplates[0].metadata.labels:
         raw had {'team': 'platform-data', 'environment': 'dev', 'cost-centre': 'cc-4471'}
```

**RED B — boilerplate not stripped** (converter still capturing labels):

```
  ERROR  StatefulSet/postgres LOST at spec.volumeClaimTemplates[0].metadata.creationTimestamp: raw had None
  ERROR  StatefulSet/postgres LOST at spec.volumeClaimTemplates[0].status: raw had {'phase': 'Pending'}
```

RED B matches the report's triage file verbatim:

> `spec.volumeClaimTemplates[0].status (5x)`
> `Raw: spec.volumeClaimTemplates[0].status: {'phase': 'Pending'}`

Two REDs rather than one because the two halves have opposite verdicts, and a
single combined RED would not show that fixing one does nothing for the other.

## 5. GREEN

```
$ python convert_to_universal_chart.py --input namespaces-showcase --output <tmp> \
    --chart ../Universal-chart --verify-fidelity --cluster-scoped
  123 objects matched (28 legitimately relocated).
  No fidelity issues found.
```

`helm template` of the affected release:

```yaml
volumeClaimTemplates:
  - metadata:
      name: data
      labels:
        cost-centre: cc-4471
        environment: dev
        team: platform-data
    spec:
      accessModes:
        - ReadWriteOnce
      volumeMode: Filesystem
      resources:
        requests:
          storage: 1Gi
```

Labels present; `status` and `creationTimestamp` gone.

```
$ python -m pytest .../StatefulSet_VolumeClaimTemplate_Labels_Lost/test/test_vct_labels.py -q
.................                                                        [100%]
17 passed in 0.76s
```

## 6. Tests created

`ERRORS_NEW_ANALYSIS/StatefulSet_VolumeClaimTemplate_Labels_Lost/test/test_vct_labels.py`
— 17 tests in 4 groups:

1. **The real loss** (7) — labels and annotations captured; each of the three
   governance labels parametrised; no `labels` key invented when absent;
   chart-owned identity labels still excluded.
2. **The benign half, and its narrowness** (7) — `status` / `kind` /
   `apiVersion` / `creationTimestamp` stripped; the strip does **not** take the
   labels or the template's `name` or `spec` with it; a StatefulSet without VCTs
   is unharmed; other kinds are untouched.
3. **Template** (1) — labels render through real `helm template`.
4. **Corpus** (1) — one VCT carries labels **and** runtime noise. Both in the
   same template on purpose: labels alone would leave the suppression untested,
   noise alone would leave the loss untested.

`test_the_strip_does_NOT_take_the_labels_with_it` is the assertion this whole
folder exists for — every other test here would pass just as well under a
blanket subtree suppression.

## 7. Validation chain

| Check | Result |
|---|---|
| RED A (labels not captured) | `LOST ... volumeClaimTemplates[0].metadata.labels` |
| RED B (noise not stripped) | `LOST ... [0].status`, `LOST ... [0].metadata.creationTimestamp` |
| Converter re-run, showcase | 123 objects matched, **no fidelity issues** |
| `helm template` | labels rendered, boilerplate absent |
| Folder's own tests | 17 passed |
| All `ERRORS_NEW_ANALYSIS` tests | 143 passed |
| `helm unittest Universal-chart` | 234 passed, 28 suites |

## 8. Count reconciliation

`error.txt` carries three numbers for this row and they are not in conflict once
split:

| Source | Count | What it counts |
|---|---|---|
| "volumeClaimTemplates metadata" | ~25 | everything: apiVersion, kind, creationTimestamp, status, labels |
| "VCT runtime fields" | ~16–17 | the strip half only |
| cross-check "VCT metadata lost" | 34 → 15 | `status`/`kind`/`apiVersion` measured in the report |
| "VCT labels (1)" | 1 | the fix half only |

So roughly 16–28 benign and ~1 real. The single real one is the reason the
suppression had to be narrow — a one-in-twenty-five signal is exactly the kind a
bucket suppression erases without anyone noticing.

## 9. What could NOT be verified

- **`statefulset-alpha-996` / `statefulset-beta-764`.** Sanitized aliases; the
  manifests are not in this repo.
- **That Kubernetes propagates template labels onto the created PVCs.** That is
  StatefulSet controller behaviour. Verified only that the labels reach the
  rendered `volumeClaimTemplates[].metadata.labels`, which is the precondition.
- **Whether a quota or cost-allocation system actually selects on these keys in
  the target.** Needs the cluster; the fix is correct either way.

## 10. OBSERVED, DEFERRED

- **`K8s_Runtime_Defaults_Benign_Suppress` (folder 17)** lists "VCT labels (1)"
  in its suppression list. That entry must be **removed** — it is the real loss
  fixed here. `error.txt` already flags it as one of the three that "do not
  belong". Recorded for that folder; not edited.
- **`StatefulSet_PVC_Retention_Policy_Lost` (carryover, folder 2)** lives on the
  same object and is unaffected — `test_carryover_1_*` still green.
- **`Pod_Template_Metadata_Name_Namespace_Lost` (folder 22)** is the same shape
  one level over: runtime-ish metadata on an embedded template. Not touched; its
  verdict (benign) is likely but is that folder's to make.

## 11. Reproducing

```powershell
python -m pytest universal/gitops-factory/output-showcase/ERRORS_NEW_ANALYSIS/StatefulSet_VolumeClaimTemplate_Labels_Lost/test/test_vct_labels.py -v

python <scratch>/make_red8.py   # rebuilds both RED variants
```
