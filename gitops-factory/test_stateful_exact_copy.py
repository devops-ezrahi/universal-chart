"""Stateful releases are converted as --exact-copy by default, so the cutover
sync restarts none of their pods.

Kubernetes rolls a workload when its pod template changes — list ORDER
included (the template hash / controller revision is computed over it) — and
refuses changes to a StatefulSet's volumeClaimTemplates, serviceName and
selector. So the proof here is the strongest one short of a live Argo CD:
every release is rendered with helm through Argo CD's own valueFiles chain
(base/<slug>.yaml, <folder>/defaults.yaml, <folder>/values/<slug>.yaml) and
the rendered workload `spec` must EQUAL the live one, as Python compares it:
lists in order, server-defaulted fields included. Argo CD syncs with
ServerSideApply, so the one field allowed to come from the live object instead
is a `kubectl rollout restart` stamp, which the chart never sets (after_sync).

Negative controls make sure a pass means something: a stateless Deployment
still gets the clean (sorted, restart-once) values by default, and
`--exact-copy false` makes the same StatefulSet come out different."""
import copy
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

HERE = Path(__file__).resolve().parent
pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="needs helm")


def container(name, image, env, ports, mounts, sc=True):
    """A container the way the API server returns it: defaults filled in."""
    c = {"name": name, "image": image, "imagePullPolicy": "IfNotPresent",
         "terminationMessagePath": "/dev/termination-log", "terminationMessagePolicy": "File",
         "resources": {}}
    if env:
        c["env"] = env
    if ports:
        c["ports"] = ports
    if mounts:
        c["volumeMounts"] = mounts
    if sc:
        c["securityContext"] = {}
    return c


def pod_spec(name, volumes):
    """Unsorted everything — ports, env, mounts, volumes, sidecars — and an
    empty securityContext: exactly what a clean conversion would reorder or
    drop, and so roll."""
    return {
        "containers": [
            container(name, f"registry.example.com/{name}:1.4.2",
                      [{"name": "ZED", "value": "1"}, {"name": "ALPHA", "value": "2"},
                       {"name": "PASSWORD", "valueFrom": {"secretKeyRef": {"name": f"{name}-creds", "key": "pw"}}}],
                      [{"name": "web", "containerPort": 8080, "protocol": "TCP"},
                       {"name": "admin", "containerPort": 9090, "protocol": "TCP"}],
                      [{"name": "z-data", "mountPath": "/data"}, {"name": "a-tmp", "mountPath": "/tmp"}]),
            container("z-proxy", "envoy:1.30", None, [{"name": "proxy", "containerPort": 15001, "protocol": "TCP"}],
                      None),
            container("a-log", "fluent-bit:3.0", None, None, [{"name": "a-tmp", "mountPath": "/var/log/app"}],
                      sc=False),
        ],
        "volumes": volumes,
        "restartPolicy": "Always", "terminationGracePeriodSeconds": 30, "dnsPolicy": "ClusterFirst",
        "schedulerName": "default-scheduler", "securityContext": {},
    }


def statefulset(ns, name):
    labels = {"app": name}
    return {"apiVersion": "apps/v1", "kind": "StatefulSet",
            "metadata": {"name": name, "namespace": ns, "labels": labels},
            "spec": {"replicas": 3, "serviceName": name, "podManagementPolicy": "OrderedReady",
                     "revisionHistoryLimit": 10,
                     "updateStrategy": {"type": "RollingUpdate", "rollingUpdate": {"partition": 0}},
                     "persistentVolumeClaimRetentionPolicy": {"whenDeleted": "Retain", "whenScaled": "Retain"},
                     "selector": {"matchLabels": labels},
                     "template": {"metadata": {"labels": labels},
                                  "spec": pod_spec(name, [{"name": "a-tmp", "emptyDir": {}}])},
                     "volumeClaimTemplates": [{
                         "apiVersion": "v1", "kind": "PersistentVolumeClaim",
                         "metadata": {"name": "z-data"},
                         "spec": {"accessModes": ["ReadWriteOnce"], "volumeMode": "Filesystem",
                                  "resources": {"requests": {"storage": "5Gi"}}}}]}}


def deployment(ns, name, pvc=None):
    labels = {"app": name}
    vols = [{"name": "z-data", "persistentVolumeClaim": {"claimName": pvc}} if pvc
            else {"name": "z-data", "emptyDir": {}}, {"name": "a-tmp", "emptyDir": {}}]
    return {"apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": name, "namespace": ns, "labels": labels},
            "spec": {"replicas": 1, "revisionHistoryLimit": 10, "progressDeadlineSeconds": 600,
                     "strategy": {"type": "Recreate"} if pvc else
                     {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": "25%", "maxUnavailable": "25%"}},
                     "selector": {"matchLabels": labels},
                     "template": {"metadata": {"labels": labels,
                                               "annotations": {"kubectl.kubernetes.io/restartedAt":
                                                               "2026-10-01T10:00:00Z"}},
                                  "spec": pod_spec(name, vols)}}}


