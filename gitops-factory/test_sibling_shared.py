"""Sibling namespaces (goldenbell-ui / goldenbell-next) are one app: their
shared.yaml files are ONE `shared` release — base/shared.yaml plus each
namespace's values/shared.yaml — never a second base/goldenbell-ui-shared.yaml,
even when the two name no shared object alike. End to end through
namespaces_to_chart.py, every case rendered back with --fidelity-strict."""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

GF = Path(__file__).resolve().parent
pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="needs helm")

def dep(ns, name, image, uses, sa=None, replicas=1):
    """A Deployment + Service whose pod references every object in `uses`."""
    labels = {"app": name}
    env_from, vols, mounts, pull = [], [], [], []
    for kind, ref in uses:
        if kind == "ConfigMap":
            env_from.append({"configMapRef": {"name": ref}})
        elif kind == "Secret":
            vols.append({"name": ref, "secret": {"secretName": ref}})
            mounts.append({"name": ref, "mountPath": f"/etc/{ref}"})
        elif kind == "pull":
            pull.append({"name": ref})
        elif kind == "PersistentVolumeClaim":
            vols.append({"name": ref, "persistentVolumeClaim": {"claimName": ref}})
            mounts.append({"name": ref, "mountPath": f"/data/{ref}"})
    c = {"name": name, "image": image, "ports": [{"containerPort": 8080, "protocol": "TCP"}]}
    if env_from: c["envFrom"] = env_from
    if mounts: c["volumeMounts"] = mounts
    pod = {"containers": [c]}
    if vols: pod["volumes"] = vols
    if pull: pod["imagePullSecrets"] = pull
    if sa: pod["serviceAccountName"] = sa
    return [
        {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": name, "namespace": ns, "labels": labels},
         "spec": {"replicas": replicas, "selector": {"matchLabels": labels},
                  "template": {"metadata": {"labels": labels}, "spec": pod}}},
        {"apiVersion": "v1", "kind": "Service", "metadata": {"name": name, "namespace": ns, "labels": labels},
         "spec": {"selector": labels, "ports": [{"name": "http", "port": 80, "targetPort": 8080, "protocol": "TCP"}]}},
    ]

def obj(ns, kind, name, payload):
    if kind == "ConfigMap":
        return {"apiVersion": "v1", "kind": kind, "metadata": {"name": name, "namespace": ns}, "data": payload}
    if kind == "Secret":
        return {"apiVersion": "v1", "kind": kind, "metadata": {"name": name, "namespace": ns}, "type": "Opaque",
                "data": payload}
    if kind == "pull":
        return {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": name, "namespace": ns},
                "type": "kubernetes.io/dockerconfigjson", "data": {".dockerconfigjson": payload}}
    if kind == "ServiceAccount":
        return {"apiVersion": "v1", "kind": kind, "metadata": {"name": name, "namespace": ns}}
    if kind == "PersistentVolumeClaim":
        return {"apiVersion": "v1", "kind": kind, "metadata": {"name": name, "namespace": ns},
                "spec": {"accessModes": ["ReadWriteMany"], "resources": {"requests": {"storage": "1Gi"}}}}

def app(ns, workloads, shared, tag):
    """workloads: list of names; shared: list of (kind, name, payload) every workload uses."""
    docs = []
    for kind, name, payload in shared:
        docs.append(obj(ns, kind, name, payload))
    sa = next((n for k, n, _ in shared if k == "ServiceAccount"), None)
    uses = [(k, n) for k, n, _ in shared if k != "ServiceAccount"]
    for i, w in enumerate(workloads):
        docs += dep(ns, w, f"reg.io/{w.split('-')[0]}:{tag}", uses, sa=sa, replicas=i + 1)
    return docs

B64 = "eyJhdXRocyI6e319"  # {"auths":{}}

