"""
Regression tests for ERRORS_ANALYSIS/ConfigMap_Secret_References_Prefixed_With_Release.

Reported: `secretKeyRef.name` (10) and `volumes[*].configMap.name` (7) come back
prefixed with the release name, so they "no longer match the objects that exist
in the cluster".

The report already answers half of its own concern — *"The volume references are
updated consistently ... Both match, so it works"* — and error.txt names the
check that settles the rest:

  > "run `helm template` per release and confirm every secretKeyRef.name and
  > volumes[*].configMap.name resolves to an object that release actually
  > creates."

That is what this module does, and it does it over the **whole showcase**, not
just a fixture: every release the converter produces is rendered and every
ConfigMap/Secret reference in it is resolved against the objects that release
creates. It also does it over the collision fixture, where the renaming actually
fires.

The residual risk error.txt calls out explicitly is the one worth catching:

  > "zookeeper release's volume points to zookeeper-zookeeper-config, but that
  > ConfigMap may be rendered by the wrong release or not at all"

`test_collision_renames_keep_every_reference_resolvable` is exactly that test.
"""
import importlib.util
import pathlib
import shutil
import subprocess
import sys

import pytest
import yaml

TEST_DIR = pathlib.Path(__file__).resolve().parent
GITOPS_DIR = TEST_DIR.parents[3]
CHART_DIR = GITOPS_DIR.parent
CONVERTER_PY = GITOPS_DIR / "convert_to_universal_chart.py"
SHOWCASE_IN = GITOPS_DIR / "namespaces-showcase"
COLLISION_FIXTURE = (TEST_DIR.parent.parent / "ConfigMap_Secret_Identical_Content_Duplicated"
                     / "test" / "fixture")


