# STATUS — StatefulSet_Checksum_Annotations_Lost (TIER 3, folder 19/26)

**Result: NOT BENIGN as shipped — it was the regression branch, and is fixed.**
The report rates this LOW because "the operator regenerates them on deploy".
`error.txt` refuses that on trust and names the check. The check was run, and the
answer was the bad one: the chart *has* the machinery and ships it **disabled**,
and the converter never turned it on — so the annotations were dropped and
nothing replaced them. Fixed by preserving the intent rather than the stale
hash. 17 new tests RED→GREEN, showcase fidelity 0 lost / 0 changed, chart's 234
green.

---

## 1. The check, and its answer

`error.txt`:

> **THE CHECK: does Universal-chart contain a `checksum/config` annotation
> template** (the usual form being sha256sum of the rendered ConfigMap)? If not,
> and no operator supplies one, this is a real regression regardless of the LOW
> rating.

Both halves turned out to be true at once:

| Question | Answer |
|---|---|
| Does the chart have a checksum template? | **Yes** — `universal-chart.checksumAnnotations`, `sha256sum` over `.Values.configMaps` and `.Values.secrets`, wired into `deployment.yaml`, `statefulset.yaml` and `daemonset.yaml` |
| Is it on? | **No** — `values.yaml` ships `checksums.enabled: false` |
| Does the converter turn it on? | **No** — it never referenced `checksums` at all |

So the machinery existed and was switched off. That lands squarely on
`error.txt`'s second branch:

> If the Universal-chart itself is expected to compute them, and it does not,
> then config changes stop triggering restarts. That is a silent, delayed
> failure — the ConfigMap updates, ArgoCD reports Synced and Healthy, and the
> pods keep serving stale configuration until something unrelated restarts them.

Which is why this folder ends with a code change rather than a suppression,
despite being in Tier 3 and rated LOW.

## 2. The fix: drop the values, preserve the intent

Two different things were being conflated under "checksum annotations lost":

| | Verdict |
|---|---|
| the hash **values** | must NOT be carried. They hash the *source cluster's* content and are stale on arrival — worse than absent, because they pin the pod template to a value that describes nothing |
| the **presence** of the keys | is information. A workload carrying them was being restarted when its config changed, and dropping that silently is the regression |

So `handle_workload` now sets `checksums.enabled: true` for any workload whose
raw pod template carried a `checksum/*` annotation, and the chart computes fresh,
correct hashes of the content it actually renders.

Verified end to end — the raw manifest's `8f14e45f...` (an MD5-shaped
placeholder) is replaced by a real sha256 of the rendered Secret:

```
raw:      checksum/secrets: 8f14e45fceea167a5a36dedd4bea2543
rendered: checksum/secrets: 2b45f65d50887e60b16e33bbf5463bf208df79acee9ffc222c3b94f77795c844
```

**Not enabled globally**, deliberately. Defaulting `checksums.enabled: true`
would add annotations to every workload that never had them — an invented field
on every diff and unrequested restarts. Per-workload, driven by evidence from
the source, is the only version that preserves behaviour without inventing it.

## 3. The suppression is conditional on evidence

Once the chart regenerates them, the raw keys are noise — but only then. So the
checker ignores raw `checksum/*` annotations **only when the render carries at
least one of its own**. A render with none means nothing regenerates them, the
behaviour really is gone, and every key reports as LOST.

That is `error.txt`'s condition enforced per object at check time, rather than a
rule that trusts the converter to have done the right thing.

The key **names** need not match — the chart emits `checksum/configmaps` and
`checksum/secrets`, the source used `checksum/config` and `checksum/secret-certs`
— because what matters is that the pod template moves when the content does, not
what the annotation is called.

## 4. Files changed