def secret(ns, name):
    return {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": name, "namespace": ns},
            "type": "Opaque", "data": {"pw": "cGFzcw=="}}


def pvc(ns, name):
    return {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": {"name": name, "namespace": ns},
            "spec": {"accessModes": ["ReadWriteOnce"], "volumeMode": "Filesystem",
                     "resources": {"requests": {"storage": "1Gi"}}}}


def service(ns, name, headless=False):
    spec = {"selector": {"app": name}, "type": "ClusterIP",
            "ports": [{"name": "web", "port": 8080, "targetPort": 8080, "protocol": "TCP"}]}
    if headless:
        spec["clusterIP"] = "None"
    return {"apiVersion": "v1", "kind": "Service", "metadata": {"name": name, "namespace": ns,
                                                               "labels": {"app": name}}, "spec": spec}


def write_input(root: Path, namespaces) -> dict:
    """Per namespace: postgres (StatefulSet), files (Deployment + PVC) and
    web (stateless Deployment). Returns {(namespace, release): workload}."""
    live = {}
    for ns in namespaces:
        d = root / ns
        d.mkdir(parents=True)
        apps = {
            "postgres": [statefulset(ns, "postgres"), service(ns, "postgres", headless=True),
                         secret(ns, "postgres-creds")],
            "files": [deployment(ns, "files", pvc="files-data"), pvc(ns, "files-data"), service(ns, "files"),
                      secret(ns, "files-creds")],
            "web": [deployment(ns, "web"), service(ns, "web"), secret(ns, "web-creds")],
        }
        for release, docs in apps.items():
            (d / f"{release}.yaml").write_text(yaml.safe_dump_all(docs, sort_keys=False), encoding="utf-8")
            live[(ns, release)] = copy.deepcopy(docs[0])
    return live


def convert(tmp_path, namespaces, *flags):
    live = write_input(tmp_path / "in", namespaces)
    out = tmp_path / "out"
    r = subprocess.run([sys.executable, str(HERE / "convert_to_universal_chart.py"), "--input", str(tmp_path / "in"),
                        "--output", str(out), "--chart", str(HERE.parent), "--verify-fidelity", "--fidelity-strict",
                        *flags], capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-4000:]
    return live, out, r


def render(out: Path, ns: str, release: str, kind: str) -> dict:
    """The workload as Argo CD would apply it: ms-applicationSet's valueFiles
    chain, in its order."""
    files = [out / "base" / f"{release}.yaml", out / ns / "defaults.yaml", out / ns / "values" / f"{release}.yaml"]
    cmd = ["helm", "template", f"{release}-{ns.rsplit('-', 1)[-1]}", str(HERE.parent), "--namespace", ns]
    for f in files:
        assert f.exists(), f
        cmd += ["-f", str(f)]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stderr
    found = [d for d in yaml.safe_load_all(r.stdout) if d and d.get("kind") == kind]
    assert len(found) == 1, [d.get("kind") for d in yaml.safe_load_all(r.stdout) if d]
    return found[0]


def diff(a, b, path="spec"):
    """First differing paths, for a readable failure."""
    if isinstance(a, dict) and isinstance(b, dict):
        return [p for k in sorted(set(a) | set(b), key=str) for p in diff(a.get(k), b.get(k), f"{path}.{k}")]
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        return [p for i, (x, y) in enumerate(zip(a, b)) for p in diff(x, y, f"{path}[{i}]")]
    return [] if a == b else [f"{path}: live={a!r} rendered={b!r}"]


RESTART_STAMP = "kubectl.kubernetes.io/restartedAt"


def after_sync(live: dict, rendered: dict) -> dict:
    """The object once Argo CD has synced `rendered` with ServerSideApply: a
    pod-template restart stamp the manifest leaves out stays, because
    `kubectl rollout restart`'s field manager owns it, not Argo CD."""
    out = copy.deepcopy(rendered)
    stamp = ((live["spec"]["template"].get("metadata") or {}).get("annotations") or {}).get(RESTART_STAMP)
    if stamp is not None:
        ann = out["spec"]["template"].setdefault("metadata", {}).setdefault("annotations", {})
        assert RESTART_STAMP not in ann, "the chart must not pin the stamp itself"
        ann[RESTART_STAMP] = stamp
    return out


def server_defaults(obj: dict) -> dict:
    """What the API server fills in on create/update whether the manifest
    says it or not, so its absence changes nothing live: a container's
    `resources` (a plain struct, always present), the pod's `securityContext`
    (defaulted to {} when nil) and a volumeClaimTemplate's apiVersion/kind.
    A container's `securityContext` is NOT here — a nil pointer stays nil, so
    exact copy has to keep the `{}`."""
    out = copy.deepcopy(obj)
    pod = out["spec"]["template"]["spec"]
    pod.setdefault("securityContext", {})
    for c in pod.get("initContainers", []) + pod["containers"]:
        c.setdefault("resources", {})
    for vct in out["spec"].get("volumeClaimTemplates", []):
        vct.setdefault("apiVersion", "v1")
        vct.setdefault("kind", "PersistentVolumeClaim")
    return out


