#!/usr/bin/env python3
"""End-to-end cutover on a real Argo CD: old app tree out, universal chart in,
no pod restarted and nothing deleted.

    old world   an app-of-apps -> an ApplicationSet -> one Application per
                microservice, from raw manifests shaped like the live objects
                that prompted this (interleaved env with secretKeyRefs, a
                selector the release name does not produce, no ServiceAccount,
                `securityContext: {}`, a Route without
                insecureEdgeTerminationPolicy, a StatefulSet with a
                volumeClaimTemplate), then `kubectl rollout restart` once
    convert     the live objects, dumped, through convert_to_universal_chart.py
                at --chart-ref
    teardown    scripts/argo-orphan-tree.sh: every old Application and
                ApplicationSet removed, every workload kept
    adopt       ms-applicationSet's ApplicationSet at --chart-ref, first with
                auto-sync off: `argocd app diff` may show only Argo's own
                tracking label; then synced
    verdict     pod UIDs, ReplicaSets and StatefulSet revisions before vs after

--expect no-restart: every check must hold (the fixed chart).
--expect restart:    the negative control - the run must SEE a restart, a
                     template/selector diff or a failed sync, proving the test
                     can tell the difference.

Needs: kubectl (KUBECONFIG) with Argo CD in `argocd`, the argocd CLI, helm,
git, python3 + PyYAML, and githttp.py serving --git-root on --git-port.
"""
import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent  # the universal-chart checkout
ORPHAN = HERE.parent / "scripts" / "argo-orphan-tree.sh"
INSTANCE = "argocd.argoproj.io/instance"


def sh(*cmd, check=True, input=None, quiet=False, cwd=None):
    p = subprocess.run(cmd, capture_output=True, text=True, input=input, cwd=cwd)
    if check and p.returncode:
        sys.exit(f"FAILED: {' '.join(map(str, cmd))}\n{p.stdout[-2000:]}\n{p.stderr[-2000:]}")
    if not quiet and p.returncode and not check:
        pass
    return p


def kubectl(*args, **kw):
    return sh("kubectl", *args, **kw)


def kjson(*args):
    return json.loads(kubectl(*args, "-o", "json").stdout)


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------- old world

