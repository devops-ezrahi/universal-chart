# STATUS — Missing_Container_Port_9125 (TIER 1, folder 6/26)

**Result: ROOT CAUSE FOUND AND FIXED.** The report records this one's cause as
UNKNOWN and calls the folder "an INVESTIGATION TASK, not a known fix — expect to
spend time here". It is the first of the two suspects `error.txt` lists, and the
mechanism is exact: the converter and the fidelity checker disagreed about what
identifies an *unnamed* container port. Reproduced as the report's error path
verbatim, fixed, corpus-covered. 12 new tests RED→GREEN, showcase fidelity 0
lost / 0 changed, chart's 234 green.

---

## 1. Root cause

Two halves of one pipeline, two different answers to "what makes a port unique".

`ports_list_to_map` — the converter's half:

```python
out[f"port-{p['containerPort']}"] = entry      # number only
```

`_item_name` — the checker's half, which states the rule in its own docstring:

```python
return f"#port:{item['containerPort']}/{item.get('protocol', 'TCP')}"
# "a container may not declare the same containerPort/protocol pair twice"
```

**The checker was right.** Kubernetes' uniqueness rule is on the
containerPort/**protocol pair**, so a container declaring `9125/TCP` and
`9125/UDP` is legal — and is the standard statsd-exporter shape, which is what
port 9125 is. Both collapsed onto the key `port-9125`, and by dict insertion
order the UDP entry overwrote the TCP one.

That is why the report says `ports[#port:9125/TCP]` and not `/UDP`: the TCP half
specifically, deterministically, every time.

Direct probe of the pre-fix function:

```
raw ports in : 3
values keys  : ['port-9125', 'metrics']
ports out    : 2
  'port-9125' -> {'containerPort': 9125, 'protocol': 'UDP', 'unnamed': True}

checker identity for each raw port (_item_name):
   #port:9125/TCP
   #port:9125/UDP
   metrics
```

Three distinct identities on the checker's side; two keys on the converter's.

**The second suspect was not the cause.** `error.txt` also names "the mapToList
helper in `_helpers.tpl`". It is innocent — it omits both `unnamed` and `name`
and renders whatever it is given. It is still pinned by a test, because it is
the half that has to survive the new key.

## 2. Files changed

| File | Lines | Change |
|---|---|---|
| `universal/gitops-factory/convert_to_universal_chart.py` | 707–740 | `ports_list_to_map`: key unnamed ports on number **and** protocol; raise on a genuine duplicate |
| `universal/gitops-factory/namespaces-showcase/platform-ops/metrics-agent.yaml` | 29–40 (new) | the colliding statsd-exporter port pair |
| `universal/gitops-factory/output-showcase/**` | regenerated | consequence |

### Design decisions

**The converter adopts the checker's identity, not the reverse.** The checker's
is the identity Kubernetes itself uses; changing it to match the converter would
have made the checker unable to distinguish two real ports, converting a visible
defect into an invisible one.

**Changing the key is free.** `universal-chart.mapToList` omits both `unnamed`
and `name`, so the synthetic key never reaches a rendered manifest. It is
bookkeeping, and bookkeeping should be as specific as correctness requires. The
only visible effect is in generated values files, where an unnamed port now
reads `port-9125-tcp` instead of `port-9125` — more informative, and the render
is byte-identical either way.

**A genuine duplicate now raises instead of being silently absorbed.** If two
ports really do share a name, or a containerPort/protocol pair, the input is
already invalid Kubernetes. The old code kept the last one and said nothing —
which *is* this defect. Refusing loudly is the only behaviour that cannot lose a
port. Covered by two tests.

**Protocol defaults to `TCP` in the key**, matching Kubernetes and matching
`_item_name`, so a port with no explicit protocol collides with an explicit TCP
one rather than sitting beside it under a different key.

## 3. RED

First attempt at a RED variant reverted only the key and kept the new duplicate
guard — so it raised instead of dropping, which demonstrated the guard but not
the defect. Rebuilt with the function's original body:

```
$ python <scratch>/red6.py --input namespaces-showcase --output <tmp> \
    --chart ../Universal-chart --verify-fidelity --cluster-scoped
  1 potential fidelity issue(s)

  field_lost_ports  1

  ERROR  DaemonSet/metrics-agent LOST at spec.template.spec.containers[0].ports[#port:9125/TCP]:
         present in raw, absent from render
```

`ports[#port:9125/TCP]` — the report's field path, character for character,
including the `#port:` synthetic-identity prefix and the TCP half being the
casualty.

## 4. GREEN

```
$ python convert_to_universal_chart.py --input namespaces-showcase --output <tmp> \
    --chart ../Universal-chart --verify-fidelity --cluster-scoped
  123 objects matched (28 legitimately relocated).
  No fidelity issues found.
```

Post-fix probe:

```
values keys  : ['port-9125-tcp', 'port-9125-udp', 'metrics']
ports out    : 3
```

`helm template` of the affected release — both ports present, neither carrying
an invented name:

```yaml
ports:
  - name: metrics
    containerPort: 9100
  - containerPort: 9125
    protocol: TCP
  - containerPort: 9125
    protocol: UDP
```

```
$ python -m pytest .../Missing_Container_Port_9125/test/test_missing_port.py -q
............                                                             [100%]
12 passed in 1.13s
```

## 5. Tests created

`ERRORS_NEW_ANALYSIS/Missing_Container_Port_9125/test/test_missing_port.py`
— 12 tests in 4 groups:

1. **The defect** (4) — both protocols on one number survive; the TCP half
   specifically is present; converter key count equals distinct `_item_name`
   count (the root cause expressed as an invariant); protocol defaults to TCP
   in the key.
2. **No regression in the rest of the function** (6) — named ports still key on
   their name; unnamed ports keep the `unnamed` marker that stops `mapToList`
   inventing a name (the defect the *previous* round fixed here); a named and an
   unnamed port on the same number coexist; genuine duplicates raise; empty and
   `None` inputs are fine.
3. **Through the chart** (1) — the second suspect, pinned: `mapToList` renders
   all three ports and invents no names.
4. **Corpus coverage** (1) — the colliding shape exists in
   `namespaces-showcase`, since the collision needs two unnamed ports on one
   number to occur at all.

## 6. Severity — deliberately not inflated

`error.txt` asks for this: *"IMPACT — verify before rating."* Its own analysis
is correct and worth preserving rather than restating as worse than it is:

> Kubernetes does not require a containerPort be declared for traffic to reach
> it, so a Service targeting 9125 BY NUMBER keeps working. What breaks is
> anything resolving it BY NAME (Service targetPort, probes), NetworkPolicy port
> references, and port-based discovery/documentation.

For statsd specifically the UDP listener is the one that matters and the UDP
entry is the one that *survived*, so the practical blast radius on
`Deployment/b2b` is likely small. The reason to fix it anyway is not the single
occurrence — it is that a converter which silently discards a declared port has
no upper bound on what else it discards, and this one was found only because a
1,313-line report happened to be read closely.

## 7. Validation chain

| Check | Result |
|---|---|
| Pre-fix probe of `ports_list_to_map` | 3 ports in, **2 out** |
| RED, full corpus | `LOST ... ports[#port:9125/TCP]` — report's path verbatim |
| Post-fix probe | 3 ports in, 3 out |
| Converter re-run, showcase | 123 objects matched, **no fidelity issues** |
| `helm template` | both 9125 ports, no invented names |
| Folder's own tests | 12 passed |
| All `ERRORS_NEW_ANALYSIS` tests | 116 passed |
| `helm unittest Universal-chart` | 234 passed, 28 suites |

## 8. What could NOT be verified

- **`Deployment/b2b` itself.** Not in this repo, so its actual port list is
  unknown. What is proven is that the shape the report's error path describes —
  an unnamed 9125/TCP alongside another 9125 entry — no longer loses a port. The
  `#port:9125/TCP` identity in the report is strong evidence for that shape,
  since the checker only emits it for a port carrying no name.
- **How 9125 is consumed on the target.** `error.txt` asks; it needs the cluster.
  See §6 — the fix is correct regardless of the answer.
- **Whether the previous key scheme lost ports anywhere else.** By construction
  it lost one for every unnamed same-number multi-protocol pair; the report found
  one. There is no way to count the rest without the source manifests.

## 9. OBSERVED, DEFERRED

- **`Container_Ports_Reordered_And_Auto_Named` (previous round)** touched this
  exact function and is still holding: the `unnamed: true` marker works and
  `mapToList` still drops it. Pinned by two tests here so this folder's change
  cannot undo that one.
- **`Number_Serialization_Drift` (folder 24)** — ports are integer-typed and a
  float round-trip would matter here, but no drift was observed on any port
  value in this run. Not investigated; that folder's job.
- **`VolumeMounts_Dict_Key_Ordering_Drift` (folder 13)** is the same family —
  a map key standing in for a list identity. `named_list_to_map` was not touched.

## 10. Reproducing

```powershell
python -m pytest universal/gitops-factory/output-showcase/ERRORS_NEW_ANALYSIS/Missing_Container_Port_9125/test/test_missing_port.py -v

# rebuild the RED variant and watch the port vanish
python <scratch>/make_red6.py
python <scratch>/red6.py --input namespaces-showcase --output <tmp> `
  --chart ../Universal-chart --verify-fidelity --cluster-scoped
```
