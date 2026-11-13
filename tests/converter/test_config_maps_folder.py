"""A namespace's ConfigMaps folder and the ApplicationSet settings that live in
rootApplicationSet.yaml.

values/configMaps.yaml is not a microservice: it lists (configMapFiles) chart
values files under <ns>/configMaps/, deployed by ms-applicationSet as one
Application. The converter must not treat it as a release (no base/ for it),
must render it the way Argo CD does when verifying, and must never call it or
the folder stale. The root ApplicationSet always carries destinationServer,
chartRepoURL, chartRevision, autoSync, preserveResourcesOnDeletion and
configMaps.*, which ms-applicationSet/values.yaml no longer defines."""
import subprocess
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO))
from converter import engine  # noqa: E402


def deployment(name, ns):
    return {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": name, "namespace": ns},
            "spec": {"selector": {"matchLabels": {"app": name}}, "template": {
                "metadata": {"labels": {"app": name}},
                "spec": {"containers": [{"name": name, "image": f"registry.example.com/{name}:1.0"}]}}}}


def convert(tmp_path, *flags, extra=None):
    """extra: {file stem: [docs]}, written multi-doc."""
    d = tmp_path / "in" / "shop-dev"
    d.mkdir(parents=True, exist_ok=True)
    for ms in ("item1", "item2"):
        (d / f"{ms}.yaml").write_text(yaml.safe_dump(deployment(ms, "shop-dev")), encoding="utf-8")
    for name, docs in (extra or {}).items():
        (d / f"{name}.yaml").write_text(yaml.safe_dump_all(docs), encoding="utf-8")
    out = tmp_path / "out"
    r = subprocess.run([sys.executable, str(REPO / "gitops-factory" / "convert_to_universal_chart.py"),
                        "--input", str(tmp_path / "in"), "--output", str(out), "--skip-verify",
                        "--argocd-manifests", "true", *flags],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]
    return out


def params(out):
    appset = yaml.safe_load((out / "rootApplicationSet.yaml").read_text(encoding="utf-8"))
    return {p["name"]: p["value"] for p in appset["spec"]["template"]["spec"]["sources"][0]["helm"]["parameters"]}


def add_config_maps(out, listed, files):
    ns = out / "shop-dev"
    (ns / "configMaps").mkdir(exist_ok=True)
    for name, data in files.items():
        (ns / "configMaps" / f"{name}.yaml").write_text(
            yaml.safe_dump({"configMaps": {name: {"data": data}}}), encoding="utf-8")
    (ns / "values" / "configMaps.yaml").write_text(yaml.safe_dump({"configMapFiles": listed}), encoding="utf-8")


def test_root_applicationset_carries_every_appset_setting(tmp_path):
    p = params(convert(tmp_path))
    assert p["destinationServer"] == "https://kubernetes.default.svc"
    assert p["chartRepoURL"] == "https://github.com/devops-ezrahi/universal-chart.git"
    assert p["chartRevision"] == "main"
    assert p["autoSync"] == "false"
    assert p["preserveResourcesOnDeletion"] == "true"
    assert p["configMaps.enabled"] == "true"
    assert p["configMaps.file"] == "configMaps.yaml"
    assert p["configMaps.dir"] == "configMaps"
    assert p["configMaps.listKey"] == "configMapFiles"
    assert "chartPath" not in p


def test_flags_set_the_root_parameters(tmp_path):
    p = params(convert(tmp_path, "--preserve-resources-on-deletion", "false", "--config-maps", "false",
                       "--config-maps-file", "shared.yaml", "--config-maps-dir", "cms",
                       "--config-maps-list-key", "files"))
    assert p["preserveResourcesOnDeletion"] == "false"
    assert (p["configMaps.enabled"], p["configMaps.file"], p["configMaps.dir"], p["configMaps.listKey"]) == \
        ("false", "shared.yaml", "cms", "files")


