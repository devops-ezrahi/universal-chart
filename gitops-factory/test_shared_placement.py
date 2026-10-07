"""An object is `shared` only where it is shared in every namespace that has
it; otherwise it lives in its microservice everywhere, so base/ can see it as
one object (unify_shared_placement)."""
import subprocess
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent


def deployment(name, ns, cm="app-config"):
    return {"apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": name, "namespace": ns, "labels": {"app": name}},
            "spec": {"selector": {"matchLabels": {"app": name}}, "template": {
                "metadata": {"labels": {"app": name}},
                "spec": {"containers": [{"name": name, "image": f"registry.example.com/{name}:1.0",
                                         "envFrom": [{"configMapRef": {"name": cm}}]}]}}}}


def configmap(ns, name="app-config"):
    return {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": name, "namespace": ns},
            "data": {"LOG": "info", "ENV": ns.rsplit("-", 1)[-1]}}


def write(root, ns, files):
    d = root / ns
    d.mkdir(parents=True)
    for name, docs in files.items():
        (d / f"{name}.yaml").write_text(yaml.safe_dump_all(docs), encoding="utf-8")


def convert(tmp_path):
    out = tmp_path / "out"
    r = subprocess.run([sys.executable, str(HERE / "convert_to_universal_chart.py"), "--input", str(tmp_path / "in"),
                        "--output", str(out), "--skip-verify"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]
    load = lambda p: (yaml.safe_load(p.read_text(encoding="utf-8")) or {}) if p.exists() else None
    return out, load


def configmaps(doc):
    return sorted(((doc or {}).get("configMaps") or {}).keys())


def test_shared_in_one_namespace_but_owned_in_another_moves_to_the_owner(tmp_path):
    root = tmp_path / "in"
    # dev: two workloads use it, so the importer filed it in shared.yaml.
    write(root, "shop-dev", {"api": [deployment("api", "shop-dev")], "web": [deployment("web", "shop-dev")],
                             "shared": [configmap("shop-dev")]})
    # prd: only api uses it, so it sits in api's own file.
    write(root, "shop-prd", {"api": [deployment("api", "shop-prd"), configmap("shop-prd")],
                             "web": [deployment("web", "shop-prd", cm="other")]})
    out, load = convert(tmp_path)
    assert configmaps(load(out / "base" / "api.yaml")) == ["app-config"]
    assert configmaps(load(out / "base" / "shared.yaml")) == []
    for ns in ("shop-dev", "shop-prd"):
        assert configmaps(load(out / ns / "values" / "shared.yaml")) == []
        assert configmaps(load(out / ns / "values" / "web.yaml")) == []


def test_identical_copies_are_not_promoted_when_another_namespace_owns_it(tmp_path):
    root = tmp_path / "in"
    # dev: api and web each carry the same ConfigMap — promotion would lift it.
    write(root, "shop-dev", {"api": [deployment("api", "shop-dev"), configmap("shop-dev")],
                             "web": [deployment("web", "shop-dev"), configmap("shop-dev")]})
    write(root, "shop-prd", {"api": [deployment("api", "shop-prd"), configmap("shop-prd")],
                             "web": [deployment("web", "shop-prd", cm="other")]})
    out, load = convert(tmp_path)
    assert configmaps(load(out / "base" / "api.yaml")) == ["app-config"]
    assert configmaps(load(out / "base" / "web.yaml")) == []
    assert configmaps(load(out / "base" / "shared.yaml")) == []


def test_shared_everywhere_stays_shared(tmp_path):
    root = tmp_path / "in"
    for ns in ("shop-dev", "shop-prd"):
        write(root, ns, {"api": [deployment("api", ns)], "web": [deployment("web", ns)],
                         "shared": [configmap(ns)]})
    out, load = convert(tmp_path)
    assert configmaps(load(out / "base" / "shared.yaml")) == ["app-config"]
    assert configmaps(load(out / "base" / "api.yaml")) == []
