"""What the chart supplies itself — its OpenShift defaults and what it derives
from other values (names, selectors, ports) — is not written by the converter,
and a live OpenShift dump still renders one-to-one."""
import subprocess
import sys
from pathlib import Path

import yaml

import convert_to_universal_chart as conv

HERE = Path(__file__).resolve().parent
CHART = HERE.parent
CHART_DEFAULTS = yaml.safe_load((CHART / "values.yaml").read_text(encoding="utf-8"))

# A Deployment/Service/Route as `oc get -o yaml` shows them: every default filled in.
LIVE = [
    {"apiVersion": "apps/v1", "kind": "Deployment",
     "metadata": {"name": "web", "namespace": "shop", "labels": {"app": "web", "team": "shop"}},
     "spec": {"replicas": 1, "revisionHistoryLimit": 2, "progressDeadlineSeconds": 600,
              "selector": {"matchLabels": {"app": "web"}},
              "strategy": {"type": "RollingUpdate",
                           "rollingUpdate": {"maxSurge": "25%", "maxUnavailable": "25%"}},
              "template": {"metadata": {"labels": {"app": "web"}}, "spec": {
                  "restartPolicy": "Always", "terminationGracePeriodSeconds": 30,
                  "dnsPolicy": "ClusterFirst", "schedulerName": "default-scheduler",
                  "securityContext": {},
                  "containers": [{"name": "web", "image": "registry.example.com/web:1.0",
                                  "imagePullPolicy": "Always",
                                  "ports": [{"name": "http", "containerPort": 8080, "protocol": "TCP"}],
                                  "terminationMessagePath": "/dev/termination-log",
                                  "terminationMessagePolicy": "File"}]}}}},
    {"apiVersion": "v1", "kind": "Service",
     "metadata": {"name": "web", "namespace": "shop", "labels": {"app": "web"}},
     "spec": {"type": "ClusterIP", "selector": {"app": "web"}, "sessionAffinity": "None",
              "ports": [{"name": "http", "port": 8080, "targetPort": 8080, "protocol": "TCP"}]}},
    {"apiVersion": "route.openshift.io/v1", "kind": "Route",
     "metadata": {"name": "web", "namespace": "shop", "labels": {"app": "web"}},
     "spec": {"host": "web.apps.example.com", "to": {"kind": "Service", "name": "web", "weight": 100},
              "port": {"targetPort": "http"}, "wildcardPolicy": "None",
              "tls": {"termination": "edge", "insecureEdgeTerminationPolicy": "Redirect"}}},
]


def convert(tmp_path, docs):
    d = tmp_path / "in" / "shop"
    d.mkdir(parents=True)
    (d / "web.yaml").write_text(yaml.safe_dump_all(docs), encoding="utf-8")
    out = tmp_path / "out"
    proc = subprocess.run([sys.executable, str(HERE / "convert_to_universal_chart.py"),
                           "--input", str(tmp_path / "in"), "--output", str(out), "--chart", str(CHART),
                           "--verify-fidelity", "--fidelity-strict"],
                          capture_output=True, text=True, cwd=HERE)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return yaml.safe_load((out / "base/web.yaml").read_text(encoding="utf-8")), out


def test_openshift_defaults_and_derivable_values_are_not_written(tmp_path):
    base, _ = convert(tmp_path, LIVE)
    for key in ("revisionHistoryLimit", "progressDeadlineSeconds", "strategy", "restartPolicy",
                "terminationGracePeriodSeconds", "dnsPolicy", "schedulerName", "image",
                "terminationMessagePath", "terminationMessagePolicy", "serviceAccount",
                "chartLabels", "selectorLabelsOnWorkload"):
        assert key not in base, key
    # The Service is the container port, named after the release, selecting the pods.
    # (Its own metadata labels are live data and stay.)
    assert set(base.get("service") or {}) <= {"name", "labels"}
    # The Route needs only what the chart cannot guess: that it exists, and its host.
    # (`name` stays: without fullnameOverride the release name is the Argo CD
    # Application's, which is not "web".)
    assert {k: v for k, v in base["route"].items() if k not in ("labels", "name")} == \
        {"enabled": True, "host": "web.apps.example.com"}
    # app: web is the selector; only the extra label stays.
    assert base["labels"] == {"team": "shop"}
    assert base["selectorLabels"] == {"app": "web"}


def test_a_live_route_without_tls_does_not_get_the_default(tmp_path):
    docs = [*LIVE[:2], {**LIVE[2], "spec": {k: v for k, v in LIVE[2]["spec"].items() if k != "tls"}}]
    base, _ = convert(tmp_path, docs)
    assert base["route"]["tls"] is None


def test_drop_chart_defaults_keeps_what_the_lower_layer_overrides():
    shared_base = {"image": {"pullPolicy": "IfNotPresent"}}
    remainder = {"image": {"pullPolicy": "Always"}, "revisionHistoryLimit": 2}
    assert conv.drop_chart_defaults(remainder, CHART_DEFAULTS, lower=shared_base) == \
        {"image": {"pullPolicy": "Always"}}


def test_drop_chart_defaults_keeps_explicit_empties_that_are_not_the_default():
    values = {"securityContext": {}, "route": {"tls": None}, "workload": {"type": "deployment"}}
    assert conv.drop_chart_defaults(values, CHART_DEFAULTS) == values


def test_drop_derivable_needs_a_pinned_name():
    v = {"service": {"name": "web", "ports": {"http": {"port": 80}}}}
    conv.drop_derivable(v)
    assert v["service"]["name"] == "web"  # the release name is not known without fullnameOverride


def test_route_to_another_service_keeps_its_port():
    v = {"fullnameOverride": "web", "service": {"ports": {"http": {"port": 80}}},
         "route": {"serviceName": "admin", "targetPort": "http"}}
    conv.drop_derivable(v)
    assert v["route"] == {"serviceName": "admin", "targetPort": "http"}


def test_root_directory_patterns_namespace_first_then_groups():
    include, exclude = conv.root_directory_patterns(2)
    assert include == ["*", "*/*"]
    assert {"base", "base/*", "*/values"} <= set(exclude)
    assert not any(p.startswith("*") and p.count("/") == 1 and not p.endswith("values") for p in exclude)
