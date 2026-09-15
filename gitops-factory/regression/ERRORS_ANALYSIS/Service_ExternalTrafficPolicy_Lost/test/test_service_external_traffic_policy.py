"""
Regression tests for ERRORS_ANALYSIS/Service_ExternalTrafficPolicy_Lost.

The report calls this MEDIUM — "Source IP not preserved for external LB traffic",
2 occurrences, `Service/zookeeper-external loses Cluster policy`. error.txt's own
note already spots that the semantics are backwards: `Local` is what preserves
the client source IP; `Cluster` SNATs and does not. `Cluster` is also the
Kubernetes default, so losing an explicit `Cluster` changes nothing at all.

Grep proof (STATUS.md §1): `externalTrafficPolicy` appears **0 times** across
namespaces/, namespaces-showcase/ and namespaces-live-demo/. The two reported
objects are not in this repo, so nothing here is affected either way.

The code gap behind it is nevertheless real, and it is not where the report
looked. The PRIMARY Service handles the field correctly — build_service_entry
captures it and service.yaml renders it — but `templates/services.yaml`, which
renders every ADDITIONAL Service, has no `externalTrafficPolicy` block at all.
So a second LoadBalancer Service silently loses it. Tested with `Local`, the
value that would actually matter.
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


def test_external_traffic_policy_now_appears_and_is_PRESERVED():
    """This asserted the repo had none at all, as the evidence for downgrading
    the severity. It fired when a later round added one — which is the tripwire
    working — and the re-check it demanded turned out well:

      raw     namespaces-showcase/platform-data/redis.yaml   externalTrafficPolicy: Local
      values  output-showcase/.../redis.yaml                 externalTrafficPolicy: Local
      render  fidelity report: 0 lost / 0 changed

    So the downgrade no longer rests on "this repo has none" — it rests on the
    stronger fact that the field now exists here and survives the round trip.
    The assertion is inverted rather than deleted: absence would mean the
    coverage had gone away again."""
    hits = []
    for d in RAW_DIRS:
        root = GITOPS_DIR / d
        if not root.is_dir():
            continue
        for f in root.rglob("*.yaml"):
            for line in f.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("externalTrafficPolicy:"):
                    hits.append(f"{f.relative_to(GITOPS_DIR)}: {line.strip()}")
    assert hits, "the corpus lost its externalTrafficPolicy coverage"
    assert all(h.endswith("externalTrafficPolicy: Local") for h in hits), hits


def test_the_converter_captures_it():
    assert conv.build_service_entry(_services()[0])["externalTrafficPolicy"] == "Local"


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_both_service_templates_render_external_traffic_policy():
    values = {
        "workload": {"type": "deployment"},
        "image": {"repository": "nginx", "tag": "1.27"},
        "service": {"enabled": True, "name": RELEASE, "type": "LoadBalancer",
                    "externalTrafficPolicy": "Local", "ports": {"http": {"port": 80}}},
        "services": {"storefront-lb": {
            "enabled": True, "type": "LoadBalancer",
            "externalTrafficPolicy": "Local", "ports": {"http": {"port": 8080}}}},
    }
    svcs = _by_name(_helm_template(values))
    assert svcs[RELEASE]["spec"]["externalTrafficPolicy"] == "Local"
    assert svcs["storefront-lb"]["spec"].get("externalTrafficPolicy") == "Local", (
        "services.yaml dropped externalTrafficPolicy on the additional Service; "
        f"got {svcs['storefront-lb']['spec']}")


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_nothing_is_invented_when_none_is_declared():
    values = {
        "workload": {"type": "deployment"},
        "image": {"repository": "nginx", "tag": "1.27"},
        "service": {"enabled": True, "ports": {"http": {"port": 80}}},
        "services": {"extra": {"enabled": True, "ports": {"http": {"port": 81}}}},
    }
    for svc in _by_name(_helm_template(values)).values():
        assert "externalTrafficPolicy" not in svc["spec"]


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_round_trip_external_traffic_policy_survives_on_every_service(tmp_path):
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

    got = {n: d["spec"].get("externalTrafficPolicy")
           for n, d in _by_name(_helm_template(merged)).items()}
    want = {s["metadata"]["name"]: s["spec"].get("externalTrafficPolicy")
            for s in _services()}
    assert got == want, f"raw {want} != rendered {got}"


def test_fidelity_checker_reports_a_lost_external_traffic_policy():
    raw = _services()[0]
    rendered = yaml.safe_load(yaml.safe_dump(raw))
    rendered["spec"].pop("externalTrafficPolicy")
    diffs = conv._fidelity_diff("Service", raw, rendered)
    assert any(d[0] == "LOST" and "externalTrafficPolicy" in d[1] for d in diffs), f"diffs={diffs}"
