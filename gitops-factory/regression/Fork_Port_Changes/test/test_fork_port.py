"""Changes ported from the forked converter (new_converter.py), items A-O of its
change log. One focused check per item; see gitops-factory/README.md, "Converter
options added in the fork merge"."""
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
import convert_to_universal_chart as conv  # noqa: E402


def ctx():
    return conv.MicroserviceContext(namespace="ns", name="app")


def dep(name, **pod_spec):
    return {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": name},
            "spec": {"template": {"metadata": {},
                                  "spec": {"containers": [{"name": "c", "image": "nginx:1"}], **pod_spec}}}}


def run_converter(src: Path, out: Path, *flags):
    subprocess.run([sys.executable, str(ROOT / "convert_to_universal_chart.py"),
                    "--input", str(src), "--output", str(out), "--skip-verify", *flags],
                   check=True, capture_output=True, env={**os.environ, "PYTHONIOENCODING": "utf-8"})


# A — flat by default; wiring only with --argocd-manifests; a flat run flags old wiring stale
def test_flat_default_argocd_manifests_and_stale_wiring(tmp_path):
    src = tmp_path / "in" / "ns"
    src.mkdir(parents=True)
    (src / "app.yaml").write_text(yaml.safe_dump(dep("app")), encoding="utf-8")
    out = tmp_path / "out"

    run_converter(tmp_path / "in", out, "--argocd-manifests")
    assert (out / "root-application.yaml").is_file()

    run_converter(tmp_path / "in", out)
    assert (out / "ns" / "values" / "app.yaml").is_file()
    assert "root-application.yaml" in (out / "report" / "stale_output.txt").read_text(encoding="utf-8")


def test_vanished_folder_without_signature_is_not_stale(tmp_path):
    tooling = tmp_path / "tooling"
    tooling.mkdir()
    (tooling / "lint.yaml").write_text("x: 1\n")
    assert conv.find_stale_output(tmp_path, {}, set()) == []


# B — --exclude / --keep
def test_exclude_matches_names_and_label_values_not_data(monkeypatch):
    monkeypatch.setattr(conv, "EXCLUDE_PATTERNS", ["kafka"])
    monkeypatch.setattr(conv, "KEEP_PATTERNS", [])
    assert conv.is_excluded({"metadata": {"name": "kafka-broker"}})
    assert conv.is_excluded({"metadata": {"name": "x", "labels": {"stack": "Kafka"}}})
    assert not conv.is_excluded({"metadata": {"name": "app-config"}, "data": {"url": "kafka:9092"}})
    monkeypatch.setattr(conv, "KEEP_PATTERNS", ["kafka-ui"])
    assert not conv.is_excluded({"metadata": {"name": "kafka-ui"}})


# C — identical duplicates promoted to shared; differing ones are not
def test_identical_configmap_is_promoted_differing_is_not():
    def cm(v):
        return {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "common"}, "data": {"k": v}}

    reg = conv.NamespaceRegistry("ns")
    shared = conv.promote_shared_candidates("ns", [("a", [cm("1")]), ("b", [cm("1")])], [], reg)
    assert [d["metadata"]["name"] for d in shared] == ["common"]
    assert reg.promotions[0]["promoted_from"] == ["a", "b"]

    reg = conv.NamespaceRegistry("ns")
    assert conv.promote_shared_candidates("ns", [("a", [cm("1")]), ("b", [cm("2")])], [], reg) == []
    assert not reg.promotions


# D — cross-reference warnings
def test_cross_reference_checks():
    pod = dep("app", serviceAccountName="app-sa",
              volumes=[{"name": "v", "configMap": {"name": "missing-cm"}},
                       {"name": "ca", "configMap": {"name": "kube-root-ca.crt"}}])
    pod["spec"]["template"]["metadata"]["annotations"] = {conv._NAD_ANNOTATION: "macvlan-net"}
    warns = "\n".join(conv.cross_reference_checks(
        "ns", [pod, {"kind": "ServiceAccount", "metadata": {"name": "app-sa"}}], set()))
    assert "ConfigMap/missing-cm" in warns
    assert "kube-root-ca.crt" not in warns
    assert "no RoleBinding" in warns
    assert "macvlan-net" in warns


# E — NetworkAttachmentDefinition verbatim; F — PVC volumeName needs its PV
def test_nad_passes_through_and_is_recorded():
    c = ctx()
    nad = {"apiVersion": "k8s.cni.cncf.io/v1", "kind": "NetworkAttachmentDefinition", "metadata": {"name": "net"}}
    conv.handle_nad(nad, c)
    assert yaml.safe_load(c.extra_deploy[0]) == nad and c.nad_names == {"net"}


