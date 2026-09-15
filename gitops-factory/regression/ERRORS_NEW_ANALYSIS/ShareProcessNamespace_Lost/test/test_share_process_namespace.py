"""
Regression tests for ERRORS_NEW_ANALYSIS/ShareProcessNamespace_Lost.

1 occurrence, benign in every pass, and the reasoning is correct this time —
unlike its neighbour in folder 20. The Kubernetes default for
`shareProcessNamespace` IS `false`, so dropping an explicit `false` changes
nothing about the running pod.

error.txt still attaches a condition, and it is the right one:

  "THE ONE CASE THAT WOULD MATTER: shareProcessNamespace: TRUE lets containers
   in a pod see each other's processes — used by debugging sidecars and by some
   service meshes. If any workload sets it to true and that is being dropped,
   containers lose the ability to signal or inspect each other and the sidecar
   silently stops working. The report only ever cites False. Grep the raw
   manifests for shareProcessNamespace: true before accepting this as closed."

`true` survives, and always did — the template rendered `if
.Values.shareProcessNamespace`, which passes exactly the dangerous value
through. So the benign verdict holds on its own terms.

THE REPORT'S RECOMMENDED ACTION — suppress — is the right one, and this file
records why the more obvious alternative is not.

Rendering the field looked strictly better at first: handle_workload already
CAPTURES it, so the value was reaching the values file and being discarded by
the template, and a one-line `hasKey` guard would have preserved it exactly.
That change was made, and a test caught the problem: `values.yaml` DECLARES
`shareProcessNamespace: false` as a chart default, so `hasKey` is always true
and the field would be stamped onto every pod the chart renders — inventing it
on the many workloads whose source never mentioned it. Making the guard work
would mean changing the chart's public values API for a field whose loss is
genuinely benign.

Its neighbour `enableServiceLinks` (folder 20) has NO declared default, so the
same guard there means what it says — and its Kubernetes default is `true`, so
an explicit `false` genuinely matters. Two adjacent fields, opposite treatments,
for two independent reasons. The comments in `_pod-spec.tpl` and
`_is_benign_default_loss` both say so, and a test below pins the values.yaml
fact the decision rests on.
"""
import importlib.util
import pathlib
import subprocess
import sys

import pytest
import yaml

TEST_DIR = pathlib.Path(__file__).resolve().parent
GITOPS_DIR = TEST_DIR.parents[3]
CHART_DIR = GITOPS_DIR.parent
CONVERTER_PY = GITOPS_DIR / "convert_to_universal_chart.py"
SHOWCASE = GITOPS_DIR / "namespaces-showcase"


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


conv = _load(CONVERTER_PY, "convert_to_universal_chart")


def _dep(**pod_spec):
    return {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "otel"},
            "spec": {"template": {"metadata": {}, "spec": {
                **pod_spec,
                "containers": [{"name": "otel", "image": "otel/opentelemetry-collector:0.98"}]}}}}


def _values(doc):
    ctx = conv.MicroserviceContext(namespace="platform-ops", name="otel")
    conv.handle_workload(doc, ctx, "Deployment")
    return ctx.values


