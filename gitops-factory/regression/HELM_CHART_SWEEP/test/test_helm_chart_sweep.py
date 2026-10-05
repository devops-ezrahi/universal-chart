"""
Regression tests for HELM_CHART_SWEEP — 57 public Helm charts (Bitnami, ingress-nginx,
cert-manager, Argo, Traefik, Vault, Harbor, …) rendered with `helm template`, split
by namespace_importer and converted. See ../STATUS.md for each defect.

Each test converts a small fixture shaped like the chart that exposed the defect and
asserts on what the universal chart RENDERS, which is what reaches the cluster.
"""
import importlib.util
import pathlib
import subprocess
import sys

import pytest
import yaml

TEST_DIR = pathlib.Path(__file__).resolve().parent
GITOPS_DIR = TEST_DIR.parents[2]
CHART_DIR = GITOPS_DIR.parent
CONVERTER = GITOPS_DIR / "convert_to_universal_chart.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


ni = _load("namespace_importer_sweep", GITOPS_DIR / "namespace_importer.py")
conv = _load("convert_sweep", CONVERTER)

needs_helm = pytest.mark.skipif(subprocess.run(["which", "helm"], capture_output=True).returncode != 0,
                                reason="helm not installed")


def _convert(tmp_path, files: dict, ns="app", *flags):
    inp, out = tmp_path / "in", tmp_path / "out"
    for name, docs in files.items():
        (inp / ns).mkdir(parents=True, exist_ok=True)
        (inp / ns / f"{name}.yaml").write_text(yaml.safe_dump_all(docs, sort_keys=False))
    r = subprocess.run([sys.executable, str(CONVERTER), "--input", str(inp), "--output", str(out),
                        "--chart", str(CHART_DIR), "--verify-fidelity", *flags],
                       capture_output=True, text=True, cwd=GITOPS_DIR)
    return r, out


def _render(out, release, ns="app"):
    files = [out / "base" / f"{release}.yaml", out / ns / "defaults.yaml", out / ns / "values" / f"{release}.yaml"]
    args = ["helm", "template", release, str(CHART_DIR), "--namespace", ns]
    for f in files:
        if f.exists():
            args += ["-f", str(f)]
    r = subprocess.run(args, capture_output=True, text=True, check=True)
    return [d for d in yaml.safe_load_all(r.stdout) if d]


def _sts(name, labels, sa=None, pod_extra=None):
    pod = {"containers": [{"name": name, "image": f"reg/{name}:1"}], **(pod_extra or {})}
    if sa:
        pod["serviceAccountName"] = sa
    return {"apiVersion": "apps/v1", "kind": "StatefulSet", "metadata": {"name": name},
            "spec": {"serviceName": "db-headless", "selector": {"matchLabels": labels},
                     "template": {"metadata": {"labels": labels}, "spec": pod}}}


def _fidelity(out):
    return (out / "report" / "fidelity_report.txt").read_text()


# --- the converter ----------------------------------------------------------

@needs_helm
def test_pod_keeps_its_token_when_the_serviceaccount_turns_its_own_off(tmp_path):
    """Bitnami/Argo/Traefik: SA `false`, pod `true`. One chart value drove both,
    and the controller lost its API token."""
    labels = {"app": "ctl"}
    sts = _sts("ctl", labels, sa="ctl", pod_extra={"automountServiceAccountToken": True})
    sa = {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": "ctl"},
          "automountServiceAccountToken": False}
    r, out = _convert(tmp_path, {"ctl": [sa, sts]})
    assert r.returncode == 0, r.stdout + r.stderr
    docs = {d["kind"]: d for d in _render(out, "ctl")}
    assert docs["StatefulSet"]["spec"]["template"]["spec"]["automountServiceAccountToken"] is True
    assert docs["ServiceAccount"]["automountServiceAccountToken"] is False


