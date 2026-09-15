# STATUS — VolumeMount_SubPath_Lost_Duplicate_Name_Collision (P0, folder 3/35)

**Result: FIXED.** Three-sided fix (converter + `mapToList` + `validateVolumeMounts`),
10 new tests RED→GREEN, folders 1–2 still green, chart's own 234 tests still green,
fidelity unchanged at 0 lost / 0 changed.

---

## 1. Were the error.txt hints accurate?

Mostly, with two small drifts. Verified before trusting:

| Hint | Reality |
|---|---|
| `named_list_to_map` at lines **514–518** | Actually **519–524**. 5-line drift; function correct. |
| volumeMounts call site at line **653** | Actually **659**. 6-line drift; call site correct. |
| `_pod-spec.tpl` lines **193–196** | **Exact.** |
| `_helpers.tpl` line **145** (`mapToList`) | **Exact** (before my edit; now 152). |

error.txt's diagnosis is exactly right, and its "the fix is TWO-SIDED" note
undercounts by one — see §3.

**It also undercounts the call sites.** error.txt discusses only the main
container. `named_list_to_map` is called for volumeMounts in **three** places:

- line 659 — main container (`handle_main_container`)
- line 1273 — CronJob job template (`handle_cronjob`)
- line 1329 — Job (`handle_job`)

Fixing only the first would have left identical, silent data loss on every
CronJob and Job. The fix is inside the shared helper, so all three are covered
at once; `test_cronjob_and_job_volume_mounts_also_survive_duplicate_names` pins
that.

The other three call sites (lines 811/1287/1343) pass **volumes**, whose names
are unique by Kubernetes API rule, so the duplicate branch never fires there.

## 2. Files changed

| File | Line | Change |
|---|---|---|
| `convert_to_universal_chart.py` | 101 | `from collections import Counter, defaultdict` |
| `convert_to_universal_chart.py` | 519–557 | `named_list_to_map`: composite key + explicit `name` for duplicated names |
| `Universal-chart/templates/_helpers.tpl` | 154 | `mapToList`: `- name: {{ $spec.name \| default $name }}` |
| `Universal-chart/templates/_helpers.tpl` | 221–224 | `validateVolumeMounts`: resolve `$mountSpec.name \| default $mountKey` |

## 3. The fix is THREE-sided, not two

error.txt calls for a composite key in the converter and composite-key parsing
in `mapToList`. Doing exactly that produces a chart that **fails to render at
all**, because a third site also keys off the map key:
`universal-chart.validateVolumeMounts` (`_helpers.tpl:221`) checks every mount
name against the declared volumes and calls `fail` when one is missing. Given a
key of `nginx-conf:nginx.conf`, it looks for a volume literally named
`nginx-conf:nginx.conf`, finds none, and aborts the render with
*"volumeMount ... is defined, but volume ... is missing"*.

`test_validate_volume_mounts_still_resolves_duplicated_names` covers this, and
`test_validate_volume_mounts_still_catches_a_genuinely_missing_volume` proves the
guard is still armed rather than defanged.

### Why composite key + explicit `name`, rather than parsing the key

error.txt suggests the template "must parse the composite key back into name +
mountPath". I did not do that — string-splitting in Go templates is brittle, and
mountPaths contain the separator characters. Instead the entry carries an
explicit `name` field and the key is purely an identifier.

This fits what the chart already did: `mapToList` already read
`toYaml (omit $spec "name")`, i.e. it already anticipated a `name` key inside
the spec. The change is a one-token edit (`$name` → `$spec.name | default $name`).

Key format is `<name>:<subPath>`, falling back to `<name>:<mountPath>` when
there is no subPath, plus a `#2` counter for the degenerate case of an identical
name *and* discriminator.

**Unique names are untouched** — bare name as key, no `name` field, so existing
values files and any GitOps override addressing `volumeMounts.<name>` keep
working. `test_named_list_to_map_leaves_unique_names_as_plain_keys` pins that,
and the zero-diff regeneration in §5 confirms it across the whole showcase.

## 4. Tests created

`ERRORS_ANALYSIS/VolumeMount_SubPath_Lost_Duplicate_Name_Collision/test/`

- `fixture/shop-web/storefront.yaml` — from the real
  `namespaces-showcase/shop-web/storefront.yaml`, with `nginx-conf` mounted
  three times on different subPaths plus one unique `site` mount as a control.
- `test_volumemount_subpath.py` — 10 tests across 6 layers (helper, converter,
  template render, validator both ways, end-to-end CLI, fidelity checker).
- `render_check.ps1` — converter → `helm template` → **real YAML parse**
  comparing the mount set against the raw manifest, plus a check that no
  composite key leaked through as a `name`.

### RED — before the fix

```
FAILED test_named_list_to_map_does_not_drop_duplicate_names
FAILED test_named_list_to_map_preserves_every_mountpath_and_subpath
FAILED test_converter_keeps_all_four_mounts_on_the_main_container
FAILED test_template_renders_every_mount_with_its_real_volume_name
FAILED test_round_trip_all_subpath_mounts_survive
FAILED test_cronjob_and_job_volume_mounts_also_survive_duplicate_names
========================= 6 failed, 4 passed in 2.98s =========================
```

The defect in one line:

```
AssertionError: named_list_to_map collapsed 4 volumeMounts into 2 (['nginx-conf', 'site'])
  — mounts sharing a volume name overwrote each other
AssertionError: handle_main_container kept 2 of 4 volumeMounts: ['nginx-conf', 'site']
```

