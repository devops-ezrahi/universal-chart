"""Per-namespace auto-sync: the root ApplicationSet reads `autoSync` from each
folder's defaults.yaml, falling back to the tree's --auto-sync, and
--namespace-auto-sync writes that key so it survives a reconversion."""
import subprocess
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
sys.path.insert(0, str(HERE))
from converter import upgrade  # noqa: E402
from test_namespace_cluster import REPO, deployment, params, run  # noqa: E402


def test_root_reads_autosync_per_folder_with_the_tree_default(tmp_path):
    out, load = run(tmp_path)
    assert params(load)["autoSync"] == '{{ dig "autoSync" "false" . | toString }}'
    assert "autoSync" not in load("shop-dev/defaults.yaml")


def test_tree_default_true_is_the_fallback(tmp_path):
    out, load = run(tmp_path, "--auto-sync", "true")
    assert params(load)["autoSync"] == '{{ dig "autoSync" "true" . | toString }}'


def test_namespace_auto_sync_writes_each_folder_of_that_namespace(tmp_path):
    out, load = run(tmp_path, "--auto-sync", "true", "--namespace-auto-sync", "shop-prd=false",
                    "--namespace-auto-sync", "shop-tst=True")
    assert load("shop-prd/defaults.yaml")["autoSync"] is False
    assert load("shop-prd/yellow/defaults.yaml")["autoSync"] is False
    assert load("shop-prd/black/defaults.yaml")["autoSync"] is False
    assert load("shop-tst/defaults.yaml")["autoSync"] is True
    assert "autoSync" not in load("shop-dev/defaults.yaml")
    # An upgrade of that tree carries both the tree default and the overrides over.
    rec = upgrade.recorded_settings(upgrade.OldTree(out))
    assert rec["auto_sync"] is True
    assert sorted(rec["namespace_auto_sync"]) == ["shop-prd=false", "shop-tst=true"]


def test_a_bad_spec_is_refused(tmp_path):
    d = tmp_path / "in" / "shop-dev"
    d.mkdir(parents=True)
    (d / "api.yaml").write_text(yaml.safe_dump(deployment("api", "shop-dev")), encoding="utf-8")
    r = subprocess.run([sys.executable, str(REPO / "convert_to_universal_chart.py"), "--input", str(tmp_path / "in"),
                        "--output", str(tmp_path / "out"), "--skip-verify", "--namespace-auto-sync", "shop-dev"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode != 0
    assert "--namespace-auto-sync" in r.stderr
