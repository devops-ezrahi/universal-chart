# STATUS — Sidecar_Container_Name_Key_Collision (P0, folder 4/35)

**Result: DOES NOT REPRODUCE. No code changed.** Both of error.txt's claims were
tested directly against this repo's converter and chart with raw-vs-rendered
proof, and both fail to reproduce. 7 regression tests added, and a **mutation
test** proves those tests would catch the defect if it were real.

This folder is *not* on the "suspected false positives" list, so I did not
assume it — I built the fixture the finding describes and ran it.

---

## 1. What error.txt claims, split in two

The file makes two distinct claims that need separating, because they have
different answers:

- **Claim A** — *"The chart template only renders the main container. Sidecars
  (oauth-proxy, topic-operator, grafana, kafbat, jaeger-query, prometheus,
  redis, sentinel, kafka-exporter) are completely missing. This is the biggest
  functional gap."*
- **Claim B** — *"handle_workload ... stores them in ctx.values["containers"]
  [container_name]. When multiple microservices share the same base name (e.g.
  juseppelad-2-441 and juseppelad-2-611 both store into containers['juseppelad']),
  the second overwrites the first."*

## 2. Hint verification — and where error.txt is factually wrong about this chart

| Hint | Reality in this repo |
|---|---|
| `_pod-spec.tpl` lines **79–85**, "container render loop, main + sidecars" | **Exact line numbers**, but the content refutes Claim A: lines 82–85 are a `range` over `.Values.sidecars` that renders every one. |
| sidecar capture at lines **761–766** | Correct location (now 772-ish after folders 1–3): `v.setdefault("sidecars", {})[extra["name"]] = entry`. |
| `ctx.values["containers"][container_name]` | **No such key exists.** The converter writes `ctx.values["sidecars"]`, and the chart reads `.Values.sidecars`. error.txt's own "RELATED CODE" quotes the real line, so the `containers[...]` phrasing is a description error in the report, not a second code path. |

