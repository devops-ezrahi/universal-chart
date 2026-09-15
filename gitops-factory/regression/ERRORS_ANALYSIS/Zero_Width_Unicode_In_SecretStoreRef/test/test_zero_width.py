"""
Regression tests for ERRORS_ANALYSIS/Zero_Width_Unicode_In_SecretStoreRef.

Report issue 23: a U+200C ZERO WIDTH NON-JOINER inside the raw value
`Se<ZWNJ>cretStore` is stored faithfully by the converter, Helm normalises it
away, and the fidelity checker reports a mismatch on
`spec.secretStoreRef.kind`.

Two things about that framing are worth stating before the fix:

  * The defect is in the SOURCE DATA. An invisible control character was
    committed into a manifest. The converter is nevertheless the only place
    allowed to repair it — the campaign's hard constraints forbid editing the
    raw manifests.
  * "Helm normalises it away" is not really what happens, and it does not
    matter: whether the character survives to the rendered output or not, a
    `kind` or a `name` containing one is a string no lookup will ever match.
    `Se<ZWNJ>cretStore` is not `SecretStore` to the API server either.

Grep proof (STATUS.md §1): **zero** invisible characters exist in this repo's
raw manifests or generated output. The character came from the original
project's data. The fixture is what makes the defect reproducible here.

The fix is deliberately narrow — identifiers only, never payload. A blanket
strip would rewrite ConfigMap/Secret *data*, where a zero-width character can be
legitimate content, and corrupting a payload to tidy up a name would be a worse
bug than the one being fixed. `test_payload_data_is_left_alone` pins that line.
"""
import importlib.util
import pathlib
import shutil
import subprocess
import sys
import tempfile

import pytest
import yaml

TEST_DIR = pathlib.Path(__file__).resolve().parent
GITOPS_DIR = TEST_DIR.parents[3]
CHART_DIR = GITOPS_DIR.parent
CONVERTER_PY = GITOPS_DIR / "convert_to_universal_chart.py"
FIXTURE_DIR = TEST_DIR / "fixture"
FIXTURE = FIXTURE_DIR / "identity-auth" / "session-store.yaml"

NS = "identity-auth"
RELEASE = "session-store"
ZWNJ = "‌"
RAW_DIRS = ["namespaces", "namespaces-showcase", "namespaces-live-demo"]


