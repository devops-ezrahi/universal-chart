"""
Regression tests for ERRORS_ANALYSIS/Route_And_Service_Names_Replaced_By_Fullname.

Defect: the chart names the primary Service and the primary Route with
`universal-chart.fullname`, so a raw `control-station-client-service` and
`control-station-client-route` both render as `control-station-client`. The
'-service' / '-route' suffixes are destroyed, and anything outside the release
that referenced those names (DNS, other Routes, hardcoded config) points at an
object that no longer exists.

Three references to the primary Service also hardcode fullname and must follow
it, or renaming the Service breaks them:
  * route.yaml    spec.to.name
  * routes.yaml   spec.to.name default (extra Routes)
  * ingress.yaml  backend.service.name

The Ingress' own metadata.name is left alone on purpose — error.txt does not
raise it and nothing in the fix requires it. See STATUS.md "OBSERVED, DEFERRED".
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
FIXTURE = FIXTURE_DIR / "shop-web" / "control-station-client.yaml"
FIXTURE_ING = FIXTURE_DIR / "shop-web" / "storefront.yaml"

RELEASE = "control-station-client"
SVC = "control-station-client-service"
ROUTE = "control-station-client-route"

# The Ingress half lives in its own release: universal-chart.validate refuses
# to render ingress.enabled together with route.enabled.
ING_RELEASE = "storefront"
ING_SVC = "storefront-service"


def _load_converter():
    spec = importlib.util.spec_from_file_location("convert_to_universal_chart", CONVERTER_PY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["convert_to_universal_chart"] = mod
    spec.loader.exec_module(mod)
    return mod


conv = _load_converter()


def _doc(kind, path=None):
    for d in yaml.safe_load_all((path or FIXTURE).read_text(encoding="utf-8")):
        if d and d.get("kind") == kind:
            return d
    raise AssertionError(f"no {kind} in fixture")


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


def _one(docs, kind):
    for d in docs:
        if d.get("kind") == kind:
            return d
    raise AssertionError(f"chart rendered no {kind}")


# --------------------------------------------------------------------------
# Layer 1 — converter must record the raw object names
# --------------------------------------------------------------------------

def test_converter_records_primary_service_name():
    ctx = conv.MicroserviceContext(namespace="shop-web", name=RELEASE)
    conv.handle_service(_doc("Service"), ctx, SVC)
    assert ctx.values["service"].get("name") == SVC, (
        "handle_service discarded the Service's own name; the chart will render "
        f"it as the release fullname {RELEASE!r} and {SVC!r} will not exist"
    )


def test_converter_records_primary_route_name_and_target():
    ctx = conv.MicroserviceContext(namespace="shop-web", name=RELEASE)
    conv.handle_route(_doc("Route"), ctx, ROUTE)
    route = ctx.values["route"]
    assert route.get("name") == ROUTE, f"Route name lost; got keys {sorted(route)}"
    assert route.get("serviceName") == SVC, (
        "the primary Route's spec.to.name was dropped, so it will target the "
        "release fullname instead of the Service the raw manifest named"
    )


def test_converter_records_ingress_backend_service_name():
    ctx = conv.MicroserviceContext(namespace="shop-web", name=ING_RELEASE)
    conv.handle_ingress(_doc("Ingress", FIXTURE_ING), ctx)
    assert ctx.values["ingress"].get("serviceName") == ING_SVC, (
        f"Ingress backend service name lost; got keys {sorted(ctx.values['ingress'])}"
    )


def test_converter_remaps_a_renamed_target_service():
    """If collision resolution renamed the Service object, every reference to it
    must follow — the same rule handle_workload applies to ConfigMaps/Secrets."""
    ctx = conv.MicroserviceContext(namespace="shop-web", name=RELEASE)
    ctx.name_map["Service"][SVC] = "other-" + SVC
    conv.handle_route(_doc("Route"), ctx, ROUTE)
    assert ctx.values["route"]["serviceName"] == "other-" + SVC


# --------------------------------------------------------------------------
# Layer 2 — templates must render those names, and still default
# --------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_templates_render_the_raw_service_and_route_names():
    values = {
        "workload": {"type": "deployment"},
        "image": {"repository": "nginx", "tag": "1.27-alpine"},
        "service": {"enabled": True, "name": SVC, "ports": {"http": {"port": 80}}},
        "route": {"enabled": True, "name": ROUTE, "serviceName": SVC},
    }
    docs = _helm_template(values)

    assert _one(docs, "Service")["metadata"]["name"] == SVC
    assert _one(docs, "Route")["metadata"]["name"] == ROUTE
    assert _one(docs, "Route")["spec"]["to"]["name"] == SVC


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_ingress_backend_follows_the_renamed_service():
    values = {
        "workload": {"type": "deployment"},
        "image": {"repository": "nginx", "tag": "1.27-alpine"},
        "service": {"enabled": True, "name": ING_SVC, "ports": {"http": {"port": 80}}},
        "ingress": {"enabled": True, "serviceName": ING_SVC,
                    "hosts": [{"host": "x.example.com",
                               "paths": [{"path": "/", "pathType": "Prefix", "portName": "http"}]}]},
    }
    docs = _helm_template(values, ING_RELEASE)
    backend = _one(docs, "Ingress")["spec"]["rules"][0]["http"]["paths"][0]["backend"]["service"]
    assert backend["name"] == ING_SVC, (
        f"Ingress still points at the release fullname; got {backend['name']!r}. "
        f"Renaming the Service without this leaves the Ingress dangling."
    )


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_templates_still_default_to_fullname_when_names_are_unset():
    """Pre-existing behaviour must survive the fix."""
    route_values = {
        "workload": {"type": "deployment"},
        "image": {"repository": "nginx"},
        "service": {"enabled": True, "ports": {"http": {"port": 80}}},
        "route": {"enabled": True},
    }
    docs = _helm_template(route_values)
    assert _one(docs, "Service")["metadata"]["name"] == RELEASE
    assert _one(docs, "Route")["metadata"]["name"] == RELEASE
    assert _one(docs, "Route")["spec"]["to"]["name"] == RELEASE

    ing_values = {
        "workload": {"type": "deployment"},
        "image": {"repository": "nginx"},
        "service": {"enabled": True, "ports": {"http": {"port": 80}}},
        "ingress": {"enabled": True,
                    "hosts": [{"host": "x.example.com",
                               "paths": [{"path": "/", "pathType": "Prefix", "portName": "http"}]}]},
    }
    docs = _helm_template(ing_values, ING_RELEASE)
    backend = _one(docs, "Ingress")["spec"]["rules"][0]["http"]["paths"][0]["backend"]["service"]
    assert backend["name"] == ING_RELEASE


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_extra_routes_default_to_the_renamed_primary_service():
    """routes.yaml's fallback must be the primary Service's REAL name, not the
    release fullname, or an extra Route with no explicit target dangles."""
    values = {
        "workload": {"type": "deployment"},
        "image": {"repository": "nginx"},
        "service": {"enabled": True, "name": SVC, "ports": {"http": {"port": 80}}},
        "routes": {"admin": {"enabled": True, "host": "admin.example.com"}},
    }
    docs = _helm_template(values)
    route = next(d for d in docs if d.get("kind") == "Route")
    assert route["spec"]["to"]["name"] == SVC


# --------------------------------------------------------------------------
# Layer 3 — end to end over the real CLI
# --------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_round_trip_object_names_match_the_raw_manifests(tmp_path):
    out = tmp_path / "out"
    proc = subprocess.run(
        [sys.executable, str(CONVERTER_PY),
         "--input", str(FIXTURE_DIR), "--output", str(out),
         "--chart", str(CHART_DIR), "--skip-verify"],
        capture_output=True, text=True, cwd=str(GITOPS_DIR),
    )
    assert proc.returncode == 0, f"converter failed:\n{proc.stdout}\n{proc.stderr}"

    merged: dict = {}
    for vf in sorted(out.rglob(f"{RELEASE}.yaml")):
        if vf.parent.name == "releases":
            continue
        merged = conv.deep_merge(merged, yaml.safe_load(vf.read_text(encoding="utf-8")) or {})

    docs = _helm_template(merged)

    raw_names = {d["kind"]: d["metadata"]["name"]
                 for d in yaml.safe_load_all(FIXTURE.read_text(encoding="utf-8")) if d}

    assert _one(docs, "Service")["metadata"]["name"] == raw_names["Service"]
    assert _one(docs, "Route")["metadata"]["name"] == raw_names["Route"]
    assert _one(docs, "Route")["spec"]["to"]["name"] == SVC

    # ...and the Ingress release from the same run
    ing_merged: dict = {}
    for vf in sorted(out.rglob(f"{ING_RELEASE}.yaml")):
        if vf.parent.name == "releases":
            continue
        ing_merged = conv.deep_merge(ing_merged, yaml.safe_load(vf.read_text(encoding="utf-8")) or {})
    ing_docs = _helm_template(ing_merged, ING_RELEASE)
    assert _one(ing_docs, "Service")["metadata"]["name"] == ING_SVC
    backend = _one(ing_docs, "Ingress")["spec"]["rules"][0]["http"]["paths"][0]["backend"]["service"]
    assert backend["name"] == ING_SVC


# --------------------------------------------------------------------------
# Layer 4 — the checker must SEE a renamed Service/Route
# --------------------------------------------------------------------------

def test_fidelity_checker_reports_a_changed_route_target():
    raw = _doc("Route")
    rendered = yaml.safe_load(yaml.safe_dump(raw))
    rendered["spec"]["to"]["name"] = RELEASE
    diffs = conv._fidelity_diff("Route", raw, rendered)
    assert any(d[0] == "CHANGED" and "to.name" in d[1] for d in diffs), f"diffs={diffs}"
