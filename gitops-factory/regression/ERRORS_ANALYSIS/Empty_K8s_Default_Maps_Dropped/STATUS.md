# STATUS — Empty_K8s_Default_Maps_Dropped (P3, folder 29/35)

**Result: CONFIRMED FALSE POSITIVE, fixed in the FIDELITY CHECKER as the brief
directs. The one item error.txt flags as possibly-not-noise is verified
separately.** 10 tests, 3 RED→GREEN.

---

## 1. The classification, and the one exception

~600 occurrences — about a third of the report's ~1,960 Medium/Low LOST lines,
and its single largest source of noise. `resources: {}` and an absent
`resources:` key produce byte-identical objects once the API server is done, so
reporting the difference is reporting nothing.

**That noise is not free.** It is the same report where folder 1's irreversible
PVC-retention loss and folder 6's never-starting pods were sitting. 600
known-ignorable lines is what makes a report get skimmed, and skimming it is how
those two survived.

**error.txt flags one item as possibly not noise** and asks for it to be checked
before the group is suppressed: `immutable: false` on ConfigMaps (11
occurrences). `false` is the default so dropping it is safe — but an
`immutable: true` being dropped would be a real change, an immutable ConfigMap
coming back mutable. Verified:

```
ConfigMap immutable values: none present
```

No ConfigMap in this repo carries the key at all. And the suppression is scoped
so it could not have hidden a `true` regardless — it skips `{}` and `[]` only.
`test_immutable_true_would_be_reported` pins that directly.

## 2. What is suppressed, and what deliberately is not

| raw value | suppressed? | why |
|---|---|---|
| `{}` / `[]` | **yes** | identical to absent, to Kubernetes |
| `false`, `0`, `""` | **no** | deliberate values; dropping one IS a loss (folder 15) |
| a populated map | **no** | obviously |
| `dnsPolicy`, `terminationGracePeriodSeconds`, `schedulerName` | **no — nothing to suppress** | see below |

The report groups the Kubernetes-populated defaults in with the empty maps, but
they are a different case: `handle_workload` **captures all three** and they
round-trip today. 293 `terminationGracePeriodSeconds: 30`, 227
`dnsPolicy: ClusterFirst`, 227 `schedulerName: default-scheduler` in this repo,
none of them lost. Suppressing them would have hidden a genuinely *changed*
value — `test_a_changed_default_is_still_reported` guards that.

`creationTimestamp` is already handled: it is in `VOLATILE_METADATA_KEYS` and
stripped before any comparison happens.

## 3. Files changed

| File | Change |
|---|---|
| `convert_to_universal_chart.py` `_fidelity_diff` | skip a LOST report when the raw value is `{}` or `[]` |

Converter and chart unchanged. This is a checker-only fix, which is what error.txt
recommends and what the brief says to do with a genuine false positive.

## 4. Tests

`Empty_K8s_Default_Maps_Dropped/test/` — 10 tests, no fixture needed (the
checker is a pure function over two dicts).

### RED — suppression removed, restored byte-identically

```
=== empty-map suppression disabled ===

AssertionError: empty resources reported as a loss;
  [('LOST', 'spec.template.spec.containers[0].resources', 'raw had {}')]
AssertionError: empty volumeMounts reported as a loss;
  [('LOST', 'spec.template.spec.containers[0].volumeMounts', 'raw had []')]
AssertionError: [('LOST', 'spec.template.spec.securityContext', 'raw had {}')]

3 failed, 7 passed
restored: True
```

The 7 that pass either way are the limits — `false`/`0`/`""` still reported,
`immutable: true` still reported, populated maps still reported, changed defaults
still reported. They pass before because the checker reported everything and
after because the suppression is narrow. Both readings are the point.

### GREEN

```
10 passed
```

## 5. Validation chain

| Step | Result |
|---|---|
| Full showcase converter run | `122 objects matched (28 relocated). No fidelity issues found.` |
| Fidelity before / after | 0 lost / 0 changed, unchanged |
| Generated-output churn | none — **0** `resources: {}` and **0** `securityContext: {}` in this repo |
| `helm lint` | 0 failed |
| Chart's own suite | 234 passed |

The ~600 belong to the original project, so the showcase report does not move.
The suppression is defensive here and load-bearing there.

## 6. What could NOT be verified without a live cluster

- **That `resources: {}` and an absent `resources:` really are identical.** It
  is the documented API behaviour (an empty ResourceRequirements sets no limits
  and no requests), not something observed.
- **That the ~600 in the original report were all genuinely empty.** The report
  quotes `raw had {}` on the lines it shows, and that is all the evidence there
  is from here.

## 7. OBSERVED, DEFERRED

- **The mirror case is not suppressed and should not be:** a raw manifest with
  no `resources:` and a render that ADDS `resources: {}` reports as ADDED, which
  is informational and correct — the chart inventing an empty block is worth
  knowing about even though it changes nothing.
- **Empty-map suppression is per-key, not recursive.** `resources: {limits: {}}`
  still reports the inner `limits` if the render drops it. That is deliberate:
  the outer key was populated, so the shapes genuinely differ.
- Earlier folders' deferred items still stand.

## 8. Reproducing

```powershell
python -m pytest universal/gitops-factory/output-showcase/ERRORS_ANALYSIS/Empty_K8s_Default_Maps_Dropped/test -v
```
