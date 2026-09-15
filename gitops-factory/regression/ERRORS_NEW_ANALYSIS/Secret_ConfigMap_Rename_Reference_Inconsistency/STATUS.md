# STATUS — Secret_ConfigMap_Rename_Reference_Inconsistency (TIER 2, folder 12/26)

**Result: DECIDED — BENIGN, and the deciding test is now permanent.** The
renames are internally consistent: every reference resolves to an object the
namespace actually renders. But the verdict matters less than the machinery,
because the report held both positions at once ("one of the 6 real issues" vs
"internally consistent after rename") purely because nobody could cheaply check.
`--verify-fidelity` now performs the check `error.txt` specified, on every run.
17 new tests RED→GREEN, a real collision added to the corpus, showcase fidelity
0 lost / 0 changed, chart's 234 green.

---

## 1. The decision, and the instruction not to pre-empt it

`error.txt` is unusually firm:

> THIS IS AN OPEN DECISION, NOT A CONFIRMED BUG. The report repeatedly hedges:
> "may be intentional", "whether they're bugs depends on reference consistency".
> **Do not "fix" it before deciding.**

and specifies the test:

> for each renamed object, render the release with `helm template` and confirm
> EVERY reference resolves to an object that same release actually creates. If
> all resolve, this is cosmetic and belongs in the suppression list. If any
> dangles, it is a P0 — the pod will fail to mount and will not start.

**Verdict: BENIGN.** With a real collision present in the corpus:

```
dangling references (rename lost a reference): 0

INFO  CronJob/nightly-report reference at spec.jobTemplate.spec.template.spec
      .containers[0].envFrom[0].configMapRef.name follows a collision rename
      (raw='job-defaults' render='nightly-report-job-defaults') — target is
      rendered by this namespace, verified by the reference-integrity check
```

So the "internally consistent after rename" entry in the benign list is right,
and the "6 real issues" entry is wrong. **Nothing needed fixing in the rename
logic.** What needed fixing was that the question was unanswerable.

## 2. What was built

Two changes, both in `verify_raw_fidelity`.

### A. A reference-integrity pass

`collect_object_references()` walks a rendered object generically and returns
every by-name reference: `configMap.name`, `configMapRef.name`,
`configMapKeyRef.name`, `secret.secretName`, `secretRef.name`,
`secretKeyRef.name`, `serviceAccountName`, and a Route's `spec.to.name`.

Walked **generically rather than by following the values schema**, deliberately.
The point is to catch a reference site the *remapping* code does not know about,
and a checker that looked only where the remapper looks could never find one.
That is why it descends into sidecars and initContainers too, which
`remap_container_refs` handles through a separate path.

### B. A CHANGED downgrade, with a hard limit

A reference that CHANGED *because the object it names was renamed* is the same
fact as the rename, already reported as its own INFO line. `_fidelity_diff`
applies exactly this reasoning to a renamed object's own `metadata.name` and
says so in a comment; downstream references were simply never covered by it —
which is why the report saw 12 CHANGEDs and could not classify them.

The downgrade is deliberately narrow:

- fires only on a known reference path, so a CHANGED to a ConfigMap's **data**
  can never be mistaken for a rename (the UTF-8 corruption of folder 4 would
  still report);
- fires only when the new name is one this namespace actually renders.

## 3. The first version of the check was wrong, and the RED caught it

The first implementation flagged only references resolving to **nothing**. A
deliberately broken converter — `remap_envfrom` stubbed out so renames are not
followed — sailed straight through it:

```
$ python <scratch>/red12.py ... --verify-fidelity
  No fidelity issues found.
  dangling references (rename lost a reference): 0
```

Because after a collision rename, an un-remapped reference **still resolves** —
to the *other* microservice's object of the same name. `CronJob/nightly-report`
kept pointing at `job-defaults` while its own copy became
`nightly-report-job-defaults`, so it silently consumed `db-migrate`'s config.
The pod starts. It mounts the wrong values. Nothing reports it.

That is strictly more dangerous than a dangling reference, which at least fails
loudly — and it is the case `error.txt`'s own framing ("resolves to an object
that **same release** actually creates") points at, which I under-implemented on
the first pass.

Second version adds the release comparison, and the RED now fires:

```
dangling references (rename lost a reference): 1

ERROR  CronJob/nightly-report (release 'nightly-report') references
       ConfigMap/job-defaults at spec.jobTemplate...envFrom[0].configMapRef,
       which is rendered by a DIFFERENT release ('db-migrate') — this release
       declared its own ConfigMap/job-defaults in the raw input and it was
       renamed on collision, so this reference should have followed the rename
       and now resolves to another microservice's object
```

**Scoped so it cannot false-positive on legitimate cross-release references.**
A reference to the namespace-shared release's CA bundle, or to an
ESO-materialised Secret, resolves to another release (or to nothing) perfectly
legitimately. The rule only fires when the *raw input shows the referrer's own
source file declared an object of that kind and name* — i.e. it had its own
copy, that copy was renamed, and the reference did not follow.

## 4. Files changed

