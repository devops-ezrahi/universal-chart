# STATUS — Grafana_ConfigMap_Trailing_Newline (P3, folder 35/35)

**Result: REFUTED — it does not reproduce in this pipeline. No code changed.
What the folder leaves behind is a permanent regression test over the repo's
real ConfigMap and Secret payloads.** 7 tests, all green.

---

## 1. The refutation, both halves of the round trip

Reported: `data.grafana.ini` renders `"\n"` where the raw had `"\n\n"`.

Every whitespace shape survives byte-exactly. The converter's own `dump_yaml`:

```
case                 dump->load
ini_blank_line       OK      ("[server]\nroot_url = http://x\n\n")
ini_single_nl        OK
no_trailing_nl       OK
pem                  OK
pem_no_nl            OK
script               OK
trailing_spaces      OK
crlf                 OK
```

And `helm template`:

```
case             result
iniBlankLine     OK
iniSingleNl      OK
noTrailingNl     OK
pem              OK
pemNoNl          OK
script           OK
trailingSpaces   OK
manyBlank        OK      ("a\n\n\n\n")

lossy cases: 0
```

The reported clipping must have come from a different YAML library or an older
chart in the original project. Nothing in this pipeline does it.

## 2. The broader check error.txt asks for — and why it mattered more

> "this is a whitespace-fidelity issue in the YAML pipeline, not a
> Grafana-specific one. Verify no OTHER embedded config blob (scripts,
> .properties files, certificates, PEM keys) is affected — trailing-newline loss
> in a PEM certificate or a shell script WOULD break things, unlike INI."

That was the right instinct, and this repo makes it concrete: the raw manifests
contain **23 PEM blobs and 10 shell scripts**. A clipped newline in a PEM
certificate breaks parsers that require the terminator; in a script it can merge
the last two lines. None of them lose anything —
`test_every_real_configmap_blob_round_trips` walks every ConfigMap and Secret
payload in all three manifest trees and asserts byte-equality.

That test is the reason this folder is worth keeping rather than just closing.
The report's real point is that a future change to the YAML pipeline could start
clipping block scalars and **nothing else in the suite would notice** — the
fidelity checker would report a CHANGED field with two values that look
identical in the terminal. Now it fails here first, on real data.

## 3. Why "harmless for INI" undersells it anyway

Recorded because it stays true if the defect ever appears: a ConfigMap content
change alters the checksum, so a `checksum/config` annotation triggers an
unnecessary pod restart, and ArgoCD shows a permanent non-empty diff — which
trains operators to ignore drift. Cosmetic at the parser, not cosmetic at the
GitOps layer.

## 4. Files changed

**None.** This is a refutation.

## 5. Tests

`Grafana_ConfigMap_Trailing_Newline/test/` — 7 tests. Fixture derives from
`namespaces-showcase/platform-ops/log-agent.yaml` with one blob per hazard:

| key | shape |
|---|---|
| `grafana.ini` | the reported case — block ending in a BLANK line (`\n\n`), written `\|+` |
| `tls.crt` | PEM certificate |
| `entrypoint.sh` | shell script |
| `padded.conf` | trailing SPACES, which a block scalar cannot represent literally |
| `many.txt` | several trailing blank lines |

`test_the_fixture_carries_the_shapes_that_could_be_clipped` asserts the fixture
still holds them — trailing whitespace is invisible, so nothing else would
notice it being edited away.

```
7 passed
```

No RED to show: the defect does not exist here. The evidence is the explicit
byte-comparison output above, and the real-data scan.

## 6. Validation chain

| Step | Result |
|---|---|
| Full showcase converter run | `122 objects matched (28 relocated). No fidelity issues found.` |
| Fidelity before / after | 0 lost / 0 changed, unchanged |
| Generated-output churn | none — no code changed |
| `helm lint` | 0 failed |
| Chart's own suite | 234 passed |

## 7. What could NOT be verified without a live cluster

- **That the original project's clipping had a different cause.** Its manifests
  and its chart are not here; I can only show this pipeline does not clip.
- **That Grafana tolerates the difference.** Irrelevant now, but it was the
  report's stated reason for LOW and it is untested.
- **Whether `kubectl apply` normalises anything on the way in.** The comparison
  here stops at the rendered manifest.

## 8. OBSERVED, DEFERRED

- **The fidelity checker cannot explain a whitespace diff.** It would report a
  CHANGED field whose two values print identically. A targeted message
  (`repr()`-style, or a trailing-whitespace note) would help, and the same gap
  applies to the invisible characters in folder 22. Neither folder owns it.
- Earlier folders' deferred items still stand.

## 9. Reproducing

```powershell
python -m pytest universal/gitops-factory/output-showcase/ERRORS_ANALYSIS/Grafana_ConfigMap_Trailing_Newline/test -v
```
