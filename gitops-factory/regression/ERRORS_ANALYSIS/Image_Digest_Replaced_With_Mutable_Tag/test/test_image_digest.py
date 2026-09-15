"""
Regression tests for ERRORS_ANALYSIS/Image_Digest_Replaced_With_Mutable_Tag.

Defect: split_image() recognises `repo:tag` but not `repo@sha256:<digest>`. Given
a digest it returns (repo, "") -- the digest is discarded -- so the chart's image
helper falls through to `repository:{{ .Chart.AppVersion }}` and renders
`postgres:2.0.0`. An immutable pin becomes a mutable tag, and the workload can
deploy a completely different binary than the manifest specified.

Note the TEMPLATE side is already correct: universal-chart.image already handles
`.Values.image.digest` and a repository that itself contains '@'. This is a
converter-only defect, which is why the fix lands entirely in split_image and its
callers. `test_chart_image_helper_already_renders_a_digest` documents that and
guards it.
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
FIXTURE = FIXTURE_DIR / "platform-data" / "postgres.yaml"

MAIN_DIGEST = "sha256:86ee6a2b6b1b1c8a1e2d0dcbbf0dcf5a3b7c9d4e5f60718293a4b5c6d7e8f900"
MAIN_IMAGE = f"postgres@{MAIN_DIGEST}"
SIDECAR_IMAGE = ("quay.io/prometheuscommunity/postgres-exporter@sha256:"
                 "1122334455667788990011223344556677889900112233445566778899001122")
INIT_IMAGE = ("busybox@sha256:"
              "9ae97d36d26566ff84e8893c64a6dc4fe8ca6d1144bf5b87b2b85a32def253c7")


def _load_converter():
    spec = importlib.util.spec_from_file_location("convert_to_universal_chart", CONVERTER_PY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["convert_to_universal_chart"] = mod
    spec.loader.exec_module(mod)
    return mod


conv = _load_converter()


def _raw_sts():
    for d in yaml.safe_load_all(FIXTURE.read_text(encoding="utf-8")):
        if d and d.get("kind") == "StatefulSet":
            return d
    raise AssertionError("no StatefulSet in fixture")


def _helm_template(values: dict, release="postgres"):
    with tempfile.TemporaryDirectory() as tmp:
        vf = pathlib.Path(tmp) / "values.yaml"
        vf.write_text(yaml.safe_dump(values, sort_keys=False), encoding="utf-8")
        proc = subprocess.run(
            ["helm", "template", release, str(CHART_DIR), "-f", str(vf)],
            capture_output=True, text=True,
        )
    assert proc.returncode == 0, f"helm template failed:\n{proc.stderr}"
    return [d for d in yaml.safe_load_all(proc.stdout) if d]


def _pod(docs):
    for d in docs:
        if d.get("kind") == "StatefulSet":
            return d["spec"]["template"]["spec"]
    raise AssertionError("chart rendered no StatefulSet")


# --------------------------------------------------------------------------
# Layer 1 — split_image itself
# --------------------------------------------------------------------------

def test_split_image_preserves_a_digest():
    repo, tag, digest = conv.split_image(MAIN_IMAGE)
    assert repo == "postgres"
    assert digest == MAIN_DIGEST, (
        "split_image discarded the @sha256 digest; the chart will substitute "
        "Chart.AppVersion and deploy a different image"
    )
    assert not tag, "a digest-pinned image has no tag"


@pytest.mark.parametrize("image,expected", [
    ("postgres:16-alpine", ("postgres", "16-alpine", "")),
    ("postgres", ("postgres", "", "")),
    ("quay.io/ns/app:1.2.3", ("quay.io/ns/app", "1.2.3", "")),
    ("registry:5000/ns/app:1.2.3", ("registry:5000/ns/app", "1.2.3", "")),
    ("registry:5000/ns/app", ("registry:5000/ns/app", "", "")),
    (f"postgres@{MAIN_DIGEST}", ("postgres", "", MAIN_DIGEST)),
    (f"quay.io/ns/app@{MAIN_DIGEST}", ("quay.io/ns/app", "", MAIN_DIGEST)),
    (f"registry:5000/ns/app@{MAIN_DIGEST}", ("registry:5000/ns/app", "", MAIN_DIGEST)),
    ("", ("", "", "")),
])
def test_split_image_forms(image, expected):
    """A registry host with a PORT (registry:5000/...) must not be mistaken for a
    tag — that is why the existing code checks for '/' after rpartition."""
    assert conv.split_image(image) == expected


# --------------------------------------------------------------------------
# Layer 2 — the converter must put the digest into values
# --------------------------------------------------------------------------

def test_converter_records_the_digest_for_the_main_container():
    ctx = conv.MicroserviceContext(namespace="platform-data", name="postgres")
    conv.handle_workload(_raw_sts(), ctx, "StatefulSet")

    full = ctx.full_image
    assert full.get("digest") == MAIN_DIGEST, (
        f"digest lost from ctx.full_image; got {full}"
    )
    assert full.get("repository") == "postgres"


def test_digest_lands_in_minimal_values_like_a_tag():
    """A digest is the version pin, exactly like a tag, so it belongs in the
    per-namespace minimal override rather than the shared base — otherwise
    bumping a pinned image forces every namespace sharing that base to move."""
    ctx = conv.MicroserviceContext(namespace="platform-data", name="postgres")
    conv.handle_workload(_raw_sts(), ctx, "StatefulSet")

    assert "digest" not in ctx.values.get("image", {}), \
        "digest must not be written into the shared base image block"
    minimal = conv.build_minimal_values(ctx)
    assert minimal["image"].get("digest") == MAIN_DIGEST, (
        f"digest missing from minimal values; got {minimal.get('image')}"
    )


def test_sidecar_and_init_container_images_keep_their_digest():
    """Those are raw pass-through strings, so the whole `repo@sha256:...` must
    survive verbatim."""
    ctx = conv.MicroserviceContext(namespace="platform-data", name="postgres")
    conv.handle_workload(_raw_sts(), ctx, "StatefulSet")
    raw_images = set(ctx.sub_raw_image.values())
    assert SIDECAR_IMAGE in raw_images, f"sidecar digest lost; got {raw_images}"
    assert INIT_IMAGE in raw_images, f"init container digest lost; got {raw_images}"


# --------------------------------------------------------------------------
# Layer 3 — the template side (already correct; guard it)
# --------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_chart_image_helper_already_renders_a_digest():
    values = {
        "workload": {"type": "statefulset"},
        "image": {"repository": "postgres", "digest": MAIN_DIGEST},
    }
    got = _pod(_helm_template(values))["containers"][0]["image"]
    assert got == MAIN_IMAGE, f"expected {MAIN_IMAGE}, got {got}"


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_digest_wins_over_a_tag_when_both_are_present():
    values = {
        "workload": {"type": "statefulset"},
        "image": {"repository": "postgres", "tag": "16-alpine", "digest": MAIN_DIGEST},
    }
    got = _pod(_helm_template(values))["containers"][0]["image"]
    assert got == MAIN_IMAGE, (
        f"a digest must win over a tag - it is the stronger pin. Got {got}"
    )


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_tagged_images_are_unaffected():
    values = {
        "workload": {"type": "statefulset"},
        "image": {"repository": "postgres", "tag": "16-alpine"},
    }
    assert _pod(_helm_template(values))["containers"][0]["image"] == "postgres:16-alpine"


# --------------------------------------------------------------------------
# Layer 4 — end to end: no ':2.0.0' anywhere
# --------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_round_trip_every_image_keeps_its_digest(tmp_path):
    out = tmp_path / "out"
    proc = subprocess.run(
        [sys.executable, str(CONVERTER_PY),
         "--input", str(FIXTURE_DIR), "--output", str(out),
         "--chart", str(CHART_DIR), "--skip-verify"],
        capture_output=True, text=True, cwd=str(GITOPS_DIR),
    )
    assert proc.returncode == 0, f"converter failed:\n{proc.stdout}\n{proc.stderr}"

    merged: dict = {}
    for vf in sorted(out.rglob("postgres.yaml")):
        if vf.parent.name == "releases":
            continue
        merged = conv.deep_merge(merged, yaml.safe_load(vf.read_text(encoding="utf-8")) or {})

    pod = _pod(_helm_template(merged))
    rendered = {c["image"] for c in pod["containers"]} | \
               {c["image"] for c in pod.get("initContainers", [])}

    raw_pod = _raw_sts()["spec"]["template"]["spec"]
    raw_images = {c["image"] for c in raw_pod["containers"]} | \
                 {c["image"] for c in raw_pod.get("initContainers", [])}

    assert rendered == raw_images, (
        "images differ after round-trip:\n"
        f"  raw      {sorted(raw_images)}\n"
        f"  rendered {sorted(rendered)}"
    )
    assert not any(":2.0.0" in i for i in rendered), (
        f"Chart.AppVersion leaked in as a tag: {sorted(rendered)}"
    )


# --------------------------------------------------------------------------
# Layer 5 — the fidelity checker must SEE a digest replaced by a tag
# --------------------------------------------------------------------------

def test_fidelity_checker_reports_a_digest_replaced_by_a_tag():
    raw = _raw_sts()
    rendered = yaml.safe_load(yaml.safe_dump(raw))
    rendered["spec"]["template"]["spec"]["containers"][0]["image"] = "postgres:2.0.0"

    diffs = conv._fidelity_diff("StatefulSet", raw, rendered)
    assert any(d[0] == "CHANGED" and d[1].endswith("image") for d in diffs), \
        f"checker did not report the image change; diffs={diffs}"