def test_the_list_file_is_not_a_release(tmp_path):
    values = tmp_path / "values"
    values.mkdir()
    for n in ("item1.yaml", "configMaps.yaml"):
        (values / n).write_text("{}\n", encoding="utf-8")
    assert [f.name for f in engine.values_files(values)] == ["item1.yaml"]
    assert [f.name for f in engine.config_maps_files(values)] == ["configMaps.yaml"]


def test_verify_renders_the_config_maps_release(tmp_path):
    out = convert(tmp_path)
    add_config_maps(out, ["env1", "namespace-conf.yaml"],
                    {"env1": {"LOG_LEVEL": "info"}, "namespace-conf": {"region": "eu"}})
    rendered, conflicts = engine.verify_no_conflicts(REPO, out)
    assert conflicts == []
    assert rendered == 3  # item1, item2 and the ConfigMaps release
    chain, problem = engine.config_maps_chain(out / "shop-dev", out / "shop-dev" / "values" / "configMaps.yaml")
    assert problem == ""
    docs = engine.helm_template(REPO, "configmaps-dev", "shop-dev", chain)
    assert sorted((d["kind"], d["metadata"]["name"]) for d in docs) == \
        [("ConfigMap", "env1"), ("ConfigMap", "namespace-conf")]


def test_verify_reports_a_missing_listed_file(tmp_path):
    out = convert(tmp_path)
    add_config_maps(out, ["env1", "nope"], {"env1": {"A": "1"}})
    _, conflicts = engine.verify_no_conflicts(REPO, out)
    assert any("BROKEN CHAIN" in c and "configMaps/nope.yaml" in c for c in conflicts), conflicts


def test_verify_reports_a_list_file_without_the_list(tmp_path):
    out = convert(tmp_path)
    add_config_maps(out, ["env1"], {"env1": {"A": "1"}})
    (out / "shop-dev" / "values" / "configMaps.yaml").write_text("other: 1\n", encoding="utf-8")
    _, conflicts = engine.verify_no_conflicts(REPO, out)
    assert any("must list the configMaps/ files" in c for c in conflicts), conflicts


def test_verify_reports_a_config_map_a_release_also_renders(tmp_path):
    cm = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "env1", "namespace": "shop-dev"},
          "data": {"A": "1"}}
    out = convert(tmp_path, extra={"item3": [deployment("item3", "shop-dev"), cm]})
    add_config_maps(out, ["env1"], {"env1": {"A": "2"}})
    _, conflicts = engine.verify_no_conflicts(REPO, out)
    assert any("NAMESPACE CONFLICT" in c and "ConfigMap/env1" in c and "configmaps" in c for c in conflicts), conflicts


def test_rerun_keeps_the_folder_and_list_file(tmp_path):
    out = convert(tmp_path)
    add_config_maps(out, ["env1"], {"env1": {"A": "1"}})
    convert(tmp_path, "--prune")
    assert (out / "shop-dev" / "values" / "configMaps.yaml").is_file()
    assert (out / "shop-dev" / "configMaps" / "env1.yaml").is_file()


def test_upgrade_keeps_the_root_settings_and_skips_the_list_file(tmp_path):
    from converter import upgrade
    out = convert(tmp_path, "--preserve-resources-on-deletion", "false", "--config-maps-dir", "cms",
                  "--config-maps-list-key", "files")
    (out / "shop-dev" / "values" / "configMaps.yaml").write_text("files: [env1]\n", encoding="utf-8")
    tree = upgrade.OldTree(out)
    rec = upgrade.recorded_settings(tree)
    assert rec["preserve_resources_on_deletion"] is False
    assert rec["config_maps"] is True
    assert (rec["config_maps_dir"], rec["config_maps_list_key"], rec["config_maps_file"]) == \
        ("cms", "files", "configMaps.yaml")
    # Not a release: an upgrade must not look for base/configMaps.yaml.
    assert sorted(tree.releases["shop-dev"]) == ["item1", "item2"]
