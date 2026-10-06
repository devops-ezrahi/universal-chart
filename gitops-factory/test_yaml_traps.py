"""Strings that Helm's YAML reader (go-yaml v2) would re-type must survive the
converter and the chart as strings: an env var `"y"` used to reach the chart as
`true`, and the API server refused the whole Deployment. Also: the verifier's
reader must accept Go's bare `=` / `<<`, and an over-long release name is a
conversion-time warning."""
import subprocess
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import convert_to_universal_chart as conv  # noqa: E402

# Each was checked against `helm template` dumping .Values: bare, Helm reads
# them as bool / int / float / null (or cannot render them at all).
TRAPS = ["y", "n", "Y", "N", "yes", "off", "0x1F", "0o17", "1_000", "+12", ".inf", "-.inf", ".nan", "1e3",
         "0755", "08", "09", "null", "~", "NULL", "true", "False", ""]
PLAIN = ["=", "<<", "1:30", "2001-12-14", "v1.2", "- dash", "#hash", "a: b", "plain"]


def test_dumper_quotes_every_string_go_yaml_would_retype():
    for v in TRAPS:
        assert conv.go_yaml_retypes(v), v
        out = yaml.dump({"k": v}, Dumper=conv._ValuesDumper)
        assert yaml.safe_load(out) == {"k": v}
        assert out.strip() not in (f"k: {v}",), f"{v!r} emitted bare"
    for v in PLAIN:
        assert not conv.go_yaml_retypes(v), v


def test_helm_output_reader_accepts_go_bare_scalars():
    assert list(yaml.load_all("env:\n- value: =\n- value: <<\n", Loader=conv._HelmOutputLoader)) == [
        {"env": [{"value": "="}, {"value": "<<"}]}]


DEPLOY = """apiVersion: apps/v1
kind: Deployment
metadata: {name: traps, namespace: edge}
spec:
  selector: {matchLabels: {app: traps}}
  template:
    metadata: {labels: {app: traps}}
    spec:
      containers:
      - name: app
        image: nginx:1.27
        env:
"""


def test_trap_env_values_render_as_the_same_strings(tmp_path):
    ns = tmp_path / "in" / "edge"
    ns.mkdir(parents=True)
    env = "".join(f"        - name: T{i}\n          value: {yaml.safe_dump(v).strip().removesuffix('...').strip()}\n"
                  for i, v in enumerate(TRAPS + PLAIN))
    (ns / "traps.yaml").write_text(DEPLOY + env, encoding="utf-8")
    out = tmp_path / "out"
    subprocess.run([sys.executable, str(HERE / "convert_to_universal_chart.py"), "--input", str(tmp_path / "in"),
                    "--output", str(out), "--chart", str(HERE.parent), "--skip-verify"], check=True)
    rendered = subprocess.run(
        ["helm", "template", "traps", str(HERE.parent), "-n", "edge", "-f", str(out / "base/traps.yaml"),
         "-f", str(out / "edge/defaults.yaml"), "-f", str(out / "edge/values/traps.yaml")],
        check=True, capture_output=True, text=True).stdout
    dep = next(d for d in yaml.load_all(rendered, Loader=conv._HelmOutputLoader) if d and d["kind"] == "Deployment")
    got = {e["name"]: e.get("value") for e in dep["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert got == {f"T{i}": v for i, v in enumerate(TRAPS + PLAIN)}


def test_release_name_over_53_chars_is_reported(tmp_path):
    name = "a" * 20 + "-very-long-name-that-is-exactly-63-chars-lo"
    ns = tmp_path / "in" / "edge"
    ns.mkdir(parents=True)
    (ns / f"{name}.yaml").write_text(DEPLOY.replace("traps", name) + "        - {name: X, value: x}\n", encoding="utf-8")
    out = tmp_path / "out"
    subprocess.run([sys.executable, str(HERE / "convert_to_universal_chart.py"), "--input", str(tmp_path / "in"),
                    "--output", str(out), "--chart", str(HERE.parent), "--skip-verify"], check=True,
                   capture_output=True)
    report = (out / "report" / "conflicts_and_warnings.txt").read_text(encoding="utf-8")
    assert f"release name '{name}' is not a valid Helm release name" in report