def _render(values, tmp_path):
    vf = tmp_path / "values.yaml"
    vf.write_text(yaml.safe_dump(values), encoding="utf-8")
    out = subprocess.run(
        ["helm", "template", "otel", str(CHART_DIR), "--namespace", "platform-ops",
         "-f", str(vf)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert out.returncode == 0, out.stderr
    return [d for d in yaml.safe_load_all(out.stdout) if d]


BASE = {"workload": {"type": "deployment"},
        "image": {"repository": "otel/opentelemetry-collector", "tag": "0.98"}}


# ---------------------------------------------------------------------------
# 1. The condition error.txt attaches to the benign verdict
# ---------------------------------------------------------------------------

def test_an_explicit_TRUE_survives_the_converter():
    """"The ONE CASE THAT WOULD MATTER". A debugging sidecar or a service mesh
    that relies on shared PIDs stops working silently if this is dropped."""
    assert _values(_dep(shareProcessNamespace=True)).get("shareProcessNamespace") is True


def test_an_explicit_TRUE_survives_the_template(tmp_path):
    docs = _render({**BASE, "shareProcessNamespace": True}, tmp_path)
    dep = next(d for d in docs if d["kind"] == "Deployment")
    assert dep["spec"]["template"]["spec"]["shareProcessNamespace"] is True


def test_no_corpus_manifest_sets_it_true_but_the_path_is_covered_anyway():
    """error.txt asks to grep for `true` before closing. None sets it, which is
    why the two tests above use a synthetic pod spec rather than the corpus —
    the dangerous value has to be exercised somewhere."""
    values = [d["spec"]["template"]["spec"].get("shareProcessNamespace")
              for f in SHOWCASE.rglob("*.yaml")
              for d in yaml.safe_load_all(f.read_text(encoding="utf-8"))
              if d and d.get("kind") in ("Deployment", "StatefulSet", "DaemonSet")]
    assert True not in values, \
        "a corpus workload now sets shareProcessNamespace: true — re-read this folder's verdict"


# ---------------------------------------------------------------------------
# 2. The false case — carried rather than suppressed
# ---------------------------------------------------------------------------

def test_an_explicit_false_is_captured_by_the_converter():
    """It reaches the values file either way — the template is what drops it."""
    assert _values(_dep(shareProcessNamespace=False)).get("shareProcessNamespace") is False


def test_the_false_diff_is_suppressed_in_the_checker():
    assert conv._is_benign_default_loss(
        "Deployment", ("spec", "template", "spec", "shareProcessNamespace"), False)


def test_TRUE_is_never_suppressed():
    """The safety boundary. `true` is the value that means something, and it is
    rendered by the chart — so it can never reach the suppression at all. Pinned
    anyway, because a path-only rule would swallow it."""
    assert not conv._is_benign_default_loss(
        "Deployment", ("spec", "template", "spec", "shareProcessNamespace"), True)


def test_the_suppression_is_scoped_to_workload_kinds_and_the_pod_spec_path():
    assert not conv._is_benign_default_loss(
        "Deployment", ("spec", "shareProcessNamespace"), False)
    assert not conv._is_benign_default_loss(
        "ConfigMap", ("spec", "template", "spec", "shareProcessNamespace"), False)


def test_the_chart_does_not_stamp_the_field_onto_every_pod(tmp_path):
    """The reason this is suppressed rather than rendered. values.yaml declares
    `shareProcessNamespace: false` as a chart default, so a hasKey guard is
    always true and would invent the field on every workload — including the
    many whose source never mentioned it."""
    dep = next(d for d in _render(BASE, tmp_path) if d["kind"] == "Deployment")
    assert "shareProcessNamespace" not in dep["spec"]["template"]["spec"]


def test_the_declared_default_is_what_makes_hasKey_useless_here():
    """Pins the fact the decision rests on. If someone removes this default,
    rendering the field becomes viable and this folder's verdict is worth
    revisiting."""
    values = yaml.safe_load((CHART_DIR / "values.yaml").read_text(encoding="utf-8"))
    assert values["shareProcessNamespace"] is False
    assert "enableServiceLinks" not in values, \
        "enableServiceLinks gained a declared default — folder 20's hasKey guard now over-renders"


# ---------------------------------------------------------------------------
# 3. The neighbours, which are NOT changed
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("field", ["hostNetwork", "hostIPC", "hostPID"])
def test_the_host_namespace_booleans_still_render_only_when_true(field, tmp_path):
    """These have the identical latent issue — captured by the converter,
    rendered only when true — and were deliberately left alone: no defect was
    observed for them and no folder covers them. Recorded here so the asymmetry
    is a known state rather than an oversight."""
    dep = next(d for d in _render({**BASE, field: False}, tmp_path) if d["kind"] == "Deployment")
    assert field not in dep["spec"]["template"]["spec"]


@pytest.mark.parametrize("field", ["hostNetwork", "hostIPC", "hostPID"])
def test_the_host_namespace_booleans_do_render_when_true(field, tmp_path):
    """The dangerous direction for those three is `true`, and it works — which is
    why their asymmetry is tolerable where enableServiceLinks' was not."""
    dep = next(d for d in _render({**BASE, field: True}, tmp_path) if d["kind"] == "Deployment")
    assert dep["spec"]["template"]["spec"][field] is True


def test_enableServiceLinks_renders_while_this_one_does_not(tmp_path):
    """Adjacent fields, opposite treatments, for two independent reasons — and
    the report bucketed them together and got one of the two wrong. This is the
    asymmetry stated as behaviour rather than as a comment."""
    docs = _render({**BASE, "enableServiceLinks": False, "shareProcessNamespace": False},
                   tmp_path)
    spec = next(d for d in docs if d["kind"] == "Deployment")["spec"]["template"]["spec"]
    assert spec["enableServiceLinks"] is False, "its K8s default is true — it must render"
    assert "shareProcessNamespace" not in spec, "its K8s default is false — suppressed instead"
