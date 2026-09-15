"""
Regression tests for ERRORS_ANALYSIS/Grafana_ConfigMap_Trailing_Newline.

Reported: `data.grafana.ini` renders "\\n" where the raw had "\\n\\n" — 2
occurrences, harmless for INI parsing.

**It does not reproduce in this pipeline.** Every whitespace shape survives both
halves of the round trip byte-exactly: the converter's own `dump_yaml`, and
`helm template`. The proof is the tests below rather than a claim, and they
cover far more than the reported case — error.txt asks for exactly that:

  > "this is a whitespace-fidelity issue in the YAML pipeline, not a
  > Grafana-specific one. Verify no OTHER embedded config blob (scripts,
  > .properties files, certificates, PEM keys) is affected — trailing-newline
  > loss in a PEM certificate or a shell script WOULD break things, unlike INI."

That broader check matters here in a way the INI case does not: this repo's raw
manifests contain **23 PEM blobs and 10 shell scripts** (STATUS.md §1). None of
them lose anything.

So this folder is a REFUTATION, and the value it leaves behind is the permanent
regression test: the whole point of the report's note is that a future change to
the YAML pipeline could start clipping block scalars, and nothing else in the
suite would notice. `test_every_real_configmap_blob_round_trips` runs over the
repo's ACTUAL ConfigMap and Secret contents, so it fails on real data the day
that happens.
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
FIXTURE = FIXTURE_DIR / "platform-ops" / "log-agent.yaml"

NS = "platform-ops"
RELEASE = "log-agent"
RAW_DIRS = ["namespaces", "namespaces-showcase", "namespaces-live-demo"]


def _load_converter():
    spec = importlib.util.spec_from_file_location("convert_to_universal_chart", CONVERTER_PY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["convert_to_universal_chart"] = mod
    spec.loader.exec_module(mod)
    return mod


conv = _load_converter()


def _configmap(path=FIXTURE):
    for d in yaml.safe_load_all(path.read_text(encoding="utf-8")):
        if d and d.get("kind") == "ConfigMap":
            return d
    raise AssertionError("no ConfigMap in fixture")


def _helm_template(values: dict, release=RELEASE):
    with tempfile.TemporaryDirectory() as tmp:
        vf = pathlib.Path(tmp) / "values.yaml"
        vf.write_text(yaml.safe_dump(values, sort_keys=False), encoding="utf-8")
        proc = subprocess.run(["helm", "template", release, str(CHART_DIR), "-f", str(vf)],
                              capture_output=True, text=True)
    assert proc.returncode == 0, f"helm template failed:\n{proc.stderr}"
    return [d for d in yaml.safe_load_all(proc.stdout) if d]


def _rendered_cm(docs, name):
    for d in docs:
        if d.get("kind") == "ConfigMap" and d["metadata"]["name"] == name:
            return d
    raise AssertionError(f"chart rendered no ConfigMap/{name}")


# --------------------------------------------------------------------------
# The premise: the fixture really does carry the hazardous shapes
# --------------------------------------------------------------------------

def test_the_fixture_carries_the_shapes_that_could_be_clipped():
    data = _configmap()["data"]
    assert data["grafana.ini"].endswith("\n\n"), "the reported case must be present"
    assert data["many.txt"].endswith("\n\n\n")
    assert "BEGIN CERTIFICATE" in data["tls.crt"]
    assert data["entrypoint.sh"].startswith("#!")
    assert data["padded.conf"].startswith("key = value   \n"), "trailing spaces"


# --------------------------------------------------------------------------
# Half 1 — the converter's own YAML dump
# --------------------------------------------------------------------------

def test_the_converters_yaml_dump_is_byte_exact():
    data = _configmap()["data"]
    dumped = conv.dump_yaml({"data": data})
    assert yaml.safe_load(dumped)["data"] == data, (
        "dump_yaml changed a blob; compare key by key to find which")


def test_handle_configmap_does_not_touch_the_values():
    ctx = conv.MicroserviceContext(namespace=NS, name=RELEASE)
    conv.handle_configmap(_configmap(), ctx, "log-agent-config")
    assert ctx.values["configMaps"]["log-agent-config"]["data"] == _configmap()["data"]


# --------------------------------------------------------------------------
# Half 2 — helm template
# --------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_helm_renders_every_blob_byte_exactly():
    data = _configmap()["data"]
    values = {"workload": {"type": "none"}, "service": {"enabled": False},
              "configMaps": {"log-agent-config": {"data": data}}}
    got = _rendered_cm(_helm_template(values), "log-agent-config")["data"]
    for k in data:
        assert got[k] == data[k], (
            f"{k}: raw {data[k]!r}\n     rendered {got[k]!r}")


# --------------------------------------------------------------------------
# End to end, and then over the repo's REAL data
# --------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_round_trip_preserves_every_blob(tmp_path):
    out = tmp_path / "out"
    proc = subprocess.run(
        [sys.executable, str(CONVERTER_PY),
         "--input", str(FIXTURE_DIR), "--output", str(out),
         "--chart", str(CHART_DIR), "--skip-verify"],
        capture_output=True, text=True, cwd=str(GITOPS_DIR),
    )
    assert proc.returncode == 0, f"converter failed:\n{proc.stdout}\n{proc.stderr}"

    merged: dict = {}
    for vf in (out / NS / "defaults.yaml", out / "base" / f"{RELEASE}.yaml",
               out / NS / "values" / f"{RELEASE}.yaml"):
        if vf.exists():
            merged = conv.deep_merge(merged, yaml.safe_load(vf.read_text(encoding="utf-8")) or {})

    got = _rendered_cm(_helm_template(merged), "log-agent-config")["data"]
    want = _configmap()["data"]
    for k in want:
        assert got[k] == want[k], f"{k}: raw {want[k]!r} != rendered {got[k]!r}"


def test_every_real_configmap_blob_round_trips():
    """The lasting value of this folder. Runs over the repo's ACTUAL ConfigMap
    and Secret payloads — 23 PEM blobs and 10 shell scripts among them — so a
    future change to the YAML pipeline that starts clipping block scalars fails
    here, on real data, rather than being discovered on a cluster."""
    checked = 0
    for d in RAW_DIRS:
        root = GITOPS_DIR / d
        if not root.is_dir():
            continue
        for f in root.rglob("*.yaml"):
            for doc in yaml.safe_load_all(f.read_text(encoding="utf-8")):
                if not doc or doc.get("kind") not in ("ConfigMap", "Secret"):
                    continue
                for field in ("data", "stringData"):
                    blob = doc.get(field) or {}
                    values = {k: v for k, v in blob.items() if isinstance(v, str)}
                    if not values:
                        continue
                    checked += len(values)
                    back = yaml.safe_load(conv.dump_yaml({field: values}))[field]
                    for k, v in values.items():
                        assert back[k] == v, (
                            f"{f.relative_to(GITOPS_DIR)} {doc['metadata']['name']}.{k} "
                            f"changed: {v!r} -> {back[k]!r}")
    assert checked > 0, "no ConfigMap/Secret payloads found — the scan premise is stale"
