"""
Regression tests for ERRORS_ANALYSIS/Service_SessionAffinity_Lost.

The report's two sources disagree: the MEDIUM table calls it "Load balancer
session affinity broken" (29 Services), while conflicts_and_warnings.txt calls
`sessionAffinity: None` "K8s default, safe to drop" (150 occurrences). Both are
true at once, and the number that decides it — how many Services carry a
NON-None value — is stated nowhere.

Grep proof (STATUS.md §1): **300 of 300** `sessionAffinity` values across
namespaces/, namespaces-showcase/ and namespaces-live-demo/ are `None`. Zero
non-None. `sessionAffinityConfig` appears zero times. So on this repo's data the
reported loss is cosmetic, exactly as conflicts_and_warnings.txt says.

That is NOT the end of it, and this is where the downgrade stops being a
downgrade. A `ClientIP` value is handled correctly for the PRIMARY Service —
build_service_entry keeps it and service.yaml renders it — but
`templates/services.yaml`, which renders every ADDITIONAL Service, has no
`sessionAffinity` block at all. So the field is genuinely dropped for any
Service beyond the first. The data does not exercise it; the code gap is real.
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
FIXTURE = FIXTURE_DIR / "shop-web" / "storefront.yaml"

NS = "shop-web"
RELEASE = "storefront"
RAW_DIRS = ["namespaces", "namespaces-showcase", "namespaces-live-demo"]


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


def _by_name(docs, kind="Service"):
    return {d["metadata"]["name"]: d for d in docs if d.get("kind") == kind}


# --------------------------------------------------------------------------
# The severity question, answered from the data rather than asserted.
# --------------------------------------------------------------------------

def test_every_session_affinity_in_this_repo_is_the_kubernetes_default():
    """The downgrade, proved. If a non-None value ever lands in the raw
    manifests this test fails and the severity has to be revisited."""
    values = []
    for d in RAW_DIRS:
        root = GITOPS_DIR / d
        if not root.is_dir():
            continue
        for f in root.rglob("*.yaml"):
            for line in f.read_text(encoding="utf-8").splitlines():
                s = line.strip()
                if s.startswith("sessionAffinity:"):
                    values.append(s.split(":", 1)[1].strip())
    if not values:
        # The mock dump that carries these (namespaces/) is not committed since
        # the merge into the chart repo — generate it to run this check:
        #   python generate_mock_environment.py --output namespaces --seed 42
        pytest.skip("no sessionAffinity in the raw trees present; namespaces/ not generated")
    non_default = sorted({v for v in values if v != "None"})
    assert not non_default, (
        f"{len(values)} sessionAffinity values found, and these are NOT the "
        f"Kubernetes default: {non_default}. The 'cosmetic' classification no "
        f"longer holds.")


def test_the_converter_keeps_a_real_value_and_drops_only_the_default():
    svc = _services()[0]
    entry = conv.build_service_entry(svc)
    assert entry["sessionAffinity"] == "ClientIP"
    assert entry["sessionAffinityConfig"] == {"clientIP": {"timeoutSeconds": 10800}}

    svc["spec"]["sessionAffinity"] = "None"
    assert "sessionAffinity" not in conv.build_service_entry(svc), (
        "None is the Kubernetes default and is deliberately not carried")


# --------------------------------------------------------------------------
# The real code gap: services.yaml, not service.yaml.
# --------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_both_service_templates_render_session_affinity():
    cfg = {"clientIP": {"timeoutSeconds": 3600}}
    values = {
        "workload": {"type": "deployment"},
        "image": {"repository": "nginx", "tag": "1.27"},
        "service": {"enabled": True, "name": RELEASE, "type": "ClusterIP",
                    "sessionAffinity": "ClientIP", "sessionAffinityConfig": cfg,
                    "ports": {"http": {"port": 80}}},
        "services": {"storefront-sticky": {
            "enabled": True, "type": "ClusterIP",
            "sessionAffinity": "ClientIP", "sessionAffinityConfig": cfg,
            "ports": {"http": {"port": 8080}}}},
    }
    svcs = _by_name(_helm_template(values))
    assert svcs[RELEASE]["spec"]["sessionAffinity"] == "ClientIP"
    assert svcs["storefront-sticky"]["spec"].get("sessionAffinity") == "ClientIP", (
        "services.yaml dropped sessionAffinity on the additional Service; got "
        f"{svcs['storefront-sticky']['spec']}")
    assert svcs["storefront-sticky"]["spec"].get("sessionAffinityConfig") == cfg


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_nothing_is_invented_when_no_affinity_is_declared():
    values = {
        "workload": {"type": "deployment"},
        "image": {"repository": "nginx", "tag": "1.27"},
        "service": {"enabled": True, "ports": {"http": {"port": 80}}},
        "services": {"extra": {"enabled": True, "ports": {"http": {"port": 81}}}},
    }
    for svc in _by_name(_helm_template(values)).values():
        assert "sessionAffinity" not in svc["spec"]


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_round_trip_session_affinity_survives_on_every_service(tmp_path):
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

    got = {n: (d["spec"].get("sessionAffinity"), d["spec"].get("sessionAffinityConfig"))
           for n, d in _by_name(_helm_template(merged)).items()}
    want = {s["metadata"]["name"]: (s["spec"].get("sessionAffinity"),
                                    s["spec"].get("sessionAffinityConfig"))
            for s in _services()}
    assert got == want, f"raw {want} != rendered {got}"


def test_fidelity_checker_reports_a_lost_session_affinity():
    raw = _services()[0]
    rendered = yaml.safe_load(yaml.safe_dump(raw))
    rendered["spec"].pop("sessionAffinity")
    diffs = conv._fidelity_diff("Service", raw, rendered)
    assert any(d[0] == "LOST" and "sessionAffinity" in d[1] for d in diffs), f"diffs={diffs}"
