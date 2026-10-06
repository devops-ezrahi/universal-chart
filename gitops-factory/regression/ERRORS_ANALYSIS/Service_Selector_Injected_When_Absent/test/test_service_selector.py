"""
Regression tests for ERRORS_ANALYSIS/Service_Selector_Injected_When_Absent.

The report calls this a checker false positive but its own analysis contradicts
itself mid-sentence — it quotes `{{- if or $spec.selector $spec.labels }}`, then
writes "Wait, line 21 says `{{- if not $spec.selector }}`". error.txt tells the
next agent to re-read the template before acting, because the conclusion depends
on which condition is really there, and to check for hand-managed Endpoints:

  > "a selector-less Service in Kubernetes is normally paired with a manually-
  > managed Endpoints object ... Injecting a selector into such a Service would
  > make it start matching pods and silently override those manual Endpoints —
  > that would be a REAL and serious break, not a false positive."

What is actually in the templates is **neither** quoted condition. Both
`service.yaml` and `services.yaml` inject the chart's own selector
unconditionally for any non-ExternalName Service — there is no `if` on
`$spec.selector` at all for the primary one. So the injection is real and
universal, not conditional.

Two findings follow, and they point in opposite directions:

  1. **The checker is already correct.** `_fidelity_ignored_path` suppresses
     `Service.spec.selector`, with a recorded reason: the selector comes from
     `universal-chart.selectorLabels`, which never reads `.Values`. So the
     report's own recommendation — "the fidelity check should account for this"
     — was already implemented before this campaign started.
  2. **The behavioural risk error.txt names is real, and unguarded.** This repo
     has 2 selector-less Services and **zero** Endpoints/EndpointSlice objects
     anywhere, so nothing here breaks. But the converter silently drops the
     "this Service has no selector" fact, and the chart then invents one. If a
     manifest set ever does pair one with hand-managed Endpoints, the migration
     breaks it with nothing said.

So: confirmed false positive on the checker, and a warning added for the case
that would not be.
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
FIXTURE = FIXTURE_DIR / "platform-ops" / "log-agent.yaml"

NS = "platform-ops"
RELEASE = "log-agent"
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
        proc = subprocess.run(["helm", "template", release, str(CHART_DIR), "-f", str(vf)],
                              capture_output=True, text=True)
    assert proc.returncode == 0, f"helm template failed:\n{proc.stderr}"
    return [d for d in yaml.safe_load_all(proc.stdout) if d]


# --------------------------------------------------------------------------
# What the templates actually do — the re-read error.txt asks for
# --------------------------------------------------------------------------

def test_neither_quoted_condition_exists_in_the_template():
    text = (CHART_DIR / "templates" / "service.yaml").read_text(encoding="utf-8")
    assert "if or $spec.selector $spec.labels" not in text
    assert "if not $spec.selector" not in text
    assert 'if ne .Values.service.type "ExternalName"' in text, (
        "the primary Service's selector is guarded only by the ExternalName "
        "check, so it is injected unconditionally for every other type")


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_a_selector_is_injected_even_though_the_raw_service_had_none():
    """Confirms the behaviour, so the classification rests on something
    observed rather than on the report's reading of the template."""
    values = {"workload": {"type": "deployment"},
              "image": {"repository": "busybox", "tag": "1.36"},
              "service": {"enabled": True, "ports": {"http": {"port": 80}}},
              "services": {"log-agent-external": {"enabled": True,
                                                  "ports": {"http": {"port": 8080}}}}}
    for d in _helm_template(values):
        if d.get("kind") == "Service":
            assert d["spec"].get("selector"), f"{d['metadata']['name']} rendered no selector"


# --------------------------------------------------------------------------
# Finding 1 — the checker was already right
# --------------------------------------------------------------------------

def test_the_fidelity_checker_reports_a_changed_selector():
    """SUPERSEDED: the selector used to be chart-derived and suppressed. The
    converter now carries the live selector (service.selector / selectorLabels),
    so a render that changes it is a real diff and must be reported."""
    assert conv._fidelity_ignored_path("Service", ("spec", "selector")) is False

    raw = _services()[0]
    rendered = yaml.safe_load(yaml.safe_dump(raw))
    rendered["spec"]["selector"] = {"app.kubernetes.io/name": "log-agent"}
    diffs = conv._fidelity_diff("Service", raw, rendered)
    assert any("selector" in d[1] for d in diffs), f"diffs={diffs}"


def test_a_real_service_field_is_still_compared():
    """Guard: suppressing the selector must not blind the checker to the rest of
    the Service spec."""
    raw = _services()[0]
    rendered = yaml.safe_load(yaml.safe_dump(raw))
    rendered["spec"]["ports"][0]["port"] = 9999
    diffs = conv._fidelity_diff("Service", raw, rendered)
    assert any(d[0] == "CHANGED" and "port" in d[1] for d in diffs), f"diffs={diffs}"


# --------------------------------------------------------------------------
# Finding 2 — the risk that is real, and the warning for it
# --------------------------------------------------------------------------

def test_this_repo_has_no_hand_managed_endpoints():
    """The check error.txt asks for. A selector-less Service is only dangerous
    when something else is populating its Endpoints; nothing here is."""
    selectorless, endpoints = [], []
    for d in RAW_DIRS:
        root = GITOPS_DIR / d
        if not root.is_dir():
            continue
        for f in root.rglob("*.yaml"):
            for doc in yaml.safe_load_all(f.read_text(encoding="utf-8")):
                if not doc:
                    continue
                if doc.get("kind") == "Service" and not (doc.get("spec") or {}).get("selector"):
                    selectorless.append(f"{f.name}/{doc['metadata']['name']}")
                if doc.get("kind") in ("Endpoints", "EndpointSlice"):
                    endpoints.append(f"{f.name}/{doc['metadata']['name']}")
    assert not endpoints, (
        "hand-managed Endpoints now exist. If any of them back a selector-less "
        f"Service, the chart's injected selector overrides them:\n"
        f"  Endpoints: {endpoints}\n  selector-less Services: {selectorless}")


def test_a_selector_less_service_is_warned_about():
    """The converter must not lose the fact silently. It cannot render a
    Service without a selector — the chart has no way to express that — so the
    least it can do is say which Services it changed."""
    ctx = conv.MicroserviceContext(namespace=NS, name=RELEASE)
    conv.handle_service(_services()[0], ctx, "log-agent")
    joined = " | ".join(ctx.warnings)
    assert "selector" in joined.lower(), (
        f"a selector-less Service was converted with no warning; warnings={ctx.warnings}")


def test_a_service_with_its_own_selector_is_not_warned_about():
    svc = _services()[0]
    svc["spec"]["selector"] = {"app": "log-agent"}
    ctx = conv.MicroserviceContext(namespace=NS, name=RELEASE)
    conv.handle_service(svc, ctx, "log-agent")
    assert not [w for w in ctx.warnings if "selector" in w.lower()], ctx.warnings
