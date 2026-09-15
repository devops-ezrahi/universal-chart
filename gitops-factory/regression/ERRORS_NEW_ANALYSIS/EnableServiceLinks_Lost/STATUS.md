# STATUS — EnableServiceLinks_Lost (TIER 3, folder 20/26)

**Result: PRESERVED, not suppressed — the report's stated reason is backwards.**
Rated benign in every pass on the grounds that "false is the K8s default". It is
not: the Kubernetes default is **true**, so an explicit `false` is a deliberate
opt-out and dropping it re-injects environment variables the source manifest
switched off. 13 new tests RED→GREEN, showcase fidelity 0 lost / 0 changed,
chart's 234 green.

---

## 1. The correction

The report, three times over:

> Raw had False. **false is the K8s default, omitted by chart. Benign.**

`error.txt` catches it:

> ONE FACTUAL CORRECTION WORTH KNOWING, because it inverts the usual reasoning:
> the Kubernetes default for `enableServiceLinks` is **TRUE**, not false. An
> explicit `enableServiceLinks: false` is therefore a DELIBERATE OPT-OUT [...]
> **Do not let the wrong premise carry the decision.**

It also attaches a condition to the verdict:

> VERIFY BEFORE SUPPRESSING: for the affected workloads, check whether any
> container env var name could collide with a `{SERVICE}_SERVICE_HOST/_PORT`
> pattern from a Service in the same namespace. If one does, re-enabling service
> links overrides the application's own configuration at runtime — a real and
> very hard-to-diagnose bug.

That check needs the affected manifests, which are not in this repo. Carrying the
field costs one line per side and makes the question unnecessary — the same
trade folder 7 made for `minReadySeconds`, and for the same reason: **the
report's rating describes the value it happened to see, not the field.**

## 2. Why the mistake was easy to make

`enableServiceLinks` sits in the pod spec beside `hostNetwork`, `hostIPC`,
`hostPID` and `shareProcessNamespace` — four booleans whose Kubernetes default
**is** `false`. For all four, rendering only when true is correct and loses
nothing. `enableServiceLinks` looks identical and behaves oppositely.

The chart had inherited that assumption: `_pod-spec.tpl` renders the others with
a plain `{{- if }}`, which for this field would drop precisely the value that
means something. So the fix is `hasKey`, and the two rules now sit next to each
other with a comment saying why they differ.

**What service links do**, since the whole verdict turns on it: Kubernetes
injects a `{SVCNAME}_SERVICE_HOST` / `{SVCNAME}_SERVICE_PORT` pair for every
Service in the namespace. Most applications ignore them. Teams disable them for
env bloat in Service-dense namespaces, and because an injected name can collide
with the application's own variable and silently override it.

## 3. Files changed

| File | Lines | Change |
|---|---|---|
| `universal/gitops-factory/convert_to_universal_chart.py` | ~1112–1128 | `enableServiceLinks` added to the pod-spec scalar whitelist |
| `universal/Universal-chart/templates/_pod-spec.tpl` | 37–48 (new) | render via `hasKey`, with the reason it differs from its neighbours |
| `universal/gitops-factory/namespaces-showcase/identity-auth-prd/opa-prd.yaml` | 17–28 | `enableServiceLinks: false` **and** `shareProcessNamespace: false` |
| `universal/gitops-factory/output-showcase/**` | regenerated | consequence |

**Both booleans in one pod spec, deliberately.** The report bucketed them
together and their defaults are opposite. Having them side by side in the corpus
is what stops the two verdicts being confused again — and it leaves folder 21's
item live and visible rather than pre-decided.

## 4. RED → GREEN

Before, on the corpus with both fields present:

```
field_lost_enableServiceLinks     1
field_lost_shareProcessNamespace  1
```

After:

```
field_lost_shareProcessNamespace  1     <- folder 21's, deliberately untouched
```

`enableServiceLinks: false` now survives converter and chart.

## 5. Tests created

13 tests in 4 groups:

1. **Converter** (4) — explicit `false` captured (the one that matters); explicit
   `true` captured; absent stays absent; captured alongside
   `shareProcessNamespace` without conflating the two.
2. **Template** (6) — an explicit `false` renders; `true` renders; omitted when
   unset; renders on StatefulSet and DaemonSet too (shared `_pod-spec.tpl`,
   asserted rather than assumed); **and the neighbouring booleans still render
   only when true**, because their default really is false and changing them
   would add three invented fields to every pod.
3. **Not suppressed** (2) — absent from `_is_benign_default_loss`, and a genuine
   loss still reports.
4. **Corpus** (1) — a workload declares an explicit `false`.

Group 2's last test is the one that keeps the fix honest: the point is not "use
`hasKey` everywhere", it is that two adjacent rules are both correct for
different fields.

## 6. Validation chain

| Check | Result |
|---|---|
| Pre-fix, corpus | `field_lost_enableServiceLinks 1` |
| Post-fix | absent; only folder 21's item remains |
| Folder's own tests | 13 passed |
| `helm unittest Universal-chart` | 234 passed, 28 suites |

## 7. What could NOT be verified

- **The env-var collision check `error.txt` asks for.** It needs the affected
  workloads (`observability-jaeger-collector`, `observability-jaeger-query`) and
  the list of Services in their namespace. Not available here. The fix removes
  the need for the answer rather than supplying it.
- **How many occurrences there are.** The report gives 3 in one pass and 12 in
  another.

## 8. OBSERVED, DEFERRED

- **`ShareProcessNamespace_Lost` (folder 21)** is next and is deliberately still
  reporting. Its default genuinely is `false`, so its verdict is likely the
  opposite of this one — which is exactly why folder 17 refused to decide either
  from inside a bulk rule.
- **`K8s_Runtime_Defaults_Benign_Suppress` (folder 17)** listed
  `enableServiceLinks (3)` for suppression. That entry should now be struck, for
  the same reason as `minReadySeconds`, `VCT labels`,
  `allocateLoadBalancerNodePorts` and `parallelism` before it — five of the
  ladder's entries turned out to be real losses.

## 9. Reproducing

```powershell
python -m pytest universal/gitops-factory/output-showcase/ERRORS_NEW_ANALYSIS/EnableServiceLinks_Lost/test/test_enable_service_links.py -v
```
