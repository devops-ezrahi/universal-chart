# STATUS — CA_Bundle_Certificate_Line_Reflow (TIER 3, folder 19 of the index /
18th processed)

**Result: BENIGN — verified, and NOTHING SUPPRESSED.** The report recommends
suppressing `data.ca-bundle.crt` in the fidelity checker. The condition
`error.txt` attaches to that recommendation was checked instead, and the bundle
round-trips **byte-identically** — including the trailing newline whose loss is
the one way this becomes a production incident. Since the content survives,
there is nothing to suppress. 14 new tests RED→GREEN, corpus upgraded from one
placeholder certificate to a realistic three, showcase fidelity 0 lost /
0 changed, chart's 234 green.

---

## 1. The condition, not the label

`error.txt` is explicit that the benign verdict is conditional:

> **BEFORE SUPPRESSING, VERIFY THE CERTIFICATES ARE ACTUALLY IDENTICAL** rather
> than trusting the label. [...] count certificates and compare fingerprints.
> A dropped or truncated certificate in a CA bundle is a TLS outage, and the
> failure mode looks exactly like a whitespace diff until it does not.

and names the specific hazard:

> **SPECIFIC RISK CALLED OUT BY THE TEXT ITSELF**: "trailing `\n` after the final
> `CERTIFICATE-----` gets clipped". Some TLS libraries require a trailing
> newline after the final END CERTIFICATE line to parse the last certificate in
> a bundle. If the clip is real rather than cosmetic, **the LAST certificate in
> every affected bundle may be silently unusable.**

Checked, all of it:

| Check | Result |
|---|---|
| certificate count preserved | 3 → 3 |
| every PEM block byte-identical | yes |
| **last** certificate intact, not truncated | yes |
| **trailing newline after the final END CERTIFICATE** | preserved |
| blank lines + `# ACCVRAIZ1`-style headers between certs | preserved |
| line count | identical |
| whole blob byte-identical | yes |

## 2. Why nothing is suppressed

The report's action is *"Suppress `data.ca-bundle.crt` in fidelity check"*. That
would be the right move if the difference were real and cosmetic. It is not
real: the bundle survives the pipeline unchanged, so there is no diff to
suppress on this converter.

A suppression is a standing promise that the bytes at that path do not matter.
Making that promise about bytes that *do* matter — and that currently survive —
would trade a non-existent problem for a real blind spot: the next genuine
truncation would be invisible. `test_nothing_about_ca_bundles_is_suppressed`
asserts a clipped trailing newline still reports.

**Where the report's 4–13 CHANGED errors came from is not determinable from
here.** Its own root-cause note describes the mechanism accurately — PyYAML's
clip mode on the way in, Go yaml.v3 on the way out — and that mechanism is real;
it simply does not fire on this converter for this shape. Folder 4 changed one
thing in that pipeline (`dump_yaml(allow_unicode=True)`), which affects escaping
but not block-scalar clipping.

## 3. Files changed

| File | Lines | Change |
|---|---|---|
| `universal/gitops-factory/namespaces-showcase/shop-web/shared.yaml` | 66–95 | CA bundle: one placeholder certificate → three, with comment headers and blank lines |

No converter change and no chart change. That is the finding.

### Why the corpus had to change

It held a **single** dummy certificate. A single-certificate bundle cannot
exercise "the last certificate in the bundle gets clipped" at all — there is no
distinction between the last certificate and the only one, and no inter-entry
structure to reflow. The stated risk was untestable against the corpus as it
stood.

It now carries three certificates separated by blank lines and `# ISRG Root X1`
/ `# ACCVRAIZ1` style headers, matching the shape the report quotes
(`\n\n# ACCVRAIZ1\n`).

## 4. Tests created

14 tests in 4 groups:

1. **The stated condition** (4) — count preserved; every PEM block
   byte-identical; the **last** certificate intact and not truncated; the
   trailing newline survives.
2. **Inter-certificate structure** (2) — blank lines and comment headers
   survive; line count identical. This is where reflow would show if it
   happened.
3. **The generic property** (7) — six blob shapes parametrised through
   `dump_yaml`, because `error.txt` warns "any future multi-line ConfigMap value
   will hit this"; plus the assertion that nothing is suppressed.
4. **Corpus** (1) — the bundle still has more than one certificate.

Fingerprints are not compared, and that is deliberate: these are placeholder
bodies, not real DER, so a fingerprint would be a hash of a hash. Comparing the
PEM blocks themselves is strictly stronger for the question being asked — "did
any byte change" — and does not need the certificates to be real.

## 5. Validation

```
$ python convert_to_universal_chart.py --input namespaces-showcase --output <tmp> \
    --chart ../Universal-chart --verify-fidelity --cluster-scoped
  128 objects matched (30 legitimately relocated).
  No fidelity issues found.
```

| Check | Result |
|---|---|
| Folder's own tests | 14 passed |
| All `ERRORS_NEW_ANALYSIS` tests | 300 passed |
| `helm unittest Universal-chart` | 234 passed, 28 suites |

## 6. What could NOT be verified

- **`ConfigMap/env-cas-image-syncer` and `ConfigMap/vault-ca`.** Not in this
  repo. What is proven is that a multi-certificate bundle of the shape the
  report describes survives this pipeline unchanged.
- **Real certificate fingerprints.** The corpus uses placeholders. If a future
  run wants fingerprint comparison, the bundle would need real DER — which
  brings its own maintenance cost (expiry) for no additional signal about byte
  preservation.
- **Whether a TLS library on the target accepts the rendered bundle.** Cluster
  behaviour, and the reason the trailing-newline test exists: it checks the
  precondition, not the outcome.

## 7. OBSERVED, DEFERRED

- **`Grafana_Ini_Changed_False_Positive` (folder 25)** is the same family — a
  multi-line ConfigMap blob reported as CHANGED — and `error.txt` is explicit
  there that "visually identical" is not a standard. The generic
  `dump_yaml` round-trip tests here cover the whitespace half; folder 25 owns
  the quoting/colon half.
- **`dump_yaml(width=100)`** is the parameter that would cause reflow if
  anything did. It does not, for block scalars, on this PyYAML version. Noted
  rather than changed — altering it would churn every generated values file for
  no observed defect.

## 8. Reproducing

```powershell
python -m pytest universal/gitops-factory/output-showcase/ERRORS_NEW_ANALYSIS/CA_Bundle_Certificate_Line_Reflow/test/test_ca_bundle.py -v
```