Two of three nginx config files silently gone.

### GREEN — after the fix

```
$ python -m pytest test_volumemount_subpath.py -q
..........                                                               [100%]
10 passed in 2.78s
```

### Accumulated regression suite (folders 1–3)

```
$ python -m pytest universal/gitops-factory/output-showcase/ERRORS_ANALYSIS -q
................................                                         [100%]
32 passed in 7.70s
```

## 5. Validation chain

**1. Converter re-run:** `122 objects matched (28 legitimately relocated). No fidelity issues found.`

**2–3. `helm template` + side-by-side, mount for mount** (`render_check.ps1`):

Generated values — note the composite keys and the recovered `name` field:

```yaml
volumeMounts:
  nginx-conf:default.conf:
    mountPath: /etc/nginx/conf.d/default.conf
    name: nginx-conf
    subPath: default.conf
  nginx-conf:mime.types:
    mountPath: /etc/nginx/mime.types
    name: nginx-conf
    subPath: mime.types
  nginx-conf:nginx.conf:
    mountPath: /etc/nginx/nginx.conf
    name: nginx-conf
    subPath: nginx.conf
  site:
    mountPath: /usr/share/nginx/html
    readOnly: true
```

Raw vs rendered:

```
   RAW mounts:
     name=nginx-conf   mountPath=/etc/nginx/conf.d/default.conf   subPath=default.conf
     name=nginx-conf   mountPath=/etc/nginx/mime.types            subPath=mime.types
     name=nginx-conf   mountPath=/etc/nginx/nginx.conf            subPath=nginx.conf
     name=site         mountPath=/usr/share/nginx/html            subPath=None
   RENDERED mounts:
     name=nginx-conf   mountPath=/etc/nginx/conf.d/default.conf   subPath=default.conf
     name=nginx-conf   mountPath=/etc/nginx/mime.types            subPath=mime.types
     name=nginx-conf   mountPath=/etc/nginx/nginx.conf            subPath=nginx.conf
     name=site         mountPath=/usr/share/nginx/html            subPath=None
   OK: all 4 mounts round-tripped exactly
   OK: every mount names a declared volume (['nginx-conf', 'site'])
```

Before the fix, RENDERED had **two** entries and the two nginx config files were gone.

**4. Fidelity report before vs after: unchanged**, 0 lost / 0 changed. No count
moved in either direction.

**5. Generated-output diff:** the only lines that differ from the committed tree
are still folder 2's two `serviceName:` additions. This fix changed **nothing**
in the showcase output — no showcase manifest mounts a volume twice — which is
the intended proof that unique-name mounts kept their plain keys.

**6. `helm lint`:** `1 chart(s) linted, 0 chart(s) failed`.

**7. Chart's own suite:** `Test Suites: 28 passed | Tests: 234 passed`. This is
the load-bearing one for this fix: `mapToList` renders `ports`, `env`,
`volumes`, `volumeMounts` and `volumeDevices` across every workload template, so
a regression here would have broken far more than volumeMounts.

## 6. Same caveat as folders 1–2 — showcase data does not exercise this

`subPath` does not appear in **any** raw manifest in this repo:

```
$ Select-String -Path namespaces-showcase\*\*.yaml -Pattern 'subPath'
(no matches)
```

So the committed fidelity report shows **0 before and 0 after**. The evidence the
defect was real is the RED output above plus the code itself. The fixture is
derived from the real `storefront.yaml`; raw manifests were **not** modified.

## 7. What could NOT be verified without a live cluster

- **That the container actually starts and finds its files.** The rendered
  manifest is correct; whether the kubelet projects those subPaths is
  Kubernetes' job.
- **That the API server accepts the manifests** — no reachable API server.
- **Behaviour of the real affected workloads** (`amdi-client`, `data-sender`,
  `data-sender-1`, `data-sender-udp-1`) — those manifests are not in this repo.
- **Key ordering.** `mapToList` ranges in sorted-key order, so the rendered
  mounts are alphabetical by composite key rather than in raw order. Kubernetes
  addresses volumeMounts by name and mountPath, never by position, so this is
  immaterial — but it is an ordering change I could not observe a cluster react
  to. The fidelity checker treats it as equivalent (`_by_name`), and my tests
  compare as sets for the same reason.

## 8. OBSERVED, DEFERRED

- **`Sidecar_Container_Name_Key_Collision` (folder 4)** — same defect family, and
  it is a *different* code path: sidecars go through
  `named_list_to_map_split_image` (line 571) and direct
  `v.setdefault("sidecars", {})[extra["name"]] = entry` assignments at lines
  ~772, 1279, 1335. My change to `named_list_to_map` does **not** cover those.
  Deliberately not touched.
- **`Container_Ports_Reordered_And_Auto_Named` (folder 26)** — the reordering
  half is inherent to `mapToList`'s sorted-key iteration, which I have now
  confirmed but not changed.
- Folders 1–2 deferred items still stand.

## 9. Reproducing

```powershell
python -m pytest universal/gitops-factory/output-showcase/ERRORS_ANALYSIS/VolumeMount_SubPath_Lost_Duplicate_Name_Collision/test/test_volumemount_subpath.py -v
& universal/gitops-factory/output-showcase/ERRORS_ANALYSIS/VolumeMount_SubPath_Lost_Duplicate_Name_Collision/test/render_check.ps1
```