| File | Lines | Change |
|---|---|---|
| `universal/gitops-factory/convert_to_universal_chart.py` | ~1085–1105 (new) | `handle_workload` sets `checksums.enabled` when raw carried `checksum/*` |
| `universal/gitops-factory/convert_to_universal_chart.py` | ~3540–3562 (new) | conditional, symmetric suppression in `_fidelity_diff` |
| `universal/gitops-factory/namespaces-showcase/platform-data/postgres.yaml` | 27–39 | pod-template `checksum/secrets` + `checksum/config` |
| `universal/gitops-factory/output-showcase/**` | regenerated | consequence |

## 5. A mistake in my first version, caught by its own test

The suppression initially applied to the **raw** side only. The chart's
regenerated `checksum/configmaps` then came back as an ADDED field on every
workload that had checksums.

`_fidelity_diff` already has a comment about exactly this, added by an earlier
round: *"Asymmetric suppression is worse than none: it makes the rule look
unreliable."* The rule now covers both sides, and
`test_the_regenerated_annotation_is_not_reported_as_ADDED_either` pins it.

Two of the three initial test failures were my own bad assertions rather than
code faults — an `rsplit(".", 1)` that split `prometheus.io/scrape` at the wrong
dot, and an expectation that ADDED would not appear. Corrected in place.

## 6. Tests created

17 tests in 4 groups:

1. **The check itself** (5) — the chart has the template; all three workload
   templates wire it in; it ships disabled (recorded, with the reason it is not
   simply flipped).
2. **Intent preserved, values not** (4) — raw checksums enable the chart's own;
   the stale hashes are not carried; a workload without them does not get
   checksums enabled; other pod annotations are unaffected.
3. **The conditional suppression** (5) — suppressed when the render regenerates;
   **not** suppressed when the render has none; names need not match; a
   non-checksum annotation is never swept up; the rule is scoped to annotation
   maps, so a ConfigMap `data` key named `checksum/config` still reports.
4. **End to end + corpus** (3) — the chart emits a real 64-character sha256; the
   corpus carries checksum annotations.

Group 3's `test_NOT_suppressed_when_the_render_has_none` is the one that makes
the rest honest: it is the regression branch, and if it ever passes silently the
whole verdict has inverted.

## 7. Validation chain

| Check | Result |
|---|---|
| Pre-fix, corpus with checksums | 2 issues — 1 CHANGED, 1 LOST |
| Post-fix | 0 issues; `checksums.enabled: true` in values |
| `helm template` | fresh 64-char sha256 in the pod template |
| Folder's own tests | 17 passed |
| All `ERRORS_NEW_ANALYSIS` tests | 317 passed |
| `helm unittest Universal-chart` | 234 passed, 28 suites |

## 8. What could NOT be verified

- **Whether a Helm operator really regenerates them in the source cluster.** The
  report claims so; it cannot be checked from here, and the fix does not depend
  on it — if an operator *does* also maintain them, the chart's annotation is
  harmless duplication, and if it does not, the behaviour is now preserved.
- **That Kubernetes performs the rolling restart.** Controller behaviour.
  Verified only that the annotation changes when the content changes, which is
  the precondition.
- **The `Deployment/tyk-yuval` instances** named in the report. Not in this repo.

## 9. OBSERVED, DEFERRED

- **`Deployment_MinReadySeconds_Lost` (folder 7)** shares an object with this
  defect in the report (lines 143–147). Its half was fixed there; only the
  checksum half belonged here.
- **`checksums.enabled` is not exposed per-container or per-subresource.** A
  CronJob's `jobPodSpec` does not get checksum annotations at all. No defect
  observed — no CronJob in the report carried them — so it was left alone rather
  than extended speculatively.

## 10. Reproducing

```powershell
python -m pytest universal/gitops-factory/output-showcase/ERRORS_NEW_ANALYSIS/StatefulSet_Checksum_Annotations_Lost/test/test_checksum_annotations.py -v

cd universal/gitops-factory
Select-String output-showcase/platform-data/values/postgres.yaml -Pattern checksums -Context 0,1
```
