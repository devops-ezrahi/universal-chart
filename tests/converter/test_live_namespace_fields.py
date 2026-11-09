"""Fields found lost converting the live namespaces of a real cluster (the
homelab k3d d2c-* namespaces), each now carried and rendered back:

- an Ingress's own metadata.labels (`app: invoice`) — handle_ingress never
  read them, though the chart renders ingress.labels;
- a CronJob pod's schedulerName (`default-scheduler`, which the API server
  fills in on every live object) — not among the job pod fields carried."""
import subprocess
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
CMD = REPO / "convert_to_universal_chart.py"

DUMP = """
apiVersion: apps/v1
kind: Deployment
metadata: {name: invoice, labels: {app: invoice}}
spec:
  selector: {matchLabels: {app: invoice}}
  template:
    metadata: {labels: {app: invoice}}
    spec:
      schedulerName: default-scheduler
      containers: [{name: invoice, image: "nginx:1.27", ports: [{containerPort: 8080, name: http}]}]
---
apiVersion: v1
kind: Service
metadata: {name: invoice, labels: {app: invoice}}
spec: {selector: {app: invoice}, ports: [{name: http, port: 80, targetPort: 8080}]}
---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata: {name: invoice, labels: {app: invoice, team: billing}}
spec:
  ingressClassName: traefik
  rules:
    - host: invoice.example.com
      http: {paths: [{path: /, pathType: Prefix, backend: {service: {name: invoice, port: {name: http}}}}]}
---
apiVersion: batch/v1
kind: CronJob
metadata: {name: invoice-report, labels: {app: invoice}}
spec:
  schedule: "0 2 * * *"
  jobTemplate:
    spec:
      template:
        metadata: {labels: {app: invoice}}
        spec:
          schedulerName: default-scheduler
          restartPolicy: OnFailure
          containers: [{name: report, image: "busybox:1.36", command: [sh, -c, "echo report"]}]
"""


def test_ingress_labels_and_job_scheduler_name_survive(tmp_path):
    (tmp_path / "in" / "billing").mkdir(parents=True)
    (tmp_path / "in" / "billing" / "invoice.yaml").write_text(DUMP, encoding="utf-8")
    out = tmp_path / "out"
    r = subprocess.run([sys.executable, str(CMD), "--input", str(tmp_path / "in"), "-o", str(out), "--chart", str(REPO),
                        "--verify-fidelity", "--fidelity-strict"], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]
    base = yaml.safe_load((out / "base" / "invoice.yaml").read_text(encoding="utf-8"))
    assert base["ingress"]["labels"] == {"app": "invoice", "team": "billing"}
    cron = next(iter(base["cronjobs"].values()))
    assert cron["jobTemplate"]["schedulerName"] == "default-scheduler"
