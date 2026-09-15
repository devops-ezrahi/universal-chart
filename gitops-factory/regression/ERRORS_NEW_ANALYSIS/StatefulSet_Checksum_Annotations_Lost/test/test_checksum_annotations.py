"""
Regression tests for ERRORS_NEW_ANALYSIS/StatefulSet_Checksum_Annotations_Lost.

Rated LOW/benign on the stated grounds that "the operator regenerates them on
deploy". error.txt refuses to take that on trust and says exactly why:

  "THEREFORE THE BENIGN VERDICT DEPENDS ENTIRELY ON WHO REGENERATES THEM, and
   this needs confirming rather than assuming:
     * If a Helm OPERATOR recreates them on every deploy (as the report claims),
       nothing is lost.
     * If the universal-chart itself is expected to compute them, and it does
       not, then config changes stop triggering restarts. That is a silent,
       delayed failure — the ConfigMap updates, ArgoCD reports Synced and
       Healthy, and the pods keep serving stale configuration until something
       unrelated restarts them. It is very hard to diagnose after the fact.

   THE CHECK: does universal-chart contain a checksum/config annotation template
   (the usual form being sha256sum of the rendered ConfigMap)? If not, and no
   operator supplies one, this is a real regression regardless of the LOW
   rating."

CHECKED, and the answer was BOTH halves:

  * the chart DOES have one — `universal-chart.checksumAnnotations`, hashing
    .Values.configMaps and .Values.secrets, wired into deployment, statefulset
    and daemonset;
  * but it ships `checksums.enabled: false`, and the converter never turned it
    on. So the machinery existed and was switched off, which lands on the
    "real regression" branch: the annotations were dropped and nothing replaced
    them.

FIX: drop the stale VALUES, preserve the INTENT. A workload whose raw manifest
carries `checksum/*` was demonstrably being restarted on config change, so the
converter now sets `checksums.enabled: true` for it. The raw hashes must NOT be
carried — they hash the source cluster's content and are stale on arrival, which
is worse than none, since they pin the pod template to a value that describes
nothing.

The checker's suppression is conditional on the same evidence: raw checksum
annotations are ignored ONLY when the render carries at least one of its own.
A render with none means the behaviour really is gone, and every key reports.
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

CHECKSUMS = {"checksum/config": "c4ca4238a0b923820dcc509a6f75849b",
             "checksum/secrets": "8f14e45fceea167a5a36dedd4bea2543"}


def _sts(annotations=None):
    return {"apiVersion": "apps/v1", "kind": "StatefulSet", "metadata": {"name": "postgres"},
            "spec": {"serviceName": "postgres", "template": {
                "metadata": {"annotations": dict(annotations or {})},
                "spec": {"containers": [{"name": "postgres", "image": "postgres:16"}]}}}}


def _values(doc):
    ctx = conv.MicroserviceContext(namespace="platform-data", name="postgres")
    conv.handle_workload(doc, ctx, "StatefulSet")
    return ctx.values


# ---------------------------------------------------------------------------
# 1. The check error.txt asks for
# ---------------------------------------------------------------------------

def test_the_chart_does_have_a_checksum_template():
    helpers = (CHART_DIR / "templates" / "_helpers.tpl").read_text(encoding="utf-8")
    assert "universal-chart.checksumAnnotations" in helpers
    assert "sha256sum" in helpers


@pytest.mark.parametrize("template", ["deployment.yaml", "statefulset.yaml", "daemonset.yaml"])
def test_every_workload_template_wires_it_in(template):
    body = (CHART_DIR / "templates" / template).read_text(encoding="utf-8")
    assert "universal-chart.checksumAnnotations" in body


def test_the_chart_ships_it_DISABLED_which_is_why_this_was_a_regression():
    """Recorded rather than changed. Defaulting it on would add annotations to
    every workload that never had them — an invented field on every diff, and
    the mirror-image defect. The converter turns it on per workload instead."""
    values = yaml.safe_load((CHART_DIR / "values.yaml").read_text(encoding="utf-8"))
    assert values["checksums"]["enabled"] is False


# ---------------------------------------------------------------------------
# 2. Intent preserved, values not
# ---------------------------------------------------------------------------

def test_raw_checksum_annotations_enable_the_charts_own():
    assert _values(_sts(CHECKSUMS)).get("checksums") == {"enabled": True}


def test_the_stale_hashes_are_NOT_carried():
    """They hash the source cluster's content. Carrying one is worse than
    carrying none: it pins the pod template to a value that describes nothing,
    so the restart it triggers is arbitrary and the next real change may not
    move it at all."""
    v = _values(_sts(CHECKSUMS))
    assert not any(k.startswith("checksum/") for k in v.get("podAnnotations", {}))


def test_a_workload_without_them_does_not_get_checksums_enabled():
    """The mirror-image failure. Enabling it everywhere would add annotations to
    workloads that never had them and cause restarts nobody asked for."""
    assert "checksums" not in _values(_sts())


def test_other_pod_annotations_are_unaffected():
    v = _values(_sts({**CHECKSUMS, "sidecar.istio.io/inject": "false"}))
    assert v["podAnnotations"] == {"sidecar.istio.io/inject": "false"}
    assert v["checksums"] == {"enabled": True}


# ---------------------------------------------------------------------------
# 3. The suppression is conditional on evidence, not on a rating
# ---------------------------------------------------------------------------

def _diff(raw_ann, rendered_ann):
    return conv._fidelity_diff(
        "StatefulSet",
        {"spec": {"template": {"metadata": {"annotations": raw_ann}}}},
        {"spec": {"template": {"metadata": {"annotations": rendered_ann}}}})


def test_suppressed_when_the_render_regenerates_them():
    assert _diff(CHECKSUMS, {"checksum/configmaps": "deadbeef"}) == []


def test_NOT_suppressed_when_the_render_has_none():
    """The regression branch, and the reason this is a condition rather than a
    rule. If nothing regenerates them, config changes stop triggering restarts
    and the report must say so."""
    diffs = _diff(CHECKSUMS, {})
    assert len(diffs) == 2
    assert all(d[0] == "LOST" for d in diffs)
    assert {d[1].rsplit(".", 1)[-1] for d in diffs} == set(CHECKSUMS)


def test_the_annotation_names_need_not_match():
    """The chart emits checksum/configmaps and checksum/secrets; the source used
    checksum/config and checksum/secret-certs. What matters is that the pod
    template moves when the content does, not what the key is called."""
    assert _diff({"checksum/secret-certs": "abc"}, {"checksum/secrets": "xyz"}) == []


def test_a_NON_checksum_annotation_is_never_swept_up():
    """The suppression must not widen to annotations generally — a lost
    prometheus.io/scrape or argocd.argoproj.io/sync-wave is a real loss."""
    diffs = _diff({**CHECKSUMS, "prometheus.io/scrape": "true"},
                  {"checksum/configmaps": "deadbeef"})
    assert [d[1] for d in diffs] == \
        ["spec.template.metadata.annotations.prometheus.io/scrape"]


def test_the_regenerated_annotation_is_not_reported_as_ADDED_either():
    """Symmetry. Suppressing only the raw side leaves the chart's own
    checksum/configmaps showing up as an invented field on every workload that
    had checksums — which _fidelity_diff already calls out as worse than no
    suppression at all, because it makes the rule look unreliable."""
    assert _diff(CHECKSUMS, {"checksum/configmaps": "deadbeef"}) == []


def test_the_suppression_is_scoped_to_annotation_maps():
    """A key literally named `checksum/config` somewhere that is not an
    annotations map is not an annotation."""
    diffs = conv._fidelity_diff(
        "ConfigMap",
        {"data": {"checksum/config": "abc"}},
        {"data": {}})
    assert diffs and diffs[0][0] == "LOST"


# ---------------------------------------------------------------------------
# 4. End to end + corpus
# ---------------------------------------------------------------------------

def test_the_chart_actually_emits_a_fresh_hash(tmp_path):
    vf = tmp_path / "values.yaml"
    vf.write_text(yaml.safe_dump({
        "workload": {"type": "statefulset"},
        "image": {"repository": "postgres", "tag": "16"},
        "checksums": {"enabled": True},
        "secrets": {"postgres-credentials": {"stringData": {"password": "x"}}},
    }), encoding="utf-8")
    out = subprocess.run(
        ["helm", "template", "postgres", str(CHART_DIR), "--namespace", "platform-data",
         "-f", str(vf)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert out.returncode == 0, out.stderr
    sts = next(d for d in yaml.safe_load_all(out.stdout) if d and d.get("kind") == "StatefulSet")
    ann = sts["spec"]["template"]["metadata"]["annotations"]
    assert any(k.startswith("checksum/") for k in ann), ann
    assert len(next(v for k, v in ann.items() if k.startswith("checksum/"))) == 64


def test_the_corpus_carries_checksum_annotations():
    stses = [d for f in SHOWCASE.rglob("*.yaml")
             for d in yaml.safe_load_all(f.read_text(encoding="utf-8"))
             if d and d.get("kind") in ("StatefulSet", "Deployment", "DaemonSet")]
    anns = [(d["spec"]["template"].get("metadata") or {}).get("annotations") or {}
            for d in stses]
    assert any(k.startswith("checksum/") for a in anns for k in a), \
        "no workload in the corpus carries checksum annotations"
