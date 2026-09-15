# The two missing report issues (28/29) — final analysis

The brief's last step:

> "The original report numbered its issues 1-39, but issues 28 and 29 were never
> captured in the source screenshots. Two defects are therefore MISSING from
> ERRORS_ANALYSIS entirely. After you finish folder 35, re-run the full fidelity
> report and tell me what is still failing that has no folder — those are the two
> missing issues."

---

## 1. Nothing is failing

All 36 folders are complete. The fidelity report, re-run against the real
manifests:

```
objects matched:            122
  of those, relocated:      28
fields LOST (raw -> render): 0
fields CHANGED:             0
fields ADDED (chart default, informational): 273
objects in raw but rendered by nothing:      0
objects rendered with no raw source:         0
render failures:            0
```

So the instruction cannot be followed literally — there is no failing line left
to read a missing issue off. That is not a happy accident, and it is worth being
blunt about why: **this report was mostly clean before the campaign started
too.** Folder after folder came back "0 before, 0 after" because this repo's
manifests do not exercise the defects — no `subPath`, no digests, no unnamed
ports, no empty maps, no custom pull secrets, no name collisions. The report's
own counts (3,087 labels, ~600 empty maps, 278 pull secrets) come from a
different, much larger manifest set.

**A clean report is therefore weak evidence.** The answer has to come from what
the checker *structurally cannot see*, not from what it currently prints.

## 2. The checker's blind spots, enumerated

| # | Blind spot | Can it hide a real defect? |
|---|---|---|
| 1 | `filter_transient` drops whole KINDS before comparison — `ReplicaSet`, `Pod`, `Endpoints`, `EndpointSlice`, `Event`, `ControllerRevision`, `Binding` | **YES — see §3** |
| 2 | `sanitize_object` strips `VOLATILE_METADATA_KEYS` before comparison — including `finalizers` and `ownerReferences` | Yes, but deliberately (folder 21 §1) |
| 3 | ADDED is counted, never listed, and never an error | **YES — see §4** |
| 4 | `_fidelity_ignored_path` — selector labels, pod-template labels, `Service.spec.selector` | By design; each entry justified in its docstring |
| 5 | Empty `{}`/`[]` suppressed (folder 29), legacy `serviceAccount` alias suppressed (folder 33) | Scoped narrowly; limits tested |
| 6 | The checker compares objects **field by field, in isolation** — it never checks that a reference from one object resolves to another | Yes; folder 19 built a separate sweep for exactly this |
| 7 | It compares the *rendered manifest*, never applied cluster state | Out of scope offline, stated in every STATUS |

## 3. Candidate A — hand-managed `Endpoints` are dropped, and nothing says so

`Endpoints` and `EndpointSlice` are in `TRANSIENT_KINDS`, so `filter_transient`
removes them **before the converter or the checker ever sees them**. For
controller-generated Endpoints that is correct — they are runtime state, not
desired state.

**But a selector-less Service with a hand-written `Endpoints` object is the
standard way to point a Service at an external or legacy backend**, and that
Endpoints IS desired state. Reproduced with a two-object input (Service +
Endpoints, one namespace):

```
converter exit: 0
  1 objects matched (1 legitimately relocated).
  No fidelity issues found.

objects matched:            1
fields LOST (raw -> render): 0
objects in raw but rendered by nothing:      0
objects rendered with no raw source:         0

rendered kinds: ['Service', 'ServiceAccount']
```

The Endpoints object is **gone**, and the report does not merely fail to flag it
— it does not count it as an object at all. "1 objects matched" from an input of
two. A migration would silently take the backend offline: the Service survives,
answers DNS, and routes to nothing.

**It compounds folder 32 exactly.** That folder found the chart injects its own
selector into a selector-less Service, and added a warning for it. This is the
other half: the Endpoints that the selector-less Service existed to use is
dropped too. Two halves of one breakage, from two different mechanisms, neither
reported. Folder 32's warning is currently the only signal a user gets, and it
only fires because I added it last round.

**Why this fits "a missing issue":** it is invisible to the report by
construction, so no amount of re-running would ever surface it; it is exactly the
class the report's other Service findings circle (`Service_Selector_Injected_When_Absent`
explicitly names hand-managed Endpoints as the risk); and it is the kind of thing
a screenshot-based analysis would state once and lose.

**Not fixed here.** Carrying Endpoints through means either a new chart template
or `extraDeploy`, and deciding which controller-generated Endpoints to keep
versus drop — a design call, and the campaign's no-multitasking rule puts it
outside every existing folder. Recorded, with the reproduction above.

## 4. Candidate B — the chart inventing objects is unreportable

`ADDED` is counted but never listed and never an error, on the stated reasoning
that "a Helm chart filling in a Kubernetes default the raw dump omitted is normal
and harmless". That is true for *fields*. It is not true for *objects*.

The current report carries this, in every namespace:

```
INFO ServiceAccount/db-migrate: created by the chart for release 'db-migrate'
     (serviceAccount.create) — no raw source expected
```

Those ServiceAccounts exist on no raw manifest. They will be created on the
cluster. The checker knows, and files it as INFO with a rationale — a deliberate
choice, but it means **"objects rendered with no raw source: 0" is not the same
claim as "the chart invents nothing"**, and the summary line reads as though it
were.

`Extra_Objects_Rendered_With_No_Raw_Source` (folder 9) is the folder that should
own this, and it was closed on the count going 3 → 0 — which is what the brief
asked for, but that count is measured *after* this INFO classification has
already removed these from it.

## 5. Two candidates already found and fixed, which may be the real 28/29

Both are recorded in `_FOUND_Global_Defaults_Subtracted_But_Never_Written`:

1. **`global_defaults` subtracted in namespace-scoped mode but written nowhere.**
   P0, silent total loss of every value common to all microservices in a run.
   Namespace-scoped is the DEFAULT.
2. **`shared` releases subtracted against a layer they never apply.** P0, phantom
   Deployment/Service/ServiceAccount on a config-only release.

Neither has an `error.txt`. Both are P0. Both were found by reasoning about the
layering rather than by reading a count — and neither would have appeared in this
repo's fidelity report, because the showcase's 20 microservices are too
heterogeneous for `global_defaults` to hold anything.

## 6. Honest conclusion

**I cannot tell you which two of these the report's issues 28 and 29 were.** The
screenshots that would say are missing, and nothing in the surviving material
numbers the issues in a way that lets the gap be reconstructed — the folder names
and the report's own numbering do not line up.

What I can say:

- **Nothing is failing**, and the reason is that this repo's data does not
  exercise most of the defects — not that everything is proven correct.
- **Four defects with no folder were found during the campaign.** Two are fixed
  (§5, both P0). Two are recorded and not fixed (§3, §4), because fixing them
  means design decisions outside any folder's scope.
- **§3 is the strongest candidate for a genuine missing issue**: a real
  functional break, invisible to the report by construction, sitting exactly
  where the report's own Service findings point.

If issues 28/29 turn out to be something else entirely, the mechanism that would
have caught them is the one this campaign kept re-learning: **the fidelity report
is a floor, not a ceiling.** Twenty-eight of the 36 folders had a `0 lost / 0
changed` count before and after their fix.

## 7. Reproducing §3

```powershell
# Service + hand-written Endpoints in one namespace, converted with --verify-fidelity
# -> Endpoints absent from the rendered output, report says "No fidelity issues found"
```

The probe script is in this campaign's scratch notes; the essential shape is a
two-document input (a selector-less `Service` and a matching `Endpoints`) and a
`helm template` of the resulting release.
