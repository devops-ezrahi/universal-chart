"""The chart renders no Service and no ServiceAccount unless asked
(service.enabled / serviceAccount.create default false), so the converter
writes them only where the source manifests had one, and never the redundant
`false`."""
import subprocess
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent


def deployment(name, sa=None):
    pod = {"containers": [{"name": name, "image": f"registry.example.com/{name}:1.0"}]}
    if sa:
        pod["serviceAccountName"] = sa
    return {"apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": name, "namespace": "shop"},
            "spec": {"selector": {"matchLabels": {"app": name}}, "template": {
                "metadata": {"labels": {"app": name}}, "spec": pod}}}


def convert(tmp_path, docs):
    d = tmp_path / "in" / "shop"
    d.mkdir(parents=True)
    (d / "api.yaml").write_text(yaml.safe_dump_all(docs), encoding="utf-8")
    out = tmp_path / "out"
    r = subprocess.run([sys.executable, str(HERE / "convert_to_universal_chart.py"), "--input", str(tmp_path / "in"),
                        "--output", str(out), "--skip-verify"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]
    return yaml.safe_load((out / "base" / "api.yaml").read_text(encoding="utf-8"))


def test_nothing_written_when_the_source_has_neither(tmp_path):
    v = convert(tmp_path, [deployment("api")])
    assert "service" not in v
    assert "serviceAccount" not in v


def test_enabled_where_the_source_has_them(tmp_path):
    v = convert(tmp_path, [
        deployment("api", sa="api-sa"),
        {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": "api-sa", "namespace": "shop"}},
        {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "api", "namespace": "shop"},
         "spec": {"selector": {"app": "api"}, "ports": [{"name": "http", "port": 80, "targetPort": 8080}]}},
    ])
    assert v["service"]["enabled"] is True
    assert v["serviceAccount"] == {"create": True, "name": "api-sa"}


def test_a_consumed_serviceaccount_is_named_not_created(tmp_path):
    v = convert(tmp_path, [deployment("api", sa="elsewhere-sa")])
    assert v["serviceAccount"] == {"name": "elsewhere-sa"}
