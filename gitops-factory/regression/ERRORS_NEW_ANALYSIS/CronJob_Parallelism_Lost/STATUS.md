# STATUS — CronJob_Parallelism_Lost (TIER 2, folder 16/26)

**Result: FIXED — field captured rather than value judged.** The report's own
P8 rating rests on the value it happened to observe being the Kubernetes
default, and it asks for that assumption to be checked against manifests this
repo does not have. Capturing the field costs one line per handler and removes
the question. `completions` captured alongside, because it is the same field's
other half and leaving it out would recreate this folder. 19 new tests
RED→GREEN, showcase fidelity 0 lost / 0 changed, chart's 234 green, lint clean.

---

## 1. Why a P8 item was fixed rather than closed

`error.txt` rates it lowest and says why:

> Technically this is the K8s default, so fixing it is optional. [...] Low
> priority — 1 is the default.

and then, in the same file, undercuts its own rating:

> **VERIFY BEFORE CLOSING** [...] confirm no CronJob anywhere sets parallelism to
> something other than 1. A parallelism of 2+ that silently drops to 1 **halves
> throughput on a batch job** and would be a real regression. The report only
> ever cites 1.

That verification is impossible from here — the manifests are not in this repo.
The choice is between accepting an unverifiable assumption and spending one line
per handler. The line is cheaper than the assumption, and it is the same
judgement folder 7 reached about `minReadySeconds`: **the rating describes the
value that was observed, not the field.**

## 2. `completions` came along, deliberately

It was in neither whitelist, is the other half of the same Job parallelism
semantics, and has an identical failure mode. Leaving it out would have
recreated this exact folder for whoever reads the next report.

This is RC-4 from `README_REGRESSION_ANALYSIS.md` — under an allow-list, *"we
fixed field X"* never generalises to field Y — and the cheapest moment to break
that pattern is while already editing the list. Four folders in this batch
(7, 9, 16, and 20/21 ahead) are the same shape.

## 3. Files changed

| File | Lines | Change |
|---|---|---|
| `universal/gitops-factory/convert_to_universal_chart.py` | ~2099–2113 | `handle_cronjob`: `parallelism`, `completions` added to the `jobTemplate.spec` list |
| `universal/gitops-factory/convert_to_universal_chart.py` | ~2207–2216 | `handle_job`: same list, kept in step |
| `universal/Universal-chart/templates/cronjob.yaml` | 50–58 (new) | render both via `hasKey` |
| `universal/Universal-chart/templates/job.yaml` | 31–38 (new) | render both via `hasKey` |
| `universal/gitops-factory/namespaces-showcase/background-jobs/db-migrate.yaml` | 6–17 | `parallelism: 4`, `completions: 4` |
| `universal/gitops-factory/namespaces-showcase/background-jobs/nightly-report.yaml` | 12–17 | `parallelism: 2` on the jobTemplate |
| `universal/gitops-factory/output-showcase/**` | regenerated | consequence |

### `hasKey`, not `with`

`parallelism: 0` is a legal way to suspend a Job, and `with` treats `0` as
empty. `backoffLimit` a few lines above already carries a comment about exactly
this trap (`backoffLimit: 0` means "never retry"), so the guard is consistent
with what the file already knows. Same trap as folder 7's `minReadySeconds` and
folder 9's `allocateLoadBalancerNodePorts: false`. Pinned by
`test_an_explicit_zero_survives` and by parametrising the template tests over
`0`.

### Both handlers, kept in step

`handle_cronjob` and `handle_job` describe the same spec, and they have drifted
before — folder 5 found pod-template labels captured for workloads and for
neither of these. `test_the_two_handlers_capture_the_same_field_set` reads the
source and asserts both whitelists carry the same fields, so the next divergence
fails a test instead of becoming folder 27.

## 4. RED

```
$ python <scratch>/red16.py --input namespaces-showcase --output <tmp> \
    --chart ../Universal-chart --verify-fidelity --cluster-scoped
  3 potential fidelity issue(s)

  field_lost_parallelism  2
  field_lost_completions  1

  ERROR  CronJob/nightly-report LOST at spec.jobTemplate.spec.parallelism: raw had 2
  ERROR  Job/db-migrate LOST at spec.parallelism: raw had 4
  ERROR  Job/db-migrate LOST at spec.completions: raw had 4
```

Both code paths, both kinds — which is what makes the "kept in step" assertion
more than a style preference.

## 5. GREEN

```
  128 objects matched (30 legitimately relocated).
  No fidelity issues found.
```

```yaml
kind: Job
  backoffLimit: 1
  parallelism: 4
  completions: 4
```

```
$ python -m pytest .../CronJob_Parallelism_Lost/test/test_parallelism.py -q
...................                                                      [100%]
19 passed in 2.67s
```

## 6. Tests created

19 tests in 3 groups:

**Converter** (10) — parallelism captured at 1/2/4/16 on a CronJob and 1/4 on a
Job; `completions` captured; an explicit `0` survives; absent stays absent; and
the two handlers' field lists are asserted identical.

**Template** (7) — both kinds render both fields, parametrised over `0` so the
`hasKey` guard is exercised rather than assumed; omitted when unset.

**Corpus** (2) — the corpus uses a **non-default** value, and both a Job and a
CronJob carry one.

`test_the_corpus_uses_a_NON_default_parallelism` is `error.txt`'s own reasoning
turned into a test: a corpus entry of `1` would prove nothing, because losing
the default is undetectable — which is exactly why the report rated this P8 and
exactly why that rating could not be checked from the report.

## 7. Validation chain

| Check | Result |
|---|---|
| RED | 3 errors across both handlers |
| Converter re-run, showcase | 128 objects matched, **no fidelity issues** |
| `helm template` | `parallelism: 4`, `completions: 4` |
| Folder's own tests | 19 passed |
| All `ERRORS_NEW_ANALYSIS` tests | 268 passed |
| `helm unittest Universal-chart` | 234 passed, 28 suites |
| `helm lint Universal-chart` | 0 failed |

## 8. What could NOT be verified

- **Whether any real CronJob sets parallelism above 1.** `error.txt` asks; the
  manifests are not here. The fix makes the answer irrelevant, which is why it
  was preferred to answering it.
- **The 2 Jaeger CronJobs.** Not in this repo.

## 9. OBSERVED, DEFERRED

- **`K8s_Runtime_Defaults_Benign_Suppress` (folder 17)** lists `parallelism (2)`
  in its suppression list. That entry is now moot — the field is carried, so
  there is nothing to suppress. It joins `minReadySeconds`, `VCT labels` and
  `allocateLoadBalancerNodePorts` on the list of entries folder 17 must drop.
  Recorded there; not edited here.
- **`error.txt` warns about the grouped CronJob suppression** — "that same bucket
  also lists 'annotations, labels', which are NOT inert (see
  CronJob_Pod_Template_Labels_Lost)". Already handled in folder 5. Both are now
  reasons the CronJob defaults bucket cannot be applied wholesale.

## 10. Reproducing

```powershell
python -m pytest universal/gitops-factory/output-showcase/ERRORS_NEW_ANALYSIS/CronJob_Parallelism_Lost/test/test_parallelism.py -v
```
