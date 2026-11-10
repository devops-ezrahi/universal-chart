"""The command's sources: -n, --link, --from-yaml, --input, --pull-only.

Live pulls run against fake_oc.py (answers `oc get` from YAML files), so the
whole path from a namespace name to a verified values tree is exercised
without a cluster."""
import filecmp
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
CMD = REPO / "gitops-factory" / "convert_to_universal_chart.py"
sys.path.insert(0, str(REPO))
from converter import sources  # noqa: E402

CONSOLE = "https://console-openshift-console.apps.ocp.example.com"
FLAT = REPO / "examples" / "converted" / "flat" / "input"
GROUPED = REPO / "examples" / "converted" / "grouped" / "input"


def run(*args, env=None, ok=True):
    r = subprocess.run([sys.executable, str(CMD), "--chart", str(REPO), *map(str, args)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       env={**os.environ, "PYTHONIOENCODING": "utf-8", **(env or {})})
    if ok:
        assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]
    return r


def fake_oc(tmp_path: Path) -> Path:
    """An executable `oc` that runs fake_oc.py (a .cmd on Windows)."""
    if os.name == "nt":
        p = tmp_path / "fake-oc.cmd"
        p.write_text(f'@"{sys.executable}" "{HERE / "fake_oc.py"}" %*\r\n', encoding="utf-8")
    else:
        p = tmp_path / "fake-oc"
        p.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{HERE / "fake_oc.py"}" "$@"\n', encoding="utf-8")
        p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return p


def tree_diff(a: Path, b: Path, ignore=("report",)) -> list[str]:
    cmp = filecmp.dircmp(a, b, ignore=list(ignore))
    out = [f"only in {a}: {x}" for x in cmp.left_only] + [f"only in {b}: {x}" for x in cmp.right_only]
    out += [f"differs: {x}" for x in cmp.common_files
            if (a / x).read_bytes().replace(b"\r\n", b"\n") != (b / x).read_bytes().replace(b"\r\n", b"\n")]
    for d in cmp.common_dirs:
        out += tree_diff(a / d, b / d, ignore)
    return out


# --------------------------------------------------------------- console links

@pytest.mark.parametrize("path", ["/topology/ns/shop-dev", "/topology/ns/shop-dev/graph", "/k8s/cluster/projects/shop-dev",
                                  "/k8s/ns/shop-dev/deployments", "/project-details/ns/shop-dev",
                                  "/k8s/cluster/namespaces/shop-dev", "/k8s/ns/shop-dev"])
def test_namespace_pages_are_the_whole_namespace(path):
    assert sources.parse_link(CONSOLE + path) == ("shop-dev", {"whole": True})


def test_topology_app_and_resource_links():
    assert sources.parse_link(f"{CONSOLE}/topology/ns/bluetorch-tst?view=graph&selectId=group%3Alapid-tst-blue") == \
        ("bluetorch-tst", {"app": "lapid-tst-blue"})
    assert sources.parse_link(f"{CONSOLE}/k8s/ns/goldenbell-next/deployments/ballistic-api-sim-1") == \
        ("goldenbell-next", {"resource": "deployments", "name": "ballistic-api-sim-1"})
    assert sources.parse_link(f"{CONSOLE}/k8s/ns/shop/apps~v1~StatefulSet/db/yaml") == \
        ("shop", {"resource": "statefulset.apps", "name": "db"})
    assert sources.parse_link(f"{CONSOLE}/k8s/ns/shop/core~v1~ConfigMap/cfg") == \
        ("shop", {"resource": "configmap", "name": "cfg"})


@pytest.mark.parametrize("link,msg", [("not a link", "not a web link"),
                                      (f"{CONSOLE}/dashboards", "not a namespace, topology or resource page"),
                                      (f"{CONSOLE}/k8s/ns/Bad_NS", "not a namespace name"),
                                      (f"{CONSOLE}/topology/ns/shop?selectId=group%3ABad_App", "not an app name")])
def test_links_that_are_not_console_pages_are_refused(link, msg):
    with pytest.raises(ValueError, match=msg):
        sources.parse_link(link)


def test_plan_merges_n_and_links_per_namespace():
    plan = sources.pull_plan(["a"], [f"{CONSOLE}/topology/ns/b?selectId=group%3Aweb",
                                     f"{CONSOLE}/k8s/ns/b/deployments/api", f"{CONSOLE}/k8s/cluster/projects/a"])
    assert plan == {"a": {"whole": True, "apps": [], "resources": []},
                    "b": {"whole": False, "apps": ["web"], "resources": [("deployments", "api")]}}
    with pytest.raises(ValueError, match="not a Kubernetes namespace name"):
        sources.pull_plan(["Not_OK"], [])


