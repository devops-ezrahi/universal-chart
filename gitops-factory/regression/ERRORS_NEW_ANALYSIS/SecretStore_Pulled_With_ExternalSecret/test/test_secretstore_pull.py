"""
namespace_importer.py pulls the SecretStore an ExternalSecret reads from, plus
what the store needs to work — the Vault CA (spec.provider.vault.caProvider,
ConfigMap or Secret) and the ServiceAccount Vault's kubernetes auth logs in as
(auth.kubernetes.serviceAccountRef). Before this, the importer looked for
`spec.storeRef`, a field no ExternalSecret has, so no store was ever pulled.

Placement rule: an object goes to shared.yaml only when MORE THAN ONE file
needs it; with a single user it goes into that microservice's own file. Either
way it is written once.

There is no cluster here: `run_oc` — the importer's one call into `oc` — is
replaced by a fake that answers `oc get` from fixture/cluster.yaml. The second
test feeds the importer's output to the converter and renders it back
(--fidelity-strict), so the store, CA and ServiceAccount are proven to land in
the chart values and render.
"""
import argparse
import importlib.util
import pathlib
import shutil
import subprocess
import sys

import pytest
import yaml

TEST_DIR = pathlib.Path(__file__).resolve().parent
GITOPS_DIR = TEST_DIR.parents[3]
CHART_DIR = GITOPS_DIR.parent
CONVERTER_PY = GITOPS_DIR / "convert_to_universal_chart.py"
FIXTURE = TEST_DIR.parent / "fixture" / "cluster.yaml"
NS = "interconn-opr"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


imp = _load("namespace_importer_under_test", GITOPS_DIR / "namespace_importer.py")

PLURALS = {**imp.RESOURCE_PLURAL_TO_KIND,
           "networkpolicies": "NetworkPolicy", "servicemonitors": "ServiceMonitor"}


class FakeOc:
    """`oc get <plural[.group]> [name] [-n ns] -o yaml`, answered from the fixture."""

    def __init__(self):
        self.objects = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))["items"]
        self.calls = []

    def __call__(self, args):
        self.calls.append(args)
        assert args[0] == "get" and args[-2:] == ["-o", "yaml"], args
        rest = args[1:-2]
        ns = None
        if "-n" in rest:
            i = rest.index("-n")
            ns, rest = rest[i + 1], rest[:i] + rest[i + 2:]
        kind = PLURALS[rest[0].split(".", 1)[0]]
        hits = [o for o in self.objects if o["kind"] == kind
                and (ns is None or o["metadata"].get("namespace") == ns)]
        if len(rest) == 2:
            hits = [o for o in hits if o["metadata"]["name"] == rest[1]]
            if not hits:
                raise subprocess.CalledProcessError(1, ["oc", *args], stderr=f"NotFound: {rest[1]}")
            return subprocess.CompletedProcess(args, 0, stdout=yaml.safe_dump(hits[0]), stderr="")
        return subprocess.CompletedProcess(
            args, 0, stdout=yaml.safe_dump({"apiVersion": "v1", "kind": "List", "items": hits}), stderr="")


@pytest.fixture
def imported(tmp_path, monkeypatch):
    oc = FakeOc()
    monkeypatch.setattr(imp, "run_oc", oc)
    monkeypatch.setattr(imp, "FETCH_FAILURES", [])
    imp.handle_full_namespace(NS, argparse.Namespace(output=str(tmp_path / "in")))
    ns_dir = tmp_path / "in" / NS
    docs = {f.stem: [d for d in yaml.safe_load_all(f.read_text(encoding="utf-8")) if d]
            for f in ns_dir.glob("*.yaml")}
    return oc, tmp_path / "in", docs


def _ids(docs):
    return sorted((d["kind"], d["metadata"]["name"]) for d in docs)


def test_store_and_its_deps_placed_once_by_number_of_users(imported):
    oc, _, docs = imported
    assert sorted(docs) == ["kafka", "shared", "solo", "zookeeper"]

    # Two microservices read zookeeper-secret-store -> it, its CA and SA are shared.
    assert _ids(docs["shared"]) == [("ConfigMap", "vault-ca"), ("NetworkPolicy", "deny-all"), ("SecretStore", "zookeeper-secret-store"),
                                    ("ServiceAccount", "vault")]
    # One microservice reads solo-store -> it and its deps sit in solo.yaml. The
    # ClusterSecretStore's CA/SA name another namespace and are not pulled.
    assert _ids(docs["solo"]) == [("ClusterSecretStore", "global-store"), ("Deployment", "solo"),
                                  ("ExternalSecret", "solo-es"), ("ExternalSecret", "solo-global-es"),
                                  ("Secret", "solo-vault-ca"), ("SecretStore", "solo-store"),
                                  ("ServiceAccount", "solo-vault")]
    assert _ids(docs["zookeeper"]) == [("Deployment", "zookeeper"), ("ExternalSecret", "zk-es")]
    assert _ids(docs["kafka"]) == [("Deployment", "kafka"), ("ExternalSecret", "kafka-es")]

    # Written once across the whole namespace.
    every = [i for d in docs.values() for i in _ids(d)]
    assert len(every) == len(set(every))

    # Cleaned like every other resource (clean_resource), content kept.
    store = next(d for d in docs["shared"] if d["kind"] == "SecretStore")
    assert set(store["metadata"]) == {"name", "labels"}
    assert "status" not in store
    assert store["metadata"]["labels"]["operationalPurpose"] == "messaging"
    assert store["spec"]["provider"]["vault"]["auth"]["kubernetes"] == {
        "mountPath": "dev", "role": "k8s-role-dev-interconn-opr-vault", "serviceAccountRef": {"name": "vault"}}

    # ServiceMonitors are not fetched at all; NetworkPolicies are (the converter
    # turns them into networkPolicies).
    asked = {c[1].split(".", 1)[0] for c in oc.calls}
    assert "servicemonitors" not in asked
    assert "networkpolicies" in asked


