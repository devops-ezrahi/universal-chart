"""Annotations Rancher writes at runtime are never carried into values.

Found converting a live namespace on a Rancher-managed k3d cluster:
field.cattle.io/publicEndpoints (rewritten by Rancher from the Service /
Ingress status) landed in base/, so Argo CD and Rancher would overwrite each
other on every reconcile. objectset.rio.cattle.io/applied is Rancher's own
last-applied blob, and cattle.io/timestamp is its "Redeploy" stamp on the pod
template — carried, it restarts every pod on the first sync."""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
CMD = REPO / "convert_to_universal_chart.py"

ENDPOINTS = '[{"addresses":["172.18.0.3"],"port":80,"protocol":"HTTP","serviceName":"shop:web"}]'
DUMP = f"""
apiVersion: apps/v1
kind: Deployment
metadata:
  name: web
  labels: {{app: web}}
  annotations:
    field.cattle.io/publicEndpoints: '{ENDPOINTS}'
    objectset.rio.cattle.io/applied: H4sIAAAAAAAA/6yRT2vDMAzFv4vO
    team: storefront
spec:
  selector: {{matchLabels: {{app: web}}}}
  template:
    metadata:
      labels: {{app: web}}
      annotations:
        cattle.io/timestamp: "2026-11-01T10:00:00Z"
        prometheus.io/scrape: "true"
    spec:
      containers:
        - name: web
          image: nginx:1.27
          ports: [{{containerPort: 8080, name: http}}]
---
apiVersion: v1
kind: Service
metadata:
  name: web
  annotations:
    field.cattle.io/publicEndpoints: '{ENDPOINTS}'
spec:
  selector: {{app: web}}
  ports: [{{name: http, port: 80, targetPort: 8080}}]
"""


def test_rancher_runtime_annotations_are_dropped_and_the_app_s_own_kept(tmp_path):
    (tmp_path / "in" / "shop").mkdir(parents=True)
    (tmp_path / "in" / "shop" / "web.yaml").write_text(DUMP, encoding="utf-8")
    out = tmp_path / "out"
    r = subprocess.run([sys.executable, str(CMD), "--input", str(tmp_path / "in"), "-o", str(out), "--chart", str(REPO),
                        "--verify-fidelity", "--fidelity-strict"], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]
    text = "".join(f.read_text(encoding="utf-8") for f in out.rglob("*.yaml") if "report" not in f.parts)
    for gone in ("publicEndpoints", "objectset.rio.cattle.io", "cattle.io/timestamp"):
        assert gone not in text, gone
    assert "team: storefront" in text and "prometheus.io/scrape" in text  # the app's own annotations stay
