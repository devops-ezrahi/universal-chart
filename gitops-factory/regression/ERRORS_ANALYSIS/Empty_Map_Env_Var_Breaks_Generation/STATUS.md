# STATUS — Empty_Map_Env_Var_Breaks_Generation (P2, folder 27/35)

**Result: FIXED on BOTH sides, as the recorded decision required. The symptom is
worse than reported — `helm template` does not render a malformed env var, it
FAILS.** 10 tests, 4 RED→GREEN.

---

## 1. The open question, answered

error.txt records a question the original session asked and never got an answer
to:

> "Fix the data (converter side) ... or fix the template (chart side)? Which
> approach do you prefer? Or should I do both for defense in depth?"

Decision: **both**. Normalise in the converter so generated values files are
correct, and guard in the template so a hand-written values file cannot produce
broken YAML either. Neither half alone is enough — the converter cannot police
values people write by hand, and the template cannot make a wrong values file
right in the values file.

**Normalised to `value: ""`, never dropped.** error.txt offers three options
(filter out / normalize to null / normalize to `{value: ""}`) and names the
third. It is right: an env var explicitly set to the empty string is meaningful —
it is how a program is told to fall back to its own default, which is exactly
what Tyk does with `TYK_GW_ANALYTICSCONFIG_TYPE`. Removing the variable is not
equivalent to setting it empty, so the other two options change behaviour rather
than preserve it. `test_no_variable_is_dropped` pins the count.

## 2. One correction: this is not a cosmetic malformation

error.txt describes the result as *"a malformed or empty env entry"*. The RED
output is blunter — with the guard removed, `helm template` **fails**:

```
AssertionError: helm template failed:
```

three separate times. So an empty env var did not produce a slightly-wrong
manifest; it produced no manifest at all. That is a broken deploy, not a cosmetic
diff.

## 3. Files changed

| File | Change |
|---|---|
| `convert_to_universal_chart.py` `env_list_to_map` | an empty body or `value: {}` becomes `{"value": ""}` |
| `_helpers.tpl` `universal-chart.mapToList` | an entry with an empty body renders `value: ""` instead of a bare `{}` |

`mapToList` is shared with ports and volumeMounts, so the guard had to be
invisible to entries that legitimately carry a body —
`test_the_guard_does_not_disturb_ports` pins that.

## 4. Grep proof

```
env value == {} : 0
env value == "" : 0
```

Zero in this repo, so the generated output is unchanged and the fix is
defensive. The report's two named files
(`values/bluetorch*/gateway-tyk-gateway-dev-tst.yaml`) belong to the original
project. `test_this_repo_has_no_empty_env_values` re-runs the scan every time.

## 5. Tests

`Empty_Map_Env_Var_Breaks_Generation/test/` — 10 tests. Fixture derives from
`namespaces-showcase/shop-web/api-gateway.yaml` with the three shapes that
reduce to an empty body:

| env var | raw form |
|---|---|
| `GW_ANALYTICS_TYPE` | `value: ""` — what the source almost certainly had |
| `GW_EMPTY_MAP` | `value: {}` — the corrupted form the report saw |
| `GW_BARE` | a name with nothing under it |
| `GW_REAL` | control — must be untouched |

### RED

```
AssertionError: GW_EMPTY_MAP -> {'value': {}}
AssertionError: helm template failed:      (x3)
```

### GREEN

```
10 passed
```

`test_the_rendered_env_is_valid_for_kubernetes` asserts the invariant rather
than the symptom: every rendered entry has a name and **exactly one** of
`value` / `valueFrom`.

## 6. Validation chain

| Step | Result |
|---|---|
| Full showcase converter run | `122 objects matched (28 relocated). No fidelity issues found.` |
| Fidelity before / after | 0 lost / 0 changed, unchanged |
| Generated-output churn | none — 0 occurrences |
| `helm lint` | 0 failed |
| Chart's own suite | 234 passed |

## 7. What could NOT be verified without a live cluster

- **That Tyk treats `""` as "use the default"** — that is application
  behaviour, and it is the reason `value: ""` was chosen over dropping. Taken
  from error.txt's own reasoning, not observed.
- **That the API server accepts `value: ""`.** It does per the PodSpec schema,
  but that is documentation.

## 8. OBSERVED, DEFERRED

- **`envfrom_list_to_map` has no equivalent guard.** An `envFrom` entry with an
  empty body would render `- configMapRef:` with no name. It cannot arise from
  this converter, but the same hand-written-values argument applies. No folder
  covers it.
- **The root cause is upstream and unfixable from here.** error.txt's reading —
  that the raw manifest had `value: ""` and something turned the empty scalar
  into an empty map — is almost certainly right, and that conversion happened
  before these manifests were exported. Both guards treat the symptom.
- Earlier folders' deferred items still stand.

## 9. Reproducing

```powershell
python -m pytest universal/gitops-factory/output-showcase/ERRORS_ANALYSIS/Empty_Map_Env_Var_Breaks_Generation/test -v
```
