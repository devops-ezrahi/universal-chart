"""Labels every microservice in a namespace carries go to that namespace's
defaults.yaml — a shared release with no labels does not block them, since
the workload labels it would inherit render on nothing it has."""
import subprocess
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent


def deployment(name, ns, env, extra=None):
    labels = {"team": "shop", "environment": env, "app": name, **(extra or {})}
    return {"apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": name, "namespace": ns, "labels": labels},
            "spec": {"selector": {"matchLabels": {"app": name}}, "template": {
                "metadata": {"labels": {"app": name, "team": "shop", "environment": env}},
                "spec": {"containers": [{"name": name, "image": f"registry.example.com/{name}:1.0",
                                         "envFrom": [{"configMapRef": {"name": "common"}}]}]}}}}


def run(tmp_path):
    for env in ("dev", "prd"):
        ns = f"shop-{env}"
        d = tmp_path / "in" / ns
        d.mkdir(parents=True)
        (d / "api.yaml").write_text(yaml.safe_dump(deployment("api", ns, env, {"tier": "back"})), encoding="utf-8")
        (d / "web.yaml").write_text(yaml.safe_dump(deployment("web", ns, env)), encoding="utf-8")
        (d / "shared.yaml").write_text(yaml.safe_dump(
            {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "common", "namespace": ns},
             "data": {"LOG": "info"}}), encoding="utf-8")
    out = tmp_path / "out"
    r = subprocess.run([sys.executable, str(HERE / "convert_to_universal_chart.py"), "--input", str(tmp_path / "in"),
                        "--output", str(out), "--skip-verify"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]
    return out, lambda p: yaml.safe_load((out / p).read_text(encoding="utf-8")) or {}


def test_labels_every_microservice_carries_are_namespace_defaults(tmp_path):
    out, load = run(tmp_path)
    for env in ("dev", "prd"):
        ns = f"shop-{env}"
        defaults = load(f"{ns}/defaults.yaml")
        # Despite shared.yaml, which has no workload and no labels.
        assert defaults["labels"] == {"team": "shop", "environment": env}
        assert defaults["podLabels"] == {"team": "shop", "environment": env}
        for ms in ("api", "web"):
            own = load(f"{ns}/values/{ms}.yaml")
            # Said once, in defaults — not again in each values file.
            assert "environment" not in (own.get("labels") or {}), own
            assert "environment" not in (own.get("podLabels") or {}), own


def test_a_label_only_some_carry_stays_with_them(tmp_path):
    out, load = run(tmp_path)
    assert "tier" not in load("shop-dev/defaults.yaml")["labels"]
    base = load("base/api.yaml")
    assert base["labels"]["tier"] == "back"
