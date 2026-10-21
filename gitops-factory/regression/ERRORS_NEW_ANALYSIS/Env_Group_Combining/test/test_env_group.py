"""
--env-group: one release in two namespaces that differs only by environment
words collapses into ONE templated base/ file, and the chart's tpl pass renders
it back to exactly the source manifests.

fixture/: filter-service-black-b2b in fac1 and filter-service-yellow-b2b in
fac2. They differ by namespace name and colour everywhere, except
NEIGHBOUR_URL, which points at the OTHER namespace and must stay literal, and
an Alertmanager-style '{{ $labels.pod }}' in a ConfigMap, which the chart must
not evaluate.

--verify-fidelity --fidelity-strict makes the converter itself render every
release through the chart and diff it against the fixture, so a passing run is
the end-to-end proof; the asserts below pin the shape.
"""
import importlib.util
import pathlib
import subprocess
import sys

import yaml

TEST_DIR = pathlib.Path(__file__).resolve().parent
GITOPS_DIR = TEST_DIR.parents[3]
CHART_DIR = GITOPS_DIR.parent
CONVERTER_PY = GITOPS_DIR / "convert_to_universal_chart.py"
FIXTURE = TEST_DIR.parent / "fixture"


def _convert(out, *extra):
    proc = subprocess.run(
        [sys.executable, str(CONVERTER_PY), "--input", str(FIXTURE), "--output", str(out),
         "--chart", str(CHART_DIR), "--verify-fidelity", "--fidelity-strict", *extra],
        capture_output=True, text=True, errors="replace", cwd=str(GITOPS_DIR))
    assert proc.returncode == 0, f"converter failed:\n{proc.stdout}\n{proc.stderr}"