def assert_identical(live: dict, rendered: dict):
    synced = server_defaults(after_sync(live, rendered))
    assert synced["metadata"]["name"] == live["metadata"]["name"]
    assert synced["metadata"].get("labels") == live["metadata"].get("labels")
    assert synced["spec"] == live["spec"], "\n".join(diff(live["spec"], synced["spec"])[:20])


def _sorted(x):
    """Lists sorted, the way the clean (non exact-copy) values render them;
    the empty container securityContext dropped likewise."""
    if isinstance(x, dict):
        return {k: _sorted(v) for k, v in x.items() if not (k == "securityContext" and v == {} and "image" in x)}
    if isinstance(x, list):
        return sorted((_sorted(i) for i in x), key=lambda i: str(i.get("name", i)) if isinstance(i, dict) else str(i))
    return x


def assert_identical_but_order(live: dict, rendered: dict):
    """A negative control must differ for the right reason: only by list
    order and the dropped `securityContext: {}` — what exact copy is for."""
    synced = server_defaults(after_sync(live, rendered))
    assert _sorted(synced["spec"]) == _sorted(live["spec"]), "\n".join(diff(_sorted(live["spec"]),
                                                                             _sorted(synced["spec"]))[:20])


STATEFUL = {"postgres": "StatefulSet", "files": "Deployment"}
NAMESPACES = ["shop-dev", "shop-prd"]


@pytest.mark.parametrize("namespaces", [["shop-dev"], NAMESPACES], ids=["one-namespace", "dev+prd-share-base"])
def test_stateful_releases_render_exactly_as_live_by_default(tmp_path, namespaces):
    live, out, r = convert(tmp_path, namespaces)
    for ns in namespaces:
        for release, kind in STATEFUL.items():
            assert_identical(live[(ns, release)], render(out, ns, release, kind))
    report = (out / "report" / "conflicts_and_warnings.txt").read_text(encoding="utf-8")
    assert "postgres: stateful (StatefulSet/postgres) — converted as --exact-copy" in report
    assert "files: stateful (Deployment/files mounts a PersistentVolumeClaim)" in report
    assert "web: stateful" not in report


def test_stateless_release_stays_clean_by_default(tmp_path):
    """Negative control: the stateless web Deployment keeps the readable
    values, so its rendered template DOES differ (sorted lists) — the
    comparison above would see it."""
    live, out, _ = convert(tmp_path, ["shop-dev"])
    base = yaml.safe_load((out / "base" / "web.yaml").read_text(encoding="utf-8"))
    assert not {"portOrder", "envOrder", "volumeMountOrder", "volumeOrder", "sidecarOrder"} & set(base)
    rendered = render(out, "shop-dev", "web", "Deployment")
    w = live[("shop-dev", "web")]
    assert server_defaults(after_sync(w, rendered))["spec"]["template"] != w["spec"]["template"]
    assert_identical_but_order(w, rendered)


def test_exact_copy_false_turns_it_off(tmp_path):
    live, out, r = convert(tmp_path, ["shop-dev"], "--exact-copy", "false")
    for release, kind in STATEFUL.items():
        w = live[("shop-dev", release)]
        rendered = render(out, "shop-dev", release, kind)
        assert server_defaults(after_sync(w, rendered))["spec"]["template"] != w["spec"]["template"], release
        assert_identical_but_order(w, rendered)
    assert "converted as --exact-copy" not in (out / "report" / "conflicts_and_warnings.txt").read_text(
        encoding="utf-8")


@pytest.mark.parametrize("flag", [["--exact-copy"], ["--exact-copy", "true"]])
def test_exact_copy_true_covers_every_release(tmp_path, flag):
    live, out, _ = convert(tmp_path, NAMESPACES, *flag)
    for ns in NAMESPACES:
        for release, kind in {**STATEFUL, "web": "Deployment"}.items():
            assert_identical(live[(ns, release)], render(out, ns, release, kind))


def test_through_namespaces_to_chart(tmp_path):
    """The usual entry point: live-style manifests in, default flags."""
    src = tmp_path / "dump"
    live = write_input(src, NAMESPACES)
    manifests = tmp_path / "all.yaml"
    manifests.write_text("\n---\n".join(p.read_text(encoding="utf-8") for p in sorted(src.rglob("*.yaml"))),
                         encoding="utf-8")
    out = tmp_path / "out"
    r = subprocess.run([sys.executable, str(HERE / "namespaces_to_chart.py"), "--from-yaml", str(manifests),
                        "-o", str(out), "--chart", str(HERE.parent), "--verify-fidelity", "--fidelity-strict"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-4000:]
    for ns in NAMESPACES:
        for release, kind in STATEFUL.items():
            assert_identical(live[(ns, release)], render(out, ns, release, kind))