# --------------------------------------------------------------- sources agree

@pytest.mark.parametrize("corpus,extra", [(FLAT, ["--env-suffixes", "dev,prd"]),
                                          (GROUPED, ["--env-suffixes", "dev,prd", "--env-group", "color=black,yellow"])],
                         ids=["flat", "grouped"])
def test_every_source_gives_the_same_tree(tmp_path, corpus, extra):
    """The same namespaces through -n (live, fake oc), --from-yaml and
    --pull-only + --input: one tree, byte for byte. (A hand-made dump given to
    --input may group workloads differently from a pull, so it is not compared.)"""
    nss = sorted(p.name for p in corpus.iterdir() if p.is_dir())
    oc = fake_oc(tmp_path)
    env = {"FAKE_OC_DIR": str(corpus)}
    run(*[a for n in nss for a in ("-n", n)], "--oc", oc, "-o", tmp_path / "live", *extra, env=env)
    run("--from-yaml", corpus, "-o", tmp_path / "yaml", *extra)
    run("--from-yaml", corpus, "--pull-only", "-o", tmp_path / "dump")
    run("--input", tmp_path / "dump", "-o", tmp_path / "two-step", *extra)
    for other in ("yaml", "two-step"):
        assert tree_diff(tmp_path / "live", tmp_path / other) == [], other
    # A pulled dump converted with --input is the same conversion again.
    run("--input", tmp_path / "dump", "-o", tmp_path / "again", *extra)
    assert tree_diff(tmp_path / "two-step", tmp_path / "again") == []


def test_pull_only_writes_only_the_dump_and_its_warnings(tmp_path):
    w = tmp_path / "w.json"
    run("--from-yaml", FLAT, "--pull-only", "-o", tmp_path / "dump", "--warnings-file", w)
    assert sorted(p.name for p in (tmp_path / "dump").iterdir()) == ["dev", "prd"]
    assert all(f.suffix == ".yaml" for f in (tmp_path / "dump").rglob("*") if f.is_file())
    assert json.loads(w.read_text(encoding="utf-8")) == []


APPS = """
apiVersion: apps/v1
kind: Deployment
metadata: {name: web, namespace: shop, labels: {app: web, app.kubernetes.io/part-of: storefront}}
spec:
  selector: {matchLabels: {app: web}}
  template:
    metadata: {labels: {app: web, app.kubernetes.io/part-of: storefront}}
    spec: {containers: [{name: web, image: "nginx:1.27", envFrom: [{configMapRef: {name: web-cfg}}]}]}
---
apiVersion: v1
kind: ConfigMap
metadata: {name: web-cfg, namespace: shop}
data: {MODE: prod}
---
apiVersion: apps/v1
kind: Deployment
metadata: {name: billing, namespace: shop, labels: {app: billing, app.kubernetes.io/part-of: payments}}
spec:
  selector: {matchLabels: {app: billing}}
  template:
    metadata: {labels: {app: billing, app.kubernetes.io/part-of: payments}}
    spec: {containers: [{name: billing, image: "busybox:1.36"}]}
"""


def _cluster(tmp_path):
    root = tmp_path / "cluster"
    (root / "shop").mkdir(parents=True)
    (root / "shop" / "all.yaml").write_text(APPS, encoding="utf-8")
    return {"FAKE_OC_DIR": str(root), "FAKE_OC_LOG": str(tmp_path / "oc.log")}


def test_topology_link_pulls_that_app_only(tmp_path):
    env = _cluster(tmp_path)
    run("--link", f"{CONSOLE}/topology/ns/shop?selectId=group%3Astorefront", "--oc", fake_oc(tmp_path),
        "--pull-only", "-o", tmp_path / "dump", env=env)
    pulled = {d["kind"] + "/" + d["metadata"]["name"]
              for f in (tmp_path / "dump" / "shop").glob("*.yaml")
              for d in yaml.safe_load_all(f.read_text(encoding="utf-8")) if d}
    assert "Deployment/web" in pulled and "ConfigMap/web-cfg" in pulled
    assert "Deployment/billing" not in pulled


def test_resource_link_pulls_the_app_it_belongs_to(tmp_path):
    env = _cluster(tmp_path)
    run("--link", f"{CONSOLE}/k8s/ns/shop/deployments/billing", "--oc", fake_oc(tmp_path),
        "--pull-only", "-o", tmp_path / "dump", env=env)
    names = {f.stem for f in (tmp_path / "dump" / "shop").glob("*.yaml")}
    text = "".join(f.read_text(encoding="utf-8") for f in (tmp_path / "dump" / "shop").glob("*.yaml"))
    assert "billing" in text and "name: web\n" not in text, names


