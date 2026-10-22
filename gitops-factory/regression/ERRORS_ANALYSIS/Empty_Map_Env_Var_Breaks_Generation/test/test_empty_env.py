"""
Regression tests for ERRORS_ANALYSIS/Empty_Map_Env_Var_Breaks_Generation.

An env var whose body is empty — `TYK_GW_ANALYTICSCONFIG_TYPE: {}` in the
report — renders through `universal-chart.mapToList` as

    - name: TYK_GW_ANALYTICSCONFIG_TYPE
      {}

which is a malformed env var: no `value`, no `valueFrom`.

error.txt records an open question the original session asked and never got an
answer to — *"Fix the data (converter side) or fix the template (chart side)?
Or should I do both for defense in depth?"* — and recommends BOTH. That is the
recorded decision, and it is what this folder implements: normalise in the
converter so the generated values files are right, and guard in the template so
no hand-written values file can produce broken YAML either.

**Normalised to `value: ""`, never dropped.** An env var explicitly set to the
empty string is meaningful — it is how a program is told to fall back to its own
default, which is exactly what Tyk does with this variable. Removing the variable
is NOT equivalent to setting it empty, so `filter it out` and `normalize to null`
(the other two options in error.txt) would both change behaviour.

Grep proof (STATUS.md §1): **0** env vars in this repo have an empty map or an
empty-string value, so nothing here is affected and the generated output is
unchanged. The report's two named files belong to the original project.
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
FIXTURE = FIXTURE_DIR / "shop-web" / "api-gateway.yaml"

NS = "shop-web"
RELEASE = "api-gateway"


def _load_converter():
    spec = importlib.util.spec_from_file_location("convert_to_universal_chart", CONVERTER_PY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["convert_to_universal_chart"] = mod
    spec.loader.exec_module(mod)
    return mod


conv = _load_converter()


def _doc():
    return next(d for d in yaml.safe_load_all(FIXTURE.read_text(encoding="utf-8"))
                if d and d.get("kind") == "Deployment")


def _helm_template(values: dict, release=RELEASE):
    with tempfile.TemporaryDirectory() as tmp:
        vf = pathlib.Path(tmp) / "values.yaml"
        vf.write_text(yaml.safe_dump(values, sort_keys=False), encoding="utf-8")
        proc = subprocess.run(["helm", "template", release, str(CHART_DIR), "-f", str(vf)],
                              capture_output=True, text=True)
    assert proc.returncode == 0, f"helm template failed:\n{proc.stderr}"
    return [d for d in yaml.safe_load_all(proc.stdout) if d]


def _env(docs, kind="Deployment"):
    for d in docs:
        if d.get("kind") == kind:
            return {e["name"]: e for e in
                    d["spec"]["template"]["spec"]["containers"][0].get("env", [])}
    raise AssertionError(f"chart rendered no {kind}")


# --------------------------------------------------------------------------
# Side 1 — the converter normalises
# --------------------------------------------------------------------------

def _env_by_name(raw):
    """conv.env_list keeps the deployment's list; index it by name here."""
    return {e["name"]: {k: v for k, v in e.items() if k != "name"} for e in conv.env_list(raw)}


def test_all_three_empty_shapes_normalise_to_an_empty_string_value():
    env = _env_by_name(
        _doc()["spec"]["template"]["spec"]["containers"][0]["env"])
    for name in ("GW_ANALYTICS_TYPE", "GW_EMPTY_MAP", "GW_BARE"):
        assert env[name] == {"value": ""}, f"{name} -> {env[name]}"


def test_a_real_value_is_untouched():
    env = _env_by_name(
        _doc()["spec"]["template"]["spec"]["containers"][0]["env"])
    assert env["GW_REAL"] == {"value": "keep-me"}


def test_no_variable_is_dropped():
    """The decision that matters. An env var set to "" is not the same as an
    absent one, so normalising must never reduce the count."""
    raw = _doc()["spec"]["template"]["spec"]["containers"][0]["env"]
    assert len(conv.env_list(raw)) == len(raw) == 4


