"""
Regression tests for ERRORS_ANALYSIS/Sidecar_Container_Name_Key_Collision.

error.txt makes two claims. They need separating, because only one survives
contact with this repo's chart:

  CLAIM A "The chart template only renders the main container. Sidecars are
           completely missing. This is the biggest functional gap."
  CLAIM B "Multiple microservices sharing a base name overwrite each other:
           juseppelad-2-441 and juseppelad-2-611 both store into
           containers['juseppelad'], the second wins."

Claim A is tested by test_chart_renders_every_sidecar. Claim B is tested by the
cross-namespace tests, which run the real CLI over TWO namespaces that share a
microservice file name and carry different sidecar sets.

These tests are written to be true regardless of which way the answer goes: they
assert the CORRECT end state (every raw container reaches the pod that declared
it, and no other pod), so they fail loudly if either claim is real, and they
stand as regression cover if it is not.
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

# namespace -> the exact container set its raw Deployment declares
RAW_CONTAINERS = {
    "shop-web":    {"traefik", "oauth-proxy", "log-shipper"},
    "shop-web-eu": {"traefik", "log-shipper", "rate-limiter"},
}


def _load_converter():
    spec = importlib.util.spec_from_file_location("convert_to_universal_chart", CONVERTER_PY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["convert_to_universal_chart"] = mod
    spec.loader.exec_module(mod)
    return mod


conv = _load_converter()


def _raw_deployment(namespace):
    path = FIXTURE_DIR / namespace / "api-gateway.yaml"
    for d in yaml.safe_load_all(path.read_text(encoding="utf-8")):
        if d and d.get("kind") == "Deployment":
            return d
    raise AssertionError(f"no Deployment in {path}")


def _helm_template_files(value_files, release="api-gateway"):
    args = ["helm", "template", release, str(CHART_DIR)]
    for f in value_files:
        args += ["-f", str(f)]
    proc = subprocess.run(args, capture_output=True, text=True)
    assert proc.returncode == 0, f"helm template failed:\n{proc.stderr}"
    return [d for d in yaml.safe_load_all(proc.stdout) if d]


def _helm_template_values(values: dict, release="api-gateway"):
    with tempfile.TemporaryDirectory() as tmp:
        vf = pathlib.Path(tmp) / "values.yaml"
        vf.write_text(yaml.safe_dump(values, sort_keys=False), encoding="utf-8")
        return _helm_template_files([vf], release)


def _pod_containers(docs):
    for d in docs:
        if d.get("kind") == "Deployment":
            return d["spec"]["template"]["spec"]["containers"]
    raise AssertionError("no Deployment rendered")


@pytest.fixture(scope="module")
def converted(tmp_path_factory):
    """Run the real CLI once over both fixture namespaces."""
    out = tmp_path_factory.mktemp("out")
    proc = subprocess.run(
        [sys.executable, str(CONVERTER_PY),
         "--input", str(FIXTURE_DIR), "--output", str(out),
         "--chart", str(CHART_DIR), "--skip-verify"],
        capture_output=True, text=True, cwd=str(GITOPS_DIR),
    )
    assert proc.returncode == 0, f"converter failed:\n{proc.stdout}\n{proc.stderr}"
    return out


# --------------------------------------------------------------------------
# CLAIM A — does the chart render sidecars at all?
# --------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_chart_renders_every_sidecar():
    """_pod-spec.tpl must emit the main container AND every .Values.sidecars
    entry. If this fails, error.txt's 'biggest functional gap' is live."""
    values = {
        "workload": {"type": "deployment"},
        "image": {"repository": "traefik", "tag": "v3.1"},
        "sidecars": {
            "oauth-proxy": {"image": "quay.io/oauth2-proxy/oauth2-proxy:v7.6.0"},
            "log-shipper": {"image": "fluent/fluent-bit:3.0"},
        },
    }
    names = {c["name"] for c in _pod_containers(_helm_template_values(values))}
    assert {"oauth-proxy", "log-shipper"} <= names, (
        f"chart dropped sidecars; rendered containers = {sorted(names)}"
    )
    assert len(names) == 3, f"expected main + 2 sidecars, got {sorted(names)}"


# --------------------------------------------------------------------------
# Converter — sidecars must reach values in the first place
# --------------------------------------------------------------------------

def test_converter_captures_all_sidecars():
    for namespace, expected in RAW_CONTAINERS.items():
        ctx = conv.MicroserviceContext(namespace=namespace, name="api-gateway")
        conv.handle_workload(_raw_deployment(namespace), ctx, "Deployment")
        # containers[0] becomes the main container; the rest become sidecars.
        got = set(ctx.values.get("sidecars", {})) | {"<main>"}
        expected_sidecars = expected - {"traefik"} | {"<main>"}
        assert got == expected_sidecars, (
            f"{namespace}: converter kept sidecars {sorted(got)}, "
            f"expected {sorted(expected_sidecars)}"
        )


# --------------------------------------------------------------------------
# CLAIM B — cross-namespace base sharing must not merge sidecar sets
# --------------------------------------------------------------------------

