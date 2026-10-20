"""A converted release must render its objects one-to-one with the live ones,
even when the ArgoCD release name differs from the live instance: no chart
identity labels, the live selector, and nothing the live pod spec did not have
(automountServiceAccountToken, a freshly minted ServiceAccount)."""
import subprocess
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent

RAW = """apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: redis-yellow-b2c-fac1
  namespace: rakia-fac1
  labels:
    app.kubernetes.io/applicationSet: redis-yellow-b2c
    app.kubernetes.io/instance: redis-yellow-b2c-fac1
    app.kubernetes.io/managed-by: Helm
    app.kubernetes.io/part-of: yellow-b2c
    app.kubernetes.io/version: 1.16.0
    app.openshift.io/runtime: redis
    argocd.argoproj.io/instance: redis-yellow-b2c-fac1
    critical: 'false'
    environment: dev
    helm.sh/chart: rakia-0.1.0
    team: rakia_app_a
spec:
  serviceName: redis-yellow-b2c-fac1
  replicas: 1
  selector:
    matchLabels:
      app.kubernetes.io/instance: redis-yellow-b2c-fac1
      app.kubernetes.io/name: redis-yellow-b2c-fac1
  template:
    metadata:
      labels:
        app.kubernetes.io/applicationSet: redis-yellow-b2c
        app.kubernetes.io/instance: redis-yellow-b2c-fac1
        app.kubernetes.io/name: redis-yellow-b2c-fac1
        app.openshift.io/runtime: redis
    spec:
      containers:
        - name: redis
          image: redis:7
          command: [redis-server]
          args: [/data/conf/redis.conf]
---
apiVersion: v1
kind: Service
metadata:
  name: redis-yellow-b2c-fac1
  namespace: rakia-fac1
  labels:
    app.kubernetes.io/instance: redis-yellow-b2c-fac1
    helm.sh/chart: rakia-0.1.0
spec:
  selector:
    app.kubernetes.io/instance: redis-yellow-b2c-fac1
    app.kubernetes.io/name: redis-yellow-b2c-fac1
  ports: [{name: redis, port: 6379, targetPort: 6379}]
"""


def test_render_matches_live_labels_selector_and_pod_spec(tmp_path):
    ns = tmp_path / "in" / "rakia-fac1"
    ns.mkdir(parents=True)
    (ns / "redis-b2c.yaml").write_text(RAW, encoding="utf-8")
    out = tmp_path / "out"
    subprocess.run([sys.executable, str(HERE / "convert_to_universal_chart.py"), "--input", str(tmp_path / "in"),
                    "--output", str(out), "--chart", str(HERE.parent), "--skip-verify"], check=True)
    # A release name unlike the live instance, as the ApplicationSet produces.
    rendered = subprocess.run(
        ["helm", "template", "redis-b2c-fac1-yellow", str(HERE.parent), "-n", "rakia-fac1",
         "-f", str(out / "base/redis-b2c.yaml"), "-f", str(out / "rakia-fac1/defaults.yaml"),
         "-f", str(out / "rakia-fac1/values/redis-b2c.yaml")],
        check=True, capture_output=True, text=True).stdout
    got = {d["kind"]: d for d in yaml.safe_load_all(rendered) if d}
    live = {d["kind"]: d for d in yaml.safe_load_all(RAW) if d}

    assert "ServiceAccount" not in got
    for kind in ("StatefulSet", "Service"):
        want = dict(live[kind]["metadata"]["labels"])
        want.pop("argocd.argoproj.io/instance", None)  # ArgoCD stamps it at sync
        want.pop("helm.sh/chart", None)  # the old chart's stamp is dropped on purpose
        assert got[kind]["metadata"]["labels"] == want, kind
    assert got["Service"]["spec"]["selector"] == live["Service"]["spec"]["selector"]
    sts, live_sts = got["StatefulSet"]["spec"], live["StatefulSet"]["spec"]
    assert sts["selector"] == live_sts["selector"]
    assert sts["template"]["metadata"]["labels"] == live_sts["template"]["metadata"]["labels"]
    assert "automountServiceAccountToken" not in sts["template"]["spec"]
    # The live template never named a ServiceAccount; rendering
    # `serviceAccountName: default` onto it is a pod-template change (a rollout).
    assert "serviceAccountName" not in sts["template"]["spec"]
