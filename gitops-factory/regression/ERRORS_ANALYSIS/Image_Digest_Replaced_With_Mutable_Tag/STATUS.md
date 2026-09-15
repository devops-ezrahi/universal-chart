# STATUS — Image_Digest_Replaced_With_Mutable_Tag (P1, folder 10/35)

**Result: FIXED.** 18 tests RED→GREEN, folders 1–9 still green, chart's own 234
still green, fidelity unchanged at 0 lost / 0 changed.

---

## 1. Hint verification

| Hint | Reality |
|---|---|
| root cause row G: *"`split_image` doesn't handle `@sha256:...` digests"* | **Correct.** `split_image` (line 357) had `if "@" in image: return image.split("@")[0], ""` — repository kept, digest discarded, tag empty. |
| *"the chart's `universal-chart.image` helper must emit `repository@sha256:digest`"* | **Already did.** The helper has handled `.Values.image.digest` and a repository containing `@` all along. |

So this is a **converter-only** defect for the main container — the one folder so
far where the template side needed nothing. `test_chart_image_helper_already_renders_a_digest`
documents and guards that, and passed before and after.

**One place error.txt did not mention needed fixing**: the CronJob/Job container
builds its image inline in `jobPodSpec`, not through the helper:

```gotemplate
image: {{ if .job.image }}{{ printf "%s:%s" .job.image.repository
          (.job.image.tag | default .Chart.AppVersion) }}{{ else }}...
```

No digest branch at all, so a digest-pinned CronJob fell back to
`Chart.AppVersion` even after the converter was fixed. Same omission pattern as
folders 3, 5 and 6.

## 2. Files changed

| File | Line | Change |
|---|---|---|
| `convert_to_universal_chart.py` | 357–386 | `split_image` returns **(repository, tag, digest)** |
| `convert_to_universal_chart.py` | 687–699 | `handle_main_container`: record `image.digest`; exclude it from base like the tag |
| `convert_to_universal_chart.py` | 1729, 1744 | `build_minimal_values`: carry `digest` alongside `repository`/`tag`, top level and for CronJob/Job |
| `convert_to_universal_chart.py` | ~1336, ~1401 | `handle_cronjob` / `handle_job`: unpack and route the digest |
| `_pod-spec.tpl` | 259 | `jobPodSpec`: digest branch mirroring `universal-chart.image` |

### Two design calls

**Three-tuple, not a repository that carries the digest.** `split_image` could
have returned `("postgres@sha256:...", "")` and let the helper's existing
`contains "@" .Values.image.repository` branch handle it — a one-line change. I
returned an explicit third element instead, because the digest then routes like a
tag (below) rather than being welded into the repository string where
`build_minimal_values` cannot see it.

**The digest goes to minimal values, not base.** A digest is the version pin,
exactly like a tag. Left in `base/<slug>.yaml`, re-pinning one namespace's image
would drag every other namespace sharing that base file with it. Pinned by
`test_digest_lands_in_minimal_values_like_a_tag`, which asserts both halves — not
in base, present in minimal.

**A digest wins over a tag** when both are present, since it is the stronger pin
(`test_digest_wins_over_a_tag_when_both_are_present`).

## 3. Tests

`Image_Digest_Replaced_With_Mutable_Tag/test/` — 18 tests.

- `fixture/platform-data/postgres.yaml` — from the real showcase StatefulSet, with
  every image digest-pinned: main container, a sidecar, and an init container.
- `test_split_image_forms` — 9 parametrised cases covering tag, no-tag, digest,
  registry-with-port (`registry:5000/ns/app`, which must **not** be read as a
  tag), and empty string. That `/`-in-tag guard is pre-existing and load-bearing;
  the new digest branch had to not break it.
- Converter capture, minimal-values routing, sidecar/init pass-through, template
  render, round-trip, and checker visibility.

### RED — before

