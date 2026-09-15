"""
Regression tests for ERRORS_NEW_ANALYSIS/CA_Bundle_Certificate_Line_Reflow.

Rated benign in three separate places — "same certificate, different YAML line
wrapping" — with a recommendation to suppress `data.ca-bundle.crt` in the
fidelity checker. error.txt attaches a condition to that verdict and it is not a
formality:

  "BEFORE SUPPRESSING, VERIFY THE CERTIFICATES ARE ACTUALLY IDENTICAL rather
   than trusting the label. Extract data.ca-bundle.crt from raw and from helm
   template output, strip whitespace, and compare the parsed PEM blocks — count
   certificates and compare fingerprints. A dropped or truncated certificate in
   a CA bundle is a TLS outage, and the failure mode looks exactly like a
   whitespace diff until it does not."

and names the specific way it stops being benign:

  "SPECIFIC RISK CALLED OUT BY THE TEXT ITSELF: 'trailing \\n after the final
   CERTIFICATE----- gets clipped'. Some TLS libraries require a trailing newline
   after the final END CERTIFICATE line to parse the last certificate in a
   bundle. If the clip is real rather than cosmetic, the LAST certificate in
   every affected bundle may be silently unusable. Test this specifically — it
   is the one way this benign-looking item becomes a production incident."

VERDICT: benign, and NOTHING IS SUPPRESSED. The bundle round-trips byte-exactly
through the real pipeline — PyYAML in, Go yaml.v3 out — including the trailing
newline. A suppression would have been a promise that the content does not
matter; it does, and it survives, so there is nothing to promise.

The corpus previously held a SINGLE dummy certificate, which cannot exercise the
"last certificate in the bundle" risk at all. It now holds three, with comment
headers and blank lines between them, matching the real shape.
"""
import importlib.util
import pathlib
import re
import subprocess
import sys

import pytest
import yaml

TEST_DIR = pathlib.Path(__file__).resolve().parent
GITOPS_DIR = TEST_DIR.parents[3]
CHART_DIR = GITOPS_DIR.parent
CONVERTER_PY = GITOPS_DIR / "convert_to_universal_chart.py"
SHOWCASE = GITOPS_DIR / "namespaces-showcase"

