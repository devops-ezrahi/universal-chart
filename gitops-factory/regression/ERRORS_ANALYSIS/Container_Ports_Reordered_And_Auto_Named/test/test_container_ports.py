"""
Regression tests for ERRORS_ANALYSIS/Container_Ports_Reordered_And_Auto_Named.

One report row, two symptoms with different answers — error.txt says so itself:
*"Port ordering is generally cosmetic to Kubernetes, but auto-naming is NOT."*

**AUTO-NAMING — real, fixed.** `ports_list_to_map` keyed every port by
`p["name"] or f"port-{containerPort}"`, and `mapToList` rendered that key back
as `name:`. So a port the raw manifest left unnamed came out named `port-9000`.
Kubernetes resolves a Service `targetPort` and a probe `port:` BY NAME, so the
object now answers to a name nothing referenced and that did not exist before.
Fixed by marking the synthetic key `unnamed: true` and having `mapToList` drop
both the marker and the `name:` line.

**ORDERING — cosmetic to Kubernetes, but it WAS being reported.** The values map
loses declaration order because Helm's `range` iterates map keys in sorted
order. Kubernetes attaches no meaning to `containers[].ports` order — nothing
selects a port positionally; Services and probes select by name or by number —
so preserving it would mean converting the ports map to a list, changing the
values schema for every chart consumer to fix something with no effect.

What DID need fixing was the checker. I assumed `_by_name` already paired ports
by name and made it a test; the test failed. `_by_name` bails out to positional
comparison as soon as ANY entry in the list is unnamed, so a single unnamed port
dropped the whole list to positional and the sorted-key reorder was reported as
one big CHANGED covering every port at once — which is almost certainly what the
report's 9 occurrences are. `_item_name` now falls back to
`containerPort/protocol` as the identity for an unnamed port, so a reorder is
invisible and a genuinely changed port is named precisely. Both are pinned
below.
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


def _doc(kind):
    for d in yaml.safe_load_all(FIXTURE.read_text(encoding="utf-8")):
        if d and d.get("kind") == kind:
            return d
    raise AssertionError(f"no {kind} in fixture")


def _raw_ports():
    return _doc("Deployment")["spec"]["template"]["spec"]["containers"][0]["ports"]


def _helm_template(values: dict, release=RELEASE):
    with tempfile.TemporaryDirectory() as tmp:
        vf = pathlib.Path(tmp) / "values.yaml"
        vf.write_text(yaml.safe_dump(values, sort_keys=False), encoding="utf-8")
        proc = subprocess.run(["helm", "template", release, str(CHART_DIR), "-f", str(vf)],
                              capture_output=True, text=True)
    assert proc.returncode == 0, f"helm template failed:\n{proc.stderr}"
    return [d for d in yaml.safe_load_all(proc.stdout) if d]


def _rendered_ports(docs):
    for d in docs:
        if d.get("kind") == "Deployment":
            return d["spec"]["template"]["spec"]["containers"][0]["ports"]
    raise AssertionError("chart rendered no Deployment")


# --------------------------------------------------------------------------
# Auto-naming — the half that is real
# --------------------------------------------------------------------------

def test_an_unnamed_port_is_marked_rather_than_named():
    """Keys gained a `-<protocol>` suffix in ERRORS_NEW_ANALYSIS/
    Missing_Container_Port_9125. Kubernetes' uniqueness rule is on the
    containerPort/**protocol pair**, so keying an unnamed port on the number
    alone let `9125/UDP` overwrite `9125/TCP` and silently lose a port — the one
    defect in that report whose root cause was recorded as UNKNOWN. The marker
    behaviour this test guards is unchanged; only the key shape moved."""
    got = conv.ports_list_to_map(_raw_ports())
    assert got["port-9000-tcp"]["unnamed"] is True
    assert got["port-8081-tcp"]["unnamed"] is True
    assert "name" not in got["port-9000-tcp"]


def test_a_named_port_keeps_its_name_and_carries_no_marker():
    got = conv.ports_list_to_map(_raw_ports())
    assert "unnamed" not in got["http"]
    assert "unnamed" not in got["metrics"]
    assert got["http"]["containerPort"] == 8080


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_the_rendered_port_has_no_invented_name():
    values = {"workload": {"type": "deployment"},
              "image": {"repository": "nginx", "tag": "1.27"},
              "ports": {"http": {"containerPort": 8080},
                        "port-9000": {"containerPort": 9000, "unnamed": True}}}
    by_number = {p["containerPort"]: p for p in _rendered_ports(_helm_template(values))}
    assert by_number[9000] == {"containerPort": 9000}, (
        f"the unnamed port acquired a name: {by_number[9000]}")
    assert by_number[8080]["name"] == "http"


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_the_marker_never_leaks_into_the_manifest():
    values = {"workload": {"type": "deployment"},
              "image": {"repository": "nginx", "tag": "1.27"},
              "ports": {"port-9000": {"containerPort": 9000, "protocol": "TCP",
                                      "unnamed": True}}}
    port = _rendered_ports(_helm_template(values))[0]
    assert "unnamed" not in port, f"bookkeeping marker rendered into the object: {port}"
    assert port == {"containerPort": 9000, "protocol": "TCP"}


# --------------------------------------------------------------------------
# Ordering — the half that is cosmetic, shown rather than asserted
# --------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_every_port_survives_even_though_the_order_changes():
    values = {"workload": {"type": "deployment"},
              "image": {"repository": "nginx", "tag": "1.27"},
              "ports": conv.ports_list_to_map(_raw_ports())}
    rendered = _rendered_ports(_helm_template(values))
    assert sorted(p["containerPort"] for p in rendered) == \
        sorted(p["containerPort"] for p in _raw_ports())
    assert {p.get("name") for p in rendered} == {p.get("name") for p in _raw_ports()}


def test_the_fidelity_checker_pairs_ports_by_name_not_position():
    """Why the reorder is invisible where it matters: the checker matches list
    entries by name, so a different order is not reported as a change."""
    raw = _doc("Deployment")
    rendered = yaml.safe_load(yaml.safe_dump(raw))
    ports = rendered["spec"]["template"]["spec"]["containers"][0]["ports"]
    ports.reverse()
    assert not conv._fidelity_diff("Deployment", raw, rendered), (
        "reordering alone was reported as a difference")


def test_a_genuinely_changed_port_is_still_reported():
    raw = _doc("Deployment")
    rendered = yaml.safe_load(yaml.safe_dump(raw))
    rendered["spec"]["template"]["spec"]["containers"][0]["ports"][1]["containerPort"] = 9999
    diffs = conv._fidelity_diff("Deployment", raw, rendered)
    assert any(d[0] == "CHANGED" and "containerPort" in d[1] for d in diffs), f"diffs={diffs}"


# --------------------------------------------------------------------------
# End to end — the reference that would actually break
# --------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_round_trip_ports_and_the_names_that_reference_them(tmp_path):
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

    docs = _helm_template(merged)
    rendered = _rendered_ports(docs)
    raw = _raw_ports()

    assert len(rendered) == len(raw) == 4
    for r in raw:
        match = [p for p in rendered if p["containerPort"] == r["containerPort"]]
        assert len(match) == 1, f"port {r['containerPort']} missing or duplicated"
        assert match[0].get("name") == r.get("name"), (
            f"port {r['containerPort']}: raw name {r.get('name')!r}, "
            f"rendered {match[0].get('name')!r}")

    # The reference that would actually break: a probe and a Service resolving
    # `http` by name must still find a port called `http`.
    names = {p.get("name") for p in rendered}
    dep = next(d for d in docs if d["kind"] == "Deployment")
    probe_port = dep["spec"]["template"]["spec"]["containers"][0]["readinessProbe"]["httpGet"]["port"]
    assert probe_port in names, f"probe references {probe_port!r}, ports are {names}"
    svc = next(d for d in docs if d["kind"] == "Service")
    assert svc["spec"]["ports"][0]["targetPort"] in names
