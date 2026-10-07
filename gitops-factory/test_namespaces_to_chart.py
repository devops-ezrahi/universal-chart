"""namespaces_to_chart.py end to end, offline (--from-yaml), with the helm
render-verify on: an Ingress must travel with its microservice (in shared.yaml
the chart refuses to render its backend), and -o must hold only the tree."""
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

HERE = Path(__file__).resolve().parent

MANIFESTS = """
apiVersion: apps/v1
kind: Deployment
metadata: {name: web, namespace: shop-dev}
spec:
  selector: {matchLabels: {app: web}}
  template:
    metadata: {labels: {app: web}}
    spec: {containers: [{name: app, image: "nginx:1.27", ports: [{containerPort: 8080}]}]}
---
apiVersion: v1
kind: Service
metadata: {name: web, namespace: shop-dev}
spec: {selector: {app: web}, ports: [{name: http, port: 8080, targetPort: 8080}]}
---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata: {name: web, namespace: shop-dev}
spec:
  rules:
    - host: web.dev.example.com
      http: {paths: [{path: /, pathType: Prefix, backend: {service: {name: web, port: {number: 8080}}}}]}
"""


def test_ingress_goes_with_its_service_and_output_is_only_the_tree(tmp_path):
    src = tmp_path / "in.yaml"
    src.write_text(MANIFESTS, encoding="utf-8")
    out = tmp_path / "out"
    r = subprocess.run([sys.executable, str(HERE / "namespaces_to_chart.py"), "--from-yaml", str(src),
                        "-o", str(out), "--chart", str(HERE.parent)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    assert "ingress" in (out / "base" / "web.yaml").read_text(encoding="utf-8")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["in.yaml", "out"]  # no report, no dump
    assert sorted(p.name for p in out.iterdir()) == ["base", "shop-dev"]


def _env_copies(envs):
    """One app in several namespaces, picked with --include by its part-of
    label: api and web share a ConfigMap whose values differ only by the env;
    'other' (not part of the app) uses it too and is left out."""
    docs = []
    for env in envs:
        ns = f"shop-{env}"
        for name, labels in (("api", {"app.kubernetes.io/part-of": "shop"}),
                             ("web", {"app.kubernetes.io/part-of": "shop"}), ("other", {})):
            docs.append({
                "apiVersion": "apps/v1", "kind": "Deployment",
                "metadata": {"name": name, "namespace": ns, "labels": {"app": name, **labels}},
                "spec": {"selector": {"matchLabels": {"app": name}}, "template": {
                    "metadata": {"labels": {"app": name, **labels}},
                    "spec": {"containers": [{"name": name, "image": f"registry.example.com/{name}:1.0",
                                             "envFrom": [{"configMapRef": {"name": "shop-common"}}]}]}}}})
        docs.append({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "shop-common", "namespace": ns},
                     "data": {"ENV": env, "API_URL": f"http://api.{ns}.svc:8080", "LOG": "info"}})
    return yaml.safe_dump_all(docs)


def _merge(base, over):
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


@pytest.mark.parametrize("extra", [[], ["--env-suffixes", "dev,tst,prd"]], ids=["plain", "env-suffixes"])
def test_shared_that_differs_only_by_env_is_one_release(tmp_path, extra):
    """Each namespace's shared ConfigMap differs only by its env, so `shared`
    is one release like api and web: base/shared.yaml once, values/shared.yaml
    in every namespace. It used to fork into base/ + values/<ns>-shared.yaml
    per namespace — one more file per namespace than the app has workloads,
    the copies identical but for the env name."""
    envs = ["dev", "tst", "prd"]
    src = tmp_path / "in.yaml"
    src.write_text(_env_copies(envs), encoding="utf-8")
    out = tmp_path / "out"
    r = subprocess.run([sys.executable, str(HERE / "namespaces_to_chart.py"), "--from-yaml", str(src),
                        "-o", str(out), "--chart", str(HERE.parent),
                        "--include", "app.kubernetes.io/part-of=shop", *extra],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    load = lambda p: yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    assert sorted(p.name for p in (out / "base").iterdir()) == ["api.yaml", "shared.yaml", "web.yaml"]
    base = load(out / "base" / "shared.yaml")
    for env in envs:
        ns = f"shop-{env}"
        assert sorted(p.name for p in (out / ns / "values").iterdir()) == ["api.yaml", "shared.yaml", "web.yaml"]
        own = load(out / ns / "values" / "shared.yaml")
        data = _merge(base, own)["configMaps"]["shop-common"]["data"]
        if extra:  # templated once in base/, nothing left per namespace
            assert own == {}
            assert data["ENV"] == "{{ .Values.environment }}"
            assert data["API_URL"] == "http://api.{{ .Release.Namespace }}.svc:8080"
            assert load(out / ns / "defaults.yaml") == {"environment": env}
        else:  # only what differs, the rest in base/
            assert own == {"configMaps": {"shop-common": {"data": {
                "ENV": env, "API_URL": f"http://api.{ns}.svc:8080"}}}}
            assert data == {"ENV": env, "API_URL": f"http://api.{ns}.svc:8080", "LOG": "info"}


def _split_placement(envs):
    """common-config is read by api and web in dev, so it is shared there; in the
    other namespaces web does not read it and it sits inside api's own file."""
    docs = []
    for env in envs:
        ns = f"myapp-{env}"
        for name, cms in (("api", ["common-config"]), ("web", ["common-config"] if env == "dev" else [])):
            docs.append({
                "apiVersion": "apps/v1", "kind": "Deployment",
                "metadata": {"name": name, "namespace": ns, "labels": {"app": name}},
                "spec": {"selector": {"matchLabels": {"app": name}}, "template": {
                    "metadata": {"labels": {"app": name}},
                    "spec": {"containers": [{"name": name, "image": f"registry.example.com/{name}:1.0",
                                             "envFrom": [{"configMapRef": {"name": c}} for c in cms]}]}}}})
        docs.append({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "common-config", "namespace": ns},
                     "data": {"KAFKA": f"kafka.{ns}.svc:9092", "LOG": "info"}})
    return yaml.safe_dump_all(docs)


def test_object_shared_in_one_namespace_is_shared_in_all(tmp_path):
    """An object one namespace shares and another keeps inside a microservice
    file goes into the shared release everywhere, so its env copies become ONE
    templated base/shared.yaml entry. It used to stay split: dev's concrete
    values in base/shared.yaml, prd's own hard-coded copy in values/api.yaml."""
    envs = ["dev", "tst", "prd"]
    src = tmp_path / "in.yaml"
    src.write_text(_split_placement(envs), encoding="utf-8")
    out = tmp_path / "out"
    r = subprocess.run([sys.executable, str(HERE / "namespaces_to_chart.py"), "--from-yaml", str(src),
                        "-o", str(out), "--chart", str(HERE.parent), "--env-suffixes", ",".join(envs),
                        "--verify-fidelity", "--fidelity-strict"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-2000:]
    load = lambda p: yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    assert load(out / "base" / "shared.yaml")["configMaps"] == {"common-config": {"data": {
        "KAFKA": "kafka.{{ .Release.Namespace }}.svc:9092", "LOG": "info"}}}
    for env in envs:
        values = out / f"myapp-{env}" / "values"
        assert sorted(p.name for p in values.iterdir()) == ["api.yaml", "shared.yaml", "web.yaml"]
        assert load(values / "shared.yaml") == {}
        assert "configMaps" not in load(values / "api.yaml")
    assert "configMaps" not in load(out / "base" / "api.yaml")
