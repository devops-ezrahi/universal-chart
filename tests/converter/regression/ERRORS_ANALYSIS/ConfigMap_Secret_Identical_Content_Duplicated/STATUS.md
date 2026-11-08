# STATUS — ConfigMap_Secret_Identical_Content_Duplicated (P1, folder 18/35)

**Result: rename KEPT, warning made actionable — the recorded decision,
implemented and tested.** 9 tests, 3 RED→GREEN.

---

## 1. Why not auto-share

Three microservices in one namespace each declare `zookeeper-config` with
byte-identical content. `claim()` gives the first the name and renames the rest
to `<owner>-<name>`, so one shared cluster object becomes three.

The report's own analysis makes the case for leaving it that way better than a
summary could:

> "The converter doesn't auto-share because it can't know if the original
> cluster actually had a shared Secret or two separate ones that happened to
> have identical content. It takes the safe path."

The asymmetry is what settles it. Auto-sharing two objects that were genuinely
independent **couples two releases forever** — a change to one silently changes
the other, and nothing in the rendered output would show it. Leaving them split
costs a duplicate object and a warning. That is the reversible mistake, and it is
the one to make.

## 2. What was actually missing: the operator's next step

The warning reported the rename and noted `(identical content)` as a bare
parenthetical. It never said what to DO about it — so the one piece of
information the converter genuinely cannot supply, and the human can, went
unasked for.

| File | Change |
|---|---|
| `convert_to_universal_chart.py` `NamespaceRegistry.claim` | identical-content and differing-content collisions get different, actionable messages |

Real output on the fixture:

```
[platform-data/zookeeper] ConfigMap/zookeeper-config: name collision between
microservices 'zookeeper-zbs' and 'zookeeper' in namespace 'platform-data'
(identical content) -> renamed 'zookeeper' copy to 'zookeeper-zookeeper-config'.
The two copies are BYTE-IDENTICAL, so this is probably one shared object in the
source cluster, split into two. To share it instead: move ConfigMap/zookeeper-config
into this namespace's shared.yaml and delete it from both microservice files;
every referring release will then point at the one copy. Left as a rename because
nothing in the raw manifests can prove they were shared rather than coincidentally
equal.
```

And for differing content, deliberately different advice — because there is no
decision to make:

```
Content DIFFERS, so these are two different objects that collided on a name; the
rename is correct and no action is needed beyond checking that nothing OUTSIDE
these releases referred to Secret/zookeeper-auth by its original name.
```

Suggesting `shared.yaml` for two objects that differ would be actively wrong, so
`test_differing_content_gets_a_different_message` asserts the word does not
appear there.

## 3. The escape hatch has to work

The advice points at `shared.yaml`, so that path is now pinned too:
`test_shared_yaml_is_still_the_only_thing_that_shares` asserts that when the
namespace's shared release claims the object first, the microservice gets
`reference_shared` **and keeps the original name** — which is what makes the
suggested fix actually resolve.

## 4. Tests

`ConfigMap_Secret_Identical_Content_Duplicated/test/` — 9 tests. The fixture
reproduces the report's case exactly: three microservices
(`zookeeper`, `zookeeper-zoonav`, `zookeeper-zbs`) in `platform-data`, all with
identical `zookeeper-config`, plus a `zookeeper-auth` Secret where one of the
three **differs** — so both message paths are exercised by one converter run.

```
9 passed in 1.05s
```

RED before the change was the three warning-content assertions; the rename
behaviour itself was already correct and its tests pass either way, which is what
makes them a guard against a future "helpful" auto-sharing change.

## 5. Validation chain

| Step | Result |
|---|---|
| Full showcase converter run | `122 objects matched (28 relocated). No fidelity issues found.` |
| Generated-output churn | none — the showcase has **zero** name collisions |
| `helm lint` | 0 failed |
| Chart's own suite | 234 passed |

## 6. What could NOT be verified without a live cluster

- **Whether the source cluster really had one shared object or three.** This is
  the crux, and it is unknowable from the manifests — which is precisely why the
  converter asks rather than decides.
- **That three identical ConfigMaps are harmless.** They are three objects where
  there was one: more to keep in sync, and a `kubectl get configmap` that looks
  unfamiliar. Correct, but not free.

## 7. OBSERVED, DEFERRED

- **The first claimant is decided by sorted filename order**, so which release
  keeps the original name is stable but arbitrary. The report noticed this
  (*"zookeeper-zbs got owner ... processed first alphabetically"*). Making it
  deliberate — e.g. the release whose workload actually mounts it — would be a
  real improvement and is not this folder's.
- Earlier folders' deferred items still stand.

## 8. Reproducing

```powershell
python -m pytest universal/gitops-factory/output-showcase/ERRORS_ANALYSIS/ConfigMap_Secret_Identical_Content_Duplicated/test -v
```
