"""
Regression tests for ERRORS_ANALYSIS/Service_AppProtocol_Lost.

Defect: `spec.ports[*].appProtocol` is neither captured by `build_service_entry`
nor rendered by `service.yaml` / `services.yaml`. A mesh reads appProtocol to
pick an L7 handler, so a port that said `grpc` comes back as plain TCP and
gRPC-aware routing, retries and telemetry silently degrade.

Two-sided, and both sides are needed — a converter-only fix leaves the value in
values.yaml and still renders a port without it.

BOTH service templates matter. The primary Service renders through
`service.yaml`; every additional Service renders through `services.yaml`, which
is a separate copy of the same port loop. This fixture has two Services for
exactly that reason.

Grep proof that this repo's own data does not exercise it (see STATUS.md §1):
`appProtocol` appears 0 times across namespaces/, namespaces-showcase/ and
namespaces-live-demo/. The gap is real in the code; the fixture is what proves
it.
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


def _services():
    return [d for d in yaml.safe_load_all(FIXTURE.read_text(encoding="utf-8"))
            if d and d.get("kind") == "Service"]


def _helm_template(values: dict, release=RELEASE):
    with tempfile.TemporaryDirectory() as tmp:
        vf = pathlib.Path(tmp) / "values.yaml"
        vf.write_text(yaml.safe_dump(values, sort_keys=False), encoding="utf-8")
        proc = subprocess.run(
            ["helm", "template", release, str(CHART_DIR), "-f", str(vf)],
            capture_output=True, text=True,
        )
    assert proc.returncode == 0, f"helm template failed:\n{proc.stderr}"
    return [d for d in yaml.safe_load_all(proc.stdout) if d]


def _rendered_ports(docs):
    """rendered Service name -> {port name: appProtocol or None}."""
    return {d["metadata"]["name"]: {p["name"]: p.get("appProtocol")
                                    for p in d["spec"]["ports"]}
            for d in docs if d.get("kind") == "Service"}


# --------------------------------------------------------------------------
# Layer 1 — converter capture
# --------------------------------------------------------------------------

def test_build_service_entry_captures_app_protocol():
    entry = conv.build_service_entry(_services()[0])
    got = {name: p.get("appProtocol") for name, p in entry["ports"].items()}
    assert got == {"http": "http", "otlp-grpc": "grpc"}, (
        f"appProtocol dropped at capture; ports = {entry['ports']}")


def test_the_other_port_fields_are_unchanged():
    """Control: capturing one more field must not disturb the rest."""
    entry = conv.build_service_entry(_services()[0])
    assert entry["ports"]["otlp-grpc"]["port"] == 4317
    assert entry["ports"]["otlp-grpc"]["targetPort"] == 4317
    assert entry["ports"]["otlp-grpc"]["protocol"] == "TCP"


def test_no_app_protocol_is_invented():
    svc = _services()[0]
    for p in svc["spec"]["ports"]:
        p.pop("appProtocol")
    entry = conv.build_service_entry(svc)
    assert all("appProtocol" not in p for p in entry["ports"].values())


def test_both_services_carry_it_through_handle_service():
    ctx = conv.MicroserviceContext(namespace=NS, name=RELEASE)
    conv.handle_service(_services()[0], ctx, "api-gateway")
    conv.handle_service(_services()[1], ctx, "api-gateway-admin")
    assert ctx.values["service"]["ports"]["otlp-grpc"]["appProtocol"] == "grpc"
    assert ctx.values["services"]["api-gateway-admin"]["ports"]["admin-grpc"]["appProtocol"] == "grpc"


# --------------------------------------------------------------------------
# Layer 2 — BOTH templates
# --------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_both_service_templates_render_app_protocol():
    values = {
        "workload": {"type": "deployment"},
        "image": {"repository": "nginx", "tag": "1.27"},
        "service": {"enabled": True, "type": "ClusterIP", "name": "api-gateway",
                    "ports": {"http": {"port": 80, "appProtocol": "http"},
                              "otlp-grpc": {"port": 4317, "appProtocol": "grpc"}}},
        "services": {"api-gateway-admin": {
            "enabled": True, "type": "ClusterIP",
            "ports": {"admin-grpc": {"port": 9090, "appProtocol": "grpc"}}}},
    }
    got = _rendered_ports(_helm_template(values))
    assert got.get("api-gateway", {}).get("otlp-grpc") == "grpc", (
        f"service.yaml dropped appProtocol; rendered {got}")
    assert got.get("api-gateway-admin", {}).get("admin-grpc") == "grpc", (
        f"services.yaml dropped appProtocol; rendered {got}")


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_a_port_without_app_protocol_renders_without_the_key():
    values = {
        "workload": {"type": "deployment"},
        "image": {"repository": "nginx", "tag": "1.27"},
        "service": {"enabled": True, "ports": {"http": {"port": 80}}},
    }
    svc = next(d for d in _helm_template(values) if d["kind"] == "Service")
    assert "appProtocol" not in svc["spec"]["ports"][0]


# --------------------------------------------------------------------------
# Layer 3 — end to end
# --------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_round_trip_every_app_protocol_survives(tmp_path):
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

    got = _rendered_ports(_helm_template(merged))
    want = {svc["metadata"]["name"]: {p["name"]: p.get("appProtocol")
                                      for p in svc["spec"]["ports"]}
            for svc in _services()}
    assert got == want, f"raw {want} != rendered {got}"


def test_fidelity_checker_reports_a_lost_app_protocol():
    raw = _services()[0]
    rendered = yaml.safe_load(yaml.safe_dump(raw))
    for p in rendered["spec"]["ports"]:
        p.pop("appProtocol")
    diffs = conv._fidelity_diff("Service", raw, rendered)
    assert any(d[0] == "LOST" and "appProtocol" in d[1] for d in diffs), f"diffs={diffs}"