def _load_converter():
    spec = importlib.util.spec_from_file_location("convert_to_universal_chart", CONVERTER_PY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["convert_to_universal_chart"] = mod
    spec.loader.exec_module(mod)
    return mod


conv = _load_converter()


def _doc(kind):
    for d in yaml.safe_load_all(FIXTURE.read_text(encoding="utf-8")):
        if d and d.get("kind") == kind:
            return d
    raise AssertionError(f"no {kind} in fixture")


def _helm_template(values: dict, release=RELEASE):
    values = dict(values)
    values.setdefault("externalSecretsApiVersion", "external-secrets.io/v1beta1")
    with tempfile.TemporaryDirectory() as tmp:
        vf = pathlib.Path(tmp) / "values.yaml"
        vf.write_text(yaml.safe_dump(values, sort_keys=False, allow_unicode=True),
                      encoding="utf-8")
        proc = subprocess.run(
            ["helm", "template", release, str(CHART_DIR), "-f", str(vf)],
            capture_output=True, text=True, encoding="utf-8",
        )
    assert proc.returncode == 0, f"helm template failed:\n{proc.stderr}"
    return [d for d in yaml.safe_load_all(proc.stdout) if d]


# --------------------------------------------------------------------------
# The premise
# --------------------------------------------------------------------------

def test_the_fixture_really_contains_invisible_characters():
    """Without this the whole folder could pass vacuously — the character is
    invisible, so nothing else would notice if it went missing from the
    fixture."""
    assert FIXTURE.read_text(encoding="utf-8").count(ZWNJ) == 3


# The one file that is SUPPOSED to contain an invisible character: standing
# coverage added by ERRORS_NEW_ANALYSIS/ExternalSecret_ZeroWidth_U200C_Source_Fix,
# whose verdict was "BOTH" — repair the source AND keep the defensive strip. A
# defensive strip with nothing to strip is untested code, so one deliberately
# broken manifest stays.
KNOWN_ZERO_WIDTH_FIXTURE = ("namespaces-showcase/identity-auth/session-store.yaml", 0x200C)


def test_the_only_invisible_characters_are_the_known_fixture():
    """This asserted the repo had NO invisible characters anywhere. It fired when
    a later round deliberately committed one as standing coverage for the strip.

    Narrowed rather than deleted: any invisible character outside that one known
    file still fails, which is the case the tripwire was built for — a real one
    arriving by copy-paste."""
    hits = []
    for d in RAW_DIRS:
        root = GITOPS_DIR / d
        if not root.is_dir():
            continue
        for f in root.rglob("*.yaml"):
            text = f.read_text(encoding="utf-8")
            for ch in conv.ZERO_WIDTH_CHARS:
                if ch in text:
                    hits.append((f.relative_to(GITOPS_DIR).as_posix(), ord(ch)))
    assert sorted(set(hits)) == [KNOWN_ZERO_WIDTH_FIXTURE], (
        "invisible characters outside the known fixture:\n%s" % (sorted(set(hits)),))


# --------------------------------------------------------------------------
# The fix — identifiers are repaired
# --------------------------------------------------------------------------

def test_strip_zero_width_removes_every_invisible_character():
    assert conv.strip_zero_width(f"Se{ZWNJ}cretStore") == "SecretStore"
    assert conv.strip_zero_width("SecretStore") == "SecretStore"
    assert conv.strip_zero_width(None) is None
    assert conv.strip_zero_width(7) == 7


def test_sanitize_object_repairs_the_secret_store_ref_and_the_name():
    raw = _doc("ExternalSecret")
    assert ZWNJ in raw["spec"]["secretStoreRef"]["kind"], "fixture premise"
    assert ZWNJ in raw["metadata"]["name"], "fixture premise"

    cleaned = conv.sanitize_object(raw)
    assert cleaned["spec"]["secretStoreRef"]["kind"] == "SecretStore"
    assert cleaned["metadata"]["name"] == "session-store-signing-key"


def test_the_repair_is_reported_and_not_silent():
    notes = []
    conv.sanitize_object(_doc("ExternalSecret"), notes)
    joined = " | ".join(notes)
    assert "U+200C" in joined, f"no warning recorded; notes={notes}"
    assert "secretStoreRef.kind" in joined and "metadata.name" in joined, notes


def test_a_clean_object_produces_no_warning():
    notes = []
    conv.sanitize_object(_doc("SecretStore"), notes)
    assert notes == []


def test_payload_data_is_left_alone():
    """The line the fix must not cross. A zero-width character inside
    ConfigMap/Secret DATA can be legitimate content — a fixture, a template, a
    translation string — and silently rewriting it to tidy up a name would be a
    worse bug than the one being fixed."""
    cleaned = conv.sanitize_object(_doc("ConfigMap"))
    assert cleaned["data"]["greeting"] == f"hello{ZWNJ}world", (
        "the sanitiser rewrote payload data; it must only touch identifiers")


# --------------------------------------------------------------------------
# End to end
# --------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_round_trip_produces_a_resolvable_reference(tmp_path):
    out = tmp_path / "out"
    proc = subprocess.run(
        [sys.executable, str(CONVERTER_PY),
         "--input", str(FIXTURE_DIR), "--output", str(out),
         "--chart", str(CHART_DIR), "--skip-verify"],
        capture_output=True, text=True, encoding="utf-8", cwd=str(GITOPS_DIR),
    )
    assert proc.returncode == 0, f"converter failed:\n{proc.stdout}\n{proc.stderr}"

    merged: dict = {}
    for vf in (out / NS / "defaults.yaml", out / "base" / f"{RELEASE}.yaml",
               out / NS / "values" / f"{RELEASE}.yaml"):
        if vf.exists():
            merged = conv.deep_merge(merged, yaml.safe_load(vf.read_text(encoding="utf-8")) or {})

    docs = _helm_template(merged)
    es = next(d for d in docs if d["kind"] == "ExternalSecret")
    ss = next(d for d in docs if d["kind"] == "SecretStore")

    assert es["metadata"]["name"] == "session-store-signing-key"
    assert es["spec"]["secretStoreRef"]["kind"] == "SecretStore"
    assert es["spec"]["secretStoreRef"]["name"] == ss["metadata"]["name"], (
        "the ExternalSecret must point at a SecretStore that actually exists")

    for d in docs:
        blob = yaml.safe_dump(d, allow_unicode=True)
        for ch in conv.ZERO_WIDTH_CHARS:
            if d["kind"] == "ConfigMap":
                continue  # payload is deliberately untouched
            assert ch not in blob, f"{d['kind']}/{d['metadata']['name']} still carries U+{ord(ch):04X}"


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_the_warning_reaches_conflicts_and_warnings(tmp_path):
    out = tmp_path / "out"
    subprocess.run(
        [sys.executable, str(CONVERTER_PY),
         "--input", str(FIXTURE_DIR), "--output", str(out),
         "--chart", str(CHART_DIR), "--skip-verify"],
        capture_output=True, text=True, encoding="utf-8", cwd=str(GITOPS_DIR), check=True,
    )
    report = (out / "report" / "conflicts_and_warnings.txt").read_text(encoding="utf-8")
    assert "U+200C" in report, (
        "the repair happened silently; it must be visible so the raw manifest "
        f"gets fixed at source. Report was:\n{report[:2000]}")
