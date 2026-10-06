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


def test_name_suffix_renames_argo_objects_only():
    appset = conv.build_root_applicationset("root-pr-42", "https://c", "ms-applicationSet", "main", ".",
                                            "https://v", "main", name_suffix="-pr-42")
    tmpl = appset["spec"]["template"]
    assert tmpl["metadata"]["name"] == "{{path.basename}}-pr-42"
    params = tmpl["spec"]["sources"][0]["helm"]["parameters"]
    assert {"name": "nameSuffix", "value": "pr-42"} in params
    assert {"name": "namespace", "value": "{{path.basename}}"} in params
    nested = conv.build_root_applicationset("r", "https://c", "ms-applicationSet", "main", ".",
                                            "https://v", "main", nested=True, name_suffix="pr-42")
    assert nested["spec"]["template"]["metadata"]["name"] == '{{ .path.path | replace "/" "-" }}-pr-42'
    app = conv.build_cluster_shared_application("platform", "https://c", ".", "main", "https://v", "main",
                                                name_suffix="pr-42")
    assert app["metadata"]["name"] == "cluster-shared-resources-pr-42"
    assert app["spec"]["destination"]["namespace"] == "platform"
    plain = conv.build_root_applicationset("r", "https://c", "ms-applicationSet", "main", ".", "https://v", "main")
    assert not any(p["name"] == "nameSuffix" for p in plain["spec"]["template"]["spec"]["sources"][0]["helm"]["parameters"])


@needs_helm
@pytest.mark.parametrize("suffix", ["pr-42", "-pr-42"])
def test_ms_applicationset_name_suffix(suffix):
    r = subprocess.run(["helm", "template", "x", str(CHART_DIR / "ms-applicationSet"),
                        "--set", "namespace=prd,folder=prd/yellow,originRepoURL=r,originBranch=m",
                        "--set", f"nameSuffix={suffix}"],
                       capture_output=True, text=True, check=True)
    doc = yaml.safe_load(r.stdout)
    tmpl = doc["spec"]["template"]
    assert doc["metadata"]["name"] == "prd-yellow-pr-42"
    assert tmpl["metadata"]["name"].endswith("}}-prd-yellow-pr-42")
    assert tmpl["metadata"]["labels"]["name-suffix"] == "pr-42"
    assert tmpl["spec"]["destination"]["namespace"] == "prd"
    assert "pr-42" not in tmpl["spec"]["sources"][0]["helm"]["releaseName"]


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
    # Substring: both web files. `common` is used by nobody and not named by
    # --include, so shared has nothing left to hold.
    assert _bases(out) == ["web", "web-worker"]
    assert "ConfigMap/common: used by no microservice" in (out / "report" / "conflicts_and_warnings.txt").read_text()


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


# --- an env token inside a microservice's own name ---------------------------

def test_token_stems_fold_only_when_another_stem_shares_the_name():
    tokens = {"dev", "prd", "project", "yellow", "black"}
    stems = {"project-api", "pay-dev", "pay-prd", "ms1-yellow", "ms1-black", "shared-prd", "web"}
    assert conv.token_exempt_stems(stems, tokens) == {"project-api"}
    assert conv.token_exempt_stems(stems, set()) == set()


def _two_namespaces(tmp_path, files):
    inp = tmp_path / "in"
    for ns in ("dev", "prd"):
        (inp / ns).mkdir(parents=True)
        for name in files(ns):
            (inp / ns / f"{name}.yaml").write_text(yaml.safe_dump(_deploy(name)))
    return inp


@pytest.mark.parametrize("flags", [["--env-suffixes", "dev,prd,project"], ["--env-group", "site=project,other"]])
def test_a_microservice_named_like_an_env_token_keeps_its_name(tmp_path, flags):
    """project-api.yaml in every namespace, with 'project' an env token: it is
    one microservice called project-api, not an 'api' in a 'project' variant."""
    inp = _two_namespaces(tmp_path, lambda ns: ["project-api", f"pay-{ns}"])
    out = tmp_path / "out"
    r = subprocess.run([sys.executable, str(CONVERTER), "--input", str(inp), "--output", str(out),
                        "--chart", str(CHART_DIR), "--skip-verify", *flags],
                       capture_output=True, text=True, cwd=GITOPS_DIR)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (out / "base" / "project-api.yaml").exists()
    assert (out / "dev" / "values" / "project-api.yaml").exists()
    assert not (out / "dev" / "project").exists()
    if "--env-suffixes" in flags:
        assert (out / "base" / "pay.yaml").exists()  # the real env copies still fold