PEM = re.compile(r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", re.S)


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


conv = _load(CONVERTER_PY, "convert_to_universal_chart")


def _raw_bundle():
    for d in yaml.safe_load_all((SHOWCASE / "shop-web" / "shared.yaml")
                                .read_text(encoding="utf-8")):
        if d and d.get("kind") == "ConfigMap" and d["metadata"]["name"] == "shared-ca-bundle":
            return d["data"]["ca.crt"]
    raise AssertionError("shared-ca-bundle not in the corpus")


def _rendered_bundle(tmp_path):
    vf = tmp_path / "values.yaml"
    vf.write_text(conv.dump_yaml({
        "workload": {"type": "none"}, "service": {"enabled": False},
        "configMaps": {"shared-ca-bundle": {"data": {"ca.crt": _raw_bundle()}}},
    }), encoding="utf-8")
    out = subprocess.run(
        ["helm", "template", "shared", str(CHART_DIR), "--namespace", "shop-web", "-f", str(vf)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert out.returncode == 0, out.stderr
    cm = next(d for d in yaml.safe_load_all(out.stdout)
              if d and d.get("kind") == "ConfigMap" and d["metadata"]["name"] == "shared-ca-bundle")
    return cm["data"]["ca.crt"]


# ---------------------------------------------------------------------------
# 1. The condition error.txt attaches to the benign verdict
# ---------------------------------------------------------------------------

def test_certificate_count_is_preserved(tmp_path):
    """"count certificates" — the first half of the stated check."""
    raw, rendered = _raw_bundle(), _rendered_bundle(tmp_path)
    assert len(PEM.findall(raw)) == 3, "the corpus bundle no longer has multiple certificates"
    assert len(PEM.findall(rendered)) == len(PEM.findall(raw))


def test_every_certificate_block_is_byte_identical(tmp_path):
    """"compare the parsed PEM blocks". Fingerprints are not computable here —
    these are placeholder bodies, not real DER — so the blocks themselves are
    compared, which is strictly stronger for this purpose."""
    assert PEM.findall(_rendered_bundle(tmp_path)) == PEM.findall(_raw_bundle())


def test_the_LAST_certificate_survives_intact(tmp_path):
    """The specific risk. A clipped bundle loses its final entry, and everything
    before it looks perfect — which is why counting alone would not catch it."""
    raw_last = PEM.findall(_raw_bundle())[-1]
    rendered_last = PEM.findall(_rendered_bundle(tmp_path))[-1]
    assert rendered_last == raw_last
    assert rendered_last.endswith("-----END CERTIFICATE-----")
    assert "CCC" in rendered_last, "the final certificate's body was truncated"


def test_the_trailing_newline_after_the_final_certificate_survives(tmp_path):
    """"trailing \\n after the final CERTIFICATE----- gets clipped" — the one
    way this becomes a production incident, since some TLS libraries will not
    parse the last certificate without it."""
    raw, rendered = _raw_bundle(), _rendered_bundle(tmp_path)
    assert raw.endswith("\n"), "the corpus bundle lost its trailing newline"
    assert rendered.endswith("\n"), "the trailing newline was clipped crossing the pipeline"
    assert rendered == raw, "the bundle is not byte-identical end to end"


# ---------------------------------------------------------------------------
# 2. The structure between certificates, which is where reflow would show
# ---------------------------------------------------------------------------

def test_blank_lines_and_comment_headers_between_certificates_survive(tmp_path):
    """The report's own example of the boundary is `\\n\\n# ACCVRAIZ1\\n`. If
    block-scalar handling collapsed blank lines, this is where it would show —
    and a bundle whose entries run together is not the same file."""
    rendered = _rendered_bundle(tmp_path)
    assert "\n\n    # ACCVRAIZ1\n" in rendered or "\n\n# ACCVRAIZ1\n" in rendered, \
        repr(rendered[:400])
    assert rendered.count("# ") == _raw_bundle().count("# ")


def test_line_count_is_preserved(tmp_path):
    """Reflow, if it happened at all, would change this."""
    assert _rendered_bundle(tmp_path).splitlines() == _raw_bundle().splitlines()


# ---------------------------------------------------------------------------
# 3. The generic property, so this holds for the next blob too
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("blob", [
    "-----BEGIN CERTIFICATE-----\nAAA\n-----END CERTIFICATE-----\n",
    "a\n\nb\n",                       # blank line in the middle
    "no trailing newline",            # clip has nothing to remove
    "trailing spaces   \nand more\n",
    "\n",                             # a lone newline
    "line\r\nwith crlf\n",
])
def test_dump_yaml_round_trips_arbitrary_blobs(blob):
    """error.txt: "Any future multi-line ConfigMap value will hit this." The
    property is asserted generically rather than only for this one bundle."""
    payload = {"configMaps": {"c": {"data": {"k": blob}}}}
    assert yaml.safe_load(conv.dump_yaml(payload)) == payload


def test_nothing_about_ca_bundles_is_suppressed():
    """The report recommends suppressing `data.ca-bundle.crt` in the checker.
    Nothing is — the content round-trips, so a suppression would be a promise
    that the bytes do not matter, made about bytes that do. A future genuine
    corruption must still report."""
    diffs = conv._fidelity_diff(
        "ConfigMap",
        {"data": {"ca-bundle.crt": "-----BEGIN CERTIFICATE-----\nAAA\n-----END CERTIFICATE-----\n"}},
        {"data": {"ca-bundle.crt": "-----BEGIN CERTIFICATE-----\nAAA\n-----END CERTIFICATE-----"}})
    assert diffs, "a clipped trailing newline in a CA bundle is no longer reported"


# ---------------------------------------------------------------------------
# 4. Corpus coverage
# ---------------------------------------------------------------------------

def test_the_corpus_bundle_has_more_than_one_certificate():
    """A single-certificate bundle cannot exercise the "last certificate in the
    bundle" risk at all, and that is what the corpus held before this folder."""
    assert len(PEM.findall(_raw_bundle())) >= 3