@needs_helm
def test_a_shared_headless_service_still_selects_both_statefulsets(tmp_path):
    """redis-headless in `shared`, selecting master and replicas by the source
    chart's labels — which the universal chart rewrites per release."""
    labels = {"app.kubernetes.io/name": "db", "app.kubernetes.io/instance": "db"}
    master = _sts("db-master", {**labels, "role": "master"})
    replicas = _sts("db-replicas", {**labels, "role": "replica"})
    headless = {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "db-headless"},
                "spec": {"clusterIP": "None", "selector": labels, "ports": [{"name": "tcp", "port": 6379}]}}
    r, out = _convert(tmp_path, {"db-master": [master], "db-replicas": [replicas], "shared": [headless]})
    assert r.returncode == 0, r.stdout + r.stderr
    svc = next(d for d in _render(out, "shared") if d["kind"] == "Service")
    pods = [d["spec"]["template"]["metadata"]["labels"]
            for rel in ("db-master", "db-replicas") for d in _render(out, rel) if d["kind"] == "StatefulSet"]
    sel = svc["spec"]["selector"]
    assert len(pods) == 2 and all(all(p.get(k) == v for k, v in sel.items()) for p in pods), (sel, pods)
    assert "selects no rendered pod" not in _fidelity(out)


@needs_helm
def test_a_podless_shared_release_creates_no_serviceaccount(tmp_path):
    """Kafka: `shared` pinned to fullnameOverride 'kafka' rendered a stray
    ServiceAccount/kafka next to the broker's own."""
    cm = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "cfg"}, "data": {"a": "1"}}
    sts = _sts("web", {"app": "web"})
    r, out = _convert(tmp_path, {"web": [sts], "shared": [cm]})
    assert r.returncode == 0, r.stdout + r.stderr
    assert not [d for d in _render(out, "shared") if d["kind"] == "ServiceAccount"]


@needs_helm
def test_an_ingress_keeps_each_paths_backend_and_numbered_port(tmp_path):
    """Harbor: / to the portal, /api to core, both by port number — rendered
    nothing at all (`portName: null`)."""
    def path(p, svc):
        return {"path": p, "pathType": "Prefix", "backend": {"service": {"name": svc, "port": {"number": 80}}}}
    ing = {"apiVersion": "networking.k8s.io/v1", "kind": "Ingress", "metadata": {"name": "harbor"},
           "spec": {"rules": [{"host": "h", "http": {"paths": [path("/api/", "core"), path("/", "portal")]}}]}}
    r, out = _convert(tmp_path, {"shared": [ing]})
    assert r.returncode == 0, r.stdout + r.stderr
    rendered = next(d for d in _render(out, "shared") if d["kind"] == "Ingress")
    backends = [p["backend"]["service"] for p in rendered["spec"]["rules"][0]["http"]["paths"]]
    assert backends == [{"name": "core", "port": {"number": 80}}, {"name": "portal", "port": {"number": 80}}]


@needs_helm
def test_a_job_keeps_its_pod_level_fields(tmp_path):
    """ingress-nginx admission hooks and cert-manager's startupapicheck."""
    job = {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": "hook"},
           "spec": {"ttlSecondsAfterFinished": 0, "template": {"spec": {
               "restartPolicy": "OnFailure", "automountServiceAccountToken": True,
               "enableServiceLinks": False, "nodeSelector": {"kubernetes.io/os": "linux"},
               "priorityClassName": "system-cluster-critical",
               "containers": [{"name": "c", "image": "reg/c:1",
                               "livenessProbe": {"httpGet": {"path": "/h", "port": 1}}}]}}}}
    r, out = _convert(tmp_path, {"hook": [job]})
    assert r.returncode == 0, r.stdout + r.stderr
    rendered = next(d for d in _render(out, "hook") if d["kind"] == "Job")
    pod = rendered["spec"]["template"]["spec"]
    assert rendered["spec"]["ttlSecondsAfterFinished"] == 0
    assert pod["automountServiceAccountToken"] is True and pod["enableServiceLinks"] is False
    assert pod["nodeSelector"] == {"kubernetes.io/os": "linux"}
    assert pod["priorityClassName"] == "system-cluster-critical"
    assert pod["containers"][0]["livenessProbe"]["httpGet"]["path"] == "/h"