```
13 failed, 5 passed in 2.06s

AssertionError: assert ('postgres', '') == ('postgres', '', 'sha256:86ee...')
AssertionError: digest lost from ctx.full_image; got {'repository': 'postgres'}
AssertionError: digest missing from minimal values; got {'repository': 'postgres'}
AssertionError: images differ after round-trip
```

The 5 that passed were the template-side guards — the evidence that the chart
half was already correct.

### GREEN — after

```
18 passed in 1.95s
```

### Accumulated (folders 1–10 + the found P0)

```
96 passed in 35.58s
```

## 4. Validation chain

**1. Converter re-run:** `122 objects matched (28 relocated). No fidelity issues found.`

**2–3. Raw vs rendered, every image reference** (`render_check.ps1`):

```
   RAW:
     busybox@sha256:9ae97d36d26566ff84e8893c64a6dc4fe8ca6d1144bf5b87b2b85a32def253c7
     postgres@sha256:86ee6a2b6b1b1c8a1e2d0dcbbf0dcf5a3b7c9d4e5f60718293a4b5c6d7e8f900
     quay.io/prometheuscommunity/postgres-exporter@sha256:11223344...
   RENDERED:
     busybox@sha256:9ae97d36d26566ff84e8893c64a6dc4fe8ca6d1144bf5b87b2b85a32def253c7
     postgres@sha256:86ee6a2b6b1b1c8a1e2d0dcbbf0dcf5a3b7c9d4e5f60718293a4b5c6d7e8f900
     quay.io/prometheuscommunity/postgres-exporter@sha256:11223344...
   OK: all 3 image references round-tripped byte-identically
```

Before the fix the main container rendered `postgres:2.0.0`. The check also
asserts `:2.0.0` appears nowhere, since Chart.AppVersion substitution is the
precise failure mode.

**4. Fidelity report before vs after: unchanged**, 0 lost / 0 changed.

**5. Generated-output diff: none from this folder.** No showcase manifest pins by
digest, so the output is identical to folder 9's state.

**6. `helm lint`:** 0 failed. **7. Chart's own suite:** 28 suites, 234 tests —
including `deployment_test.yaml`'s image assertions, unaffected by the new branch.

## 5. Caveat — showcase data does not exercise this

No raw manifest in this repo uses `@sha256:`. The committed fidelity count was 0
before and after; the evidence is the RED output plus the code. Raw manifests were
not modified; the fixture is a copy with digests substituted.

## 6. What could NOT be verified without a live cluster

- **That the digest resolves to a real image.** The digests in the fixture are
  fabricated hex — structurally valid, not pullable. Verifying a pull needs a
  registry and a kubelet.
- **That the previously-rendered `:2.0.0` would have been the wrong binary.**
  That depends on what `postgres:2.0.0` happens to point at in the target
  registry — the danger error.txt describes, unobservable here.
- **The 9 (or 31) real occurrences.** Those manifests are not in this repo.

## 7. OBSERVED, DEFERRED

- **`ImagePullPolicy_Always_Downgraded_To_IfNotPresent` (folder 11, next).**
  error.txt for this folder notes the two compound: *"a pinned-by-digest workload
  becomes pinned to a mutable tag AND stops re-pulling"*. Half of that is now
  fixed. `_pod-spec.tpl:260` still renders the **release-level**
  `.Values.image.pullPolicy` for the CronJob/Job container rather than a per-job
  value — folder 11's, untouched.
- **`values.schema.json` already declares `image.digest`** (`{"digest": {"type": "string"}}`),
  so no schema change was needed. Worth knowing: the chart's values contract
  anticipated this defect; only the converter never filled the field.
- Folders 1–9 deferred items still stand.

## 8. Reproducing

```powershell
python -m pytest universal/gitops-factory/output-showcase/ERRORS_ANALYSIS/Image_Digest_Replaced_With_Mutable_Tag/test/test_image_digest.py -v
& universal/gitops-factory/output-showcase/ERRORS_ANALYSIS/Image_Digest_Replaced_With_Mutable_Tag/test/render_check.ps1
```
