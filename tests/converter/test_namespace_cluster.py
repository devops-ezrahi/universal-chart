"""--namespace-cluster: each namespace can deploy to a cluster of its own. The
cluster is written to the namespace's defaults.yaml (variant folders too),
and the root ApplicationSet reads it from there, falling back to the tree's
one destination."""
import subprocess
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]  # the universal-chart checkout


def deployment(name, ns):
    return {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": name, "namespace": ns},
            "spec": {"selector": {"matchLabels": {"app": name}}, "template": {
                "metadata": {"labels": {"app": name}},
                "spec": {"containers": [{"name": name, "image": f"registry.example.com/{name}:1.0"}]}}}}


def run(tmp_path, *flags):
    for ns in ("shop-dev", "shop-prd", "shop-tst"):
        d = tmp_path / "in" / ns
        d.mkdir(parents=True)
        for ms in ("api-black", "api-yellow"):
            (d / f"{ms}.yaml").write_text(yaml.safe_dump(deployment(ms, ns)), encoding="utf-8")
    out = tmp_path / "out"
    r = subprocess.run([sys.executable, str(REPO / "gitops-factory" / "convert_to_universal_chart.py"), "--input", str(tmp_path / "in"),
                        "--output", str(out), "--skip-verify", "--argocd-manifests", "true",
                        "--env-group", "color=black,yellow", *flags],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]
    return out, lambda p: yaml.safe_load((out / p).read_text(encoding="utf-8")) or {}


def params(load):
    appset = load("rootApplicationSet.yaml")
    return {p["name"]: p["value"] for p in appset["spec"]["template"]["spec"]["sources"][0]["helm"]["parameters"]}


def test_each_namespace_folder_names_its_cluster(tmp_path):
    out, load = run(tmp_path, "--namespace-cluster", "shop-dev=https://api.dev.example.com:6443",
                    "--namespace-cluster", "shop-prd=prod-east")
    assert load("shop-dev/defaults.yaml")["cluster"] == {"server": "https://api.dev.example.com:6443"}
    assert load("shop-dev/yellow/defaults.yaml")["cluster"] == {"server": "https://api.dev.example.com:6443"}
    assert load("shop-prd/black/defaults.yaml")["cluster"] == {"name": "prod-east"}
    assert "cluster" not in load("shop-tst/defaults.yaml")
    # Read per folder, with the tree's destination for a folder that names none.
    p = params(load)
    assert p["destinationServer"] == '{{ dig "cluster" "server" "https://kubernetes.default.svc" . }}'
    assert p["destinationName"] == '{{ if dig "cluster" "server" "" . }}{{ else }}{{ dig "cluster" "name" "" . }}{{ end }}'


def test_without_it_the_root_applicationset_is_unchanged(tmp_path):
    out, load = run(tmp_path, "--argocd-cluster", "hub")
    p = params(load)
    assert p["destinationServer"] == "https://kubernetes.default.svc"
    assert p["destinationName"] == "hub"
    assert "cluster" not in load("shop-dev/defaults.yaml")


def test_an_unknown_namespace_is_reported(tmp_path):
    out, _ = run(tmp_path, "--namespace-cluster", "shop-qa=https://api.qa:6443")
    assert "no namespace shop-qa in this run" in (out / "report" / "conflicts_and_warnings.txt").read_text(encoding="utf-8")
