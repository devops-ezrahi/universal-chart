"""A namespace's ConfigMaps folder and the ApplicationSet settings that live in
rootApplicationSet.yaml.

Every <ns>/configMaps/<name>.yaml starts at `data:` and is its own Application
(<name>-cm-<ns>) deploying ConfigMap/<name>: no list file. The converter must
render each one the way Argo CD does when verifying, never call the folder
stale, and migrate the old shape (values/configMaps.yaml list + files wrapped
in configMaps: <name>:). The root ApplicationSet always carries
destinationServer, chartRepoURL, chartRevision, autoSync,
preserveResourcesOnDeletion and configMaps.*, which
ms-applicationSet/values.yaml no longer defines."""
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


def add_config_maps(out, files, ns="shop-dev"):
    """files: {name: data}, written in the current shape (starting at data:)."""
    d = out / ns / "configMaps"
    d.mkdir(exist_ok=True)
    for name, data in files.items():
        (d / f"{name}.yaml").write_text(yaml.safe_dump({"data": data}, allow_unicode=True), encoding="utf-8")


def add_legacy_config_maps(out, listed, files, ns="shop-dev"):
    """The old shape: values/configMaps.yaml lists, each file wraps its ConfigMap."""
    d = out / ns / "configMaps"
    d.mkdir(exist_ok=True)
    for name, spec in files.items():
        (d / f"{name}.yaml").write_text(yaml.safe_dump({"configMaps": {name: spec}}, allow_unicode=True),
                                        encoding="utf-8")
    (out / ns / "values" / "configMaps.yaml").write_text(yaml.safe_dump({"configMapFiles": listed}),
                                                         encoding="utf-8")


def test_root_applicationset_carries_every_appset_setting(tmp_path):
    p = params(convert(tmp_path))
    assert p["destinationServer"] == "https://kubernetes.default.svc"
    assert p["chartRepoURL"] == "https://github.com/devops-ezrahi/universal-chart.git"
    assert p["chartRevision"] == "main"
    assert p["autoSync"] == "false"
    assert p["preserveResourcesOnDeletion"] == "true"
    assert p["configMaps.enabled"] == "true"
    assert p["configMaps.dir"] == "configMaps"
    assert "configMaps.file" not in p and "configMaps.listKey" not in p
    assert "chartPath" not in p


def test_flags_set_the_root_parameters(tmp_path):
    p = params(convert(tmp_path, "--preserve-resources-on-deletion", "false", "--config-maps", "false",
                       "--config-maps-dir", "cms"))
    assert p["preserveResourcesOnDeletion"] == "false"
    assert (p["configMaps.enabled"], p["configMaps.dir"]) == ("false", "cms")


def test_the_old_list_file_is_not_a_release(tmp_path):
    values = tmp_path / "values"
    values.mkdir()
    for n in ("item1.yaml", "configMaps.yaml"):
        (values / n).write_text("{}\n", encoding="utf-8")
    assert [f.name for f in engine.values_files(values)] == ["item1.yaml"]


def test_verify_renders_each_config_map_file_as_its_own_release(tmp_path):
    out = convert(tmp_path)
    add_config_maps(out, {"env1": {"LOG_LEVEL": "info"}, "namespace-conf": {"greeting": "שלום עולם"}})
    rendered, conflicts = engine.verify_no_conflicts(REPO, out)
    assert conflicts == []
    assert rendered == 4  # item1, item2 and one release per ConfigMap file
    vf = out / "shop-dev" / "configMaps" / "namespace-conf.yaml"
    assert engine.config_maps_release(vf) == "namespace-conf-cm"
    files, sets = engine.config_maps_render_args(vf)
    docs = engine.helm_template(REPO, "namespace-conf-cm-dev", "shop-dev", files, sets)
    assert [(d["kind"], d["metadata"]["name"], d["data"]) for d in docs] == \
        [("ConfigMap", "namespace-conf", {"greeting": "שלום עולם"})]


def test_verify_still_renders_a_file_in_the_old_wrapped_shape(tmp_path):
    out = convert(tmp_path)
    d = out / "shop-dev" / "configMaps"
    d.mkdir()
    (d / "env1.yaml").write_text(yaml.safe_dump({"configMaps": {"env1": {"data": {"A": "1"}}}}), encoding="utf-8")
    rendered, conflicts = engine.verify_no_conflicts(REPO, out)
    assert conflicts == [] and rendered == 3


