# STATUS — Number_Serialization_Drift (TIER 3, folder 24/26)

**VERIFIED_AND_FIXED.**

**Result: NOT benign, NOT cosmetic — the report is wrong about this one.** It is
filed as *"Serialization issue"*, severity **Low**, on the grounds that `1e+09`
and `1000000000` are the same number. They are. But every field carrying this
value is **string-typed** in the Kubernetes API, so the rendered object is
**rejected by the API server's decoder** rather than merely printed differently.

Measured on the real render, before the fix:

```
REJECTED  ConfigMap/numdrift-config    data.maxBytes            is int (1000000000), schema requires string
REJECTED  Deployment/numdrift          env[MAX_BYTES].value     is int (1000000000), schema requires string
REJECTED  Deployment/numdrift          resources.limits.memory  is int (1000000000), schema requires string

3 field(s) would be rejected by the API server decoder.
```

25 new tests, showcase 0 lost / 0 changed, all `ERRORS_NEW_ANALYSIS` 402 passed,
`helm unittest` 234 passed, no new failures in the previous round.

---

## 1. The mechanism is not the one the report guesses

`error.txt` describes it as *"the raw manifest stores the value in scientific
notation as a YAML float. The pipeline parses it to a Python float and re-emits
it in decimal form."* Measured, that is not what happens — PyYAML never produces
a float at all:

```
raw manifest       memory: 1e+09        (unquoted)
PyYAML parses      '1e+09'              -> a STRING. Python type: str
values file        memory: 1e+09        -> emitted BARE, because PyYAML sees a string
Helm / Go yaml.v3  1e9                  -> a FLOAT
rendered manifest  memory: 1000000000   -> an INT where a string is required
```

**PyYAML resolves scalars with YAML 1.1 rules, which require a decimal point
before an exponent.** `1e+09` is therefore not a number to PyYAML, so its emitter
has no reason to quote it. Helm reads the values file with Go's yaml.v3, which
uses the **YAML 1.2** core schema, where `1e+09` *is* a float. The type is
changed by the handoff between two YAML versions — not by anything either side
does wrong on its own.

That also explains the neighbours that never drifted. `0.10`, `1.20` and
`9007199254740993` are numbers under **both** versions, so PyYAML's emitter has
always quoted them:

```
maxBytes: '1e+09'              <- needed the fix
ratio: '0.10'                  <- PyYAML already quoted these
version: '1.20'
SERIES_LIMIT: '9007199254740993'
```

## 2. Both of `error.txt`'s "check before closing" items resolve to "real bug"

| Check it asks for | Answer |
|---|---|
| **FLOAT vs INT TYPING** — "Confirm which field carries this value" | Three field types, all string-typed: `ConfigMap.data` (`map[string]string`), `EnvVar.value` (string), and a resource quantity (string-typed `Quantity`). All three are decoder errors, not coercions. |
| **PRECISION AT SCALE** — values past 2^53 | Not reachable here, and now unreachable by construction: the value never becomes a float on the Python side, and after the fix it never becomes one on the Go side either. `9007199254740993` (2^53+1) is in the corpus as the control case. |
| **ALSO CHECK: quantities must remain STRINGS** | This is exactly what was happening. `error.txt` calls that *"a real bug of the same family and far more damaging than this instance"* — it is not a different bug, it **is** this one. |

## 3. The fix, and why not the one `error.txt` recommends

`error.txt` proposes: *"normalize both sides before comparison in the fidelity
checker. The checker-side fix is cheaper and safer than changing how values are
emitted."*

That would have deleted the evidence and shipped the broken manifests. The
checker was reporting a true difference; the values file was the thing that was
wrong. The fix is in the emitter.

| File | Change |
|---|---|
| `convert_to_universal_chart.py` | `YAML12_ONLY_NUMERIC_RE` + `_represent_str_quoting_go_numerics` + `_ValuesDumper`; `dump_yaml`'s two `yaml.safe_dump` calls now use that dumper |
| `namespaces-showcase/platform-ops/shared.yaml` | ConfigMap fixture: `MAX_SAMPLE_BYTES: "1e+09"` plus the three-value control group |
| `namespaces-showcase/platform-ops/metrics-agent.yaml` | literal env var and an `ephemeral-storage: "2e+09"` quantity — the two other converter paths |

```python
YAML12_ONLY_NUMERIC_RE = re.compile(
    r"""^[-+]?(?:
          (?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)[eE][-+]?[0-9]+   # exponent form
        | 0o[0-7]+                                          # 1.2 octal
        )$""", re.VERBOSE)
```

**Scoped to the gap, not to strings in general.** Quoting every string would also
fix it and would be much worse — it would churn every values file in the repo and
reformat every script, certificate and `grafana.ini`. The representer touches
only scalars PyYAML would emit **bare** and Go would read as **numbers**.
`500m`, `64Mi`, `nginx` and `1.36-alpine` are asserted to stay bare; `0.10`,
`1.20` and the big integer are asserted to stay exactly as PyYAML already had
them.

**A subclassed dumper, not a global representer.** Registering on `SafeDumper`
would change YAML output for every other consumer in the process merely because
this module was imported.

## 4. Proof — RED

Purpose-built input through the whole pipeline, converter untouched:

```
    fields LOST                                  0
    fields CHANGED                               3

  ERROR  ConfigMap/numdrift-config CHANGED at data.maxBytes: raw='1e+09' render=1000000000
  ERROR  Deployment/numdrift CHANGED at spec.template.spec.containers[0].env[MAX_BYTES].value: raw='1e+09' render=1000000000
  ERROR  Deployment/numdrift CHANGED at spec.template.spec.containers[0].resources.limits.memory: raw='1e+09' render=1000000000
```

