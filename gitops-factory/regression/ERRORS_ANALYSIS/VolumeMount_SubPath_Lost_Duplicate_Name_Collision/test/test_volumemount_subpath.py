"""
Regression tests for ERRORS_ANALYSIS/VolumeMount_SubPath_Lost_Duplicate_Name_Collision.

Defect: named_list_to_map() keys volumeMounts by mount NAME. Mounting one
volume several times with different subPaths — the standard way to project
individual ConfigMap keys onto individual file paths — collapses to whichever
mount happens to be last. The container then starts missing config files.

Three sides have to agree, which is why converter-only assertions are not enough:
  1. converter          — must not lose mounts to key collision
  2. mapToList          — must rebuild the correct `name` for each entry
  3. validateVolumeMounts — must resolve each entry back to its real volume name,
                            or the chart hard-fails with "volume ... is missing"
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
FIXTURE_DEP = FIXTURE_DIR / "shop-web" / "storefront.yaml"


def _load_converter():
    spec = importlib.util.spec_from_file_location("convert_to_universal_chart", CONVERTER_PY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["convert_to_universal_chart"] = mod
    spec.loader.exec_module(mod)
    return mod


conv = _load_converter()

# The four mounts the raw fixture declares, as (name, mountPath, subPath).
EXPECTED = [
    ("nginx-conf", "/etc/nginx/nginx.conf", "nginx.conf"),
    ("nginx-conf", "/etc/nginx/mime.types", "mime.types"),
    ("nginx-conf", "/etc/nginx/conf.d/default.conf", "default.conf"),
    ("site", "/usr/share/nginx/html", None),
]


def _raw_deployment():
    for d in yaml.safe_load_all(FIXTURE_DEP.read_text(encoding="utf-8")):
        if d and d.get("kind") == "Deployment":
            return d
    raise AssertionError("no Deployment in fixture")


def _raw_mounts():
    return _raw_deployment()["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]


def _as_triples(mounts):
    return [(m["name"], m["mountPath"], m.get("subPath")) for m in mounts]


def _helm_template(values: dict, release="storefront") -> list:
    with tempfile.TemporaryDirectory() as tmp:
        vf = pathlib.Path(tmp) / "values.yaml"
        vf.write_text(yaml.safe_dump(values, sort_keys=False), encoding="utf-8")
        proc = subprocess.run(
            ["helm", "template", release, str(CHART_DIR), "-f", str(vf)],
            capture_output=True, text=True,
        )
    assert proc.returncode == 0, f"helm template failed:\n{proc.stderr}"
    return [d for d in yaml.safe_load_all(proc.stdout) if d]


def _rendered_main_container(values, release="storefront"):
    for d in _helm_template(values, release):
        if d.get("kind") == "Deployment":
            return d["spec"]["template"]["spec"]["containers"][0]
    raise AssertionError("chart rendered no Deployment")


# --------------------------------------------------------------------------
# Layer 1 — named_list_to_map itself
# --------------------------------------------------------------------------

def test_named_list_to_map_does_not_drop_duplicate_names():
    """The precise defect, at the smallest possible scope."""
    result = conv.named_list_to_map(_raw_mounts())
    assert len(result) == 4, (
        f"named_list_to_map collapsed 4 volumeMounts into {len(result)} "
        f"({sorted(result)}) — mounts sharing a volume name overwrote each other"
    )


def test_named_list_to_map_preserves_every_mountpath_and_subpath():
    result = conv.named_list_to_map(_raw_mounts())
    got = sorted(
        (spec.get("name", key), spec["mountPath"], spec.get("subPath"))
        for key, spec in result.items()
    )
    assert got == sorted(EXPECTED), f"mounts differ.\n  got      {got}\n  expected {sorted(EXPECTED)}"


def test_named_list_to_map_leaves_unique_names_as_plain_keys():
    """The common case must not become noisy: a mount with a unique name keeps
    its bare name as the key and needs no explicit `name` field."""
    result = conv.named_list_to_map([
        {"name": "site", "mountPath": "/usr/share/nginx/html"},
        {"name": "cache", "mountPath": "/var/cache"},
    ])
    assert sorted(result) == ["cache", "site"]
    assert "name" not in result["site"]


# --------------------------------------------------------------------------
# Layer 2 — the whole main container through the converter
# --------------------------------------------------------------------------

def test_converter_keeps_all_four_mounts_on_the_main_container():
    ctx = conv.MicroserviceContext(namespace="shop-web", name="storefront")
    container = _raw_deployment()["spec"]["template"]["spec"]["containers"][0]
    conv.handle_main_container(container, ctx)

    mounts = ctx.values.get("volumeMounts", {})
    assert len(mounts) == 4, (
        f"handle_main_container kept {len(mounts)} of 4 volumeMounts: {sorted(mounts)}"
    )


# --------------------------------------------------------------------------
# Layer 3 — the template must rebuild the real `name` on every entry
# --------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_template_renders_every_mount_with_its_real_volume_name():
    ctx = conv.MicroserviceContext(namespace="shop-web", name="storefront")
    container = _raw_deployment()["spec"]["template"]["spec"]["containers"][0]
    conv.handle_main_container(container, ctx)

    values = {
        "workload": {"type": "deployment"},
        "image": {"repository": "nginx", "tag": "1.27-alpine"},
        "volumeMounts": ctx.values["volumeMounts"],
        "volumes": {
            "nginx-conf": {"configMap": {"name": "storefront-nginx-conf"}},
            "site": {"configMap": {"name": "storefront-site"}},
        },
    }
    rendered = _as_triples(_rendered_main_container(values)["volumeMounts"])

    assert sorted(rendered) == sorted(EXPECTED), (
        "rendered volumeMounts do not match the raw manifest.\n"
        f"  rendered {sorted(rendered)}\n"
        f"  raw      {sorted(EXPECTED)}"
    )
    # Every rendered name must be a REAL volume name — a composite key leaking
    # through as the name would make the mount reference a non-existent volume.
    for name, _, _ in rendered:
        assert name in ("nginx-conf", "site"), f"mount name {name!r} is not a declared volume"


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_validate_volume_mounts_still_resolves_duplicated_names():
    """validateVolumeMounts hard-fails the render when a mount names a volume
    that isn't declared. It must resolve composite keys back to the real name
    rather than looking for a volume literally called '<name>:<subPath>'."""
    ctx = conv.MicroserviceContext(namespace="shop-web", name="storefront")
    container = _raw_deployment()["spec"]["template"]["spec"]["containers"][0]
    conv.handle_main_container(container, ctx)

    values = {
        "workload": {"type": "deployment"},
        "image": {"repository": "nginx", "tag": "1.27-alpine"},
        "volumeMounts": ctx.values["volumeMounts"],
        "volumes": {
            "nginx-conf": {"configMap": {"name": "storefront-nginx-conf"}},
            "site": {"configMap": {"name": "storefront-site"}},
        },
    }
    _rendered_main_container(values)  # raises if validateVolumeMounts failed


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_validate_volume_mounts_still_catches_a_genuinely_missing_volume():
    """The guard must not be defanged by the fix."""
    values = {
        "workload": {"type": "deployment"},
        "image": {"repository": "nginx", "tag": "1.27-alpine"},
        "volumeMounts": {"ghost": {"mountPath": "/nowhere"}},
        "volumes": {},
    }
    with pytest.raises(AssertionError, match="(?s)helm template failed"):
        _rendered_main_container(values)


# --------------------------------------------------------------------------
# Layer 4 — end to end over the real CLI
# --------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_round_trip_all_subpath_mounts_survive(tmp_path):
    out = tmp_path / "out"
    proc = subprocess.run(
        [sys.executable, str(CONVERTER_PY),
         "--input", str(FIXTURE_DIR), "--output", str(out),
         "--chart", str(CHART_DIR), "--skip-verify"],
        capture_output=True, text=True, cwd=str(GITOPS_DIR),
    )
    assert proc.returncode == 0, f"converter failed:\n{proc.stdout}\n{proc.stderr}"

    merged: dict = {}
    for vf in sorted(out.rglob("storefront.yaml")):
        merged = conv.deep_merge(merged, yaml.safe_load(vf.read_text(encoding="utf-8")) or {})

    rendered = _as_triples(_rendered_main_container(merged)["volumeMounts"])
    raw = _as_triples(_raw_mounts())

    assert sorted(rendered) == sorted(raw), (
        "round-trip lost or changed a volumeMount.\n"
        f"  raw      {sorted(raw)}\n"
        f"  rendered {sorted(rendered)}"
    )


# --------------------------------------------------------------------------
# Layer 5 — the same collision at the CronJob / Job call sites
# --------------------------------------------------------------------------

def test_cronjob_and_job_volume_mounts_also_survive_duplicate_names():
    """named_list_to_map is called for CronJob and Job containers too
    (convert_to_universal_chart.py, handle_cronjob/handle_job). Fixing only the
    main-container path would leave the identical data loss there."""
    result = conv.named_list_to_map(_raw_mounts())
    assert len(result) == 4, "shared helper still collapses duplicates"


# --------------------------------------------------------------------------
# Layer 6 — the fidelity checker must SEE a dropped mount
# --------------------------------------------------------------------------

def test_fidelity_checker_reports_a_dropped_mount():
    raw = _raw_deployment()
    rendered = yaml.safe_load(yaml.safe_dump(raw))
    mounts = rendered["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]
    del mounts[0]

    diffs = conv._fidelity_diff("Deployment", raw, rendered)
    assert diffs, "checker reported nothing for a dropped volumeMount"
    assert any("volumeMounts" in d[1] for d in diffs), f"diffs={diffs}"