# --- shared holds only what the selection still shares ------------------------

def _uses(name, cms=(), sa=None, labels=None):
    d = _deploy(name)
    pod = d["spec"]["template"]["spec"]
    pod["containers"][0]["envFrom"] = [{"configMapRef": {"name": c}} for c in cms]
    if sa:
        pod["serviceAccountName"] = sa
    if labels:
        d["metadata"]["labels"] = labels
    return d


def _cm(name, labels=None):
    return {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": name, **({"labels": labels} if labels else {})},
            "data": {"a": "1"}}


def _selection(tmp_path, *flags):
    sa = {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": "billing-sa"}}
    role = {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role", "metadata": {"name": "billing-role"}, "rules": []}
    rb = {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding", "metadata": {"name": "billing-rb"},
          "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "billing-role"},
          "subjects": [{"kind": "ServiceAccount", "name": "billing-sa"}]}
    svc = {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "worker-svc"},
           "spec": {"selector": {"app": "worker"}, "ports": [{"name": "http", "port": 80}]}}
    ing = {"apiVersion": "networking.k8s.io/v1", "kind": "Ingress", "metadata": {"name": "worker-ing"},
           "spec": {"rules": [{"host": "w", "http": {"paths": [{"path": "/", "pathType": "Prefix", "backend": {
               "service": {"name": "worker-svc", "port": {"name": "http"}}}}]}}]}}
    files = {
        "web": [_uses("web", cms=["cm-ab", "cm-ac"])],
        "worker": [_uses("worker", cms=["cm-ab"])],
        "billing": [_uses("billing", cms=["cm-ac"], sa="billing-sa", labels={"team": "billing"})],
        "shared": [_cm("cm-ab"), _cm("cm-ac"), sa, role, rb, svc, ing,
                   _cm("loose-billing", {"team": "billing"}), _cm("loose-other")],
    }
    r, out = _convert(tmp_path, files, "app", "--skip-verify", *flags)
    assert r.returncode == 0, r.stdout + r.stderr
    load = lambda rel: yaml.safe_load((out / "base" / f"{rel}.yaml").read_text()) if (out / "base" / f"{rel}.yaml").exists() else None
    return load, (out / "report" / "conflicts_and_warnings.txt").read_text()


def test_include_keeps_shared_for_what_two_picked_microservices_share(tmp_path):
    load, report = _selection(tmp_path, "--include", "=web,=worker")
    shared, web, worker = load("shared"), load("web"), load("worker")
    assert list(shared["configMaps"]) == ["cm-ab"]               # web + worker
    assert "cm-ac" in web["configMaps"]                          # billing left: web alone uses it
    assert worker["service"]["name"] == "worker-svc"             # selects only worker's pods: it is worker's now
    assert worker["ingress"]["enabled"]                          # routes to worker's Service
    assert "rbac" not in shared and "serviceAccount" not in {k for k in shared if shared[k] and k == "rbac"}
    assert "billing-role" in report and "left out of this run" in report
    assert "loose-other" in report and "loose-billing" in report  # nobody uses them, not included


def test_exclude_trims_shared_the_same_way(tmp_path):
    load, _ = _selection(tmp_path, "--exclude", "=billing")
    shared = load("shared")
    # Nobody uses the two loose ones, and `=billing` excludes neither by name.
    assert sorted(shared["configMaps"]) == ["cm-ab", "loose-billing", "loose-other"]
    assert "cm-ac" in load("web")["configMaps"]


def test_a_label_pattern_selects_by_key_and_value(tmp_path):
    load, _ = _selection(tmp_path, "--include", "team=billing")
    billing, shared = load("billing"), load("shared")
    assert load("web") is None
    # billing alone now: everything it uses comes home, and the included loose one stays shared.
    assert "cm-ac" in billing["configMaps"]
    assert billing["rbac"]["roleBindings"]["billing-rb"]
    assert list((shared or {}).get("configMaps", {})) == ["loose-billing"]


def test_label_patterns_need_the_exact_label():
    doc = {"metadata": {"name": "x", "labels": {"team": "billing-ops"}}}
    assert not conv.matches_patterns(doc, ["team=billing"])
    assert conv.matches_patterns(doc, ["team=billing-ops"])
    assert not conv.matches_patterns(doc, ["owner=billing-ops"])
