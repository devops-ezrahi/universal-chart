"""
Regression tests for a defect FOUND during folders 8/9 that has no ERRORS_ANALYSIS
folder of its own. Candidate for one of the two missing report issues (28/29).

THE DEFECT, as originally found
-------------------------------
    convert_to_universal_chart.py, main(), Pass 2:

        if not NAMESPACE_SCOPED_ONLY:
            (output_dir / DEFAULTS_FILE_NAME).write_text(... global_defaults ...)
        for ms_list in per_namespace_ms.values():
            for _, ctx in ms_list:
                ctx.values = subtract_defaults(ctx.values, global_defaults)   # unconditional

The tree-root defaults.yaml was written ONLY in cluster-scoped mode, but the
subtraction of global_defaults from every microservice's values ran in BOTH
modes. Namespace-scoped is the DEFAULT, so by default every value that happened
to be identical across all microservices in the run was removed from the output
and written nowhere. namespace_defaults is computed in Pass 3 from ctx.values
AFTER that subtraction, so nothing taken could ever reappear: a silent, total
loss of workload.type, containerName, ports, service.ports and image.repository
whenever the input was homogeneous enough for them to be common.

WHAT GUARDS IT NOW
------------------
There is no global defaults layer at all any more. The ms-applicationSet chart
layers exactly three files per release — base/<file> -> <ns>/defaults.yaml ->
<ns>/values/<file> — and nothing is subtracted against a layer that is not in
that chain. These tests hold the general invariant the defect violated rather
than the specific line that violated it:

  * every value common to all microservices survives into the effective values
    (test_namespace_scoped_output_keeps_every_common_value);
  * every valueFile the chart references exists on disk, since ArgoCD fails the
    whole sync on a missing one
    (test_every_value_file_the_chart_references_is_actually_on_disk);
  * no tree-root defaults.yaml is written, in either mode, because no layer
    would read it (test_cluster_scoped_mode_keeps_every_common_value_too).

SECOND CASE, found later while probing folder 15
------------------------------------------------
The same "subtracted but never layered back" shape survived the first fix for
one release: `shared`, which at the time had a dedicated Application whose
valueFiles chain deliberately skipped both defaults layers. Observed:
`workload.type: none` and `service.enabled: false` disappeared from
base/shared.yaml and the release rendered a Deployment and a Service it never
declared.

`shared` now goes through the SAME three-file chain as every other release, so
the asymmetry that caused this is gone. The risk inverted, though: a chain it
DOES read can ADD to it. That is why <ns>/defaults.yaml is computed with the
shared release in the pool — common_subtree only promotes a key present with an
identical value in EVERY member — and why
test_namespace_defaults_never_add_anything_to_the_shared_release guards it.
"""
import importlib.util
import pathlib
import re
import shutil
import subprocess
import sys

import pytest
import yaml

TEST_DIR = pathlib.Path(__file__).resolve().parent
GITOPS_DIR = TEST_DIR.parents[3]
CHART_DIR = GITOPS_DIR.parent
CONVERTER_PY = GITOPS_DIR / "convert_to_universal_chart.py"
FIXTURE_DIR = TEST_DIR / "fixture"
# Second fixture, for the 'shared' half of the same defect (see the module
# docstring's SECOND CASE): two namespaces whose microservices are config-only,
# so global_defaults holds workload.type/service.enabled — the very values each
# namespace's `shared` release also carries.
SHARED_FIXTURE_DIR = TEST_DIR / "fixture-shared"

# Values every microservice in the fixture shares, so all of them land in
# global_defaults and all of them disappear unless the layer is preserved.
EXPECTED_IN_EFFECTIVE_VALUES = {
    ("workload", "type"): "deployment",
    ("containerName",): "nginx",
    ("ports", "http", "containerPort"): 8080,
    ("service", "enabled"): True,
    ("service", "ports", "http", "port"): 80,
}