@needs_helm
def test_envfrom_keeps_a_configmap_and_secret_of_one_name_in_order(tmp_path):
    """Harbor's harbor-core: a ConfigMap and a Secret named alike, raw order kept."""
    sts = _sts("core", {"app": "core"}, pod_extra={})
    sts["spec"]["template"]["spec"]["containers"][0]["envFrom"] = [
        {"secretRef": {"name": "zz"}}, {"configMapRef": {"name": "core"}},
        {"secretRef": {"name": "core"}, "prefix": "S_"}]
    r, out = _convert(tmp_path, {"core": [sts]})
    assert r.returncode == 0, r.stdout + r.stderr
    c = next(d for d in _render(out, "core") if d["kind"] == "StatefulSet")["spec"]["template"]["spec"]["containers"][0]
    assert c["envFrom"] == [{"secretRef": {"name": "zz"}}, {"configMapRef": {"name": "core"}},
                            {"secretRef": {"name": "core"}, "prefix": "S_"}]


@needs_helm
def test_rbac_keeps_foreign_subject_namespaces_and_aggregation_labels(tmp_path):
    rb = {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding", "metadata": {"name": "rb"},
          "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "r"},
          "subjects": [{"kind": "ServiceAccount", "name": "a", "namespace": "app"},
                       {"kind": "ServiceAccount", "name": "b", "namespace": "default"}]}
    cr = {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRole",
          "metadata": {"name": "view-x", "labels": {"rbac.authorization.k8s.io/aggregate-to-view": "true"}},
          "rules": []}
    r, out = _convert(tmp_path, {"shared": [rb, cr]})
    assert r.returncode == 0, r.stdout + r.stderr
    docs = {d["kind"]: d for d in _render(out, "shared")}
    assert [s.get("namespace") for s in docs["RoleBinding"]["subjects"]] == [None, "default"]
    assert docs["ClusterRole"]["metadata"]["labels"]["rbac.authorization.k8s.io/aggregate-to-view"] == "true"


def test_fidelity_reports_reordered_init_containers():
    raw = [{"name": "wait-db"}, {"name": "migrate"}]
    rendered = [{"name": "migrate"}, {"name": "wait-db"}]
    diffs = conv._fidelity_diff("Deployment", raw, rendered, ("spec", "template", "spec", "initContainers"))
    assert any(sev == "CHANGED" for sev, _, _ in diffs)


def test_fidelity_treats_null_and_api_defaults_as_absent():
    raw = {"envFrom": None, "hostNetwork": False, "priorityClassName": "", "enableServiceLinks": False}
    diffs = conv._fidelity_diff("Deployment", raw, {}, ("spec", "template", "spec"))
    assert [p for _, p, _ in diffs] == ["spec.template.spec.enableServiceLinks"]


# --- the importer -----------------------------------------------------------

def test_a_trailing_tab_after_a_scalar_parses():
    """prometheus-community's alertmanager renders `- apiVersion: v1<TAB>`."""
    text = "kind: List\nitems:\n- apiVersion: v1\t\n  kind: ConfigMap\n  metadata: {name: a}\n"
    assert [d["metadata"]["name"] for d in ni.parse_yaml_stream(text)] == ["a"]


def test_two_kinds_of_one_name_get_two_keys():
    """fluentd: DaemonSet and StatefulSet both 'fluentd'; the second replaced the first."""
    ds = {"kind": "DaemonSet", "metadata": {"name": "fluentd"}}
    sts = {"kind": "StatefulSet", "metadata": {"name": "fluentd"}}
    keys = ni.workload_keys([ds, sts])
    assert sorted(keys.values()) == ["fluentd", "fluentd-statefulset"]