`raw='1e+09' render=1000000000` — the report's example, reproduced verbatim, on
all three paths. The generated values file showed the bare scalar that causes it:

```
  MAX_BYTES:
    value: 1e+09          <- bare
  BIG_INT:
    value: '9007199254740993'   <- quoted by PyYAML, never affected
```

Then `helm template` was run and the output type-checked offline (§ top of file):
3 fields rejected.

## 5. Proof — GREEN

```
    fields LOST                                  0
    fields CHANGED                               0
  OK - nothing lost, nothing changed.

    0 field(s) would be rejected by the API server decoder.
```

And in the showcase's own generated values, all three paths quoted:

```
    value: '1e+09'                       <- literal env var
    ephemeral-storage: '2e+09'           <- resource quantity
      MAX_SAMPLE_BYTES: '1e+09'          <- ConfigMap data
      RULE_SCHEMA_VERSION: '1.20'        <- control group, unchanged
      SCRAPE_RATIO: '0.10'
      SERIES_LIMIT: '9007199254740993'
```

Non-targeted output is byte-identical to the previous emitter, checked directly
by loading both modules and diffing their `dump_yaml` output — including the
multi-line case, which is the one a careless representer mangles:

```
OLD (pre-folder-24) == NEW  ->  True
```

## 6. Tests

```
python -m pytest output-showcase/ERRORS_NEW_ANALYSIS/Number_Serialization_Drift/test -q
25 passed
```

Three groups: the gap itself (six exponent/octal forms quoted; the control group
untouched; ordinary strings including `500m` and `64Mi` still bare; multi-line
round-trip; the UTF-8 folder's `allow_unicode` still in force); why it matters
(the three string-typed field types recorded with the measured rejections); and
the corpus (all three converter paths carry the scalar, the control group is
present, and no generated values file leaves a Go-numeric bare).

One test in this suite failed on first run — **its own premise was wrong, not the
code.** It asserted `dump_yaml` emits multi-line strings as literal blocks. It
never has: it emits single-quoted multi-line scalars, before and after this
change alike. The test now asserts the round-trip instead, and records the
byte-identical comparison. (The docstring on `dump_yaml` describing a "literal
block" is pre-existing and slightly inaccurate; left alone as another folder's
text.)

## 7. Validation chain

| Check | Result |
|---|---|
| Defect reproduced end-to-end | yes — 3 CHANGED, all three paths |
| Rendered output rejected before fix | yes — 3 fields, offline type check |
| Rendered output valid after fix | yes — 0 fields |
| Showcase fidelity | **0 lost / 0 changed / 0 render failures** |
| Non-targeted YAML output unchanged | yes — byte-identical to previous emitter |
| Folder's own tests | **25 passed** |
| All `ERRORS_NEW_ANALYSIS` tests | **402 passed** |
| `ERRORS_ANALYSIS` (previous round) | 12 failed / 320 passed — same 12 as before, none new |
| `helm unittest Universal-chart` | **234 passed**, 28 suites |

## 8. A repo-state change this folder caused, disclosed in full

Regenerating the committed `output-showcase/` was done **in `--cluster-scoped`
mode**, and the committed tree had been generated in the default namespace-scoped
mode. That left the directory holding both layouts at once, and the converter's
own stale-output detector caught it — one leftover file:

```
platform-ops-prd\values\shared.yaml
```

Repaired with the tool's own remedy, `--prune`, and re-verified:

```
Pruned 1 stale generated file(s) (0 release pointer(s))
Rendered 25 releases.
No duplicate resource ownership detected across any of the 25 rendered releases. Conflict-free.

    fields LOST 0 / fields CHANGED 0 / render failures 0
```

**The committed `output-showcase/` is now a cluster-scoped generation** — it
gained `cluster-shared/`, `base/platform-ops-prd-shared.yaml` and
`platform-ops-prd/values/platform-ops-prd-shared.yaml`, and lost
`platform-ops-prd/values/shared.yaml`. This is not cosmetic churn and it is not
this folder's defect: namespace-scoped mode **cannot** currently run on this
corpus, because two namespaces both claim `ClusterRole/log-agent-reader`, and
`run_all.py` itself prints `--cluster-scoped` as the remedy. The mode switch was
forced by the input, not chosen — but it was made here, so it is recorded here.

## 9. OBSERVED, DEFERRED

- **`ClusterRole/log-agent-reader` claimed by `platform-ops/log-agent` and
  `platform-ops-prd/log-agent`. RESOLVED — the showcase is cluster-scoped by
  intent.** The collision turned out to be **deliberate coverage** for the
  cluster-conflict detector, with a test asserting the exact message
  (`ERRORS_ANALYSIS/Missing_Objects_Not_Rendered_By_Any_Release`). Renaming
  either copy would have deleted that coverage, so neither was renamed. What was
  missing was the note saying so; `namespaces-showcase/platform-ops/log-agent.yaml`
  now carries it, along with the `--cluster-scoped` command that regenerates the
  committed output.
- **12 pre-existing `ERRORS_ANALYSIS` failures**, unchanged and untouched
  (attribution measured in folder 22 §7).
- **The 2-vs-6 count discrepancy** `error.txt` notes is unresolvable here; both
  numbers come from runs this repo does not have.

## 10. Reproducing

```powershell
cd c:\repos\homelab\universal\gitops-factory
python -m pytest output-showcase/ERRORS_NEW_ANALYSIS/Number_Serialization_Drift/test -v
python run_all.py --cluster-scoped --input namespaces-showcase --output <scratch>\out24
```

---

**VERIFIED_AND_FIXED**
