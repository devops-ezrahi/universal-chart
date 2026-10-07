"""namespaces_to_chart.py end to end, offline (--from-yaml), with the helm
render-verify on: an Ingress must travel with its microservice (in shared.yaml
the chart refuses to render its backend), and -o must hold only the tree."""
import json
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


def _dep(ns, name, configmaps=(), annotations=None):
    tpl_meta = {"labels": {"app": name}, **({"annotations": annotations} if annotations else {})}
    return {"apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": name, "namespace": ns, "labels": {"app": name}},
            "spec": {"selector": {"matchLabels": {"app": name}}, "template": {
                "metadata": tpl_meta,
                "spec": {"containers": [{"name": name, "image": f"registry.example.com/{name}:1.0",
                                         "envFrom": [{"configMapRef": {"name": c}} for c in configmaps]}]}}}}


def _cm(ns, name, **data):
    return {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": name, "namespace": ns}, "data": data}


def _convert(tmp_path, docs, *extra):
    src = tmp_path / "in.yaml"
    src.write_text(yaml.safe_dump_all(docs), encoding="utf-8")
    out, report = tmp_path / "out", tmp_path / "report"
    r = subprocess.run([sys.executable, str(HERE / "namespaces_to_chart.py"), "--from-yaml", str(src),
                        "-o", str(out), "--report", str(report), "--chart", str(HERE.parent),
                        "--verify-fidelity", "--fidelity-strict", *extra],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-2000:]
    load = lambda p: yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return out, (report / "conflicts_and_warnings.txt").read_text(encoding="utf-8"), load


def test_another_apps_same_named_object_is_left_alone(tmp_path):
    """billing-prd's own app-config is not an env copy of shop's shared one: it
    stays in its microservice, and shop-dev/shop-prd still share one templated
    base/shared.yaml."""
    docs = []
    for ns in ("shop-dev", "shop-prd"):
        docs += [_dep(ns, "api", ["app-config"]), _dep(ns, "web", ["app-config"]),
                 _cm(ns, "app-config", URL=f"http://shop.{ns}.svc")]
    docs += [_dep("billing-prd", "ledger", ["app-config"]), _cm("billing-prd", "app-config", DB="ledger")]
    out, _, load = _convert(tmp_path, docs, "--env-suffixes", "dev,prd")
    assert load(out / "base" / "shared.yaml")["configMaps"] == {
        "app-config": {"data": {"URL": "http://shop.{{ .Release.Namespace }}.svc"}}}
    assert load(out / "shop-prd" / "values" / "shared.yaml") == {}
    assert not (out / "billing-prd" / "values" / "shared.yaml").exists()
    assert load(out / "base" / "ledger.yaml")["configMaps"] == {"app-config": {"data": {"DB": "ledger"}}}


def test_one_namespace_never_matches_its_own_shared_release(tmp_path):
    """e2e's test-config only looks like the shared 'config' once 'test' is
    stripped; with no other namespace it stays e2e's own."""
    ns = "shop-prd"
    docs = [_dep(ns, "api", ["config"]), _dep(ns, "web", ["config"]), _cm(ns, "config", A="1"),
            _dep(ns, "e2e", ["test-config"]), _cm(ns, "test-config", B="2")]
    out, _, load = _convert(tmp_path, docs)
    assert "test-config" in load(out / "base" / "e2e.yaml")["configMaps"]
    assert "test-config" not in load(out / "base" / "shared.yaml")["configMaps"]


def test_checksummed_pods_keep_their_configmap(tmp_path):
    """prd's api restarts on config changes (checksum/ annotation), and the chart
    only hashes a release's own ConfigMaps: moving common-config to shared would
    silently end that, so it stays in api, with a report line saying why."""
    docs = [_dep("myapp-dev", "api", ["common-config"]), _dep("myapp-dev", "web", ["common-config"]),
            _cm("myapp-dev", "common-config", LOG="info"),
            _dep("myapp-prd", "api", ["common-config"], annotations={"checksum/config": "0123abcd"}),
            _cm("myapp-prd", "common-config", LOG="info")]
    out, warnings, load = _convert(tmp_path, docs)
    assert "common-config" in load(out / "myapp-prd" / "values" / "api.yaml").get("configMaps", {}) \
        or "common-config" in load(out / "base" / "api.yaml").get("configMaps", {})
    assert not (out / "myapp-prd" / "values" / "shared.yaml").exists()
    assert "checksum/ annotations" in warnings


def test_include_keeps_a_shared_object_shared_when_an_env_copy_shares_it(tmp_path):
    """--include leaves prd's worker out, so only api reads cfg there and the
    selection would take it into api — but dev shares it, so it stays in prd's
    shared release, with one accurate note and no promotion record."""
    dump = tmp_path / "dump"
    for ns, users in (("myapp-dev", ("api", "web")), ("myapp-prd", ("api", "worker"))):
        (dump / ns).mkdir(parents=True)
        (dump / ns / "shared.yaml").write_text(yaml.safe_dump(_cm(ns, "cfg", URL=f"http://x.{ns}.svc")))
        for u in users:
            (dump / ns / f"{u}.yaml").write_text(yaml.safe_dump(_dep(ns, u, ["cfg"])))
    out = tmp_path / "out"
    r = subprocess.run([sys.executable, str(HERE / "convert_to_universal_chart.py"), "--input", str(dump),
                        "--output", str(out), "--chart", str(HERE.parent), "--env-suffixes", "dev,prd",
                        "--include", "=api,=web"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-2000:]
    load = lambda p: yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    assert load(out / "base" / "shared.yaml")["configMaps"] == {
        "cfg": {"data": {"URL": "http://x.{{ .Release.Namespace }}.svc"}}}
    assert "configMaps" not in load(out / "myapp-prd" / "values" / "api.yaml")
    warnings = (out / "report" / "conflicts_and_warnings.txt").read_text(encoding="utf-8")
    assert "only 'api' still uses it — moved into that microservice" not in warnings
    assert "[myapp-prd] shared ConfigMap/cfg: only 'api' uses it here, but myapp-dev's shared release " \
           "holds it too — kept shared" in warnings
    renames = json.loads((out / "report" / "renames.json").read_text(encoding="utf-8"))
    assert not [x for x in renames if x.get("namespace") == "myapp-prd" and x.get("from") == "cfg"]
