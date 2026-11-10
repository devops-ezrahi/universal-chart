"""env in the explicit form: NAME: {value: ...} / NAME: {valueFrom: ...}.

The chart side runs real `helm template` (so it needs no helm plugin); the
converter side converts small namespaces and reads what it wrote."""
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
CMD = REPO / "gitops-factory" / "convert_to_universal_chart.py"
sys.path.insert(0, str(REPO))
from converter import engine  # noqa: E402


def render(tmp_path, *layers, ok=True):
    files = []
    for i, layer in enumerate(layers):
        f = tmp_path / f"l{i}.yaml"
        # The converter's own writer: plain PyYAML leaves `y`, `08`, `1e3` bare and
        # Helm would read them as a bool / number — the trap dump_yaml quotes.
        f.write_text(engine.dump_yaml(layer), encoding="utf-8")
        files += ["-f", str(f)]
    r = subprocess.run(["helm", "template", "t", str(REPO), *files], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if not ok:
        return r
    assert r.returncode == 0, r.stderr[-2000:]
    dep = next(d for d in yaml.safe_load_all(r.stdout) if d and d.get("kind") == "Deployment")
    return {e["name"]: {k: v for k, v in e.items() if k != "name"}
            for e in dep["spec"]["template"]["spec"]["containers"][0].get("env") or []}


BASE = {"workload": {"type": "deployment"}, "image": {"repository": "nginx", "tag": "1.27"}}
REF = {"secretKeyRef": {"name": "db", "key": "password"}}


# ----------------------------------------------------------------- the chart

@pytest.mark.parametrize("value", ["y", "on", "08", "1e3", "true", "null-ish", "", "multi\nline\n", "a: b", "{{ x }}"])
def test_a_value_renders_as_the_exact_string(tmp_path, value):
    v = value.replace("{{ x }}", '{{ "{{" }} x }}')  # the converter escapes a literal {{ this way
    env = render(tmp_path, {**BASE, "env": {"V": {"value": v}}})
    assert env["V"] == {"value": value.replace("{{ x }}", "{{ x }}")}


@pytest.mark.parametrize("ref", [REF, {"configMapKeyRef": {"name": "c", "key": "k"}},
                                 {"fieldRef": {"fieldPath": "metadata.name"}},
                                 {"resourceFieldRef": {"resource": "limits.cpu", "divisor": "1m"}}],
                         ids=["secret", "configmap", "field", "resource"])
def test_every_valuefrom_kind(tmp_path, ref):
    assert render(tmp_path, {**BASE, "env": {"V": {"valueFrom": ref}}})["V"] == {"valueFrom": ref}


@pytest.mark.parametrize("low,high,want", [
    ({"value": "a"}, {"value": "b"}, {"value": "b"}),
    ({"valueFrom": REF}, {"valueFrom": {"secretKeyRef": {"name": "db", "key": "other"}}},
     {"valueFrom": {"secretKeyRef": {"name": "db", "key": "other"}}}),
    ({"value": "a"}, {"valueFrom": REF, "value": None}, {"valueFrom": REF}),
    ({"valueFrom": REF}, {"value": "b", "valueFrom": None}, {"value": "b"}),
], ids=["value-value", "ref-ref", "value-then-ref", "ref-then-value"])
def test_an_override_in_a_later_file(tmp_path, low, high, want):
    assert render(tmp_path, {**BASE, "env": {"V": low}}, {"env": {"V": high}})["V"] == want


def test_a_flip_without_the_null_fails_naming_the_var(tmp_path):
    r = render(tmp_path, {**BASE, "env": {"DB_PASS": {"valueFrom": REF}}}, {"env": {"DB_PASS": {"value": "x"}}}, ok=False)
    assert r.returncode != 0 and "env DB_PASS has both value and valueFrom" in r.stderr


def test_null_deletes_a_var_a_lower_file_set(tmp_path):
    env = render(tmp_path, {**BASE, "env": {"A": {"value": "1"}, "B": {"value": "2"}}}, {"env": {"B": None}})
    assert env == {"A": {"value": "1"}}


def test_trees_written_before_still_render(tmp_path):
    """Deployed trees track the chart's main branch: every older env form must
    keep rendering exactly as it did."""
    shorthand = render(tmp_path, {**BASE, "env": {"A": "plain", "B": REF, "C": 8, "D": {}}})
    assert shorthand == {"A": {"value": "plain"}, "B": {"valueFrom": REF}, "C": {"value": "8"}, "D": {"value": ""}}
    as_list = render(tmp_path, {**BASE, "env": [{"name": "Z", "value": "1"}, {"name": "A", "valueFrom": REF}]})
    assert as_list == {"Z": {"value": "1"}, "A": {"valueFrom": REF}}


def test_envorder_and_dollar_refs(tmp_path):
    env = render(tmp_path, {**BASE, "env": {"B": {"value": "$(A)-x"}, "A": {"value": "1"}, "C": {"value": "c"}},
                            "envOrder": ["B", "A"]})
    assert list(env) == ["B", "A", "C"]  # envOrder first, the rest sorted


def test_switching_the_kind_of_ref_needs_the_old_kind_nulled(tmp_path):
    field = {"fieldRef": {"fieldPath": "spec.nodeName"}}
    r = render(tmp_path, {**BASE, "env": {"V": {"valueFrom": REF}}}, {"env": {"V": {"valueFrom": field}}}, ok=False)
    assert r.returncode != 0 and "more than one source in valueFrom (fieldRef, secretKeyRef)" in r.stderr
    env = render(tmp_path, {**BASE, "env": {"V": {"valueFrom": REF}}},
                 {"env": {"V": {"valueFrom": {**field, "secretKeyRef": None}}}})
    assert env["V"] == {"valueFrom": field}


# ------------------------------------------------------------- the converter

def test_env_map_writes_only_the_explicit_form():
    m = engine.env_map([{"name": "A", "value": "x"}, {"name": "B", "value": 8}, {"name": "C"},
                        {"name": "D", "value": None}, {"name": "E", "valueFrom": REF}])
    assert m == {"A": {"value": "x"}, "B": {"value": "8"}, "C": {"value": ""}, "D": {"value": ""},
                 "E": {"valueFrom": REF}}


@pytest.mark.parametrize("lower,upper,want", [
    ({"env": {"A": {"valueFrom": REF}}}, {"env": {"A": {"value": "x"}}}, {"A": {"value": "x", "valueFrom": None}}),
    ({"env": {"A": {"value": "y"}}}, {"env": {"A": {"valueFrom": REF}}}, {"A": {"valueFrom": REF, "value": None}}),
    ({"env": {"A": {"value": "y"}}}, {"env": {"A": {"value": "x"}}}, {"A": {"value": "x"}}),
    ({}, {"env": {"A": {"value": "x"}}}, {"A": {"value": "x"}}),
])
def test_null_env_flips(lower, upper, want):
    engine.null_env_flips(lower, upper)
    assert upper["env"] == want


def test_null_env_flips_reaches_jobs_and_cronjobs():
    lower = {"jobs": {"m": {"env": {"A": {"valueFrom": REF}}}},
             "cronjobs": {"c": {"env": {"B": {"value": "1"}}}}}
    upper = {"jobs": {"m": {"env": {"A": {"value": "x"}}}}, "cronjobs": {"c": {"env": {"B": {"valueFrom": REF}}}}}
    assert engine.null_env_flips(lower, upper) == 2
    kinds = {"env": {"A": {"valueFrom": {"fieldRef": {"fieldPath": "x"}}}}}
    assert engine.null_env_flips({"env": {"A": {"valueFrom": REF}}}, kinds) == 1
    assert kinds["env"]["A"]["valueFrom"] == {"fieldRef": {"fieldPath": "x"}, "secretKeyRef": None}
    assert upper["jobs"]["m"]["env"]["A"]["valueFrom"] is None and upper["cronjobs"]["c"]["env"]["B"]["value"] is None


@pytest.mark.parametrize("bad", [{"A": "x"}, {"A": REF}, {"A": {"value": 8}}, {"A": {"value": "x", "valueFrom": REF}},
                                 {"A": {}}, {"A": {"valueFrom": {}}}, {"A": {"other": 1}},
                                 {"A": {"valueFrom": {**REF, "fieldRef": {"fieldPath": "x"}}}}],
                         ids=["shorthand", "bare-ref", "non-string", "both", "empty", "empty-ref", "unknown-key", "two-refs"])
def test_the_linter_refuses_every_other_shape(bad):
    assert engine.env_shape_problems({"env": bad}, "f.yaml")


def test_the_linter_accepts_the_explicit_form_and_skips_kubernetes_lists():
    ok = {"env": {"A": {"value": "x"}, "B": {"valueFrom": REF}, "C": None, "D": {"value": "y", "valueFrom": None}},
          "jobs": {"m": {"env": {"A": {"value": "1"}}}},
          "sidecars": {"s": {"env": [{"name": "X", "value": "1"}]}},
          "initContainers": {"i": {"env": [{"name": "X", "value": "1"}]}},
          "extraDeploy": [{"spec": {"env": [{"name": "X"}]}}]}
    assert engine.env_shape_problems(ok, "f.yaml") == []


FLIP = """
apiVersion: apps/v1
kind: Deployment
metadata: {{name: api, namespace: {ns}, labels: {{app: api}}}}
spec:
  selector: {{matchLabels: {{app: api}}}}
  template:
    metadata: {{labels: {{app: api}}}}
    spec:
      containers:
        - name: api
          image: registry.example.com/api:1.0
          env:
            - name: SAME
              value: everywhere
            - name: DB_PASS
{db}
            - name: POD
              valueFrom: {{fieldRef: {{fieldPath: metadata.name}}}}
"""


def test_a_var_that_is_a_value_in_one_namespace_and_a_ref_in_another(tmp_path):
    """dev reads DB_PASS from a Secret, prd sets it literally: the tree must
    render each exactly, and every env in it must be in the explicit form."""
    inp = tmp_path / "in"
    for ns, db in (("shop-dev", "              valueFrom: {secretKeyRef: {name: db, key: password}}"),
                   ("shop-prd", "              value: literal"),
                   ("shop-tst", "              valueFrom: {secretKeyRef: {name: db, key: password}}")):
        (inp / ns).mkdir(parents=True)
        (inp / ns / "api.yaml").write_text(FLIP.format(ns=ns, db=db), encoding="utf-8")
    out = tmp_path / "out"
    r = subprocess.run([sys.executable, str(CMD), "--input", str(inp), "-o", str(out), "--chart", str(REPO),
                        "--verify-fidelity", "--fidelity-strict"], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]
    assert engine.lint_env_shape(out) == []
    for f in out.rglob("*.yaml"):
        if "report" in f.parts:
            continue
        text = f.read_text(encoding="utf-8")
        assert "SAME: everywhere" not in text and "DB_PASS: literal" not in text, f
