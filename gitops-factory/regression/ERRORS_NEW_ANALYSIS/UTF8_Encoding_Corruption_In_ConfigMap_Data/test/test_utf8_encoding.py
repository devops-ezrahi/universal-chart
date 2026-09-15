"""
Regression tests for ERRORS_NEW_ANALYSIS/UTF8_Encoding_Corruption_In_ConfigMap_Data.

The only defect in this batch that is REAL DATA CORRUPTION rather than a lost
field or a formatting diff. error.txt is explicit about the difference: "Unlike
the CA-bundle reflow and grafana.ini items in this same report, the rendered
content genuinely differs from the source and the mangled bytes would be
deployed to the cluster. A corrupted name inside navlogback.xml ships as
corrupted; a corrupted character inside init-config.sh is executed as a shell
script."

ROOT CAUSE — one call, one missing keyword. helm_template() ran

    subprocess.run(cmd, capture_output=True, text=True)

`text=True` with no `encoding=` decodes the child's stdout using
locale.getpreferredencoding(). On Windows that is the ANSI codepage; Helm always
emits UTF-8. Decoding UTF-8 bytes with a single-byte codepage is textbook
mojibake, and the report's two signatures identify it precisely:

    Ľ  U+013D  -> UTF-8 C4 BD       -> cp1252 "Ä¾"     (2 chars)
    —  U+2014  -> UTF-8 E2 80 94    -> cp1252 "â€\""   (3 chars)

This is the ONE place the pipeline crosses a process boundary — every
read_text/write_text in the converter already passes encoding="utf-8".
error.txt guessed the same thing and named the in-repo fix pattern: "run_all.py
calls subprocess.run(..., capture_output=True, text=True, encoding='utf-8',
errors='replace')".

SECOND HALF, also named by error.txt ("dump_yaml() — confirm it passes
allow_unicode=True"): PyYAML escaped non-ASCII to \\uXXXX and collapsed the
value into one quoted scalar. That round-trips correctly, so it is not
corruption — but it made the file the converter's own docs call "hand-editable"
unreadable. Fixed alongside.
"""
import importlib.util
import pathlib
import subprocess
import sys

import pytest
import yaml

TEST_DIR = pathlib.Path(__file__).resolve().parent
GITOPS_DIR = TEST_DIR.parents[3]
CHART_DIR = GITOPS_DIR.parent
CONVERTER_PY = GITOPS_DIR / "convert_to_universal_chart.py"
SHOWCASE = GITOPS_DIR / "namespaces-showcase"


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


conv = _load(CONVERTER_PY, "convert_to_universal_chart")

L_CARON = "Ľ"   # Ľ
S_CARON = "š"   # š
A_ACUTE = "á"   # á
EM_DASH = "—"   # —
SCRIPT = (f"#!/bin/sh\n# Maintainer: {L_CARON}ubo{S_CARON} Nov{A_ACUTE}k {EM_DASH} team\n"
          f'echo "cache {EM_DASH} warming up"\n')


# ---------------------------------------------------------------------------
# 1. The diagnosis itself — pinned, because it is what identifies the bug class
# ---------------------------------------------------------------------------

def test_the_mojibake_signatures_are_utf8_bytes_read_as_a_single_byte_codepage():
    """Not testing our code — testing that the report's evidence means what this
    fix assumes. If UTF-8-through-cp1252 did not reproduce the report's strings,
    the diagnosis would be wrong and `encoding=` would be the wrong fix.

    Two of the three match the report exactly. The third is off by one bit and
    the arithmetic is asserted rather than the transcription:

        report writes  Ľuboš -> Ä¾uboÅ¡
        Ľ  U+013D -> C4 BD -> cp1252 "Ä½"   (NOT "Ä¾")
        ľ  U+013E -> C4 BE -> cp1252 "Ä¾"   <- what the report's string implies

    So the report's mangled half corresponds to the LOWERCASE ľ while its clean
    half is written with the uppercase Ľ. Since the report was transcribed from
    screenshots, that is a one-character reading error in the evidence, not a
    different bug — the byte mechanism is identical either way, and both
    codepoints are affected equally. Recorded here rather than silently
    normalised, because the next reader will compare this test against the
    report and needs to know why they differ."""
    assert L_CARON.encode("utf-8").decode("cp1252") == "Ä½"          # Ľ U+013D
    assert "ľ".encode("utf-8").decode("cp1252") == "Ä¾"              # ľ U+013E, the report's string
    assert S_CARON.encode("utf-8").decode("cp1252") == "Å¡"          # matches the report exactly
    # The report renders the em-dash's third byte as a plain quote; 0x94 in
    # cp1252 is the right double quotation mark U+201D, which is what an OCR
    # pass would flatten to ".
    assert EM_DASH.encode("utf-8").decode("cp1252") == "â€”"


# ---------------------------------------------------------------------------
# 2. The fix — helm output survives the process boundary
# ---------------------------------------------------------------------------

