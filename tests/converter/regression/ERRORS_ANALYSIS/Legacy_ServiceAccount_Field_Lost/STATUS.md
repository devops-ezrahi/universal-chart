# STATUS — Legacy_ServiceAccount_Field_Lost (P3, folder 33/35)

**Result: CONFIRMED FALSE POSITIVE, and fixed where the brief says to fix one —
in the FIDELITY CHECKER, not the converter.** 7 tests, 2 RED→GREEN.

---

## 1. Grep proof

```
legacy `serviceAccount:` vs `serviceAccountName:` in pod specs
    both keys present: 0   legacy-only: 0   name-only: 265
    DISAGREEING values: none
```

Zero pod specs in this repo use the deprecated key; 265 use the modern one. The
report's own Discoveries section already reached the same conclusion —
*"Functionally identical. Severity: NONE."* — and it is correct: Kubernetes
treats `serviceAccount` as a deprecated alias and the API server keeps both
populated with the same value. Rendering only `serviceAccountName` is right.

`test_no_pod_spec_in_this_repo_uses_the_legacy_key` re-runs the scan every time.

**The MEDIUM table's caveat** — *"deprecated but used by some tools for SA
binding"* — names no such tool, and I could not find one to name either. That is
not proof of absence, so it is recorded here rather than dismissed.

## 2. What was actually worth fixing

The checker compared pod specs key by key, so a raw manifest carrying both keys
produced a permanent `LOST spec.template.spec.serviceAccount` on **every**
workload. The brief's guidance for a genuine false positive is to teach the
checker to stop reporting it, and that noise is not harmless: it is the same
report where folder 6's real defect lived — pods naming a ServiceAccount that
does not exist — and a report full of known-ignorable lines is a report people
skim.

| File | Change |
|---|---|
| `convert_to_universal_chart.py` `_fidelity_diff` | skip the legacy key when the modern one is present with the SAME value |

**Matched on values, not on a path.** Both keys holding the same string only
ever happens inside a pod spec, so the suppression cannot fire anywhere else,
and it needs no list of paths to maintain.

**Three cases stay reported**, each with its own test:

| case | reported? | why |
|---|---|---|
| both keys, same value | no | the alias; the modern key carries it |
| both keys, **different** values | **yes** | something upstream is wrong |
| legacy key **alone** | **yes** | a real loss — nothing else carries the value |
| `serviceAccountName` itself lost or changed | **yes** | folder 6's defect must stay visible |

That last one is the guard that matters: `test_the_modern_key_is_still_compared`
exists so this suppression can never grow into hiding the defect it sits next to.

## 3. Tests

`Legacy_ServiceAccount_Field_Lost/test/` — 7 tests, no fixture needed (the
checker is a pure function over two dicts).

### RED — before

```
2 failed, 5 passed in 17.41s

AssertionError: the deprecated alias is still reported as a loss;
  diffs=[('LOST', 'spec.template.spec.serviceAccount', "raw had 'grafana-sa'")]
AssertionError: diffs=[('LOST', 'spec.template.spec.serviceAccount', "raw had 'sa'")]
```

The 5 that passed are the limits — the disagreement case, the legacy-alone case
and the two folder-6 guards. They passed before the change because the checker
reported *everything*; they pass after because the suppression is narrow. Both
readings are the point.

### GREEN — after

```
7 passed in 14.20s
```

## 4. Validation chain

| Step | Result |
|---|---|
| Full showcase converter run | `122 objects matched (28 relocated). No fidelity issues found.` |
| Fidelity before / after | 0 lost / 0 changed, unchanged (nothing in this repo uses the key) |
| Generated-output churn | none |
| `helm lint` | 0 failed |
| Chart's own suite | 234 passed |

## 5. What could NOT be verified without a live cluster

- **Whether any tool in the target environment reads `serviceAccount`
  specifically.** An admission controller or a legacy operator doing so would
  make this a real loss. Nothing in the report names one, and nothing offline
  can settle it — this is the one open question in the folder.
- **That the API server really does populate both keys.** Documented Kubernetes
  behaviour, not observed here.

## 6. OBSERVED, DEFERRED

- **Do not confuse this with `ServiceAccountName_Overridden_By_Release_Name`
  (folder 6)** — that one was real, was fixed, and is about the *value* being
  wrong. This folder is only about the deprecated *key* being absent while the
  correct key carries the correct value.
- Earlier folders' deferred items still stand.

## 7. Reproducing

```powershell
python -m pytest universal/gitops-factory/output-showcase/ERRORS_ANALYSIS/Legacy_ServiceAccount_Field_Lost/test -v
```