def old_manifests(ns):
    """The live objects that prompted this, as raw manifests."""
    sel = {"app": "filter-service-black-b2b-tst"}
    filter_service = [
        {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "filter-secret", "namespace": ns},
         "type": "Opaque", "stringData": {"db-password": "hunter2", "api-key": "k-123"}},
        {"apiVersion": "apps/v1", "kind": "Deployment",
         "metadata": {"name": "filter-service", "namespace": ns,
                      "labels": {**sel, "app.kubernetes.io/part-of": "b2b", "team": "filters"}},
         "spec": {"replicas": 2, "selector": {"matchLabels": sel},
                  "template": {"metadata": {"labels": {**sel, "app.openshift.io/runtime": "nodejs", "version": "v1"},
                                            "annotations": {"opentelemetry.io/enabled": "true"}},
                               "spec": {"containers": [{
                                   "name": "filter-service", "image": "nginx:1.27-alpine",
                                   "imagePullPolicy": "IfNotPresent",
                                   "ports": [{"name": "http", "containerPort": 80, "protocol": "TCP"}],
                                   # interleaved plain / secretKeyRef, NOT sorted: order is part of the template
                                   "env": [
                                       {"name": "ZZ_HOST", "value": "db.filters.svc"},
                                       {"name": "DB_PASSWORD", "valueFrom": {"secretKeyRef": {"name": "filter-secret", "key": "db-password"}}},
                                       {"name": "MODE", "value": "b2b"},
                                       {"name": "API_KEY", "valueFrom": {"secretKeyRef": {"name": "filter-secret", "key": "api-key"}}},
                                       {"name": "A_URL", "value": "postgres://$(ZZ_HOST):5432/filters"},
                                   ],
                                   "securityContext": {},
                               }]}}}},
        {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "filter-service", "namespace": ns, "labels": dict(sel)},
         "spec": {"selector": dict(sel), "ports": [{"name": "http", "port": 8080, "targetPort": "http", "protocol": "TCP"}]}},
        {"apiVersion": "route.openshift.io/v1", "kind": "Route", "metadata": {"name": "filter-service", "namespace": ns, "labels": dict(sel)},
         "spec": {"host": f"filter-service-{ns}.apps.example.com", "to": {"kind": "Service", "name": "filter-service", "weight": 100},
                  "port": {"targetPort": "http"}, "tls": {"termination": "edge"}, "wildcardPolicy": "None"}},
    ]
    rsel = {"app": "redis-black-b2b-tst"}
    redis = [
        {"apiVersion": "apps/v1", "kind": "StatefulSet",
         "metadata": {"name": "redis", "namespace": ns, "labels": {**rsel, "app.kubernetes.io/component": "cache"}},
         "spec": {"serviceName": "redis", "replicas": 1, "selector": {"matchLabels": rsel},
                  "template": {"metadata": {"labels": {**rsel, "app.kubernetes.io/component": "cache"}},
                               "spec": {"containers": [{
                                   "name": "redis", "image": "redis:7.2-alpine", "imagePullPolicy": "IfNotPresent",
                                   "ports": [{"name": "redis", "containerPort": 6379, "protocol": "TCP"}],
                                   "securityContext": {},
                                   "volumeMounts": [{"name": "data", "mountPath": "/data"}]}]}},
                  "volumeClaimTemplates": [{"metadata": {"name": "data"},
                                            "spec": {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": "1Gi"}}}}]}},
        {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "redis", "namespace": ns, "labels": dict(rsel)},
         "spec": {"selector": dict(rsel), "ports": [{"name": "redis", "port": 6379, "targetPort": 6379, "protocol": "TCP"}]}},
    ]
    return {"filter-service": filter_service, "redis": redis}


def git_push_dir(src: Path, bare: Path, branch="main", msg="e2e"):
    """Commit `src` as the whole content of `branch` in `bare`."""
    if not bare.exists():
        sh("git", "init", "-q", "--bare", "-b", "main", str(bare))
    work = src.parent / f".push-{src.name}"
    shutil.rmtree(work, ignore_errors=True)
    shutil.copytree(src, work)
    sh("git", "init", "-q", "-b", branch, cwd=work)
    sh("git", "add", "-A", cwd=work)
    sh("git", "-c", "user.email=e2e@local", "-c", "user.name=e2e", "commit", "-qm", msg, cwd=work)
    sh("git", "push", "-q", "--force", str(bare), f"HEAD:{branch}", cwd=work)
    shutil.rmtree(work)


def wait(desc, fn, timeout=300, every=3):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        ok, last = fn()
        if ok:
            return last
        time.sleep(every)
    sys.exit(f"TIMEOUT waiting for {desc}: {last}")


def apps_status(prefix):
    items = kjson("-n", "argocd", "get", "applications.argoproj.io")["items"]
    return {a["metadata"]["name"]: (a.get("status", {}).get("sync", {}).get("status"),
                                     a.get("status", {}).get("health", {}).get("status"),
                                     (a.get("status", {}).get("operationState") or {}).get("phase"))
            for a in items if a["metadata"]["name"].startswith(prefix)}


def admit_routes(ns):
    """Play the OpenShift router: there is none on this cluster, and Argo CD
    reports a Route nobody admitted as Progressing forever. Status only - the
    spec Argo compares is untouched."""
    for r in kjson("-n", ns, "get", "routes.route.openshift.io")["items"]:
        if (r.get("status") or {}).get("ingress"):
            continue
        status = {"status": {"ingress": [{"host": r["spec"].get("host", ""), "routerName": "default",
                                          "conditions": [{"type": "Admitted", "status": "True"}]}]}}
        kubectl("-n", ns, "patch", "routes.route.openshift.io", r["metadata"]["name"], "--subresource=status",
                "--type=merge", "-p", json.dumps(status), check=False)


# ---------------------------------------------------------------- snapshots

def snapshot(ns):
    pods = {p["metadata"]["name"]: p["metadata"]["uid"] for p in kjson("-n", ns, "get", "pods")["items"]}
    rs = sorted(r["metadata"]["name"] for r in kjson("-n", ns, "get", "replicasets")["items"])
    sts = {s["metadata"]["name"]: (s["status"].get("currentRevision"), s["status"].get("updateRevision"))
           for s in kjson("-n", ns, "get", "statefulsets")["items"]}
    objs = {}
    for kind in ("deployments", "statefulsets", "services", "secrets", "routes.route.openshift.io", "persistentvolumeclaims"):
        for o in kjson("-n", ns, "get", kind)["items"]:
            if o["metadata"]["name"].startswith("default-token") or o["metadata"]["name"] == "kube-root-ca.crt":
                continue
            objs[f"{o['kind']}/{o['metadata']['name']}"] = o["metadata"]["uid"]
    return {"pods": pods, "rs": rs, "sts": sts, "objs": objs}


def compare(before, after, label):
    problems = []
    if before["pods"] != after["pods"]:
        problems.append(f"pods changed: {sorted(before['pods'])} -> {sorted(after['pods'])}")
    if before["rs"] != after["rs"]:
        problems.append(f"ReplicaSets changed: {before['rs']} -> {after['rs']}")
    if before["sts"] != after["sts"]:
        problems.append(f"StatefulSet revisions changed: {before['sts']} -> {after['sts']}")
    for k, uid in before["objs"].items():
        if after["objs"].get(k) != uid:
            problems.append(f"{k}: {'deleted' if k not in after['objs'] else 'recreated'}")
    print(f"\n---- {label}")
    print(f"  {'pods':<14} {len(before['pods'])} -> {len(after['pods'])}  same UIDs: {before['pods'] == after['pods']}")
    print(f"  {'ReplicaSets':<14} {before['rs']} -> {after['rs']}")
    print(f"  {'StatefulSets':<14} {before['sts']} -> {after['sts']}")
    print(f"  {'objects':<14} {len(before['objs'])} kept with the same UID: "
          f"{sum(after['objs'].get(k) == v for k, v in before['objs'].items())}/{len(before['objs'])}")
    for p in problems:
        print(f"  !! {p}")
    return problems


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ns", default="cutover")
    ap.add_argument("--chart-ref", default="HEAD", help="universal-chart commit whose chart + converter adopt the workloads")
    ap.add_argument("--expect", choices=["no-restart", "restart"], default="no-restart")
    ap.add_argument("--work", default="")
    ap.add_argument("--git-root", required=True, help="directory githttp.py serves")
    ap.add_argument("--git-url", required=True, help="how the cluster reaches githttp.py, e.g. http://192.0.2.2:8765")
    a = ap.parse_args()

    ns, git = a.ns, a.git_url.rstrip("/")
    work = Path(a.work or f"/tmp/argo-cutover-e2e/{ns}")
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    gitroot = Path(a.git_root)
    gitroot.mkdir(parents=True, exist_ok=True)
    chart_ref = sh("git", "rev-parse", a.chart_ref, cwd=REPO).stdout.strip()
    branch = f"e2e-{ns}"
    log(f"namespace {ns}, chart + converter at {chart_ref[:10]} ({a.chart_ref}), expecting {a.expect}")

    # The chart repo Argo renders from, and a worktree of it for the converter.
    chart_bare = gitroot / "chart.git"
    if not chart_bare.exists():
        sh("git", "init", "-q", "--bare", "-b", "main", str(chart_bare))
    sh("git", "push", "-q", "--force", str(chart_bare), f"{chart_ref}:refs/heads/{branch}", cwd=REPO)
    wt = work / "chart"
    sh("git", "worktree", "add", "-q", "--detach", str(wt), chart_ref, cwd=REPO)
    try:
        return run(a, ns, git, work, gitroot, wt, branch)
    finally:
        sh("git", "worktree", "remove", "--force", str(wt), cwd=REPO, check=False)


def clean(ns):
    """Leftovers of an earlier run of this namespace: its ApplicationSets, their
    Applications (with their resources) and the namespace itself."""
    for kind in ("applicationsets.argoproj.io", "applications.argoproj.io"):
        for o in kjson("-n", "argocd", "get", kind)["items"]:
            n = o["metadata"]["name"]
            if n in (ns, f"old-appset-{ns}", f"old-root-{ns}") or n.startswith("old-") and n.endswith(f"-{ns}") \
                    or (kind.startswith("applications.") and n.endswith(f"-{ns.split('-')[-1]}") and not n.startswith("old-")):
                kubectl("-n", "argocd", "delete", kind, n, "--wait=false", check=False)
    kubectl("delete", "ns", ns, "--ignore-not-found", "--timeout=180s", check=False)
    wait(f"namespace {ns} to be gone", lambda: (kubectl("get", "ns", ns, check=False).returncode != 0, ns), 240)


def run(a, ns, git, work, gitroot, wt, branch):
    clean(ns)
    # ---------------------------------------------------------------- old world
    old = work / "old"
    for ms, docs in old_manifests(ns).items():
        d = old / ms
        d.mkdir(parents=True)
        (d / "manifests.yaml").write_text(yaml.safe_dump_all(docs, sort_keys=False))
    appset = {
        "apiVersion": "argoproj.io/v1alpha1", "kind": "ApplicationSet",
        "metadata": {"name": f"old-appset-{ns}", "namespace": "argocd"},
        "spec": {"goTemplate": True,
                 "generators": [{"git": {"repoURL": f"{git}/old-{ns}.git", "revision": "main",
                                         "directories": [{"path": "*"}, {"path": "_root", "exclude": True}]}}],
                 "template": {"metadata": {"name": f"old-{{{{ .path.basename }}}}-{ns}",
                                           "finalizers": ["resources-finalizer.argocd.argoproj.io"]},
                              "spec": {"project": "default",
                                       "source": {"repoURL": f"{git}/old-{ns}.git", "targetRevision": "main", "path": "{{ .path.path }}"},
                                       "destination": {"server": "https://kubernetes.default.svc", "namespace": ns},
                                       "syncPolicy": {"automated": {"prune": True}, "syncOptions": ["CreateNamespace=true"]}}}}}
    (old / "_root").mkdir()
    (old / "_root" / "appset.yaml").write_text(yaml.safe_dump(appset, sort_keys=False))
    git_push_dir(old, gitroot / f"old-{ns}.git", msg="old world")
    root = {"apiVersion": "argoproj.io/v1alpha1", "kind": "Application",
            "metadata": {"name": f"old-root-{ns}", "namespace": "argocd", "finalizers": ["resources-finalizer.argocd.argoproj.io"]},
            "spec": {"project": "default",
                     "source": {"repoURL": f"{git}/old-{ns}.git", "targetRevision": "main", "path": "_root"},
                     "destination": {"server": "https://kubernetes.default.svc", "namespace": "argocd"},
                     "syncPolicy": {"automated": {"prune": True}}}}
    kubectl("apply", "-f", "-", input=yaml.safe_dump(root))
    log("old world: root app-of-apps applied; waiting for its tree to sync")

    def old_ready():
        admit_routes(ns)
        st = apps_status("old-")
        mine = {k: v for k, v in st.items() if k.endswith(ns)}
        ok = len(mine) == 3 and all(v[0] == "Synced" and v[1] == "Healthy" for v in mine.values())
        return ok, mine
    wait("the old tree to be Synced/Healthy", old_ready, timeout=420)
    kubectl("-n", ns, "rollout", "status", "deploy/filter-service", "--timeout=180s")
    kubectl("-n", ns, "rollout", "status", "sts/redis", "--timeout=180s")
    # A manual restart, as on the live cluster: restartedAt is now in the template.
    kubectl("-n", ns, "rollout", "restart", "deploy/filter-service")
    kubectl("-n", ns, "rollout", "status", "deploy/filter-service", "--timeout=180s")
    time.sleep(5)
    log("old world up: " + json.dumps(apps_status("old-")))

    # ---------------------------------------------------------------- convert
    inp = work / "in" / ns
    inp.mkdir(parents=True)
    for ms in ("filter-service", "redis"):
        app = f"old-{ms}-{ns}"
        docs = []
        for kind in ("deployments", "statefulsets", "services", "secrets", "routes.route.openshift.io"):
            docs += kjson("-n", ns, "get", kind, "-l", f"{INSTANCE}={app}")["items"]
        (inp / f"{ms}.yaml").write_text(yaml.safe_dump_all(docs, sort_keys=False))
    out = work / "out"
    conv = wt / "gitops-factory" / "convert_to_universal_chart.py"
    args = ["python3", str(conv), "--input", str(work / "in"), "--output", str(out), "--chart", str(wt),
            "--argocd-manifests", "true", "--values-repo-url", f"{git}/new-{ns}.git", "--values-revision", "main",
            "--chart-repo-url", f"{git}/chart.git", "--chart-revision", branch, "--root-set-name", f"new-{ns}"]
    p = sh(*args, "--verify-fidelity", check=False)
    if p.returncode:
        log("converter rejected --verify-fidelity at this revision; converting without it")
        p = sh(*args)
    fid = out / "report" / "fidelity_report.txt"
    if fid.exists():
        log("converter fidelity: " + next((l.strip() for l in fid.read_text().splitlines() if "fields LOST" in l), "?"))
    git_push_dir(out, gitroot / f"new-{ns}.git", msg="converted")
    log(f"converted {sorted(x.name for x in (out / ns / 'values').glob('*.yaml'))} -> new-{ns}.git")

    # ---------------------------------------------------------------- baseline
    base = snapshot(ns)
    log(f"baseline: {len(base['pods'])} pods, RS {base['rs']}, STS {base['sts']}")

    # ---------------------------------------------------------------- teardown
    env = dict(os.environ, KUBECTL="kubectl")
    dry = sh("bash", str(ORPHAN), f"old-root-{ns}", "-n", "argocd", check=False, cwd=work)
    print("\n---- argo-orphan-tree.sh (dry run)\n" + "\n".join("  " + l for l in dry.stdout.splitlines()[-14:]))
    run_ = subprocess.run(["bash", str(ORPHAN), f"old-root-{ns}", "-n", "argocd", "--apply"], input=f"old-root-{ns}\n",
                          capture_output=True, text=True, env=env, cwd=work)
    print("---- argo-orphan-tree.sh --apply (tail)\n" + "\n".join("  " + l for l in run_.stdout.splitlines()[-10:]))
    if run_.returncode:
        sys.exit(f"orphan script failed: {run_.stderr[-1500:]}")
    wait("the old Applications to be gone", lambda: (not {k for k in apps_status("old-") if k.endswith(ns)}, apps_status("old-")), 180)
    left = [x["metadata"]["name"] for x in kjson("-n", "argocd", "get", "applicationsets.argoproj.io")["items"] if x["metadata"]["name"] == f"old-appset-{ns}"]
    if left:
        sys.exit(f"old ApplicationSet still there: {left}")
    time.sleep(10)
    after_teardown = snapshot(ns)
    teardown_problems = compare(base, after_teardown, "teardown: old Argo tree removed")

    # ---------------------------------------------------------------- adopt: diff first
    vals = ["--set", f"namespace={ns}", "--set", f"originRepoURL={git}/new-{ns}.git", "--set", "originBranch=main",
            "--set", f"folder={ns}", "--set", f"chartRepoURL={git}/chart.git", "--set", f"chartRevision={branch}",
            "--set", "chartPath=.", "--set", "project=default", "--set", "destinationServer=https://kubernetes.default.svc"]
    rendered = sh("helm", "template", "appset", str(wt / "ms-applicationSet"), *vals).stdout
    new_set = next(d for d in yaml.safe_load_all(rendered) if d and d.get("kind") == "ApplicationSet")
    auto = new_set["spec"]["template"]["spec"]["syncPolicy"].pop("automated", None)
    kubectl("apply", "-n", "argocd", "-f", "-", input=yaml.safe_dump(new_set))
    log(f"applied ApplicationSet {new_set['metadata']['name']} with auto-sync OFF (was {auto})")

    def new_apps():
        st = {k: v for k, v in apps_status("").items() if not k.startswith("old-") and k.split("-")[-1] == ns.split("-")[-1]}
        return len(st) == 2 and all(v[0] in ("Synced", "OutOfSync") for v in st.values()), st
    apps = sorted(wait("the new Applications to be compared", new_apps, 240))
    log(f"new Applications: {apps}")

    pw = base64.b64decode(kjson("-n", "argocd", "get", "secret", "argocd-initial-admin-secret")["data"]["password"]).decode()
    # The script runs on a cluster node, which reaches a ClusterIP directly.
    server = kjson("-n", "argocd", "get", "svc", "argocd-server")["spec"]["clusterIP"]
    try:
        sh("argocd", "login", f"{server}:80", "--plaintext", "--username", "admin", "--password", pw, "--grpc-web")
        diff_problems = []
        for app in apps:
            d = sh("argocd", "app", "diff", app, "--grpc-web", "--refresh", check=False)
            changed = [l for l in d.stdout.splitlines() if l[:2] in ("< ", "> ")]
            # `<   annotations: {}`: the live object's last-applied-configuration
            # and controller-owned annotations, which the diff hides, leave an
            # empty map on the live side. Nothing is applied to it.
            bad = [l for l in changed if INSTANCE not in l and l.strip() not in ("<   annotations: {}", "< annotations: {}")
                   and not (l.startswith("< ") and l[2:].strip() == "annotations: {}")]
            print(f"\n---- argocd app diff {app}: {len(changed)} changed line(s), {len(bad)} beyond {INSTANCE}")
            for l in (bad or changed)[:25]:
                print("  " + l)
            if bad:
                diff_problems.append(f"{app}: {len(bad)} diff line(s) beyond the tracking label")
            if any("selector" in l or "template" in l for l in bad):
                diff_problems.append(f"{app}: the diff touches the pod template or selector")

        # ---------------------------------------------------------------- adopt: sync
        sync_problems = []
        for app in apps:
            s = sh("argocd", "app", "sync", app, "--grpc-web", "--timeout", "180", check=False)
            if s.returncode:
                sync_problems.append(f"{app}: sync failed: {(s.stdout + s.stderr).strip().splitlines()[-1][:300]}")
        admit_routes(ns)  # a Route Argo recreated has lost its admission
        if not sync_problems:
            for app in apps:
                w = sh("argocd", "app", "wait", app, "--grpc-web", "--health", "--sync", "--timeout", "180", check=False)
                if w.returncode:
                    sync_problems.append(f"{app}: not Synced/Healthy: {(w.stdout + w.stderr).strip().splitlines()[-1][:300]}")
    finally:
        pass
    time.sleep(15)
    after = snapshot(ns)
    adopt_problems = compare(base, after, "adoption: new ApplicationSet synced")
    for p in diff_problems + sync_problems:
        print(f"  !! {p}")
    final = apps_status("")
    print("\n---- Applications: " + json.dumps({k: v for k, v in final.items() if k in apps}))

    # ---------------------------------------------------------------- verdict
    if teardown_problems:
        print("\nVERDICT: FAIL — the teardown deleted or recreated something")
        return 1
    saw_problem = bool(adopt_problems or diff_problems or sync_problems)
    if a.expect == "no-restart":
        print(f"\nVERDICT: {'FAIL' if saw_problem else 'PASS'} — {'the adoption changed running workloads' if saw_problem else 'nothing deleted, nothing restarted, apps Synced/Healthy'}")
        return 1 if saw_problem else 0
    print(f"\nVERDICT: {'PASS' if saw_problem else 'FAIL'} — negative control {'saw the restart / template diff it should' if saw_problem else 'saw NOTHING: the test cannot tell a broken chart from a fixed one'}")
    return 0 if saw_problem else 1


if __name__ == "__main__":
    sys.exit(main())