def test_kubeconfig_reaches_every_oc_call(tmp_path):
    env = _cluster(tmp_path)
    run("-n", "shop", "--oc", fake_oc(tmp_path), "--kubeconfig", tmp_path / "kc", "-o", tmp_path / "out", env=env)
    calls = [f.read_text(encoding="utf-8") for f in (tmp_path / "oc.log").iterdir()]
    assert calls and all(c.startswith(f"--kubeconfig {tmp_path / 'kc'} get ") for c in calls)


def test_an_empty_namespace_is_a_warning_in_the_report(tmp_path):
    env = _cluster(tmp_path)
    (tmp_path / "cluster" / "empty").mkdir()
    r = run("-n", "shop", "-n", "empty", "--oc", fake_oc(tmp_path), "-o", tmp_path / "out", env=env)
    warn = (tmp_path / "out" / "report" / "pull_warnings.txt").read_text(encoding="utf-8")
    assert "Namespace 'empty'" in warn and "Namespace 'empty'" in r.stderr
    assert (tmp_path / "out" / "shop").is_dir() and not (tmp_path / "out" / "empty").exists()


def test_given_yaml_is_all_app(tmp_path):
    """--from-yaml converts everything given, not only what workloads reference:
    a Role nobody mounts still lands in the tree."""
    src = tmp_path / "in.yaml"
    src.write_text(APPS + """---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata: {name: reader, namespace: shop}
rules: [{apiGroups: [""], resources: [pods], verbs: [get]}]
""", encoding="utf-8")
    run("--from-yaml", src, "-o", tmp_path / "out")
    assert "reader" in "".join(f.read_text(encoding="utf-8") for f in (tmp_path / "out").rglob("*.yaml")
                               if "report" not in f.parts)


def test_cluster_scoped_yaml_without_namespace_goes_with_the_first_namespace(tmp_path):
    src = tmp_path / "in.yaml"
    src.write_text(APPS + """---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata: {name: shop-reader}
rules: [{apiGroups: [""], resources: [pods], verbs: [get]}]
""", encoding="utf-8")
    run("--from-yaml", src, "--pull-only", "-o", tmp_path / "dump")
    assert "shop-reader" in "".join(f.read_text(encoding="utf-8") for f in (tmp_path / "dump" / "shop").glob("*.yaml"))


@pytest.mark.parametrize("args,msg", [
    (["--input", "x", "-n", "a"], "--input is a source of its own"),
    (["--pull-only"], "--pull-only needs a source"),
    (["--dump", "d"], "--dump needs a source"),
    (["--from-yaml", "x.yaml", "--link", f"{CONSOLE}/k8s/ns/a"], "cannot be combined with --from-yaml"),
    (["--link", f"{CONSOLE}/dashboards"], "not a namespace, topology or resource page"),
    (["--from-yaml", "does-not-exist.yaml"], "not found"),
])
def test_bad_source_combinations_are_refused(tmp_path, args, msg):
    r = run(*args, "-o", tmp_path / "out", ok=False)
    assert r.returncode != 0 and msg in r.stderr, r.stderr[-2000:]


def test_a_document_without_namespace_needs_a_default(tmp_path):
    src = tmp_path / "in.yaml"
    src.write_text("apiVersion: v1\nkind: ConfigMap\nmetadata: {name: c}\ndata: {a: b}\n", encoding="utf-8")
    r = run("--from-yaml", src, "-o", tmp_path / "out", ok=False)
    assert "--default-namespace" in r.stderr
    run("--from-yaml", src, "--default-namespace", "solo", "--pull-only", "-o", tmp_path / "dump")
    assert (tmp_path / "dump" / "solo").is_dir()


def test_a_pull_is_the_same_whatever_the_hash_seed(tmp_path):
    """The importer walked Python sets of references, whose order changes per
    process: the same namespaces pulled twice could write their documents in a
    different order. Sorted now; pinned here across hash seeds."""
    src = REPO / "examples" / "showcase"
    nss = [a for p in sorted(src.iterdir()) if p.is_dir() for a in ("-n", p.name)]
    oc = fake_oc(tmp_path)
    dumps = []
    for seed in ("0", "1", "2"):
        d = tmp_path / f"seed{seed}"
        run(*nss, "--oc", oc, "--pull-only", "-o", d, env={"PYTHONHASHSEED": seed, "FAKE_OC_DIR": str(src)})
        dumps.append(d)
    assert tree_diff(dumps[0], dumps[1]) == [] and tree_diff(dumps[0], dumps[2]) == []