def goldenbell(ns, style, overlap):
    w = ns.rsplit("-", 1)[1]
    names = {"named": [ns, f"{ns}-worker"], "plain": ["web", "worker"]}[style]
    if overlap == "disjoint":   # the reported case: nothing named alike
        shared = [("ConfigMap", f"{w}-settings" if w == "ui" else f"flags-{w}", {"MODE": w, "URL": f"https://{ns}.example.org"}),
                  ("Secret", f"{ns}-db", {"PASSWORD": "cGFzcw=="})]
    elif overlap == "partial":  # one object in common, one not
        shared = [("pull", "regcred", B64), ("ConfigMap", f"{w}-only-{w}", {"K": w})]
    elif overlap == "rich":     # SA, PVC, CM, secret, with ns-named objects
        shared = [("ServiceAccount", f"{ns}-sa", None), ("PersistentVolumeClaim", f"{ns}-data", None),
                  ("ConfigMap", f"{ns}-config", {"A": "1", "HOST": f"{ns}.svc"}),
                  ("Secret", "tls-" + w, {"tls.crt": "Y3J0"})]
    tag = {"ui": "1.0", "next": "2.0", "admin": "3.0"}[w]
    return app(ns, names, shared, tag)

def run(tmp_path, docs, *flags):
    src = tmp_path / "in.yaml"; src.write_text(yaml.safe_dump_all(docs), encoding="utf-8")
    out, rep = tmp_path / "out", tmp_path / "report"
    r = subprocess.run([sys.executable, str(GF / "namespaces_to_chart.py"), "--from-yaml", str(src), "-o", str(out),
                        "--report", str(rep), "--chart", str(GF.parent), "--verify-fidelity", "--fidelity-strict", *flags],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-4000:]
    fid = (rep / "fidelity_report.txt").read_text(encoding="utf-8")
    assert "fields LOST (raw -> render): 0" in fid, fid[-3000:]
    assert "objects in raw but rendered by nothing:      0" in fid, fid[-3000:]
    return out, rep, r

def load(p):
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}

def check_one_shared(out, namespaces):
    base = sorted(p.name for p in (out / "base").iterdir())
    assert [b for b in base if "shared" in b] == ["shared.yaml"], base
    for ns in namespaces:
        vals = sorted(p.name for p in (out / ns / "values").iterdir())
        assert [v for v in vals if "shared" in v] == ["shared.yaml"], (ns, vals)
    # Every namespace's shared objects are in base/shared.yaml + its own values
    # (templated or concrete) — the strict fidelity render in run() proves the
    # values; here: no object lives in a base file other than shared.yaml.
    names = lambda d: {(k, n) for k in ("configMaps", "secrets", "pvc") for n in (d.get(k) or {})}
    others = [p for p in (out / "base").iterdir() if p.name != "shared.yaml"]
    for p in others:
        assert not names(load(p)) & names(load(out / "base" / "shared.yaml")), p

PAIR = ["goldenbell-ui", "goldenbell-next"]

@pytest.mark.parametrize("style", ["named", "plain"])
@pytest.mark.parametrize("overlap", ["disjoint", "partial", "rich"])
@pytest.mark.parametrize("flags", [[], ["--include", "goldenbell,web,worker"], ["--cluster-scoped"],
                                   ["--argocd-manifests", "true"]], ids=["plain-run", "include", "cluster", "argocd"])
def test_pair(tmp_path, style, overlap, flags):
    docs = [d for ns in PAIR for d in goldenbell(ns, style, overlap)]
    out, rep, _ = run(tmp_path, docs, *flags)
    check_one_shared(out, PAIR)

@pytest.mark.parametrize("style", ["named", "plain"])
def test_three_siblings(tmp_path, style):
    nss = PAIR + ["goldenbell-admin"]
    docs = [d for ns in nss for d in goldenbell(ns, style, "disjoint")]
    out, _, _ = run(tmp_path, docs)
    check_one_shared(out, nss)

def test_unrelated_app_in_same_run_stays_apart(tmp_path):
    """platform-data / platform-ops: different apps, different workloads,
    shared files with nothing alike -> still two shared releases."""
    docs = [d for ns in PAIR for d in goldenbell(ns, "named", "disjoint")]
    docs += app("platform-data", ["postgres", "backup"], [("ConfigMap", "pg-conf", {"X": "1"})], "1")
    docs += app("platform-ops", ["log-agent", "metrics"], [("ConfigMap", "ops-conf", {"Y": "2"})], "1")
    out, _, _ = run(tmp_path, docs)
    base = sorted(p.name for p in (out / "base").iterdir() if "shared" in p.name)
    assert "shared.yaml" in base and len(base) == 3, base   # goldenbell one + one per platform app
    for ns in PAIR:
        assert (out / ns / "values" / "shared.yaml").exists()