def _load_converter():
    spec = importlib.util.spec_from_file_location("convert_to_universal_chart", CONVERTER_PY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["convert_to_universal_chart"] = mod
    spec.loader.exec_module(mod)
    return mod


conv = _load_converter()


def _dig(d, path):
    for key in path:
        if not isinstance(d, dict) or key not in d:
            return None
        d = d[key]
    return d


def _run(out, extra_args=()):
    proc = subprocess.run(
        [sys.executable, str(CONVERTER_PY),
         "--input", str(FIXTURE_DIR), "--output", str(out),
         "--chart", str(CHART_DIR), "--skip-verify", *extra_args],
        capture_output=True, text=True, cwd=str(GITOPS_DIR),
    )
    assert proc.returncode == 0, f"converter failed:\n{proc.stdout}\n{proc.stderr}"


def _effective_values(out, namespace, release):
    """Layer exactly what the generated ApplicationSet layers, in order."""
    chain = []
    root_defaults = out / "defaults.yaml"
    if root_defaults.exists():
        chain.append(root_defaults)
    ns_defaults = out / namespace / "defaults.yaml"
    if ns_defaults.exists():
        chain.append(ns_defaults)
    base = out / "base" / f"{release}.yaml"
    if base.exists():
        chain.append(base)
    chain.append(out / namespace / "values" / f"{release}.yaml")

    merged: dict = {}
    for f in chain:
        merged = conv.deep_merge(merged, yaml.safe_load(f.read_text(encoding="utf-8")) or {})
    return merged


@pytest.mark.parametrize("release", ["storefront", "catalog"])
def test_namespace_scoped_output_keeps_every_common_value(tmp_path, release):
    """The load-bearing test: nothing common to all microservices may vanish."""
    out = tmp_path / "out"
    _run(out)

    values = _effective_values(out, "shop-web", release)
    missing = {
        ".".join(p): want for p, want in EXPECTED_IN_EFFECTIVE_VALUES.items()
        if _dig(values, p) != want
    }
    assert not missing, (
        f"{release}: these values were subtracted into the un-written global "
        f"defaults layer and are absent from every generated file: {missing}\n"
        f"effective values were: {sorted(values)}"
    )


def test_every_value_file_the_chart_references_is_actually_on_disk(tmp_path):
    """Whatever layering the ms-applicationSet chart declares must exist.

    The chain moved out of the converter's own output and into the chart, so
    this renders the chart exactly as the generated root ApplicationSet would
    (one release per namespace directory, `namespace` parameterised) and
    resolves every valueFiles entry against the tree on disk. ArgoCD fails the
    ENTIRE sync on one missing valueFile, so a chain that references a file the
    converter never wrote breaks every release in that namespace at once.
    """
    if shutil.which("helm") is None:
        pytest.skip("helm not installed")
    out = tmp_path / "out"
    _run(out)

    appset_chart = CHART_DIR / "ms-applicationSet"
    assert appset_chart.is_dir(), f"the ms-applicationSet chart is missing from {CHART_DIR}"

    checked = 0
    for ns_dir in sorted(p for p in out.iterdir() if p.is_dir()):
        values_dir = ns_dir / "values"
        if not values_dir.is_dir():
            continue
        proc = subprocess.run(
            ["helm", "template", "appset", str(appset_chart),
             "--set", f"namespace={ns_dir.name}",
             "--set", "originRepoURL=ssh://git@example/gitops.git",
             "--set", "originBranch=main"],
            capture_output=True, text=True,
        )
        assert proc.returncode == 0, f"ms-applicationSet render failed:\n{proc.stderr}"
        doc = yaml.safe_load(proc.stdout)
        source = next(s for s in doc["spec"]["template"]["spec"]["sources"] if "helm" in s)

        for vf in source["helm"]["valueFiles"]:
            rel = vf.split("$Values/", 1)[1]
            for values_file in sorted(values_dir.glob("*.yaml")):
                # ArgoCD's git files generator: .path.path is the file's folder
                # (repo-relative), .path.filename its name.
                resolved = re.sub(r"\{\{\s*\.path\.filename\s*\}\}", values_file.name, rel)
                # base/ reads `.path.path | trimPrefix "<folder>/values"` to
                # mirror a values/ sub-folder; evaluate that pipe too.
                path = values_file.parent.relative_to(out).as_posix()
                resolved = re.sub(r'\{\{\s*\.path\.path\s*(?:\|\s*trimPrefix\s*"([^"]*)"\s*)?\}\}',
                                  lambda m: path.removeprefix(m.group(1) or ""), resolved)
                assert (out / resolved).exists(), (
                    f"{ns_dir.name}: the chart references {vf} -> {resolved}, "
                    f"which the converter never wrote")
                checked += 1
    assert checked, "no value files were checked - the fixture produced no releases"


def test_cluster_scoped_mode_keeps_every_common_value_too(tmp_path):
    """The other half of the invariant: --cluster-scoped changes what is
    deduplicated, never what is layered."""
    out = tmp_path / "out"
    _run(out, extra_args=("--cluster-scoped",))

    assert not (out / "defaults.yaml").exists(), (
        "a tree-root defaults.yaml is referenced by no layer in the chart's chain "
        "- writing one means values that nothing applies")

    values = _effective_values(out, "shop-web", "storefront")
    missing = {
        ".".join(p): want for p, want in EXPECTED_IN_EFFECTIVE_VALUES.items()
        if _dig(values, p) != want
    }
    assert not missing, f"cluster-scoped mode lost values too: {missing}"


# ---------------------------------------------------------------------------
# SECOND CASE — the 'shared' release. It now goes through the SAME three-file
# chain as every other release (base/ -> <ns>/defaults.yaml -> <ns>/values/),
# which is only safe because <ns>/defaults.yaml is computed with the shared
# release in the pool. common_subtree promotes a key only when it is present
# with an identical value in EVERY member of that pool, so a defaults layer can
# never hand the shared release something it did not already declare.
# ---------------------------------------------------------------------------

def _run_shared(out, extra_args=()):
    proc = subprocess.run(
        [sys.executable, str(CONVERTER_PY),
         "--input", str(SHARED_FIXTURE_DIR), "--output", str(out),
         "--chart", str(CHART_DIR), "--skip-verify", *extra_args],
        capture_output=True, text=True, cwd=str(GITOPS_DIR),
    )
    assert proc.returncode == 0, f"converter failed:\n{proc.stdout}\n{proc.stderr}"


def _shared_slug(out, namespace):
    """base_registry forks the slug (base/<ns>-shared.yaml) when two namespaces'
    shared content differs, so the release name is discovered, not assumed."""
    candidates = [p.stem for p in (out / namespace / "values").glob("*.yaml")
                  if p.stem.endswith("shared")]
    assert len(candidates) == 1, f"expected one shared release in {namespace}, got {candidates}"
    return candidates[0]


def _shared_effective_values(out, namespace):
    return _effective_values(out, namespace, _shared_slug(out, namespace))


@pytest.mark.parametrize("mode", ["namespace-scoped", "cluster-scoped"])
def test_shared_release_keeps_what_it_declared_in_both_modes(tmp_path, mode):
    """Nothing may be subtracted from the shared release into a layer it does
    not read, and nothing may be added to it by a layer it does."""
    out = tmp_path / "out"
    _run_shared(out, extra_args=() if mode == "namespace-scoped" else ("--cluster-scoped",))

    values = _shared_effective_values(out, "platform-ops")
    assert _dig(values, ("workload", "type")) == "none", (
        f"[{mode}] the shared release lost workload.type; it will render the "
        f"chart's default workload instead. effective values: {sorted(values)}")
    # The chart renders no Service unless service.enabled is true.
    assert _dig(values, ("service", "enabled")) in (None, False), (
        f"[{mode}] the shared release turned service.enabled on; it will render a "
        f"Service it never declared. effective values: {sorted(values)}")
    assert _dig(values, ("configMaps", "platform-ops-shared-config", "data", "LOG_LEVEL")) == "info"


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_shared_release_renders_no_phantom_workload_or_service(tmp_path):
    """The consequence, end to end: a config-only shared release must render a
    ConfigMap and nothing else."""
    out = tmp_path / "out"
    _run_shared(out, extra_args=("--cluster-scoped",))

    import tempfile
    values = _shared_effective_values(out, "platform-ops")
    with tempfile.TemporaryDirectory() as tmp:
        vf = pathlib.Path(tmp) / "values.yaml"
        vf.write_text(yaml.safe_dump(values, sort_keys=False), encoding="utf-8")
        proc = subprocess.run(
            ["helm", "template", "shared", str(CHART_DIR), "-f", str(vf)],
            capture_output=True, text=True,
        )
    assert proc.returncode == 0, f"helm template failed:\n{proc.stderr}"
    kinds = sorted({d["kind"] for d in yaml.safe_load_all(proc.stdout) if d})
    # Scoped to the two kinds this defect produced. The release also renders a
    # ServiceAccount nobody declared — a separate, pre-existing chart default
    # (serviceAccount.create: true) that has nothing to do with the subtraction;
    # it is recorded in this folder's STATUS.md §6 rather than asserted here.
    assert "Deployment" not in kinds and "Service" not in kinds, (
        f"the shared release rendered {kinds}; a defaults layer handed it "
        f"workload/service settings it never declared")
    assert "ConfigMap" in kinds, f"the shared release lost its ConfigMap; rendered {kinds}"


@pytest.mark.parametrize("mode", ["namespace-scoped", "cluster-scoped"])
def test_namespace_defaults_never_add_anything_to_the_shared_release(tmp_path, mode):
    """Guard on the invariant itself rather than on one symptom.

    The shared release reads <ns>/defaults.yaml like every other release now.
    Every key in that file must therefore already be present, with the same
    value, in what the converter captured for the shared release — otherwise
    the layer is ADDING something (a Route, an HPA, a Service) to a release
    that declared no such thing and has no workload to back it.
    """
    out = tmp_path / "out"
    _run_shared(out, extra_args=() if mode == "namespace-scoped" else ("--cluster-scoped",))

    for namespace in ("platform-ops", "platform-data"):
        ns_defaults = yaml.safe_load(
            (out / namespace / "defaults.yaml").read_text(encoding="utf-8")) or {}
        slug = _shared_slug(out, namespace)
        declared = conv.deep_merge(
            yaml.safe_load((out / "base" / f"{slug}.yaml").read_text(encoding="utf-8")) or {},
            yaml.safe_load(
                (out / namespace / "values" / f"{slug}.yaml").read_text(encoding="utf-8")) or {},
        )
        added = {k: v for k, v in ns_defaults.items()
                 if k not in declared or declared[k] != v}
        assert not added, (
            f"[{mode}] {namespace}/defaults.yaml supplies {sorted(added)} to the shared "
            f"release '{slug}', which never declared them. The shared release must be in "
            f"the pool common_subtree computes that file from.")