def test_a_store_already_placed_is_not_duplicated():
    """Existing placement counts as a user: an SA a workload file already holds,
    needed by a store another file uses, moves to shared.yaml once."""
    sa = ("vault", "ServiceAccount", yaml.safe_dump({"apiVersion": "v1", "kind": "ServiceAccount",
                                                     "metadata": {"name": "vault"}}))
    es = ("x-ext", "ExternalSecret", yaml.safe_dump(
        {"kind": "ExternalSecret", "metadata": {"name": "b-es"},
         "spec": {"secretStoreRef": {"name": "st"}}}))
    store = {"apiVersion": "external-secrets.io/v1beta1", "kind": "SecretStore", "metadata": {"name": "st"},
             "spec": {"provider": {"vault": {"auth": {"kubernetes": {"serviceAccountRef": {"name": "vault"}}}}}}}
    ms = {"a": {"workload": None, "resources": [sa]}, "b": {"workload": None, "resources": [es]}}
    orig = imp.fetch_resource
    imp.fetch_resource = lambda ns, rt, name, api_group=None: yaml.safe_dump(store) if name == "st" else None
    try:
        imp.place_secret_store_dependencies("n", ms)
    finally:
        imp.fetch_resource = orig
    assert ms["a"]["resources"] == []
    assert [(n, k) for n, k, _ in ms["b"]["resources"]] == [("x-ext", "ExternalSecret"), ("st", "SecretStore")]
    assert [(n, k) for n, k, _ in ms[imp.SHARED_KEY]["resources"]] == [("vault", "ServiceAccount")]


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_converter_renders_the_pulled_store_ca_and_sa(imported, tmp_path):
    _, inp, _ = imported
    out = tmp_path / "out"
    proc = subprocess.run(
        [sys.executable, str(CONVERTER_PY), "--input", str(inp), "--output", str(out),
         # --cluster-scoped: the ClusterSecretStore goes to cluster-shared/;
         # namespace-scoped mode drops it by design (reported, not converted).
         "--chart", str(CHART_DIR), "--cluster-scoped", "--verify-fidelity", "--fidelity-strict"],
        capture_output=True, text=True, errors="replace", cwd=str(GITOPS_DIR))
    assert proc.returncode == 0, proc.stdout + proc.stderr

    def values(release):
        merged = {}
        for f in (out / "base" / f"{release}.yaml", out / NS / "defaults.yaml",
                  out / NS / "values" / f"{release}.yaml"):
            if f.exists():
                merged.update(yaml.safe_load(f.read_text(encoding="utf-8")) or {})
        return merged

    shared = values("shared")
    assert "zookeeper-secret-store" in shared["secretStores"]
    assert "vault-ca" in shared["configMaps"]
    assert shared["serviceAccount"]["name"] == "vault"

    solo = values("solo")
    assert "solo-store" in solo["secretStores"]
    assert "solo-vault-ca" in solo["secrets"]
    # solo's pod runs as `default`; the Vault SA beside it must not become the
    # chart's serviceAccount (that would move the pod onto it).
    assert solo["serviceAccount"]["create"] is False
    assert any("name: solo-vault" in e for e in solo["extraDeploy"])
    assert "global-store" in (out / "cluster-shared" / "values" / "cluster-shared-values.yaml").read_text(
        encoding="utf-8")


def test_clean_resource_drops_source_argocd_and_apply_stamps():
    """The live object in the console carries the SOURCE Application's
    tracking-id and a full last-applied copy; neither is configuration."""
    cleaned = imp.clean_resource({"kind": "SecretStore", "metadata": {
        "name": "zookeeper-secret-store", "uid": "u", "annotations": {
            "argocd.argoproj.io/tracking-id": "dev-interconn-opr-zookeeper:...",
            "kubectl.kubernetes.io/last-applied-configuration": "{}",
            "team": "interconn"}}})
    assert cleaned["metadata"]["annotations"] == {"team": "interconn"}
