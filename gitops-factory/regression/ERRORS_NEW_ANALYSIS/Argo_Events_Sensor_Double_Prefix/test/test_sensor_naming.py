"""
Regression tests for ERRORS_NEW_ANALYSIS/Argo_Events_Sensor_Double_Prefix.

error.txt makes a prediction and asks a question. The prediction turns out to be
wrong FOR THIS CONVERTER; the question finds something worse than the reported
defect.

THE PREDICTION:
  "This is the same fullname/prefix machinery that produced the ServiceAccount
   doubling [...] Fixing the idempotence guard likely resolves both folders at
   once."

Not here. Sensor and EventSource are not in NS_RAW_NAME_KINDS, so they never
reach NamespaceRegistry.claim at all — they fall through to extraDeploy and are
emitted verbatim. Folder 10's guard is irrelevant to them because the code path
it guards is never entered. A Sensor in this converter cannot acquire a doubled
prefix, and the tests below prove that rather than assuming it.

THE QUESTION, which was the productive one:
  "Confirm the converter has a real handler for Sensor/EventSource rather than
   falling through to a generic path that applies workload naming rules — a
   generic path is the most probable source of the double prefix."

It does fall through to a generic path, and that path was broken — differently
and worse. templates/extra-deploy.yaml renders every entry through `tpl`. An
Argo Events Sensor that passes a parameter to an Argo Workflow carries
`value: "{{workflow.parameters.target}}"`, which is ordinary Argo syntax and
means nothing to Helm. `tpl` tried to evaluate it:

    Error: ... error calling tpl: cannot parse template "apiVersio...

That is not a mangled reference — it is a HARD RENDER FAILURE that takes the
whole release down. Fixed by escaping the opening delimiter when the converter
serialises a raw manifest into extraDeploy (as_extra_deploy).

error.txt's real warning is also covered here:
  "ALSO CHECK the Sensor's own spec: dependencies[].eventSourceName and
   triggers[].template.name. A correct Sensor name with a mangled internal
   reference is the more likely and more dangerous variant." A Sensor whose
   dependency points at the wrong EventSource never fires — silently, with no
   crash and no event.
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

SENSOR = {
    "apiVersion": "argoproj.io/v1alpha1", "kind": "Sensor",
    "metadata": {"name": "nightly-report"},
    "spec": {
        "dependencies": [{"name": "nightly-tick", "eventSourceName": "nightly-report",
                          "eventName": "nightly"}],
        "triggers": [{"template": {"name": "trigger-nightly-report", "k8s": {"operation": "create"}}}],
    },
}


def _render(values, tmp_path, release="nightly-report"):
    vf = tmp_path / "values.yaml"
    vf.write_text(yaml.safe_dump(values), encoding="utf-8")
    out = subprocess.run(
        ["helm", "template", release, str(CHART_DIR), "--namespace", "background-jobs",
         "-f", str(vf)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    return out


def _render_ok(values, tmp_path, release="nightly-report"):
    out = _render(values, tmp_path, release)
    assert out.returncode == 0, out.stderr
    return [d for d in yaml.safe_load_all(out.stdout) if d]


# ---------------------------------------------------------------------------
# 1. The reported defect — a Sensor cannot acquire a prefix here
# ---------------------------------------------------------------------------

def test_sensor_is_not_routed_through_the_naming_registry():
    """The structural reason no doubling can happen. If Sensor is ever ADDED to
    NS_RAW_NAME_KINDS, the reported defect becomes possible and this test says
    so before it ships."""
    assert "Sensor" not in conv.NS_RAW_NAME_KINDS
    assert "EventSource" not in conv.NS_RAW_NAME_KINDS
    assert "Sensor" not in conv.CLUSTER_SCOPED_KINDS


def test_a_sensor_named_after_its_release_keeps_its_name(tmp_path):
    """The doubling scenario, end to end: release name == Sensor name is exactly
    where `f"{owner}-{name}"` produced <name>-<name> for other kinds."""
    docs = _render_ok({
        "workload": {"type": "none"}, "service": {"enabled": False},
        "extraDeploy": [conv.as_extra_deploy(SENSOR)],
    }, tmp_path, release="nightly-report")
    sensor = next(d for d in docs if d["kind"] == "Sensor")
    assert sensor["metadata"]["name"] == "nightly-report"
    assert "nightly-report-nightly-report" not in yaml.safe_dump(sensor)


def test_the_sensors_internal_references_survive(tmp_path):
    """error.txt's "more likely and more dangerous variant". A Sensor whose
    eventSourceName no longer matches its EventSource never fires, and nothing
    reports it."""
    docs = _render_ok({
        "workload": {"type": "none"}, "service": {"enabled": False},
        "extraDeploy": [conv.as_extra_deploy(SENSOR)],
    }, tmp_path)
    sensor = next(d for d in docs if d["kind"] == "Sensor")
    dep = sensor["spec"]["dependencies"][0]
    assert dep["eventSourceName"] == "nightly-report"
    assert dep["eventName"] == "nightly"
    assert dep["name"] == "nightly-tick"
    assert sensor["spec"]["triggers"][0]["template"]["name"] == "trigger-nightly-report"


def test_sensor_round_trips_through_the_converter_unmodified():
    """extraDeploy is a verbatim copy. Anything else would be the converter
    editing a CRD it does not model."""
    restored = yaml.safe_load(conv.as_extra_deploy(SENSOR).replace('{{ "{{" }}', "{{"))
    assert restored == SENSOR


# ---------------------------------------------------------------------------
# 2. The defect that was actually there — `tpl` vs foreign template syntax
# ---------------------------------------------------------------------------

SENSOR_WITH_EXPR = {
    **SENSOR,
    "spec": {
        **SENSOR["spec"],
        "triggers": [{"template": {
            "name": "trigger-nightly-report",
            "parameters": [{"dest": "spec.arguments.parameters.0.value",
                            "value": "{{workflow.parameters.target}}"}],
        }}],
    },
}


def test_an_argo_parameter_expression_does_not_break_the_render(tmp_path):
    """The regression this folder actually found. Before the escape, this exact
    shape failed with `error calling tpl: cannot parse template` and took the
    whole release with it."""
    out = _render({
        "workload": {"type": "none"}, "service": {"enabled": False},
        "extraDeploy": [conv.as_extra_deploy(SENSOR_WITH_EXPR)],
    }, tmp_path)
    assert out.returncode == 0, f"tpl choked on Argo syntax:\n{out.stderr}"


def test_the_expression_renders_back_byte_identically(tmp_path):
    """Escaping must round-trip. An escape that survived the render but changed
    the value would be a subtler version of the same bug."""
    docs = _render_ok({
        "workload": {"type": "none"}, "service": {"enabled": False},
        "extraDeploy": [conv.as_extra_deploy(SENSOR_WITH_EXPR)],
    }, tmp_path)
    sensor = next(d for d in docs if d["kind"] == "Sensor")
    assert sensor["spec"]["triggers"][0]["template"]["parameters"][0]["value"] == \
        "{{workflow.parameters.target}}"


def test_unescaped_content_still_fails_so_the_test_above_means_something(tmp_path):
    """Guards against a false pass: if `tpl` had stopped evaluating extraDeploy
    for some other reason, the escape test would pass without the escape doing
    anything. This asserts the hazard is real."""
    out = _render({
        "workload": {"type": "none"}, "service": {"enabled": False},
        "extraDeploy": [conv.dump_yaml(SENSOR_WITH_EXPR)],   # NOT escaped
    }, tmp_path)
    assert out.returncode != 0, "tpl no longer evaluates extraDeploy — re-check as_extra_deploy"


@pytest.mark.parametrize("value", [
    "{{workflow.parameters.target}}",
    "{{ .Values.notAHelmValue }}",
    "prefix {{x}} suffix",
    "{{{{ doubled }}}}",
    "no braces at all",
])
def test_every_brace_shape_survives(value, tmp_path):
    doc = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "cm"},
           "data": {"k": value}}
    docs = _render_ok({
        "workload": {"type": "none"}, "service": {"enabled": False},
        "extraDeploy": [conv.as_extra_deploy(doc)],
    }, tmp_path)
    cm = next(d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "cm")
    assert cm["data"]["k"] == value


def test_hand_written_extraDeploy_entries_are_still_templated(tmp_path):
    """The escape belongs in the CONVERTER, not the template. extraDeploy's
    documented purpose is "raw Kubernetes YAML, with full Helm templating
    support", and a hand-written entry using {{ .Release.Name }} must keep
    working. Disabling tpl would have been the easier fix and the wrong one."""
    docs = _render_ok({
        "workload": {"type": "none"}, "service": {"enabled": False},
        "extraDeploy": ["apiVersion: v1\nkind: ConfigMap\nmetadata:\n"
                        "  name: {{ .Release.Name }}-special\ndata:\n  k: v\n"],
    }, tmp_path, release="nightly-report")
    assert any(d["kind"] == "ConfigMap" and d["metadata"]["name"] == "nightly-report-special"
               for d in docs)


# ---------------------------------------------------------------------------
# 3. Corpus coverage
# ---------------------------------------------------------------------------

def _corpus(kind):
    return [d for f in SHOWCASE.rglob("*.yaml")
            for d in yaml.safe_load_all(f.read_text(encoding="utf-8"))
            if d and d.get("kind") == kind]


def test_the_corpus_contains_a_sensor_and_its_eventsource():
    sensors, sources = _corpus("Sensor"), _corpus("EventSource")
    assert sensors and sources, "no Argo Events resources in the corpus"
    names = {d["metadata"]["name"] for d in sources}
    for s in sensors:
        for dep in s["spec"]["dependencies"]:
            assert dep["eventSourceName"] in names, \
                f"corpus Sensor points at a non-existent EventSource: {dep['eventSourceName']}"


def test_the_corpus_sensor_carries_a_template_expression():
    """Without it the `tpl` hazard is untested by the standing run — and it is a
    hard render failure, the worst class of regression to rediscover."""
    blob = yaml.safe_dump(_corpus("Sensor"))
    assert "{{" in blob, "corpus Sensor no longer exercises the tpl escape"