def test_file_unclaimed_gives_every_pasted_object_a_home(tmp_path):
    ns = tmp_path / "app"
    ns.mkdir()
    (ns / "web.yaml").write_text(yaml.safe_dump({"kind": "Deployment", "metadata": {"name": "web"}}))
    docs = [{"kind": "Deployment", "metadata": {"name": "web"}},
            {"kind": "ConfigMap", "metadata": {"name": "argocd-cm"}},
            {"kind": "Role", "metadata": {"name": "r"}},
            {"kind": "StatefulSet", "metadata": {"name": "orphan"}},
            {"kind": "NetworkPolicy", "metadata": {"name": "np"}},
            {"kind": "Pod", "metadata": {"name": "t", "annotations": {"helm.sh/hook": "test"}}}]
    ni.file_unclaimed(ns, docs)
    shared = [(d["kind"], d["metadata"]["name"]) for d in yaml.safe_load_all((ns / "shared.yaml").read_text())]
    assert shared == [("ConfigMap", "argocd-cm"), ("Role", "r")]
    assert (ns / "orphan.yaml").exists()


# --- Argo CD wiring ---------------------------------------------------------

def test_generated_wiring_leaves_sync_manual_by_default():
    appset = conv.build_root_applicationset("root", "https://c", "ms-applicationSet", "main", ".",
                                            "https://v", "main")
    params = appset["spec"]["template"]["spec"]["sources"][0]["helm"]["parameters"]
    assert {"name": "autoSync", "value": "false"} in params
    app = conv.build_cluster_shared_application("platform", "https://c", ".", "main", "https://v", "main")
    assert "automated" not in app["spec"]["syncPolicy"]


def test_auto_sync_true_turns_it_back_on():
    appset = conv.build_root_applicationset("root", "https://c", "ms-applicationSet", "main", ".",
                                            "https://v", "main", auto_sync=True)
    params = appset["spec"]["template"]["spec"]["sources"][0]["helm"]["parameters"]
    assert {"name": "autoSync", "value": "true"} in params


@needs_helm
@pytest.mark.parametrize("flag,automated", [([], True), (["--set", "autoSync=false"], False),
                                            (["--set-string", "autoSync=false"], False)])
def test_ms_applicationset_autosync(flag, automated):
    r = subprocess.run(["helm", "template", "x", str(CHART_DIR / "ms-applicationSet"),
                        "--set", "namespace=a,originRepoURL=r,originBranch=m", *flag],
                       capture_output=True, text=True, check=True)
    doc = yaml.safe_load(r.stdout)
    assert ("automated" in doc["spec"]["template"]["spec"]["syncPolicy"]) is automated


# --- --include and exact `=name` patterns ------------------------------------

def _deploy(name):
    return {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": name},
            "spec": {"selector": {"matchLabels": {"app": name}},
                     "template": {"metadata": {"labels": {"app": name}},
                                  "spec": {"containers": [{"name": name, "image": f"reg/{name}:1"}]}}}}


def _bases(out):
    return sorted(p.stem for p in (out / "base").glob("*.yaml"))


def test_include_converts_only_the_matching_microservices(tmp_path):
    cm = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "common"}, "data": {"a": "1"}}
    r, out = _convert(tmp_path, {"web": [_deploy("web")], "web-worker": [_deploy("web-worker")],
                                 "billing": [_deploy("billing")], "shared": [cm]},
                      "app", "--skip-verify", "--include", "web")
    assert r.returncode == 0, r.stdout + r.stderr
    # Substring: both web files; shared.yaml is never filtered by --include.
    assert _bases(out) == ["shared", "web", "web-worker"]


def test_an_exact_pattern_matches_one_name_and_exclude_still_wins(tmp_path):
    files = {"web": [_deploy("web")], "web-worker": [_deploy("web-worker")], "billing": [_deploy("billing")]}
    r, out = _convert(tmp_path, files, "app", "--skip-verify", "--include", "=web,=billing", "--exclude", "=billing")
    assert r.returncode == 0, r.stdout + r.stderr
    assert _bases(out) == ["web"]


def test_matches_patterns_exact_is_by_name_only():
    doc = {"metadata": {"name": "web-worker", "labels": {"app": "web"}}}
    assert conv.matches_patterns(doc, ["web"])
    assert not conv.matches_patterns(doc, ["=web"])
    assert conv.matches_patterns(doc, ["=web-worker"])
