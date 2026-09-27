# STATUS — Thanos_OwnerReferences_Lost (TIER 3, folder 23/26)

**VERIFIED_AND_FIXED.**

**Result: BENIGN — but the report's reason is the wrong one, and following it
would have left a live defect in place.** The report waves `ownerReferences`
through because *"(operator re-creates them)"*. `error.txt` is right to call that
a dependency rather than a property. The verdict survives anyway, on stronger
ground that needs no operator at all — and pursuing it turned up the half of the
defect nobody had noticed: **the pod template's `ownerReferences` were still
being carried**, and those are the dangerous ones.

RED→GREEN on the real pipeline: 1 LOST → 0 LOST. 15 new tests, all
`ERRORS_NEW_ANALYSIS` 377 passed, `helm unittest` 234 passed, lint clean, zero
new failures in the previous round.

---

## 1. The dependency `error.txt` objects to, and why it does not decide this

> *"CLASSIFICATION: benign IF AND ONLY IF the Thanos Banzaicloud operator is
> running in the target cluster. The entire justification is the parenthetical
> '(operator re-creates them)' — that is a dependency, not a property of the
> field."*

Correct, and this folder does not try to verify that dependency, because the
answer does not turn on it. An `ownerReference` **is a `uid`**, and a uid from
the *source* cluster does not exist in the target:

| | Consequence in the target cluster |
|---|---|
| **Drop** the reference | the garbage-collection link must be re-established — by the operator, or by ArgoCD/Helm ownership. Recoverable. |
| **Keep** the reference | the API server's collector sees an owner that is gone and **deletes the object**, usually within seconds. Not recoverable. |

So the choice is not "benign loss vs. faithful carry". It is "a link to
re-establish" vs. "an object that deletes itself on arrival". Dropping is correct
with no operator anywhere, which is what makes the operator question moot rather
than unanswered.

`error.txt` also flags an apparent contradiction with the previous round, where
`ExternalSecret` `ownerReferences` were called a real defect needing a fix. There
is no contradiction in the code: `_eso_metadata` reached the same conclusion and
wrote it down —

> *"`ownerReferences` is deliberately NOT here, even though the report asks for
> it. […] an ownerReference is a `uid`, and a uid from the SOURCE cluster does
> not exist in the target. […] Dropping it does not break garbage collection —
> keeping it breaks the object."*

Both kinds strip. One verdict, reached twice, from two directions.

## 2. What was actually still broken

The verdict was already implemented for **outer** object metadata — every kind,
via `VOLATILE_METADATA_KEYS`. It was **not** implemented for the **pod
template**, and nothing had ever tested it, because the showcase corpus
contained the string `ownerReferences` exactly **zero** times. The defect class
scored 0 in every fidelity run by having nothing to look at — the same blind spot
Route labels had before folder 3.

Pod-template `ownerReferences` are the worse half. Pod template metadata is
stamped onto **every pod the controller creates**, so a carried reference gives
each new pod an owner that does not exist and the collector deletes the pods
about as fast as the DaemonSet makes them — a crash-loop-shaped symptom with no
crash in it.

## 3. Files changed

| File | Location | Change |
|---|---|---|
| `universal/gitops-factory/convert_to_universal_chart.py` | `sanitize_object`, pod-template strip | added `ownerReferences` to the key list, with the uid rationale |
| `universal/gitops-factory/namespaces-showcase/platform-ops/metrics-agent.yaml` | `metadata` + `spec.template.metadata` | corpus fixture: an operator-managed workload carrying a `StoreEndpoint` owner at the object level and a `Thanos` owner at the pod-template level, both with uids |
| `.../ERRORS_NEW_ANALYSIS/Pod_Template_Metadata_Name_Namespace_Lost/test/test_pod_template_metadata.py` | 1 test | folder 22's deferred-verdict test, inverted and re-documented — see §7 |
| `.../ERRORS_NEW_ANALYSIS/Thanos_OwnerReferences_Lost/test/` | new | 15 tests |

```python
    if kind in WORKLOAD_KINDS and isinstance(spec, dict):
        pod_meta = (spec.get("template") or {}).get("metadata")
        if isinstance(pod_meta, dict):
            for key in ("name", "namespace", "creationTimestamp", "ownerReferences"):
                pod_meta.pop(key, None)
```

**Still an explicit list, not `VOLATILE_METADATA_KEYS`.** Every key there has a
reason written against pod templates specifically. The remaining members of that
tuple (`resourceVersion`, `uid`, `generation`, `selfLink`, `managedFields`,
`finalizers`) have never been observed on a pod template, and widening a rule
past its evidence is the failure mode folder 17 exists to warn about. A test
pins this so the shortcut is caught rather than discovered.

## 4. Proof — RED

Fixture added, converter untouched:

```
python convert_and_verify.py --cluster-scoped --input namespaces-showcase --output <scratch>\out23RED

    fields LOST                                  1
    fields CHANGED                               0
    render failures                              0

  field_lost_ownerReferences  1
  ERROR  DaemonSet/metrics-agent LOST at spec.template.metadata.ownerReferences:
         raw had [{'apiVersion': 'monitoring.banzaicloud.io/v1alpha1',
                   'kind': 'Thanos', 'name': 'thanos',
                   'uid': '2a7d4b61-8c05-4e77-b3aa-5d9e6f70a1b2'}]
```

