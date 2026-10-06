"""
base/ mirrors values/ grouping sub-folders: values/folder1/ms1.yaml layers
base/folder1/ms1.yaml, which is what the ms-applicationSet chart reads.

  * a rerun leaves both moved files where they are, renders the chain and
    reports neither stale;
  * values moved into folder1 with base left flat is a BROKEN CHAIN.

Reuses the Env_Suffix_Folders fixture (ms1 in four color folders, ms2 flat).
"""
import pathlib
import shutil
import subprocess
import sys

TEST_DIR = pathlib.Path(__file__).resolve().parent
GITOPS_DIR = TEST_DIR.parents[3]
CHART_DIR = GITOPS_DIR.parent
CONVERTER_PY = GITOPS_DIR / "convert_to_universal_chart.py"
FIXTURE = TEST_DIR.parents[1] / "Env_Suffix_Folders" / "fixture"
FOLDERS = [f"{ns}/{c}" for ns in ("dev", "prd") for c in ("black", "yellow")]


def _convert(out):
    return subprocess.run(
        [sys.executable, str(CONVERTER_PY), "--input", str(FIXTURE), "--output", str(out),
         "--chart", str(CHART_DIR), "--env-group", "color=black,yellow", "--env-suffixes", "dev,prd",
         "--argocd-manifests", "true"],
        capture_output=True, text=True, errors="replace", cwd=str(GITOPS_DIR))


def _move(src, dst):
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(src, dst)


def test_base_follows_values_subfolder(tmp_path):
    out = tmp_path / "out"
    assert _convert(out).returncode == 0
    for f in FOLDERS:
        _move(out / f / "values/ms1.yaml", out / f / "values/folder1/ms1.yaml")
    _move(out / "base/ms1.yaml", out / "base/folder1/ms1.yaml")

    proc = _convert(out)
    log = proc.stdout + proc.stderr
    assert proc.returncode == 0, log
    assert "BROKEN CHAIN" not in log and "stale" not in log.lower(), log
    files = sorted(p.relative_to(out).as_posix() for p in out.rglob("ms1.yaml"))
    assert files == sorted(["base/folder1/ms1.yaml"] + [f"{f}/values/folder1/ms1.yaml" for f in FOLDERS])


def test_flat_base_under_grouped_values_is_broken(tmp_path):
    out = tmp_path / "out"
    assert _convert(out).returncode == 0
    for f in FOLDERS:
        _move(out / f / "values/ms1.yaml", out / f / "values/folder1/ms1.yaml")

    proc = _convert(out)
    assert "BROKEN CHAIN" in proc.stdout + proc.stderr
    assert proc.returncode != 0