def test_helm_template_decodes_utf8_not_the_ansi_codepage(tmp_path):
    """End-to-end through the real subprocess: a ConfigMap whose data contains
    Ľ, š, á and an em-dash must come back with those exact codepoints."""
    vf = tmp_path / "values.yaml"
    vf.write_text(yaml.safe_dump({
        "workload": {"type": "deployment"},
        "image": {"repository": "redis", "tag": "7"},
        "configMaps": {"redis-cache-conf": {"data": {"init-config.sh": SCRIPT}}},
    }, allow_unicode=True), encoding="utf-8")

    docs = conv.helm_template(CHART_DIR, "redis", "platform-data", [vf])
    cm = next(d for d in docs if d.get("kind") == "ConfigMap")
    rendered = cm["data"]["init-config.sh"]

    assert rendered == SCRIPT, f"content changed crossing the helm boundary: {rendered!r}"
    for ch in (L_CARON, S_CARON, A_ACUTE, EM_DASH):
        assert ch in rendered, f"{ch!r} (U+{ord(ch):04X}) did not survive"


def test_no_mojibake_signature_appears_in_the_render(tmp_path):
    """The positive test above would also pass if BOTH sides were equally
    corrupted. This one names the failure directly."""
    vf = tmp_path / "values.yaml"
    vf.write_text(yaml.safe_dump({
        "workload": {"type": "deployment"},
        "image": {"repository": "redis", "tag": "7"},
        "configMaps": {"c": {"data": {"s.sh": SCRIPT}}},
    }, allow_unicode=True), encoding="utf-8")
    rendered = next(d for d in conv.helm_template(CHART_DIR, "redis", "platform-data", [vf])
                    if d.get("kind") == "ConfigMap")["data"]["s.sh"]
    for bad in ("Ä¾", "â€”", "�"):
        assert bad not in rendered, f"mojibake {bad!r} in rendered output"


def test_helm_template_pins_the_encoding_explicitly(monkeypatch):
    """A guard against the exact regression: someone tidying this call back to
    `subprocess.run(cmd, capture_output=True, text=True)` reintroduces the
    corruption silently, on Windows only, and only for non-ASCII content."""
    seen = {}

    class _Result:
        returncode = 0
        stdout = "kind: ConfigMap\nmetadata:\n  name: x\n"
        stderr = ""

    def _fake_run(cmd, **kwargs):
        seen.update(kwargs)
        return _Result()

    monkeypatch.setattr(conv.subprocess, "run", _fake_run)
    conv.helm_template(CHART_DIR, "r", "ns", [])
    assert seen.get("encoding") == "utf-8", \
        f"helm_template no longer pins encoding=utf-8; kwargs were {seen}"
    assert seen.get("errors") == "replace", \
        "a non-UTF-8 byte should surface as U+FFFD in the diff, not abort the run"


# ---------------------------------------------------------------------------
# 3. dump_yaml — readable, and still round-trips
# ---------------------------------------------------------------------------

def test_dump_yaml_emits_real_characters_not_escapes():
    out = conv.dump_yaml({"configMaps": {"c": {"data": {"s.sh": SCRIPT}}}})
    assert L_CARON in out and EM_DASH in out
    assert "\\u013d" not in out.lower(), "non-ASCII was escaped instead of emitted"


def test_dump_yaml_keeps_a_multiline_script_as_a_block_scalar():
    """The practical cost of the escaping: a string containing \\uXXXX cannot be
    a literal block, so the whole script collapsed onto one quoted line in a
    file these values are documented as hand-editable."""
    out = conv.dump_yaml({"configMaps": {"c": {"data": {"s.sh": SCRIPT}}}})
    assert "#!/bin/sh" in out
    assert "# Maintainer:" in out
    body = out.split("s.sh:", 1)[1]
    assert "\\n" not in body.split("\n", 1)[0], "script emitted as a single escaped scalar"


@pytest.mark.parametrize("text", [
    SCRIPT,
    f"{L_CARON}ubo{S_CARON}",
    f"a {EM_DASH} b",
    "plain ascii only",
    "trailing newline preserved\n",
    "",
])
def test_dump_yaml_round_trips_every_shape_byte_exactly(text):
    """Readability must not cost fidelity. This is the invariant the whole
    folder rests on."""
    payload = {"configMaps": {"c": {"data": {"k": text}}}}
    assert yaml.safe_load(conv.dump_yaml(payload)) == payload


# ---------------------------------------------------------------------------
# 4. Corpus coverage — the reason a corruption bug went unseen
# ---------------------------------------------------------------------------

def test_the_corpus_contains_non_ascii_at_all():
    """error.txt: "the bug affects 100% of non-ASCII content, so the true blast
    radius equals however much of it there is." The corpus had none, so the
    blast radius measured as zero and the round-trip check was blind."""
    hits = [p.name for p in SHOWCASE.rglob("*.yaml")
            if any(ord(c) > 127 for c in p.read_text(encoding="utf-8"))]
    assert hits, "no non-ASCII byte in the corpus — the standing check cannot see this defect"


def test_the_corpus_non_ascii_includes_the_codepoints_the_report_names():
    text = "".join(p.read_text(encoding="utf-8") for p in SHOWCASE.rglob("*.yaml"))
    assert L_CARON in text, "L-caron (U+013D) coverage removed"
    assert EM_DASH in text, "em-dash (U+2014) coverage removed"


def test_every_corpus_file_is_valid_utf8():
    """A broader check error.txt asks for. A file that is not UTF-8 at rest
    would corrupt on read, before any of the above applies."""
    bad = []
    for p in SHOWCASE.rglob("*.yaml"):
        try:
            p.read_bytes().decode("utf-8")
        except UnicodeDecodeError as exc:
            bad.append((p.name, str(exc)))
    assert not bad, f"non-UTF-8 raw manifests: {bad}"
