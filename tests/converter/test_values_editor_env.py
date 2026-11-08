"""values_editor.py writes env as NAME: value (the chart's env map), and routes a
literal env var to the namespace's minimal values file while a reference
(NAME: {secretKeyRef: ...}) stays in base/."""
import subprocess
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]  # the universal-chart checkout
sys.path.insert(0, str(REPO / "tools"))
import values_editor as ve  # noqa: E402


def run(*args):
    r = subprocess.run([sys.executable, str(REPO / "tools" / "values_editor.py"), *args],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stdout + r.stderr
    return r


def test_literal_env_is_minimal_and_a_reference_is_not():
    assert ve.is_minimal_path("env.LOG_LEVEL", "debug")
    assert ve.is_minimal_path("env.LOG_LEVEL.value", "debug")  # older form
    assert not ve.is_minimal_path("env.DB_PASS", {"secretKeyRef": {"name": "db", "key": "pw"}})
    assert not ve.is_minimal_path("env.DB_PASS.secretKeyRef.name", "db")
    assert not ve.is_minimal_path("envFrom.x", "y")


def test_new_and_set_write_env_as_a_map(tmp_path):
    app = tmp_path / "app"
    run("new", "--app-dir", str(app), "--namespace", "dev", "--name", "api",
        "--image", "registry.example.org/api:1.0", "--env", "LOG_LEVEL=info", "--env", "PORT=8080")
    minimal = app / "dev" / "values" / "api.yaml"
    assert yaml.safe_load(minimal.read_text(encoding="utf-8"))["env"] == {"LOG_LEVEL": "info", "PORT": "8080"}

    run("set", "--app-dir", str(app), "--namespace", "dev", "--name", "api",
        "--key", "env.LOG_LEVEL", "--value", "debug")
    env = yaml.safe_load(minimal.read_text(encoding="utf-8"))["env"]
    assert env == {"LOG_LEVEL": "debug", "PORT": "8080"}