def test_a_value_from_reference_is_not_mistaken_for_empty():
    env = _env_by_name([
        {"name": "SECRET", "valueFrom": {"secretKeyRef": {"name": "s", "key": "k"}}}])
    assert "valueFrom" in env["SECRET"] and "value" not in env["SECRET"]


# --------------------------------------------------------------------------
# Side 2 — the template guards independently
# --------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_the_template_renders_a_hand_written_empty_entry_safely():
    """Defence in depth: this values file never went through the converter."""
    values = {"workload": {"type": "deployment"},
              "image": {"repository": "nginx", "tag": "1.27"},
              "env": {"GW_EMPTY": {}, "GW_REAL": {"value": "keep-me"}}}
    env = _env(_helm_template(values))
    assert env["GW_EMPTY"] == {"name": "GW_EMPTY", "value": ""}, (
        f"malformed env var rendered: {env['GW_EMPTY']}")
    assert env["GW_REAL"]["value"] == "keep-me"


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_the_rendered_env_is_valid_for_kubernetes():
    """Every entry must have a name and exactly one of value / valueFrom."""
    values = {"workload": {"type": "deployment"},
              "image": {"repository": "nginx", "tag": "1.27"},
              "env": {"A": {}, "B": {"value": "x"},
                      "C": {"valueFrom": {"fieldRef": {"fieldPath": "metadata.name"}}}}}
    for name, e in _env(_helm_template(values)).items():
        assert "name" in e, e
        assert ("value" in e) ^ ("valueFrom" in e), f"{name} has neither or both: {e}"


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_the_guard_does_not_disturb_ports():
    """mapToList is shared with ports and volumeMounts, so the guard has to be
    invisible to entries that legitimately carry a body."""
    values = {"workload": {"type": "deployment"},
              "image": {"repository": "nginx", "tag": "1.27"},
              "ports": {"http": {"containerPort": 8080}}}
    dep = next(d for d in _helm_template(values) if d["kind"] == "Deployment")
    ports = dep["spec"]["template"]["spec"]["containers"][0]["ports"]
    assert ports == [{"name": "http", "containerPort": 8080}], ports


# --------------------------------------------------------------------------
# End to end
# --------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_round_trip_every_variable_survives_with_the_right_value(tmp_path):
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

    env = _env(_helm_template(merged))
    assert set(env) == {"GW_ANALYTICS_TYPE", "GW_EMPTY_MAP", "GW_BARE", "GW_REAL"}
    for name in ("GW_ANALYTICS_TYPE", "GW_EMPTY_MAP", "GW_BARE"):
        assert env[name].get("value") == "", f"{name} -> {env[name]}"
    assert env["GW_REAL"]["value"] == "keep-me"


def test_this_repo_has_no_empty_env_values():
    """Grep proof, as a live check."""
    hits = []
    for d in ("namespaces", "namespaces-showcase", "namespaces-live-demo"):
        root = GITOPS_DIR / d
        if not root.is_dir():
            continue
        for f in root.rglob("*.yaml"):
            for doc in yaml.safe_load_all(f.read_text(encoding="utf-8")):
                if not doc:
                    continue
                for path in (("spec", "template", "spec"),
                             ("spec", "jobTemplate", "spec", "template", "spec")):
                    cur = doc
                    for k in path:
                        cur = cur.get(k) if isinstance(cur, dict) else None
                    if not isinstance(cur, dict):
                        continue
                    for c in (cur.get("containers") or []) + (cur.get("initContainers") or []):
                        for e in (c.get("env") or []):
                            if e.get("value") in ({}, "") or (
                                    "value" not in e and "valueFrom" not in e):
                                hits.append(f"{f.name}: {e.get('name')}")
    assert not hits, "empty env values now exist in the raw manifests: " + str(hits)