def test_pvc_volume_name_kept_only_when_pv_is_in_dump():
    doc = {"kind": "PersistentVolumeClaim", "metadata": {"name": "data"}, "spec": {"volumeName": "pv-1"}}
    c = ctx()
    conv.handle_pvc(doc, c, "data", {"pv-1"})
    assert c.values["pvc"]["data"]["volumeName"] == "pv-1"
    c = ctx()
    conv.handle_pvc(doc, c, "data", set())
    assert "volumeName" not in c.values["pvc"]["data"]
    assert any("[P0]" in w for w in c.warnings)


# G — redis lint; K — emptyDir sizeLimit
def test_redis_and_emptydir_lint():
    c = ctx()
    conv.handle_configmap({"kind": "ConfigMap", "metadata": {"name": "r"},
                           "data": {"redis.conf": "dir /tmp\n",
                                    "sentinel.conf": "sentinel down-after-milliseconds mymaster 1000\n"}}, c, "r")
    assert sum("[P1]" in w for w in c.warnings) == 2
    c = ctx()
    conv.remap_volumes({"scratch": {"emptyDir": {}}, "sized": {"emptyDir": {"sizeLimit": "1Gi"}}}, c)
    assert any("[P2]" in w and "scratch" in w for w in c.warnings)
    assert not any("sized" in w for w in c.warnings)


# H — PDB / ServiceMonitor verbatim
def test_pdb_and_servicemonitor_go_verbatim_to_extra_deploy():
    c = ctx()
    pdb = {"apiVersion": "policy/v1", "kind": "PodDisruptionBudget", "metadata": {"name": "app-pdb"},
           "spec": {"minAvailable": 1, "selector": {"matchLabels": {"app": "x"}}}}
    sm = {"apiVersion": "monitoring.coreos.com/v1", "kind": "ServiceMonitor", "metadata": {"name": "app-sm"},
          "spec": {"endpoints": [{"port": "a"}, {"port": "b"}]}}
    conv.handle_pdb(pdb, c)
    conv.handle_servicemonitor(sm, c)
    assert "pdb" not in c.values and "serviceMonitor" not in c.values
    assert [yaml.safe_load(e) for e in c.extra_deploy] == [pdb, sm]


# I — init container order
def test_init_container_order_is_recorded():
    c = ctx()
    conv.handle_workload(dep("app", initContainers=[{"name": "z-first", "image": "a:1"},
                                                    {"name": "a-second", "image": "b:1"}]), c, "Deployment")
    assert c.values["initContainerOrder"] == ["z-first", "a-second"]


# J — Service fields and nodePort-on-ClusterIP
def test_service_lb_fields_and_nodeport_on_clusterip_warning():
    svc = {"kind": "Service", "metadata": {"name": "s"},
           "spec": {"selector": {"a": "b"}, "loadBalancerIP": "1.2.3.4",
                    "loadBalancerSourceRanges": ["10.0.0.0/8"], "ports": [{"port": 80, "nodePort": 30080}]}}
    entry = conv.build_service_entry(svc)
    assert entry["loadBalancerIP"] == "1.2.3.4" and entry["loadBalancerSourceRanges"] == ["10.0.0.0/8"]
    c = ctx()
    conv.handle_service(svc, c, "s")
    assert any("[P0]" in w and "nodePort" in w for w in c.warnings)


# L — multi-line strings as literal blocks
def test_multiline_strings_are_literal_blocks():
    assert "key: |" in conv.dump_yaml({"key": "line1\nline2\n"})
    assert conv.dump_yaml({"n": "1e+09"}).strip() == "n: '1e+09'"


# N — CronJob name length counts the key only
def test_cronjob_name_length_counts_the_key_only():
    _, warns = conv.validate_resource_names("ns", "a" * 40, {"nameOverride": "a" * 40, "cronjobs": {"b" * 20: {}}})
    assert not warns
    _, warns = conv.validate_resource_names("ns", "x", {"cronjobs": {"b" * 53: {}}})
    assert warns


# O — hpa/vpa/pdb dropped without a workload
def test_hpa_dropped_without_workload():
    cluster_values = {"rbac": {"clusterRoles": {}, "clusterRoleBindings": {}}, "storageClasses": {},
                      "persistentVolumes": {}, "scc": {}, "clusterSecretStores": {}}
    hpa = {"apiVersion": "autoscaling/v2", "kind": "HorizontalPodAutoscaler", "metadata": {"name": "h"},
           "spec": {"minReplicas": 1, "maxReplicas": 2}}
    c = conv.build_microservice("ns", "cfg", [hpa], conv.NamespaceRegistry("ns"), conv.ClusterRegistry(),
                                cluster_values, [], owner_label="cfg")
    assert "hpa" not in c.values and c.values["workload"]["type"] == "none"
    assert any("hpa dropped" in w for w in c.warnings)
