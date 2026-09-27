"""namespaces_to_chart.py end to end, offline (--from-yaml), with the helm
render-verify on: an Ingress must travel with its microservice (in shared.yaml
the chart refuses to render its backend), and -o must hold only the tree."""
import subprocess
import sys
from pathlib import Path

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