def test_a_leftover_list_file_is_not_rendered(tmp_path):
    out = convert(tmp_path)
    add_config_maps(out, {"env1": {"A": "1"}})
    (out / "shop-dev" / "values" / "configMaps.yaml").write_text("configMapFiles: [env1]\n", encoding="utf-8")
    rendered, conflicts = engine.verify_no_conflicts(REPO, out)
    assert conflicts == [] and rendered == 3


def test_migration_keeps_the_list_file_when_deletes_cascade(tmp_path):
    out = convert(tmp_path)
    add_legacy_config_maps(out, ["env1"], {"env1": {"data": {"A": "1"}}})
    convert(tmp_path, "--preserve-resources-on-deletion", "false")
    assert (out / "shop-dev" / "values" / "configMaps.yaml").is_file()
    assert yaml.safe_load((out / "shop-dev" / "configMaps" / "env1.yaml").read_text(encoding="utf-8")) == \
        {"data": {"A": "1"}}
    report = (out / "report" / "config_maps_migration.txt").read_text(encoding="utf-8")
    assert "kept shop-dev/values/configMaps.yaml" in report


def test_verify_reports_a_config_map_a_release_also_renders(tmp_path):
    cm = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "env1", "namespace": "shop-dev"},
          "data": {"A": "1"}}
    out = convert(tmp_path, extra={"item3": [deployment("item3", "shop-dev"), cm]})
    add_config_maps(out, {"env1": {"A": "2"}})
    _, conflicts = engine.verify_no_conflicts(REPO, out)
    assert any("NAMESPACE CONFLICT" in c and "ConfigMap/env1" in c and "env1-cm" in c for c in conflicts), conflicts


def test_rerun_keeps_the_folder(tmp_path):
    out = convert(tmp_path)
    add_config_maps(out, {"env1": {"A": "1"}})
    convert(tmp_path, "--prune")
    assert yaml.safe_load((out / "shop-dev" / "configMaps" / "env1.yaml").read_text(encoding="utf-8")) == \
        {"data": {"A": "1"}}


def test_rerun_migrates_the_old_shape(tmp_path):
    out = convert(tmp_path)
    add_legacy_config_maps(out, ["env1"], {
        "env1": {"data": {"A": "שלום"}, "labels": {"team": "shop"}, "annotations": {"o": "ops"}},
        "extra": {"binaryData": {"b": "aGk="}},
    })
    # A file named after nothing in particular, holding two ConfigMaps: split.
    (out / "shop-dev" / "configMaps" / "pair.yaml").write_text(
        yaml.safe_dump({"configMaps": {"one": {"data": {"X": "1"}}, "two": {"data": {"Y": "2"}}}}),
        encoding="utf-8")
    convert(tmp_path)
    d = out / "shop-dev" / "configMaps"
    assert not (out / "shop-dev" / "values" / "configMaps.yaml").exists()
    assert sorted(f.name for f in d.iterdir()) == ["env1.yaml", "extra.yaml", "one.yaml", "two.yaml"]
    env1 = (d / "env1.yaml").read_text(encoding="utf-8")
    assert "שלום" in env1  # written as Hebrew, not \\u escapes
    assert yaml.safe_load(env1) == {"data": {"A": "שלום"}, "commonLabels": {"team": "shop"},
                                    "commonAnnotations": {"o": "ops"}}
    assert yaml.safe_load((d / "one.yaml").read_text(encoding="utf-8")) == {"data": {"X": "1"}}
    report = (out / "report" / "config_maps_migration.txt").read_text(encoding="utf-8")
    assert "NOW DEPLOYED, not listed before" in report and "extra.yaml" in report and "one.yaml" in report
    rendered, conflicts = engine.verify_no_conflicts(REPO, out)
    assert conflicts == [] and rendered == 6


def test_upgrade_keeps_the_root_settings_and_skips_the_old_list_file(tmp_path):
    from converter import upgrade
    out = convert(tmp_path, "--preserve-resources-on-deletion", "false", "--config-maps-dir", "cms")
    (out / "shop-dev" / "values" / "configMaps.yaml").write_text("configMapFiles: [env1]\n", encoding="utf-8")
    tree = upgrade.OldTree(out)
    rec = upgrade.recorded_settings(tree)
    assert rec["preserve_resources_on_deletion"] is False
    assert rec["config_maps"] is True
    assert rec["config_maps_dir"] == "cms"
    assert "config_maps_file" not in rec and "config_maps_list_key" not in rec
    # Not a release: an upgrade must not look for base/configMaps.yaml.
    assert sorted(tree.releases["shop-dev"]) == ["item1", "item2"]
