# STATUS — ServiceAccount_Secrets_And_ImagePullSecrets_Lost (P1, folder 17/35)

**Result: the unfinished analysis is FINISHED, and the answer is "cosmetic here,
CRITICAL in general". Both halves fixed.** 8 tests, 3 RED→GREEN.

---

## 1. The question the report never answered

error.txt says the deep dive *"was started but not fully concluded"*, which is
why the report carries two contradictory ratings — LOW #19 (*"cluster
auto-provisions dockercfg secrets"*) and CRITICAL/High in Discoveries
(*"real credentials ... ImagePullBackOff"*). The deciding question:

> "If the migration target is still OpenShift, this is cosmetic. If the target
> is vanilla Kubernetes or the SA carries a MANUALLY-added pull secret for a
> private registry, this is CRITICAL."

Target is **vanilla Kubernetes** (recorded decision). error.txt then asks for the
per-object check — auto-generated `<sa>-dockercfg-XXXXX` or operator-added?
Answer, over every ServiceAccount in all three manifest trees:

```
ServiceAccounts scanned: 316
  imagePullSecrets  auto-generated pattern: 278   CUSTOM: 0
  secrets           auto-generated pattern: 556   CUSTOM: 0
  auto-pattern secrets naming a DIFFERENT SA:   0
```

**100% auto-generated, zero custom.** So on this data the loss is cosmetic.

**And it is stronger than cosmetic: carrying them would be actively wrong.**
Those Secrets are created by OpenShift's integrated-registry controller. On a
vanilla cluster they never exist, so copying the reference points every affected
ServiceAccount at an object that never appears. Dropping them is the correct
behaviour, not a tolerable one.

`secrets:` is the easier half and both sources agree: ServiceAccount token
references, created and rotated by Kubernetes itself. Declaring them from a chart
would fight the controller that owns them. Never carried.

## 2. The CRITICAL half is real, and was unguarded

A **custom** credential — a private-registry pull secret somebody added by hand —
was dropped by exactly the same code path, with nothing said. That is the
Discoveries rating, and it is the case that ends in ImagePullBackOff on every pod
using the ServiceAccount.

| File | Change |
|---|---|
| `convert_to_universal_chart.py` | new `OPENSHIFT_AUTO_SECRET` pattern |
| `convert_to_universal_chart.py` `handle_serviceaccount` | carry `imagePullSecrets` that do NOT match it; never carry `secrets` |
| `templates/serviceaccount.yaml` | render `imagePullSecrets`; documented why `secrets` is not rendered |

The auto-generated ones are dropped **silently** rather than warned about. That
is a deliberate exception to this campaign's usual rule: the pattern is
unambiguous, it is 100% of this repo's, and 278 warnings would bury the
conflicts file — which is the same noise problem folders 29 and 33 are about.

## 3. Tests

`ServiceAccount_Secrets_And_ImagePullSecrets_Lost/test/` — 8 tests. Fixture
derives from `namespaces-showcase/shop-web/api-gateway.yaml`, with a
ServiceAccount carrying **both** kinds at once, because telling them apart is
the whole folder:

| reference | kind |
|---|---|
| `api-gateway-dockercfg-x9z8y` | OpenShift-generated — must be dropped |
| `acme-registry-creds` | hand-added — must survive |
| `api-gateway-token-a1b2c` | token reference — never carried |

### RED — capture and render reverted, restored byte-identically

```
AssertionError: the hand-added registry credential was dropped; got []. Every pod
  using this ServiceAccount would fail ImagePullBackOff.
AssertionError: template dropped the pull secret; got None
AssertionError: expected only the custom credential; got []

restored: True
```

### GREEN

```
8 passed
```

`test_every_pull_secret_in_this_repo_is_openshift_auto_generated` re-runs the
triage every time, so a custom credential appearing in the raw manifests fails
the suite — the moment the severity flips from cosmetic to CRITICAL.

## 4. Validation chain

| Step | Result |
|---|---|
| Full showcase converter run | `122 objects matched (28 relocated). No fidelity issues found.` |
| Fidelity before / after | 0 lost / 0 changed, unchanged |
| Generated-output churn | none — 0 custom pull secrets in this repo |
| `helm lint` | 0 failed |
| Chart's own suite | 234 passed |

## 5. What could NOT be verified without a live cluster

- **That the target cluster is really vanilla Kubernetes.** The whole
  classification rests on that decision. On OpenShift the auto-generated Secrets
  DO exist and dropping them is still right (the controller re-creates them), so
  the fix is safe either way — but the *severity* answer depends on it.
- **That pods pull successfully** with the custom credential restored.
- **That no other OpenShift naming convention exists** for generated Secrets.
  The pattern covers `dockercfg` and `token` with a 5+ char suffix, which is what
  all 834 references in this repo use.

## 6. OBSERVED, DEFERRED

- **The pattern is a heuristic, not a guarantee.** A human could name a real
  credential `payments-dockercfg-abcde` and it would be dropped. Nothing in a
  raw manifest distinguishes "generated" from "named to look generated"; the
  alternative — carrying everything — breaks 278 ServiceAccounts to protect a
  case that has never occurred. Worth knowing before trusting it blindly.
- **`extraDeploy` ServiceAccounts bypass this entirely.** A release's second and
  subsequent ServiceAccounts are emitted verbatim (`handle_serviceaccount`'s
  else-branch), so their auto-generated pull secrets DO travel through. That is
  a different code path and no folder covers it.
- Earlier folders' deferred items still stand.

## 7. Reproducing

```powershell
python -m pytest universal/gitops-factory/output-showcase/ERRORS_ANALYSIS/ServiceAccount_Secrets_And_ImagePullSecrets_Lost/test -v
```