One error, and **only** the pod-template one — which is itself the measurement
that the outer level was already handled. The fixture carries owners at both
levels; only the unhandled level reported.

## 5. Proof — GREEN

```
python convert_and_verify.py --cluster-scoped --input namespaces-showcase --output <scratch>\out23GREEN

    fields LOST                                  0
    fields CHANGED                               0
    objects in raw but rendered by nothing       0
    objects rendered with no raw source          0
    render failures                              0
  OK - nothing lost, nothing changed.

--- ownerReferences lines in report ---
  (none)
```

The generated values were then diffed against the committed showcase output:

```
diff -r <scratch>\out23GREEN/platform-ops/values  output-showcase/platform-ops/values
platform-ops values IDENTICAL to committed showcase output
```

The fixture adds a defect to the corpus without changing a single generated
value — which is what "stripped at the boundary" is supposed to look like.

## 6. Tests

```
python -m pytest output-showcase/ERRORS_NEW_ANALYSIS/Thanos_OwnerReferences_Lost/test -q
...............                                                          [100%]
15 passed in 0.69s
```

Four groups:

1. **Both levels** (6) — outer stripped, pod template stripped, both at once,
   across all three workload kinds, and an assertion that the fixtures carry a
   `uid` (a test built on a uid-less reference would not be testing the case
   that matters).
2. **Scope** (4) — pod-template labels and annotations survive intact; a
   non-workload kind's `spec.template.metadata` is untouched; the outer strip
   stays kind-independent, which the previous round's ESO objects depend on; and
   the rule has not silently widened to all of `VOLATILE_METADATA_KEYS`.
3. **Capability preserved** (1) — the chart still renders an `ownerReferences`
   key when a values file sets one **by hand**, i.e. against a uid that exists in
   the target. This folder removes an inherited uid, not the ability to express
   ownership.
4. **Corpus** (3) — both levels present in the showcase, no generated values file
   carries an inherited reference, and the 2-vs-4 count discrepancy recorded as
   unresolvable here.

## 7. This folder changed folder 22's test, on purpose

Folder 22 deliberately left `ownerReferences` alone and shipped a test asserting
it was still present, naming this folder as the owner of the question. That test
failed the moment the rule changed — the deferred-verdict mechanism working
exactly as it did for folder 17 → folder 21. It is now inverted and rewritten to
record what folder 23 decided and on what ground, so the four keys of one rule
are legible from the folder that owns three of them.

## 8. Validation chain

| Check | Result |
|---|---|
| Corpus reproduces the defect | yes — 1 LOST, pod-template level |
| Outer level already handled | yes — confirmed by its absence from the RED report |
| Showcase fidelity after fix | **0 lost / 0 changed / 0 render failures** |
| Generated values unchanged by the fixture | yes — `diff -r` identical |
| Folder's own tests | **15 passed** |
| All `ERRORS_NEW_ANALYSIS` tests | **377 passed** |
| `ERRORS_ANALYSIS` (previous round) | 12 failed / 320 passed — **the same 12 as before this folder**, none new |
| `helm unittest Universal-chart` | **234 passed**, 28 suites |
| `helm lint Universal-chart` | 0 failed |

STEP 2's single INVENTED name (`ConfigMap/nightly-report-job-defaults`) is
folder 13's deliberate rename, unchanged and unrelated.

## 9. What could NOT be verified, and what it does not change

- **Whether the Thanos Banzaicloud operator runs in the migration target.** It is
  not this repo's cluster and `error.txt` names no way to check it from here. The
  fix is deliberately built so the answer cannot change the outcome: carrying a
  source-cluster uid is wrong in both worlds. What the answer *does* change is
  the follow-up — with no operator, something else must own these objects
  (ArgoCD or Helm), and that is a migration decision, not a converter one.
- **The second risk `error.txt` raises — ArgoCD treating operator-created
  resources as unmanaged drift.** Real, and unaffected here: ArgoCD tracks by the
  `argocd.argoproj.io/instance` label, which the previous round made sure is
  carried (`_eso_metadata`, `raw_labels`). A stale `ownerReference` would not
  have helped it and a missing one does not hurt it.
- **The real Thanos deployments** (`thanos-query`, `thanos-endpoint-store`) are
  not in this repo. Both levels are now covered synthetically and in the corpus.

## 10. OBSERVED, DEFERRED

- **`Job` / `CronJob` pod templates** are still outside `WORKLOAD_KINDS` and so
  outside this rule — measured in folder 22 §9 and unchanged here. The
  ownerReferences argument applies to them identically; no observed defect, so
  no speculative widening.
- **12 pre-existing `ERRORS_ANALYSIS` failures**, inherited from folders 1–21 and
  untouched by folders 22–23 (attribution measured in folder 22 §7). They need a
  pass of their own.

## 11. Reproducing

```powershell
cd c:\repos\homelab\universal\gitops-factory
python -m pytest output-showcase/ERRORS_NEW_ANALYSIS/Thanos_OwnerReferences_Lost/test -v
python convert_and_verify.py --cluster-scoped --input namespaces-showcase --output <scratch>\out23
```

---

**VERIFIED_AND_FIXED**
