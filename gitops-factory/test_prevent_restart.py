"""--prevent-restart: the list-order keys and the empty securityContext that only
keep the pod template byte-identical (so the cutover restarts nothing) are
written under the flag alone. Orders that change behaviour are kept always.
Workload labels/annotations are outside the template and stay editable."""
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

HERE = Path(__file__).resolve().parent


def workload(env):
    return {"apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": "api", "namespace": "shop-dev", "labels": {"app": "api"}},
            "spec": {"selector": {"matchLabels": {"app": "api"}}, "template": {
                "metadata": {"labels": {"app": "api"}},
                "spec": {
                    "initContainers": [{"name": "z-migrate", "image": "busybox:1"},
                                       {"name": "a-warm", "image": "busybox:1"}],
                    "containers": [
                        {"name": "api", "image": "registry.example.com/api:1.0", "securityContext": {},
                         "ports": [{"name": "web", "containerPort": 8080}, {"name": "admin", "containerPort": 9090}],
                         "env": env,
                         "envFrom": [{"secretRef": {"name": "z-sec"}}, {"configMapRef": {"name": "a-cm"}}],
                         "volumeMounts": [{"name": "z-data", "mountPath": "/data"},
                                          {"name": "a-tmp", "mountPath": "/tmp"}]},
                        {"name": "z-proxy", "image": "envoy:1"}, {"name": "a-log", "image": "fluent:1"}],
                    "volumes": [{"name": "z-data", "emptyDir": {"sizeLimit": "1Gi"}},
                                {"name": "a-tmp", "emptyDir": {"sizeLimit": "1Gi"}}]}}}}


PLAIN_ENV = [{"name": "ZED", "value": "1"}, {"name": "ALPHA", "value": "2"}]
# A_URL reads HOST: sorted, A_URL would come first and $(HOST) would no longer
# expand, so envOrder is written. With URL (sorts after HOST) it is not needed.
REF_ENV = [{"name": "HOST", "value": "db"}, {"name": "A_URL", "value": "postgres://$(HOST):5432"}]
SORTED_REF_ENV = [{"name": "HOST", "value": "db"}, {"name": "URL", "value": "postgres://$(HOST):5432"}]


def convert(tmp_path, env, *flags):
    ns = tmp_path / "in" / "shop-dev"
    ns.mkdir(parents=True)
    (ns / "api.yaml").write_text(yaml.safe_dump(workload(env)), encoding="utf-8")
    out = tmp_path / "out"
    r = subprocess.run([sys.executable, str(HERE / "convert_to_universal_chart.py"), "--input", str(tmp_path / "in"),
                        "--output", str(out), "--skip-verify", *flags],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]
    return yaml.safe_load((out / "base" / "api.yaml").read_text(encoding="utf-8"))


COSMETIC = ("portOrder", "volumeOrder", "sidecarOrder")
MOUNTS = [{"name": "z-data", "mountPath": "/data"}, {"name": "a-tmp", "mountPath": "/tmp"}]


def test_clean_by_default(tmp_path):
    v = convert(tmp_path, PLAIN_ENV)
    assert not [k for k in COSMETIC if k in v], v
    assert "securityContext" not in v
    # Meaning, not cosmetics: init containers run in order, a later envFrom wins.
    assert v["initContainerOrder"] == ["z-migrate", "a-warm"]
    assert v["envFromOrder"] == ["z-sec", "a-cm"]
    # env is a NAME: value map; with no $(VAR) between them the chart's sorted
    # order is harmless, so no envOrder. volumeMounts stays a list.
    assert v["env"] == {"ZED": "1", "ALPHA": "2"}
    assert v["volumeMounts"] == MOUNTS
    assert "envOrder" not in v and "volumeMountOrder" not in v


def test_env_order_kept_when_a_value_reads_another(tmp_path):
    v = convert(tmp_path, REF_ENV)
    assert v["env"] == {"HOST": "db", "A_URL": "postgres://$(HOST):5432"}
    assert v["envOrder"] == ["HOST", "A_URL"]


def test_no_env_order_when_sorting_keeps_the_reference(tmp_path):
    v = convert(tmp_path, SORTED_REF_ENV)
    assert "envOrder" not in v


@pytest.mark.parametrize("flag", [["--prevent-restart"], ["--prevent-restart", "true"], ["--preventRestart"]])
def test_prevent_restart_keeps_the_live_template(tmp_path, flag):
    v = convert(tmp_path, PLAIN_ENV, *flag)
    assert v["portOrder"] == ["web", "admin"]
    assert v["env"] == {"ZED": "1", "ALPHA": "2"}
    assert v["envOrder"] == ["ZED", "ALPHA"]
    assert v["volumeMounts"] == MOUNTS
    assert v["volumeOrder"] == ["z-data", "a-tmp"]
    assert v["sidecarOrder"] == ["z-proxy", "a-log"]
    assert v["securityContext"] == {}
    # Only the pod template is frozen: the chart's link annotations on the
    # workload metadata stay on (they restart nothing).
    assert "links" not in v