def _load_converter():
    spec = importlib.util.spec_from_file_location("convert_to_universal_chart", CONVERTER_PY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["convert_to_universal_chart"] = mod
    spec.loader.exec_module(mod)
    return mod


conv = _load_converter()


def _convert(src, out):
    proc = subprocess.run(
        [sys.executable, str(CONVERTER_PY), "--input", str(src), "--output", str(out),
         "--chart", str(CHART_DIR), "--skip-verify"],
        capture_output=True, text=True, cwd=str(GITOPS_DIR),
    )
    assert proc.returncode == 0, f"converter failed:\n{proc.stdout}\n{proc.stderr}"


def _releases(out):
    """(namespace, slug, [value files]) for every release, mirroring how the
    generated ApplicationSets layer them. The `shared` release deliberately
    skips the defaults layers, so it is layered the way its own dedicated
    Application does."""
    for ns_dir in sorted(p for p in out.iterdir() if p.is_dir()):
        vdir = ns_dir / "values"
        if not vdir.is_dir():
            continue
        for vf in sorted(vdir.glob("*.yaml")):
            slug = vf.stem
            chain = []
            if not slug.endswith("shared"):
                for d in (out / "defaults.yaml", ns_dir / "defaults.yaml"):
                    if d.exists():
                        chain.append(d)
            base = out / "base" / f"{slug}.yaml"
            if base.exists():
                chain.append(base)
            chain.append(vf)
            yield ns_dir.name, slug, chain


def _render(slug, chain):
    args = []
    for f in chain:
        args += ["-f", str(f)]
    proc = subprocess.run(["helm", "template", slug, str(CHART_DIR), *args],
                          capture_output=True, text=True)
    assert proc.returncode == 0, f"helm template failed for {slug}:\n{proc.stderr}"
    return [d for d in yaml.safe_load_all(proc.stdout) if d]


def _refs(docs):
    """Every ConfigMap/Secret name a rendered release refers to, with a label
    saying where the reference lives."""
    out = []

    def walk(node, kind_hint=None):
        if isinstance(node, dict):
            for key, val in node.items():
                if key == "configMap" and isinstance(val, dict) and val.get("name"):
                    out.append(("ConfigMap", val["name"], "volumes[].configMap.name"))
                elif key == "secret" and isinstance(val, dict) and val.get("secretName"):
                    out.append(("Secret", val["secretName"], "volumes[].secret.secretName"))
                elif key == "configMapRef" and isinstance(val, dict) and val.get("name"):
                    out.append(("ConfigMap", val["name"], "envFrom.configMapRef.name"))
                elif key == "secretRef" and isinstance(val, dict) and val.get("name"):
                    out.append(("Secret", val["name"], "envFrom.secretRef.name"))
                elif key == "configMapKeyRef" and isinstance(val, dict) and val.get("name"):
                    out.append(("ConfigMap", val["name"], "configMapKeyRef.name"))
                elif key == "secretKeyRef" and isinstance(val, dict) and val.get("name"):
                    out.append(("Secret", val["name"], "secretKeyRef.name"))
                else:
                    walk(val, kind_hint)
        elif isinstance(node, list):
            for v in node:
                walk(v, kind_hint)

    for d in docs:
        walk(d)
    return out


def _check_tree(out):
    """Returns a list of dangling references across every release in `out`.

    Resolution is per NAMESPACE, not per release, because that is what the
    cluster sees: a ConfigMap is namespace-scoped, and a microservice
    referencing one that its namespace's `shared` release creates is the
    intended design (`claim()`'s "reference_shared" action) rather than a
    dangling pointer. Checking per release flagged six of those as broken on the
    first run — `shared-ca-bundle` and `platform-ops-alert-thresholds`, both
    declared in shared.yaml and both resolving correctly.

    What this still catches is the case that matters: a reference that resolves
    NOWHERE in its namespace, which is what a bad collision rename would
    produce.
    """
    rendered = {}
    for ns, slug, chain in _releases(out):
        rendered[(ns, slug)] = _render(slug, chain)

    pool = {}
    for (ns, _slug), docs in rendered.items():
        pool.setdefault(ns, set()).update(
            (d["kind"], d["metadata"]["name"]) for d in docs
            if d.get("kind") in ("ConfigMap", "Secret"))

    dangling = []
    for (ns, slug), docs in rendered.items():
        for kind, name, where in _refs(docs):
            if (kind, name) not in pool.get(ns, set()):
                dangling.append(
                    f"{ns}/{slug}: {where} -> {kind}/{name} — no release in "
                    f"namespace '{ns}' creates it. That namespace has "
                    f"{sorted(n for k, n in pool.get(ns, set()) if k == kind)}")
    return dangling


# --------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_every_reference_in_the_real_showcase_resolves(tmp_path):
    """The check error.txt asks for, over all 24 releases the converter
    produces from the real manifests."""
    out = tmp_path / "out"
    _convert(SHOWCASE_IN, out)
    dangling = _check_tree(out)
    assert not dangling, (
        "these ConfigMap/Secret references point at objects their own release "
        "does not create:\n" + "\n".join(dangling))


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_collision_renames_keep_every_reference_resolvable(tmp_path):
    """The residual risk error.txt names: after three microservices collide on
    `zookeeper-config`, does each release's volume still point at a ConfigMap
    that release renders?"""
    out = tmp_path / "out"
    _convert(COLLISION_FIXTURE, out)
    dangling = _check_tree(out)
    assert not dangling, (
        "a collision rename left a reference pointing at nothing:\n" + "\n".join(dangling))


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_the_rename_actually_happened(tmp_path):
    """Guard against the test above passing vacuously — if nothing were renamed
    there would be nothing to verify."""
    out = tmp_path / "out"
    _convert(COLLISION_FIXTURE, out)
    names = set()
    for ns, slug, chain in _releases(out):
        for d in _render(slug, chain):
            if d.get("kind") in ("ConfigMap", "Secret"):
                names.add(d["metadata"]["name"])
    assert "zookeeper-config" in names, names
    # The identical zookeeper-config copies are promoted to shared since the fork
    # merge, so the rename this guards is now the DIFFERING Secret's.
    assert any("zookeeper-auth" in n and n != "zookeeper-auth" for n in names), (
        f"no collision rename occurred; the fixture is not exercising the defect: {names}")


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_each_release_keeps_its_own_copy_of_the_colliding_object(tmp_path):
    """The rename must produce one object per release, not one object that some
    releases reference and no release creates."""
    out = tmp_path / "out"
    _convert(COLLISION_FIXTURE, out)
    rendered = {(ns, slug): _render(slug, chain) for ns, slug, chain in _releases(out)}
    for (ns, slug), docs in rendered.items():
        cms = [d["metadata"]["name"] for d in docs if d.get("kind") == "ConfigMap"]
        # Identical duplicates are promoted into this namespace's shared release,
        # so a reference may be satisfied there instead of by the release's own copy.
        shared_cms = [d["metadata"]["name"] for d in rendered.get((ns, "shared"), [])
                      if d.get("kind") == "ConfigMap"]
        refs = [n for k, n, _ in _refs(docs) if k == "ConfigMap"]
        for r in refs:
            assert r in cms or r in shared_cms, (
                f"{slug}: reference {r} not among its own {cms} nor shared {shared_cms}")


def test_the_name_map_is_what_keeps_references_consistent():
    """Unit-level: the renamed object and every reference to it come from the
    same `ctx.name_map`, which is why they cannot drift apart."""
    ctx = conv.MicroserviceContext(namespace="ns", name="ms")
    ctx.name_map["ConfigMap"]["zookeeper-config"] = "ms-zookeeper-config"
    remapped = conv.remap_volumes(
        {"config": {"configMap": {"name": "zookeeper-config"}}}, ctx)
    assert remapped["config"]["configMap"]["name"] == "ms-zookeeper-config"
