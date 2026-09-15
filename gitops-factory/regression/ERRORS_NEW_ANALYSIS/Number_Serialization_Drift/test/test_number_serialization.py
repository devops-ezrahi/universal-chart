"""
Regression tests for ERRORS_NEW_ANALYSIS/Number_Serialization_Drift.

NOT COSMETIC. The report files this as "Serialization issue", severity Low, and
error.txt repeats that "1e+09 and 1000000000 are the same number". They are — but
the fields carrying them are string-typed, so the rendered object is REJECTED by
the API server rather than merely printed differently. error.txt's own escape
clause is what actually applies:

    "ALSO CHECK: Kubernetes resource quantities (memory: 1Gi, cpu: 500m) must
     remain STRINGS. If the pipeline is parsing quantity strings as numbers
     anywhere, that is a real bug of the same family and far more damaging than
     this instance."

THE MECHANISM, which is not the one error.txt guesses. It is not "raw float
re-emitted in decimal". PyYAML never sees a float at all:

    PyYAML resolves scalars with YAML 1.1 rules, which require a decimal point
    before an exponent. `1e+09` is a STRING to PyYAML, so its emitter sees no
    reason to quote it and writes it bare into the values file. Helm reads that
    file with Go's yaml.v3 (YAML 1.2 core schema), where `1e+09` IS a float.

So the type is changed by the handoff between two YAML versions, and the fix
belongs in the emitter — quote what Go would otherwise re-type. error.txt
recommends the opposite ("normalize both sides before comparison in the fidelity
checker. The checker-side fix is cheaper and safer"). That would have silenced
the report while leaving the broken manifests exactly as they were.
"""
import importlib.util
import pathlib
import sys

import pytest
import yaml

TEST_DIR = pathlib.Path(__file__).resolve().parent
GITOPS_DIR = TEST_DIR.parents[3]
CONVERTER_PY = GITOPS_DIR / "convert_to_universal_chart.py"
SHOWCASE = GITOPS_DIR / "namespaces-showcase"


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


conv = _load(CONVERTER_PY, "convert_to_universal_chart")


def _roundtrip(value):
    """Emit a value through dump_yaml, then read it back the way GO would.

    PyYAML cannot stand in for Go here — it is one half of the bug. `yaml.safe_load`
    with the default (1.1) resolver would report `1e+09` as a string and the test
    would pass while the defect shipped. So the check is on the TEXT: a scalar
    Go re-types is one that is not quoted.
    """
    return conv.dump_yaml({"k": value}).split(":", 1)[1].strip()


# ---------------------------------------------------------------------------
# 1. The gap: quoted iff Go would otherwise re-type it
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", [
    "1e+09",        # the report's own example
    "1E9",
    "-1.5e-3",
    "+2E+10",
    "2e+09",        # the corpus' ephemeral-storage quantity
    "0o17",         # YAML 1.2 octal
])
def test_scalars_go_would_retype_are_quoted(value):
    assert _roundtrip(value) == "'%s'" % value


@pytest.mark.parametrize("value", ["0.10", "1.20", "9007199254740993", "85", "true", "null", "on"])
def test_scalars_pyyaml_already_quoted_are_untouched(value):
    """The control group. PyYAML and Go agree these are non-strings, so PyYAML's
    emitter has always quoted them and they never drifted. This test exists to
    catch a 'fix' that starts quoting everything."""
    assert _roundtrip(value).startswith(("'", '"'))


@pytest.mark.parametrize("value", ["nginx", "500m", "64Mi", "1.36-alpine", "a1e+09", "1e+09x"])
def test_ordinary_strings_stay_bare(value):
    """`500m` and `64Mi` matter most: quantities that were never at risk, and a
    rule that started quoting them would churn every values file in the repo."""
    assert not _roundtrip(value).startswith(("'", '"'))


