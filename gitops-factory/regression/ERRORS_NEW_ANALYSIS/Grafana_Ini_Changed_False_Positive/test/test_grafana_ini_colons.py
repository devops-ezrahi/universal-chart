"""
Regression tests for ERRORS_NEW_ANALYSIS/Grafana_Ini_Changed_False_Positive.

error.txt refuses to let this be closed on the report's own evidence, and it is
right to:

  > "NOTE THE INTERNAL INCONSISTENCY IN THE SOURCE: the remediation table calls
  >  this 'Fix grafana.ini colon bug (1)' — implying a real parsing bug involving
  >  a colon — while the issue list calls it a 'likely false positive' whose text
  >  is 'visually identical'. Those are different claims."

  > "ACTION FOR THE NEXT AGENT: do not accept 'visually identical' — diff the two
  >  byte strings directly and print repr() of both."

  > "[test_blob_fidelity.py] proves whitespace is safe; it does NOT prove
  >  colon/quoting handling is safe. Extend it rather than assuming it already
  >  covers this."

So this file is the colon half of the previous round's whitespace test. Every
comparison below is `==` on the exact string, through both halves of the round
trip — `dump_yaml` and a real `helm template` — never a visual one.

RESULT: false positive, now demonstrated rather than assumed. Not one of the
colon shapes below drifts by a byte.

WHY IT IS WORTH TESTING ANYWAY, given that it passes. YAML has two genuine
colon-shaped traps, and a values pipeline that ever loses one of them will lose
it silently:

  1. `key: value` inside a scalar. A plain scalar containing ": " would re-parse
     as a nested MAPPING. PyYAML's emitter quotes it; nothing checked that it
     kept doing so.
  2. Sexagesimal. Under YAML **1.1**, `12:30` is the integer 750 (base 60), and
     PyYAML resolves it that way. It quotes such strings on output for exactly
     that reason — and Go's yaml.v3 (YAML 1.2) has no sexagesimal at all, so the
     two disagree about what `12:30` even is. That is the same 1.1-vs-1.2 fault
     line folder 24 found a live bug on, approached from the other side: there,
     PyYAML under-quoted; here, it over-quotes, and over-quoting is safe.
"""
import importlib.util
import pathlib
import subprocess
import sys
import tempfile

import pytest
import yaml

TEST_DIR = pathlib.Path(__file__).resolve().parent
GITOPS_DIR = TEST_DIR.parents[3]
CHART_DIR = GITOPS_DIR.parent
CONVERTER_PY = GITOPS_DIR / "convert_to_universal_chart.py"


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


conv = _load(CONVERTER_PY, "convert_to_universal_chart")


# A real grafana.ini, carrying every colon shape the file format actually
# produces: a URL scheme, a host:port, an interpolated port, a section header, a
# bare time, and a `key: value` line that is not INI syntax but appears inside
# INI comments constantly.
GRAFANA_INI = """[server]
protocol = http
http_port = 3000
domain = grafana.example.com
root_url = %(protocol)s://%(domain)s:%(http_port)s/grafana/

[database]
url = postgres://grafana:secret@postgres.platform-data:5432/grafana

[auth.generic_oauth]
enabled = true
auth_url = https://keycloak.example.com/realms/devops/protocol/openid-connect/auth
scopes = openid profile email

[smtp]
host = smtp.example.com:587
; note: the next line is a comment containing a colon: like this
from_address = grafana@example.com

[unified_alerting]
evaluation_timeout = 30s
max_attempts = 3
quiet_hours = 22:30
"""

COLON_CASES = {
    "grafana.ini": GRAFANA_INI,
    # 1. `key: value` — would re-parse as a nested mapping if emitted bare
    "mapping_lookalike": "datasource: prometheus",
    # 2. sexagesimal under YAML 1.1 — PyYAML reads this as the integer 750
    "sexagesimal_short": "12:30",
    "sexagesimal_long": "12:30:45",
    # 3. colons in the shapes that appear in real config
    "url": "http://prometheus.platform-ops:9090",
    "windows_path": "C:\\ProgramData\\grafana",
    "leading_colon": ":30",
    "trailing_colon": "section:",
    "bare_colon": ":",
    "colon_space_hash": "note: a value # with a hash",
    # 4. the neighbouring quoting hazards, since they travel the same code path
    "yes_no": "no",
    "tilde": "~",
    "empty": "",
    "star": "*glob",
    "at": "@reboot",
    "percent": "%(protocol)s",
    # 5. folder 24's scalar, inside a blob rather than as a whole value
    "exponent": "1e+09",
}