| File | Lines | Change |
|---|---|---|
| `universal/gitops-factory/convert_to_universal_chart.py` | 94 | `import ast` |
| `universal/gitops-factory/convert_to_universal_chart.py` | ~3251–3300 (new) | `_REFERENCE_PATH_TAILS`, `_is_followed_rename()`, `collect_object_references()` |
| `universal/gitops-factory/convert_to_universal_chart.py` | ~3590 | `dangling_refs` stat |
| `universal/gitops-factory/convert_to_universal_chart.py` | ~3753–3772 | CHANGED-reference downgrade |
| `universal/gitops-factory/convert_to_universal_chart.py` | ~3795–3840 (new) | the reference-integrity pass |
| `universal/gitops-factory/convert_to_universal_chart.py` | ~4085, ~4100 | report line + `problems` total |
| `universal/gitops-factory/namespaces-showcase/background-jobs/db-migrate.yaml` | 24–26, 55–72 | `envFrom` + `ConfigMap/job-defaults` |
| `universal/gitops-factory/namespaces-showcase/background-jobs/nightly-report.yaml` | 42–48, 119–131 | `envFrom` + a *differing* `ConfigMap/job-defaults` |
| `universal/gitops-factory/output-showcase/**` | regenerated | consequence |

### The corpus collision

Two jobs each shipping their own `job-defaults` ConfigMap is ordinary; that they
collide in one namespace is exactly what `NamespaceRegistry.claim` exists for.
The converter resolves it as designed:

```
[background-jobs/nightly-report] ConfigMap/job-defaults: name collision between
microservices 'db-migrate' and 'nightly-report' in namespace 'background-jobs'
(DIFFERING content) -> renamed 'nightly-report' copy to 'nightly-report-job-defaults'.
```

**Content deliberately differs** between the two copies. Identical copies would
make the rename a judgement call; differing ones make it unambiguously correct
*and* make a wrong reference observable, because the two objects carry different
values (`JOB_TIMEOUT_SECONDS: 300` vs `900`).

**Both owners reference it**, because a collision nobody references would
exercise the rename but not the reference-following — which is the half the
report was unsure about.

## 5. Tests created

17 tests in 3 groups:

1. **Every reference site is collected** (10) — the six `error.txt` names, plus
   `serviceAccountName` and a Route backend, parametrised one per site; sidecars
   and initContainers are walked; an object making no references yields none.
2. **The downgrade and its limits** (4) — a rename to a rendered name is
   downgraded; a rename to a name nothing renders is **not**; a changed *data*
   field is never treated as a rename; a malformed detail string is not
   downgraded.
3. **The corpus collision is real** (3) — two owners declare the name, their
   content differs, and both reference it.

Group 2 is the safety group: every one of those tests fails closed, so the
downgrade cannot become a way of hiding the defect it was built to classify.

## 6. Validation chain

| Check | Result |
|---|---|
| Corpus collision present | `job-defaults` declared by 2 microservices, differing content |
| Rename performed | `nightly-report-job-defaults`, warned |
| Reference followed | INFO line, `dangling references: 0` |
| RED (`remap_envfrom` stubbed) | **1 dangling** — resolves to another release's object |
| RED, first version of the check | 0 — the gap that forced the second version |
| Converter re-run, showcase | 128 objects matched, **no fidelity issues** |
| Folder's own tests | 17 passed |
| All `ERRORS_NEW_ANALYSIS` tests | 203 passed |
| `sanitize_fidelity_report` suite | 41 passed |
| `helm unittest Universal-chart` | 234 passed, 28 suites |

## 7. What could NOT be verified

- **The 12 real CHANGEDs** on `profiles-manager-yellow-b2b-{cluster}`,
  `rakia-zk-{*}` and `rakia-redis-eight-testing-redis`. Not in this repo. The
  check that would classify them now exists and runs on every conversion, so the
  next run over that input answers the question mechanically instead of
  producing another undecided report row.
- **References from OUTSIDE the release**, which `error.txt` flags as "the
  remaining risk". A reference held by an object this converter never sees — a
  hand-applied manifest, another team's chart — cannot be checked from here. The
  converter already warns about this in its collision message ("no action is
  needed beyond checking that nothing OUTSIDE these releases referred to
  ConfigMap/name by its original name"), and that remains a human step.
- **That the pod mounts correctly.** Cluster behaviour.

## 8. OBSERVED, DEFERRED

- **`Helm_Naming_Prefix_Collisions` (folder 10)** rested its "benign" reading on
  the same consistency claim. That claim is now backed by a running check rather
  than by inspection of one line.
- **`Extra_And_Relocated_Objects` (folder 14)** asks a related but distinct
  question — objects rendered by no release, and rendered with no raw source.
  The reference pass here does not answer it; that folder owns it.
- **The reference walk covers by-NAME references only.** Label selectors
  (NetworkPolicy podSelector, Service selector) are references too, and are not
  checked. Out of scope for this folder's question, and noted rather than
  attempted.

## 9. Reproducing

```powershell
python -m pytest universal/gitops-factory/output-showcase/ERRORS_NEW_ANALYSIS/Secret_ConfigMap_Rename_Reference_Inconsistency/test/test_reference_integrity.py -v

# watch the check catch an un-followed rename
python <scratch>/make_red12.py
python <scratch>/red12.py --input namespaces-showcase --output <tmp> `
  --chart ../Universal-chart --verify-fidelity --cluster-scoped
```
