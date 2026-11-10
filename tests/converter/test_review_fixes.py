"""Regression tests for an independent review of --upgrade, the sources and
the env editors. Each reproduces a reported failure."""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
CMD = REPO / "gitops-factory" / "convert_to_universal_chart.py"
FIX = HERE / "upgrade"
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(HERE))
from converter import engine, upgrade  # noqa: E402
from test_sources import fake_oc  # noqa: E402

FLAT_IN = REPO / "examples" / "converted" / "flat" / "input"


def run(*args, env=None, ok=True):
    r = subprocess.run([sys.executable, str(CMD), *map(str, args)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env={**os.environ, "PYTHONIOENCODING": "utf-8", **(env or {})})
    if ok:
        assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-4000:]
    return r


def files(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def upgraded_tree(tmp_path) -> Path:
    """A current tree (v3.1.0 grouped, upgraded once) to upgrade again in place."""
    d = FIX / "v3.1.0" / "grouped"
    out = tmp_path / "tree"
    run("--upgrade", d / "tree", "--oc", fake_oc(tmp_path), "-o", out, env={"FAKE_OC_DIR": str(d / "cluster")})
    shutil.rmtree(out / "report")
    return out


def partial_cluster(tmp_path, drop: str) -> Path:
    """The v3.1.0 grouped cluster without the objects of `drop` (a namespace, or a file stem)."""
    src = FIX / "v3.1.0" / "grouped" / "cluster"
    dst = tmp_path / f"cluster-without-{drop.replace('/', '_')}"
    shutil.copytree(src, dst)
    for p in list(dst.rglob("*")):
        rel = p.relative_to(dst).as_posix()
        if p.is_dir() and rel == drop:
            shutil.rmtree(p)
        elif p.is_file() and rel == drop + ".yaml":
            p.unlink()
    return dst


# 1 ------------------------------------------------------------------------
def test_in_place_upgrade_keeps_a_folder_whose_namespace_pulled_nothing(tmp_path):
    tree = upgraded_tree(tmp_path)
    before = {k: v for k, v in files(tree).items() if k.startswith("prd/")}
    assert before
    r = run("--upgrade", tree, "-o", tree, "--oc", fake_oc(tmp_path),
            env={"FAKE_OC_DIR": str(partial_cluster(tmp_path, "prd"))})
    after = {k: v for k, v in files(tree).items() if k.startswith("prd/")}
    assert after == before, "prd/ was touched"
    assert "Traceback" not in r.stderr
    assert "stale_output.txt" in (tree / "report" / "upgrade.txt").read_text(encoding="utf-8")


def test_in_place_upgrade_with_prune_refuses_when_a_namespace_pulled_nothing(tmp_path):
    tree = upgraded_tree(tmp_path)
    before = files(tree)
    r = run("--upgrade", tree, "-o", tree, "--prune", "--oc", fake_oc(tmp_path), ok=False,
            env={"FAKE_OC_DIR": str(partial_cluster(tmp_path, "prd"))})
    assert r.returncode != 0 and "nothing was pulled for prd" in r.stderr
    assert files(tree) == before


def test_stale_files_are_listed_once(tmp_path):
    tree = upgraded_tree(tmp_path)
    stale = engine.find_stale_output(tree, {"dev": set(), "dev/black": set(), "dev/yellow": set()}, set())
    assert len(stale) == len(set(stale))


# 2 ------------------------------------------------------------------------
def test_in_place_upgrade_finishes_when_a_release_is_gone_from_the_cluster(tmp_path):
    tree = upgraded_tree(tmp_path)
    old = tree / "dev" / "yellow" / "values" / "ms1.yaml"
    old.write_text("env:\n  FEATURE_FLAG: 'on'\n", encoding="utf-8")  # an earlier run's shorthand
    r = run("--upgrade", tree, "-o", tree, "--oc", fake_oc(tmp_path),
            env={"FAKE_OC_DIR": str(partial_cluster(tmp_path, "dev/yellow-ms1"))})
    assert "converter bug" not in r.stderr
    assert old.exists()  # reported stale, not deleted
    assert "dev/yellow/values/ms1.yaml" in (tree / "report" / "stale_output.txt").read_text(encoding="utf-8").replace("\\", "/")


# 3 ------------------------------------------------------------------------
def test_pull_only_into_a_values_tree_is_refused(tmp_path):
    tree = upgraded_tree(tmp_path)
    before = files(tree)
    r = run("--upgrade", tree, "--pull-only", "-o", tree, "--oc", fake_oc(tmp_path), ok=False,
            env={"FAKE_OC_DIR": str(FIX / "v3.1.0" / "grouped" / "cluster")})
    assert r.returncode != 0 and "is a values tree" in r.stderr
    assert files(tree) == before


def test_pull_only_will_not_mix_into_a_namespace_already_there(tmp_path):
    dump = tmp_path / "dump"
    run("--from-yaml", FLAT_IN, "--pull-only", "-o", dump, "-n", "dev")
    run("--from-yaml", FLAT_IN, "--pull-only", "-o", dump, "-n", "prd")  # another namespace: fine
    r = run("--from-yaml", FLAT_IN, "--pull-only", "-o", dump, "-n", "dev", ok=False)
    assert r.returncode != 0 and "already holds dev" in r.stderr


# 4 ------------------------------------------------------------------------
def test_report_dir_keeps_what_is_already_in_it(tmp_path):
    keep = tmp_path / "keep"
    keep.mkdir()
    (keep / "notes.txt").write_text("mine", encoding="utf-8")
    run("--from-yaml", FLAT_IN, "-o", tmp_path / "out", "--report", keep)
    assert (keep / "notes.txt").read_text(encoding="utf-8") == "mine"
    assert (keep / "render_conflicts.txt").is_file()
    r = run("--from-yaml", FLAT_IN, "-o", tmp_path / "out2", "--report", tmp_path / "out2", ok=False)
    assert r.returncode != 0 and "--report must not be the output folder" in r.stderr


# 5 ------------------------------------------------------------------------
def test_env_lint_ignores_unrelated_yaml_under_the_output(tmp_path):
    out = tmp_path / "repo"
    (out / ".github" / "workflows").mkdir(parents=True)
    (out / ".github" / "workflows" / "ci.yaml").write_text("env: {NODE_VERSION: '20'}\n", encoding="utf-8")
    run("--input", FLAT_IN, "-o", out, "--env-suffixes", "dev,prd")
    assert (out / "base" / "ms1.yaml").is_file()


# 6 ------------------------------------------------------------------------
def test_values_editor_set_replaces_a_valuefrom_in_the_same_file(tmp_path):
    app = tmp_path / "app"
    ed = [sys.executable, str(REPO / "tools" / "values_editor.py")]
    subprocess.run(ed + ["new", "--app-dir", str(app), "--namespace", "dev", "--name", "api",
                         "--image", "registry.example.org/api:1.0"], check=True, capture_output=True)
    vf = app / "dev" / "values" / "api.yaml"
    doc = yaml.safe_load(vf.read_text(encoding="utf-8"))
    doc["env"] = {"FOO": {"valueFrom": {"secretKeyRef": {"name": "s", "key": "k"}}}}
    vf.write_text(yaml.safe_dump(doc), encoding="utf-8")
    subprocess.run(ed + ["set", "--app-dir", str(app), "--namespace", "dev", "--name", "api",
                         "--key", "env.FOO", "--value", "bar"], check=True, capture_output=True)
    assert yaml.safe_load(vf.read_text(encoding="utf-8"))["env"]["FOO"] == {"value": "bar"}


# 7, 8 ---------------------------------------------------------------------
def _layout(root: Path, files_: dict) -> None:
    for rel, text in files_.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")


def test_place_like_gives_an_instance_its_base_in_the_sub_folder(tmp_path):
    old, out = tmp_path / "old", tmp_path / "out"
    _layout(old, {"base/team-a/x.yaml": "a: 1\n", "dev/defaults.yaml": "{}\n",
                  "dev/values/team-a/x-1.yaml": "BaseFile: x\ninstance: '1'\n"})
    _layout(out, {"base/x.yaml": "a: 1\n", "dev/defaults.yaml": "{}\n",
                  "dev/values/x-1.yaml": "BaseFile: x\ninstance: '1'\n"})
    upgrade.place_like(upgrade.OldTree(old), out)
    assert (out / "dev/values/team-a/x-1.yaml").is_file()
    assert (out / "base/team-a/x.yaml").is_file() and not (out / "base/x.yaml").exists()


def test_place_like_keeps_a_flat_base_another_folder_still_reads(tmp_path):
    old, out = tmp_path / "old", tmp_path / "out"
    _layout(old, {"base/ms2.yaml": "a: 1\n", "base/team-a/ms2.yaml": "a: 1\n",
                  "dev/defaults.yaml": "{}\n", "dev/values/team-a/ms2.yaml": "{}\n",
                  "prd/defaults.yaml": "{}\n", "prd/values/ms2.yaml": "{}\n"})
    _layout(out, {"base/ms2.yaml": "a: 1\n", "dev/defaults.yaml": "{}\n", "dev/values/ms2.yaml": "{}\n",
                  "prd/defaults.yaml": "{}\n", "prd/values/ms2.yaml": "{}\n"})
    upgrade.place_like(upgrade.OldTree(old), out)
    assert (out / "base/ms2.yaml").is_file() and (out / "base/team-a/ms2.yaml").is_file()
    assert (out / "dev/values/team-a/ms2.yaml").is_file() and (out / "prd/values/ms2.yaml").is_file()


# 9 ------------------------------------------------------------------------
def test_values_path_of_a_september_root_applicationset(tmp_path):
    _layout(tmp_path, {"dev/defaults.yaml": "{}\n", "dev/values/a.yaml": "{}\n",
                       "root-applicationSet.yaml": yaml.safe_dump({
                           "apiVersion": "argoproj.io/v1alpha1", "kind": "ApplicationSet",
                           "metadata": {"name": "root"},
                           "spec": {"generators": [{"git": {"repoURL": "https://git.example.com/v.git",
                                                            "revision": "main",
                                                            "directories": [{"path": "apps/values/*"}]}}]}})})
    assert upgrade.recorded_settings(upgrade.OldTree(tmp_path))["values_path"] == "apps/values"


@pytest.mark.parametrize("path", ["**/defaults.yaml", "*"])
def test_values_path_is_empty_at_the_repo_root(tmp_path, path):
    key = "files" if "**" in path else "directories"
    _layout(tmp_path, {"dev/defaults.yaml": "{}\n", "dev/values/a.yaml": "{}\n",
                       "rootApplicationSet.yaml": yaml.safe_dump({
                           "kind": "ApplicationSet", "metadata": {"name": "root"},
                           "spec": {"generators": [{"git": {"repoURL": "https://git.example.com/v.git",
                                                            key: [{"path": path}]}}]}})})
    assert "values_path" not in upgrade.recorded_settings(upgrade.OldTree(tmp_path))