def _helm_render_configmap(data: dict, name="grafana", release="rel"):
    """Render the chart with this ConfigMap and read the blob back out."""
    values = {"configMaps": {name: {"data": data}}}
    with tempfile.TemporaryDirectory() as tmp:
        vf = pathlib.Path(tmp) / "values.yaml"
        vf.write_text(conv.dump_yaml(values), encoding="utf-8")
        proc = subprocess.run(
            ["helm", "template", release, str(CHART_DIR), "-f", str(vf)],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert proc.returncode == 0, "helm template failed:\n%s" % proc.stderr
    for d in yaml.safe_load_all(proc.stdout):
        if d and d.get("kind") == "ConfigMap" and d["metadata"]["name"].endswith(name):
            return d["data"]
    raise AssertionError("chart rendered no ConfigMap ending in %r" % name)


# ---------------------------------------------------------------------------
# 1. Half one — the converter's own emitter
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("key", sorted(COLON_CASES))
def test_dump_yaml_round_trips_each_colon_shape_byte_exactly(key):
    value = COLON_CASES[key]
    out = yaml.safe_load(conv.dump_yaml({"data": {key: value}}))["data"][key]
    assert out == value, "\nraw : %r\nout : %r" % (value, out)


def test_the_two_shapes_that_MUST_be_quoted_actually_are():
    """The mechanism, not just the outcome. If PyYAML ever stopped quoting these
    the round-trip above would still pass on the Python side while Helm read
    something else entirely — which is precisely how folder 24's bug worked."""
    for key in ("mapping_lookalike", "sexagesimal_short", "sexagesimal_long"):
        line = conv.dump_yaml({key: COLON_CASES[key]}).split(":", 1)[1].strip()
        assert line.startswith(("'", '"')), "%s emitted bare as %s" % (key, line)


def test_pyyaml_really_does_read_a_bare_sexagesimal_as_an_integer():
    """Pins the hazard itself, so the test above cannot become vacuous. This is
    the YAML 1.1 rule Go's yaml.v3 does not share."""
    assert yaml.safe_load("v: 12:30")["v"] == 750


# ---------------------------------------------------------------------------
# 2. Half two — through a real helm template
# ---------------------------------------------------------------------------

def test_helm_renders_every_colon_shape_byte_exactly():
    rendered = _helm_render_configmap(COLON_CASES)
    drifted = {k: (v, rendered.get(k)) for k, v in COLON_CASES.items()
               if rendered.get(k) != v}
    assert drifted == {}, "\n".join(
        "%s\n  raw : %r\n  out : %r" % (k, a, b) for k, (a, b) in drifted.items())


def test_the_reported_blob_is_identical_end_to_end():
    """The specific claim in the report, checked as bytes rather than by eye."""
    rendered = _helm_render_configmap({"grafana.ini": GRAFANA_INI})["grafana.ini"]
    assert rendered == GRAFANA_INI
    assert rendered.encode("utf-8") == GRAFANA_INI.encode("utf-8")


# ---------------------------------------------------------------------------
# 3. The verdict, and what it is NOT
# ---------------------------------------------------------------------------

def test_the_previous_rounds_whitespace_test_still_exists():
    """error.txt says to extend that test rather than assume it covers colons.
    This file is the extension; if that one is ever deleted, the whitespace half
    of the guarantee goes with it and this note should stop being true."""
    prior = (GITOPS_DIR / "regression" / "ERRORS_ANALYSIS" /
             "Grafana_ConfigMap_Trailing_Newline" / "test" / "test_blob_fidelity.py")
    assert prior.is_file()


def test_no_suppression_was_added_for_this():
    """A false positive that cannot be reproduced needs no suppression rule, and
    adding one would hide a real drift the day the pipeline changes. The report
    lists it under both 'fix' and 'suppress'; the answer is neither."""
    import inspect
    rule = inspect.getsource(conv._fidelity_ignored_path)
    assert "grafana" not in rule.lower(), rule
    assert ".ini" not in rule