Here is the loop error.txt says does not exist
([_pod-spec.tpl:79-85](../../../../Universal-chart/templates/_pod-spec.tpl#L79-L85)):

```gotemplate
containers:
  {{- include "universal-chart.mainContainer" . | nindent 2 }}
  {{- /* ── Sidecar containers (map-based) ──────────── */}}
  {{- range $name, $spec := .Values.sidecars }}
  - name: {{ $name }}
    {{- toYaml $spec | nindent 4 }}
  {{- end }}
```

**Claim A is refuted by inspection and by test.**

## 3. Claim B tested properly — the base-sharing mechanism is real, the collision is not

Claim B is not about one pod. Container names within a pod are unique by
Kubernetes API rule, so nothing can collide there. It is about the converter's
**base-file sharing**: microservices are grouped by file name across namespaces
([convert_to_universal_chart.py:3037-3061](../../../convert_to_universal_chart.py#L3037-L3061)),
and a group with more than one member gets **one shared `base/<name>.yaml`**.

That mechanism genuinely exists, and it genuinely fires in this repo — `shared`
appears in 5 namespaces under `namespaces/` and 2 under `namespaces-showcase/`.
So the premise is sound and worth testing.

I built exactly the scenario: two namespaces, same microservice file name
(`api-gateway`), deliberately different sidecar sets.

| | shop-web | shop-web-eu |
|---|---|---|
| `traefik` (main) | yes | yes |
| `oauth-proxy` | **only here** | – |
| `log-shipper` | yes, byte-identical | yes, byte-identical |
| `rate-limiter` | – | **only here** |

Both resolve onto one `base/api-gateway.yaml` (asserted as a precondition by
`test_both_namespaces_share_one_base_file` — otherwise the test would prove
nothing).

### Raw vs rendered — the proof

```
== 2. confirm both namespaces really do share one base file
   shared base: ...\base\api-gateway.yaml

== 3. namespace: shop-web
   RAW      containers: ['traefik', 'oauth-proxy', 'log-shipper']
   RENDERED containers: ['api-gateway', 'log-shipper', 'oauth-proxy']
   OK: all 2 sidecars present, none leaked from the other namespace

== 3. namespace: shop-web-eu
   RAW      containers: ['traefik', 'log-shipper', 'rate-limiter']
   RENDERED containers: ['api-gateway', 'log-shipper', 'rate-limiter']
   OK: all 2 sidecars present, none leaked from the other namespace

PASS: no sidecar loss and no cross-namespace leak.
```

Nothing lost, nothing leaked. (`traefik` → `api-gateway` is the *separate*
main-container rename, folder 5.)

**Why it holds.** `common_subtree` hoists into `base/` only what is present with
an *identical* value in **every** member of the group; `subtract_defaults` then
gives each namespace its own remainder. A sidecar that differs, or that only one
namespace has, therefore never reaches the shared base — it stays in that
namespace's own `values/<slug>.yaml`. Deep-merge only ever *adds* on top, so a
namespace cannot inherit a sidecar it never declared.

**Claim B is refuted by execution.**

## 4. Mutation test — proof these tests are not vacuous

A suite that was green before any change proves nothing on its own, so I broke
the chart on purpose: deleted the `range .Values.sidecars` loop from
`_pod-spec.tpl` and re-ran.

```
MUTATION APPLIED: sidecar render loop removed from _pod-spec.tpl

F..FFF.                                                                  [100%]
AssertionError: chart dropped sidecars; rendered containers = ['api-gateway']
AssertionError: shop-web: sidecars declared in raw but ABSENT from render:
    ['log-shipper', 'oauth-proxy']; rendered = ['api-gateway']
AssertionError: shop-web-eu: sidecars declared in raw but ABSENT from render:
    ['log-shipper', 'rate-limiter']; rendered = ['api-gateway']
AssertionError: both namespaces rendered the same container set (['api-gateway'])
    — one namespace's sidecars overwrote the other's
4 failed, 3 passed
```

That failure output is *exactly* error.txt's reported symptom — "containers
present in raw, absent from render". So if this defect existed here, these tests
would report it in those words.

Reverted immediately and verified byte-identical:

```
MUTATION REVERTED
=== git diff for _pod-spec.tpl (must be empty) ===
(clean - file restored byte-identical)
7 passed in 2.66s
```

## 5. Files changed

**None.** No converter, chart or checker change was warranted.

Test artifacts added under
`ERRORS_ANALYSIS/Sidecar_Container_Name_Key_Collision/test/`:

- `fixture/shop-web/api-gateway.yaml` + `fixture/shop-web-eu/api-gateway.yaml` —
  same file name in two namespaces, overlapping-but-different sidecar sets.
  Derived from the real `namespaces-showcase/shop-web/api-gateway.yaml`.
- `test_sidecar_collision.py` — 7 tests.
- `render_check.ps1` — prints the raw-vs-rendered container list per namespace.

## 6. Validation chain

| Step | Result |
|---|---|
| Converter re-run | `122 objects matched (28 relocated). No fidelity issues found.` |
| `helm template` per namespace | see §3 |
| Raw vs rendered, actual values | see §3 |
| Fidelity before/after | unchanged, 0 lost / 0 changed; no count moved |
| Generated-output diff | none from this folder (no code changed) |
| `helm lint` | `1 chart(s) linted, 0 chart(s) failed` |
| Chart's own suite | `28 suites, 234 tests passed` |
| Accumulated suite (folders 1–4) | **39 passed** |

## 7. Why the report likely says otherwise

The chart error.txt describes — one that renders only the main container — is
not this chart. The most economical explanation is that the finding is real but
**stale**: it was captured against an earlier Universal-chart, and the sidecar
loop at `_pod-spec.tpl:82-85` is the fix that has since landed. `_INDEX.txt`
notes the analysis was transcribed from screenshots of an older session.

I cannot confirm that from this repo alone — there is no chart history here to
diff against — so I am reporting **what is true now**, with the evidence, rather
than asserting why it changed.

## 8. What could NOT be verified without a live cluster

- **That the sidecars actually start and serve.** The rendered pod spec contains
  them with the right images, args and ports; whether oauth-proxy proxies is
  runtime behaviour.
- **The nine specific sidecars error.txt names** (oauth-proxy, topic-operator,
  grafana, kafbat, jaeger-query, prometheus, redis, sentinel, kafka-exporter) —
  none of those manifests exist in this repo. I reproduced the *shape* of the
  problem, not those exact workloads.
- **That no other input shape triggers a collision.** I tested the mechanism
  error.txt names (cross-namespace base sharing) plus the in-pod duplicate case.
  A collision via some third path is not excluded — only these two are.

## 9. This repo's data does not exercise sidecars at all

A full survey of both input trees:

```
===== namespaces-showcase =====
  MULTI-CONTAINER pods: NONE
  microservice names in >1 namespace: {'shared-prd': [...], 'shared': [...]}
  files declaring >1 workload: NONE
===== namespaces =====
  MULTI-CONTAINER pods: NONE
  microservice names in >1 namespace: {'shared': [5 namespaces]}
  files declaring >1 workload: NONE
```

Not one multi-container pod in either tree, which is why the committed fidelity
report shows 0 and why a fixture was mandatory here. Raw manifests were **not**
modified.

## 10. OBSERVED, DEFERRED

- **The bare container name IS still a dict key for sidecars** — line ~772,
  `v.setdefault("sidecars", {})[extra["name"]] = entry`, plus the CronJob (1279)
  and Job (1335) equivalents. Unlike volumeMounts (folder 3), this is safe today
  *only* because Kubernetes forbids duplicate container names in a pod, so the
  API server rejects the colliding input before the converter's behaviour
  matters. `test_two_sidecars_with_the_same_name_in_one_pod_do_not_silently_merge`
  documents the current last-one-wins behaviour so a future change is deliberate.
  Left alone.
- **`Main_Container_Name_Forced_To_Release_Name` (folder 5, next, STOP-AND-ASK)** —
  visible in every render above as `traefik` → `api-gateway`. Confirmed but not
  touched. error.txt for this folder explicitly flags the two as distinct
  defects that both surface as "container X missing", which my tests keep
  separate by comparing sidecars exactly and only requiring the main container
  to exist.
- Folders 1–3 deferred items still stand.

## 11. Reproducing

```powershell
python -m pytest universal/gitops-factory/output-showcase/ERRORS_ANALYSIS/Sidecar_Container_Name_Key_Collision/test/test_sidecar_collision.py -v
& universal/gitops-factory/output-showcase/ERRORS_ANALYSIS/Sidecar_Container_Name_Key_Collision/test/render_check.ps1
```
