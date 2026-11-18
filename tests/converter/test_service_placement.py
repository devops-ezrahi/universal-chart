"""A Service (and the Route in front of it) goes to the microservice whose pods
it selects. Kubernetes' rule: every selector label is on the pod — the selector
is usually a SUBSET of the pod labels. Matched the other way round, a Service
selecting `app: api` in front of pods labelled `app: api, deployment: api` was
"claimed by no workload" and landed in shared.yaml with its Route."""
import subprocess
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
NS = "shop-dev"


def deployment(name, labels, app=None):
    return {"apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": name, "namespace": NS, "labels": {"app": app or name}},
            "spec": {"selector": {"matchLabels": labels}, "template": {
                "metadata": {"labels": labels},
                "spec": {"containers": [{"name": name, "image": f"registry.example.com/{name}:1"}]}}}}


def service(name, selector):
    return {"apiVersion": "v1", "kind": "Service", "metadata": {"name": name, "namespace": NS},
            "spec": {"selector": selector, "ports": [{"name": "http", "port": 80, "targetPort": 8080}]}}


def route(name, svc):
    return {"apiVersion": "route.openshift.io/v1", "kind": "Route", "metadata": {"name": name, "namespace": NS},
            "spec": {"to": {"kind": "Service", "name": svc}, "port": {"targetPort": "http"}}}


def pull(tmp_path, docs):
    src = tmp_path / "in.yaml"
    src.write_text(yaml.safe_dump_all(docs), encoding="utf-8")
    r = subprocess.run([sys.executable, str(REPO / "gitops-factory" / "convert_to_universal_chart.py"),
                        "--from-yaml", str(src), "--pull-only", "-o", str(tmp_path / "dump")],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]
    placed = {}
    for f in sorted((tmp_path / "dump" / NS).glob("*.yaml")):
        for d in yaml.safe_load_all(f.read_text(encoding="utf-8")):
            if d and d.get("kind") in ("Service", "Route"):
                placed[(d["kind"], d["metadata"]["name"])] = f.stem
    return placed


def test_subset_selector_goes_to_its_workload(tmp_path):
    placed = pull(tmp_path, [
        deployment("api", {"app": "api", "deployment": "api"}),
        deployment("web", {"app": "web", "deployment": "web"}),
        service("api", {"app": "api"}), service("web", {"app": "web"}),
        route("api", "api"), route("web", "web")])
    assert placed == {("Service", "api"): "api", ("Route", "api"): "api",
                      ("Service", "web"): "web", ("Route", "web"): "web"}


def test_service_named_after_one_of_two_selected_workloads_goes_to_it(tmp_path):
    # One app, two workloads both carrying tier: backend.
    placed = pull(tmp_path, [
        deployment("api", {"app": "shop", "tier": "backend", "deployment": "api"}, app="shop"),
        deployment("worker", {"app": "shop", "tier": "backend", "deployment": "worker"}, app="shop"),
        service("api", {"tier": "backend"}), route("api", "api")])
    assert placed == {("Service", "api"): "api", ("Route", "api"): "api"}


def test_service_spanning_two_apps_by_no_name_is_shared(tmp_path):
    placed = pull(tmp_path, [
        deployment("api", {"app": "api", "tier": "backend"}),
        deployment("worker", {"app": "worker", "tier": "backend"}),
        service("backend", {"tier": "backend"}), route("backend", "backend")])
    assert placed == {("Service", "backend"): "shared", ("Route", "backend"): "shared"}


def test_service_spanning_two_apps_named_after_one_goes_to_it(tmp_path):
    placed = pull(tmp_path, [
        deployment("api", {"app": "api", "tier": "backend"}),
        deployment("worker", {"app": "worker", "tier": "backend"}),
        service("api-internal", {"tier": "backend"}), route("api-internal", "api-internal")])
    assert placed == {("Service", "api-internal"): "api", ("Route", "api-internal"): "api"}