def _values(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_env_group_merges_into_one_templated_base(tmp_path):
    out = tmp_path / "out"
    _convert(out, "--env-group", "color=black,yellow")

    assert sorted(p.name for p in (out / "base").glob("*.yaml")) == ["filter-service-b2b.yaml"]
    base = _values(out / "base" / "filter-service-b2b.yaml")
    assert base["route"]["host"] == "filter.{{ .Release.Namespace }}.{{ .Values.color }}.example.org"
    assert base["fullnameOverride"] == "filter-service-{{ .Values.color }}-b2b"
    # env is one list in the deployment's order, and NEIGHBOUR_URL (below) cannot
    # be templated, so the whole list stays in each namespace's values/.
    assert "env" not in base
    # A source '{{' is escaped so the chart's tpl emits it literally.
    assert base["configMaps"]["filter-alerts"]["data"]["ALERT"] == \
        '{{ "{{" }} $labels.pod }} is down in {{ .Release.Namespace }}'
    # A volume named after its colour is one templated key, not a copy per namespace.
    vol = "certs-{{ .Values.color }}"
    assert base["volumes"][vol]["secret"]["secretName"] == vol
    assert base["volumeMounts"] == [{"name": vol, "mountPath": "/etc/certs/{{ .Values.color }}"}]
    # Every other key too: a ConfigMap, a CronJob (its image follows the
    # renamed key into values/), a sidecar and the list inside it.
    cm = "settings-{{ .Values.color }}"
    assert base["configMaps"][cm]["data"]["mode"] == "{{ .Values.color }}"
    cron = base["cronjobs"]["report-{{ .Values.color }}"]["jobTemplate"]
    assert cron["envFrom"][cm] == {"type": "configMapRef"}
    assert cron["env"] == [{"name": "COLOR", "value": "{{ .Values.color }}"}]
    assert base["sidecars"]["proxy-{{ .Values.color }}"]["args"] == ["--color", "{{ .Values.color }}"]

    # The color token in each file name moved the release into a folder of
    # that name; the folder's defaults.yaml says which color it is.
    for ns, color, other in (("fac1", "black", "fac2"), ("fac2", "yellow", "fac1")):
        assert _values(out / ns / color / "defaults.yaml")["color"] == color
        values = _values(out / ns / color / "values" / "filter-service-b2b.yaml")
        # Names another namespace, not its own: never templated.
        assert values["env"] == [
            {"name": "COLOR", "value": color},
            {"name": "SELF_URL", "value": f"http://api.{ns}.svc:8080"},
            {"name": "NEIGHBOUR_URL", "value": f"http://db.{other}.svc:5432"}]


def test_without_env_group_output_is_unchanged(tmp_path):
    out = tmp_path / "out"
    _convert(out)
    assert sorted(p.name for p in (out / "base").glob("*.yaml")) == \
        ["filter-service-black-b2b.yaml", "filter-service-yellow-b2b.yaml"]
    assert "{{ ." not in "".join(p.read_text(encoding="utf-8") for p in out.rglob("*.yaml"))


def test_combine_values_rules():
    spec = importlib.util.spec_from_file_location("conv_env_group", CONVERTER_PY)
    conv = sys.modules["conv_env_group"] = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(conv)
    color = conv.EnvGroup("color", {"black": "black", "yellow": "yellow"})

    template, injection = conv.combine_values({"a": "x.a.black", "b": "x.b.yellow"}, [color])
    assert conv._PH_RE.sub(r"{{ \1 }}", template) == "x.{{ .Release.Namespace }}.{{ .Values.color }}"
    assert injection == {"color": {"a": "black", "b": "yellow"}}
    # A word every copy shares is not an environment difference.
    assert conv.combine_values({"a": "black.a", "b": "black.b"}, [color])[1] == {}
    # Ambiguous (two tokens of one group) or not reducible: left alone.
    assert conv.combine_values({"a": "black-yellow", "b": "yellow"}, [color]) is None
    assert conv.combine_values({"a": "db.b", "b": "db.a"}, [color]) is None

    assert conv.strip_name_tokens("filter-service-black-b2b-tst", {"black", "tst"}) == "filter-service-b2b"
    assert conv.strip_name_tokens("attest-latest", {"tst", "test"}) == "attest-latest"
    assert conv.strip_name_tokens("black", {"black"}) == "black"


def test_env_list_is_kept_whole_and_in_order(tmp_path):
    """env is a list in the deployment's own order, and Helm replaces a list
    rather than merging it: when one var differs between namespaces, each
    namespace's values/ carries its whole list, in order, and base/ none."""
    for ns, fruit in (("ns1", "banana"), ("ns2", "tapuz")):
        (tmp_path / "in" / ns).mkdir(parents=True)
        (tmp_path / "in" / ns / "app.yaml").write_text(f"""\
apiVersion: apps/v1
kind: Deployment
metadata: {{name: app, namespace: {ns}}}
spec:
  selector: {{matchLabels: {{app: app}}}}
  template:
    metadata: {{labels: {{app: app}}}}
    spec:
      containers:
        - name: app
          image: registry.example.org/app:1.0
          env:
            - {{name: foo, value: bar}}
            - {{name: test, value: {fruit}}}
""", encoding="utf-8")
    out = tmp_path / "out"
    proc = subprocess.run(
        [sys.executable, str(CONVERTER_PY), "--input", str(tmp_path / "in"), "--output", str(out),
         "--chart", str(CHART_DIR), "--verify-fidelity", "--fidelity-strict"],
        capture_output=True, text=True, errors="replace", cwd=str(GITOPS_DIR))
    assert proc.returncode == 0, proc.stdout + proc.stderr

    assert "env" not in _values(out / "base" / "app.yaml")
    assert _values(out / "ns1" / "values" / "app.yaml")["env"] == [
        {"name": "foo", "value": "bar"}, {"name": "test", "value": "banana"}]
    assert _values(out / "ns2" / "values" / "app.yaml")["env"] == [
        {"name": "foo", "value": "bar"}, {"name": "test", "value": "tapuz"}]
    assert "['env'] differ" in (out / "report" / "conflicts_and_warnings.txt").read_text(encoding="utf-8")
