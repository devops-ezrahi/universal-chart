"""
Regression tests for ERRORS_NEW_ANALYSIS/EnableServiceLinks_Lost.

Rated benign in every pass of the report, on a premise that is factually
backwards. The report says:

  "Line 131: Deployment/observability-jaeger-collector LOST at
   spec.template.spec.enableServiceLinks. Raw had False. **false is the K8s
   default, omitted by chart. Benign.**"

error.txt catches it:

  "ONE FACTUAL CORRECTION WORTH KNOWING, because it inverts the usual reasoning:
   the Kubernetes default for enableServiceLinks is TRUE, not false. An explicit
   `enableServiceLinks: false` is therefore a DELIBERATE OPT-OUT, not a
   redundant restatement of the default — and dropping it means the rendered
   pods get service-link environment variables injected that the raw manifests
   explicitly disabled."

and attaches a condition to the verdict:

  "THEREFORE VERIFY BEFORE SUPPRESSING: for the affected workloads, check
   whether any container env var name could collide with a
   {SERVICE}_SERVICE_HOST/_PORT pattern from a Service in the same namespace. If
   one does, re-enabling service links overrides the application's own
   configuration at runtime — a real and very hard-to-diagnose bug."

VERDICT: PRESERVE, not suppress. The collision check needs manifests this repo
does not have, and carrying the field is one line per side — so the field is
carried and the question does not need answering. Same shape as folder 7's
minReadySeconds: the report's rating describes the value it saw, and its stated
reason is wrong about the field.

WHAT SERVICE LINKS ACTUALLY DO, since the whole verdict turns on it: Kubernetes
injects a {SVCNAME}_SERVICE_HOST and {SVCNAME}_SERVICE_PORT pair for every
Service in the namespace. Most applications ignore them. Teams disable them for
two reasons — env bloat in namespaces with many Services, and because an
injected variable name can collide with the application's own and silently
override it.
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
    return {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "opa"},
            "spec": {"template": {"metadata": {}, "spec": {
                **pod_spec, "containers": [{"name": "opa", "image": "openpolicyagent/opa:0.65"}]}}}}


def _values(doc):
    ctx = conv.MicroserviceContext(namespace="identity-auth-prd", name="opa")
    conv.handle_workload(doc, ctx, "Deployment")
    return ctx.values


def _render(values, tmp_path):
    vf = tmp_path / "values.yaml"
    vf.write_text(yaml.safe_dump(values), encoding="utf-8")
    out = subprocess.run(
        ["helm", "template", "opa", str(CHART_DIR), "--namespace", "identity-auth-prd",
         "-f", str(vf)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert out.returncode == 0, out.stderr
    return [d for d in yaml.safe_load_all(out.stdout) if d]


BASE = {"workload": {"type": "deployment"},
        "image": {"repository": "openpolicyagent/opa", "tag": "0.65"}}


# ---------------------------------------------------------------------------
# 1. Converter
# ---------------------------------------------------------------------------

def test_explicit_false_is_captured():
    """The value that actually means something, and the one the report's
    reasoning would have thrown away."""
    assert _values(_dep(enableServiceLinks=False)).get("enableServiceLinks") is False


def test_explicit_true_is_captured_too():
    assert _values(_dep(enableServiceLinks=True)).get("enableServiceLinks") is True


def test_absent_stays_absent():
    assert "enableServiceLinks" not in _values(_dep())


def test_it_is_captured_alongside_shareProcessNamespace_without_confusing_the_two():
    """These two sit next to each other in the pod spec and the report bucketed
    them together. Their defaults are opposite — enableServiceLinks true,
    shareProcessNamespace false — so they cannot share a verdict."""
    v = _values(_dep(enableServiceLinks=False, shareProcessNamespace=False))
    assert v.get("enableServiceLinks") is False
    assert v.get("shareProcessNamespace") is False


# ---------------------------------------------------------------------------
# 2. Template — the `if` vs `hasKey` distinction
# ---------------------------------------------------------------------------

def test_template_renders_an_explicit_false(tmp_path):
    """The whole point. The three neighbouring booleans (hostNetwork, hostIPC,
    hostPID) are rendered with plain `if`, which is correct for them because
    their default is false — and would silently drop exactly this value."""
    docs = _render({**BASE, "enableServiceLinks": False}, tmp_path)
    dep = next(d for d in docs if d["kind"] == "Deployment")
    assert dep["spec"]["template"]["spec"]["enableServiceLinks"] is False


def test_template_renders_an_explicit_true(tmp_path):
    docs = _render({**BASE, "enableServiceLinks": True}, tmp_path)
    dep = next(d for d in docs if d["kind"] == "Deployment")
    assert dep["spec"]["template"]["spec"]["enableServiceLinks"] is True


def test_template_omits_it_when_unset(tmp_path):
    """Rendering `true` by default would be an invented field on every workload
    that never declared one."""
    dep = next(d for d in _render(BASE, tmp_path) if d["kind"] == "Deployment")
    assert "enableServiceLinks" not in dep["spec"]["template"]["spec"]


@pytest.mark.parametrize("kind,wl", [("StatefulSet", "statefulset"), ("DaemonSet", "daemonset")])
def test_it_renders_on_every_workload_kind(kind, wl, tmp_path):
    """_pod-spec.tpl is shared, so this should hold — asserted rather than
    assumed, since a per-kind template could have diverged."""
    docs = _render({**BASE, "workload": {"type": wl}, "enableServiceLinks": False}, tmp_path)
    obj = next(d for d in docs if d["kind"] == kind)
    assert obj["spec"]["template"]["spec"]["enableServiceLinks"] is False


def test_the_neighbouring_booleans_still_render_only_when_true(tmp_path):
    """Their default IS false, so `if` is right for them and changing it would
    add three invented fields to every pod. The two rules coexist on purpose."""
    dep = next(d for d in _render({**BASE, "hostNetwork": False, "hostIPC": False},
                                  tmp_path) if d["kind"] == "Deployment")
    spec = dep["spec"]["template"]["spec"]
    assert "hostNetwork" not in spec and "hostIPC" not in spec


# ---------------------------------------------------------------------------
# 3. Not suppressed
# ---------------------------------------------------------------------------

def test_it_is_not_on_the_benign_suppression_list():
    """Folder 17 deliberately left this open rather than deciding it from inside
    a bulk rule. This is the decision, and it went the other way."""
    assert not conv._is_benign_default_loss(
        "Deployment", ("spec", "template", "spec", "enableServiceLinks"), False)


def test_a_genuine_loss_would_still_report():
    diffs = conv._fidelity_diff(
        "Deployment",
        {"spec": {"template": {"spec": {"enableServiceLinks": False}}}},
        {"spec": {"template": {"spec": {}}}})
    assert [d[0] for d in diffs] == ["LOST"]


# ---------------------------------------------------------------------------
# 4. Corpus
# ---------------------------------------------------------------------------

def test_the_corpus_declares_an_explicit_false():
    deps = [d for f in SHOWCASE.rglob("*.yaml")
            for d in yaml.safe_load_all(f.read_text(encoding="utf-8"))
            if d and d.get("kind") in ("Deployment", "StatefulSet", "DaemonSet")]
    values = [d["spec"]["template"]["spec"].get("enableServiceLinks") for d in deps]
    assert False in values, "no workload in the corpus disables service links"