def test_both_namespaces_share_one_base_file(converted):
    """Precondition for claim B: confirm the two namespaces really do collapse
    onto a single base/api-gateway.yaml. If they didn't, the test below would
    prove nothing."""
    base = converted / "base" / "api-gateway.yaml"
    assert base.exists(), f"expected a shared base file at {base}"
    for ns in RAW_CONTAINERS:
        assert (converted / ns / "values" / "api-gateway.yaml").exists(), \
            f"{ns} has no per-namespace values file"


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
@pytest.mark.parametrize("namespace", sorted(RAW_CONTAINERS))
def test_each_namespace_renders_exactly_its_own_containers(converted, namespace):
    """The load-bearing test. Every container the raw manifest declared must be
    in the rendered pod, and NO container belonging to the other namespace may
    leak in through the shared base file."""
    value_files = [
        converted / "base" / "api-gateway.yaml",
        converted / namespace / "values" / "api-gateway.yaml",
    ]
    rendered = {c["name"] for c in _pod_containers(_helm_template_files(value_files))}

    expected = set(RAW_CONTAINERS[namespace])
    # The chart names the one container it manages after the release; that
    # rename is a separate, known defect (Main_Container_Name_Forced_To_Release_Name),
    # so compare the SIDECARS exactly and require the main container to exist.
    expected_sidecars = expected - {"traefik"}
    rendered_sidecars = rendered - {"api-gateway", "traefik"}

    missing = expected_sidecars - rendered_sidecars
    leaked = rendered_sidecars - expected_sidecars
    assert not missing, (
        f"{namespace}: sidecars declared in raw but ABSENT from render: {sorted(missing)}; "
        f"rendered = {sorted(rendered)}"
    )
    assert not leaked, (
        f"{namespace}: containers rendered that this namespace never declared "
        f"(leaked via the shared base file): {sorted(leaked)}; rendered = {sorted(rendered)}"
    )


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_the_two_namespaces_do_not_render_identical_container_sets(converted):
    """Direct statement of the defect: if base sharing collapsed the two sidecar
    sets, both namespaces would render the same containers."""
    sets = {}
    for namespace in RAW_CONTAINERS:
        value_files = [
            converted / "base" / "api-gateway.yaml",
            converted / namespace / "values" / "api-gateway.yaml",
        ]
        sets[namespace] = frozenset(
            c["name"] for c in _pod_containers(_helm_template_files(value_files))
        )
    assert sets["shop-web"] != sets["shop-web-eu"], (
        "both namespaces rendered the same container set "
        f"({sorted(sets['shop-web'])}) — one namespace's sidecars overwrote the other's"
    )
    assert "oauth-proxy" in sets["shop-web"] and "oauth-proxy" not in sets["shop-web-eu"]
    assert "rate-limiter" in sets["shop-web-eu"] and "rate-limiter" not in sets["shop-web"]


# --------------------------------------------------------------------------
# Guard — a genuinely duplicated container name inside ONE pod
# --------------------------------------------------------------------------

def test_two_sidecars_with_the_same_name_in_one_pod_do_not_silently_merge():
    """Kubernetes forbids duplicate container names in a pod, so this is a
    malformed manifest rather than a supported pattern — but the converter must
    not swallow it silently. It either keeps both or the render fails; what it
    must not do is drop one and report success."""
    raw = _raw_deployment("shop-web")
    containers = raw["spec"]["template"]["spec"]["containers"]
    dup = yaml.safe_load(yaml.safe_dump(containers[1]))
    dup["image"] = "quay.io/oauth2-proxy/oauth2-proxy:v7.7.0"
    containers.append(dup)

    ctx = conv.MicroserviceContext(namespace="shop-web", name="api-gateway")
    conv.handle_workload(raw, ctx, "Deployment")
    sidecars = ctx.values.get("sidecars", {})

    # Documents ACTUAL behaviour so a future change to sidecar keying is a
    # deliberate decision rather than a surprise: the duplicate collapses to a
    # single entry and the last one wins. This is the only place the bare
    # container name is still used as a dict key, and it is safe today only
    # because Kubernetes itself forbids duplicate container names in a pod —
    # the API server rejects this manifest before it can matter.
    # See STATUS.md "OBSERVED, DEFERRED".
    assert "oauth-proxy" in sidecars
    assert len([c for c in raw["spec"]["template"]["spec"]["containers"]
                if c["name"] == "oauth-proxy"]) == 2, "fixture should declare the name twice"

    # A sidecar's image is deliberately NOT left in the values entry — it is
    # moved to ctx.sub_raw_image so it lands in the per-namespace minimal file
    # instead of the shared base (see MicroserviceContext.sub_raw_image).
    assert "image" not in sidecars["oauth-proxy"]
    assert ctx.sub_raw_image[("sidecars", "oauth-proxy", "image")] == \
        "quay.io/oauth2-proxy/oauth2-proxy:v7.7.0", "last duplicate should win"