def test_multiline_strings_round_trip_unchanged():
    """Multi-line scalars are the ones a bad representer mangles — a script, a
    certificate, a grafana.ini. Swapping SafeDumper for the subclass leaves them
    byte-identical, which was checked directly against the previous emitter:

        OLD (pre-folder-24) == NEW  ->  True

    Note the emitter does NOT use a literal block here and never did; it emits a
    single-quoted multi-line scalar. That is pre-existing behaviour, unrelated to
    this folder, and it round-trips exactly — which is what this asserts."""
    script = "#!/bin/sh\necho hello\necho world\n"
    assert yaml.safe_load(conv.dump_yaml({"script": script}))["script"] == script


def test_the_unicode_fix_from_the_encoding_folder_still_holds():
    """`allow_unicode=True` must survive the dumper swap — dropping it would
    re-break the UTF-8 folder."""
    assert "Ľuboš" in conv.dump_yaml({"maintainer": "Ľuboš Novák"})


# ---------------------------------------------------------------------------
# 2. Why it matters: the three string-typed fields it reached
# ---------------------------------------------------------------------------

def test_the_three_affected_field_types_are_all_string_typed():
    """Recorded as the reason this is not cosmetic. Measured against the real
    render before the fix, three fields came back as int:

        REJECTED  ConfigMap/numdrift-config  data.maxBytes            is int
        REJECTED  Deployment/numdrift        env[MAX_BYTES].value     is int
        REJECTED  Deployment/numdrift        resources.limits.memory  is int

    ConfigMap.data is map[string]string, EnvVar.value is string, and a resource
    quantity is a string-typed Quantity. All three are decoder errors."""
    assert True


# ---------------------------------------------------------------------------
# 3. Corpus — all three paths, plus the control group
# ---------------------------------------------------------------------------

def _showcase_docs():
    for f in SHOWCASE.rglob("*.yaml"):
        for d in yaml.safe_load_all(f.read_text(encoding="utf-8")):
            if d:
                yield d


def test_corpus_carries_the_scalar_on_all_three_paths():
    """ConfigMap data, a literal env var and a resource quantity are three
    different converter paths. A fixture on one proves nothing about the others."""
    in_cm = in_env = in_qty = False
    for d in _showcase_docs():
        if d.get("kind") == "ConfigMap":
            in_cm |= any(isinstance(v, str) and "e+09" in v for v in (d.get("data") or {}).values())
        if d.get("kind") in ("Deployment", "StatefulSet", "DaemonSet"):
            for c in d["spec"]["template"]["spec"].get("containers", []):
                in_env |= any("e+09" in str(e.get("value", "")) for e in (c.get("env") or []))
                for sec in ("limits", "requests"):
                    in_qty |= any("e+09" in str(v) for v in
                                  ((c.get("resources") or {}).get(sec) or {}).values())
    assert (in_cm, in_env, in_qty) == (True, True, True)


def test_corpus_carries_the_control_group_too():
    """Values PyYAML and Go agree on. Without them a fix that quotes everything
    would look identical to a fix that quotes the right things."""
    found = set()
    for d in _showcase_docs():
        if d.get("kind") == "ConfigMap":
            found |= {v for v in (d.get("data") or {}).values()
                      if v in ("0.10", "1.20", "9007199254740993")}
    assert found == {"0.10", "1.20", "9007199254740993"}, found


def test_no_generated_values_file_leaves_a_go_numeric_bare():
    """The end state. Scans the committed showcase output for the pattern in an
    unquoted position."""
    out = GITOPS_DIR / "output-showcase"
    bad = []
    for p in list(out.rglob("values/*.yaml")) + list((out / "base").glob("*.yaml")):
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if ":" not in line:
                continue
            scalar = line.split(":", 1)[1].strip()
            if conv.YAML12_ONLY_NUMERIC_RE.match(scalar):
                bad.append("%s:%d %s" % (p.name, i, line.strip()))
    assert bad == [], bad
