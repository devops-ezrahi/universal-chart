# STATUS — Container_Ports_Reordered_And_Auto_Named (P2, folder 26/35)

**Result: FIXED — the auto-naming in the converter and chart, and a checker
weakness I found by testing an assumption I had made and got wrong.**

8 tests, 4 RED→GREEN, chart's own 234 still green, fidelity unchanged.

---

## 1. One report row, two symptoms, two different answers

error.txt says it plainly: *"Port ordering is generally cosmetic to Kubernetes,
but auto-naming is NOT."* Both halves checked separately.

### Auto-naming — real

`ports_list_to_map` keyed every port by `p["name"] or f"port-{containerPort}"`,
and `mapToList` rendered that key straight back as `name:`. A port the raw
manifest left **unnamed** therefore came out named `port-9000`.

That is not cosmetic. Kubernetes resolves a Service `targetPort:` and a probe
`port:` **by name**, so the object now answers to a name nothing referenced and
that did not exist before — and a `targetPort: port-9000` written against the
converted output would silently stop matching if the port is ever named
properly.

| File | Change |
|---|---|
| `convert_to_universal_chart.py` `ports_list_to_map` | mark a synthesised key `unnamed: true` |
| `_helpers.tpl` `universal-chart.mapToList` | drop the marker AND the `name:` line for such an entry |

The map still needs a key — values maps do — so `port-<number>` stays as
bookkeeping. What changed is that it no longer leaks into the manifest.
`test_the_marker_never_leaks_into_the_manifest` pins that the marker itself is
never rendered either.

### Ordering — cosmetic, but it WAS being reported

The values map loses declaration order because Helm's `range` iterates map keys
in sorted order. Kubernetes attaches no meaning to `containers[].ports` order —
nothing selects a port positionally — so preserving it would mean converting the
ports map to a list, changing the values schema for every consumer of this chart
to fix something with no effect. Not done, deliberately.

## 2. What I got wrong, and what it turned up

I assumed the fidelity checker already paired ports by name and wrote that as a
test. **The test failed.**

`_by_name` bails out to positional comparison as soon as **any** entry in the
list is unnamed. One unnamed port therefore dropped the whole list to positional,
and the sorted-key reorder was reported as a single CHANGED covering every port
at once:

```
AssertionError: reordering alone was reported as a difference
CHANGED spec.template.spec.containers[0].ports
  raw=[{'name': 'metrics', ...}, {'name': 'http', ...}, {'containerPort': 9000, ...}, ...]
  render=[{...}, {...}, {...}, {...}]
```

That is almost certainly what the report's 9 occurrences actually are: not nine
broken ports, one noisy comparison repeated. And it hid detail — a genuinely
changed `containerPort` was reported as "the whole list differs" rather than
being named.

`_item_name` now falls back to `containerPort/protocol` as the identity of an
unnamed port (a container may not declare the same pair twice, so it is unique).
A reorder is invisible; a real change is named precisely —
`test_a_genuinely_changed_port_is_still_reported` asserts the path contains
`containerPort` rather than just "something differs".

## 3. Tests

`Container_Ports_Reordered_And_Auto_Named/test/` — 8 tests. Fixture derives from
`namespaces-showcase/shop-web/api-gateway.yaml` with four ports:

| port | why |
|---|---|
| `metrics` 9090 | named, declared FIRST so alphabetical order visibly differs from declaration order |
| `http` 8080 | named, and referenced BY NAME by both the Service `targetPort` and the readiness probe |
| 9000 | **unnamed** — the case that matters |
| 8081 | a second unnamed port, so two synthetic keys are needed |

### RED

```
AssertionError: the unnamed port acquired a name: {'name': 'port-9000', 'containerPort': 9000}
AssertionError: reordering alone was reported as a difference
```

### GREEN

```
8 passed in 1.83s
```

The end-to-end test closes the loop on the reference that would actually break:
after conversion, the probe's `port: http` and the Service's `targetPort: http`
must still resolve against a port that is called `http`.

## 4. Validation chain

| Step | Result |
|---|---|
| Full showcase converter run | `122 objects matched (28 relocated). No fidelity issues found.` |
| Fidelity before / after | 0 lost / 0 changed, unchanged |
| Generated-output churn | none — every container port in this repo's manifests is named |
| `helm lint` | 0 failed |
| Chart's own suite | 234 passed |

## 5. What could NOT be verified without a live cluster

- **That an invented port name actually breaks a reference.** It is the
  documented resolution rule (`targetPort` and probe `port` accept a name), not
  something observed here.
- **That the 9 reported occurrences are the checker noise I think they are.**
  Those manifests are not in this repo. The reasoning is that the report's row
  bundles "ordering + auto-naming" under one count, and ordering alone produced
  exactly this whole-list CHANGED before the fix.
- **That no downstream consumer relies on `port-<number>`.** If anything was
  written against the *converted* output during this campaign, it would now
  break — the names are going away, which is the point.

## 6. OBSERVED, DEFERRED

- **Service ports still synthesise names** (`build_service_entry`, `name =
  p.get("name") or f"port-{p['port']}"`). Unlike container ports this is
  harmless *today*, because `service.yaml` renders `- name: {{ $name }}`
  unconditionally and a Service port in a multi-port Service is required to have
  a name anyway. A single-port Service is not, so the same invented-name
  question applies there. Different function, different template, no folder
  covers it.
- **`_by_name`'s all-or-nothing rule affects more than ports.** Any list where
  some entries are unnamed still falls back to positional. Ports were the case
  that occurred; `env` with a nameless entry would behave the same way. The
  fallback key is deliberately port-specific rather than generic — inventing an
  identity for an arbitrary list is how a checker starts pairing things that are
  not the same object.
- Earlier folders' deferred items still stand.

## 7. Reproducing

```powershell
python -m pytest universal/gitops-factory/output-showcase/ERRORS_ANALYSIS/Container_Ports_Reordered_And_Auto_Named/test -v
```
