"""
--env-group color=black,yellow --env-suffixes dev,prd on flat namespaces dev/
and prd/, each holding ms1-yellow.yaml, ms1-black.yaml and ms2.yaml:

  * a color token in a file name moves the release into a folder of that name
    inside its namespace (dev/yellow/values/ms1.yaml, release 'ms1');
  * a release without one stays in the namespace folder (dev/values/ms2.yaml);
  * env suffixes never make folders; values differing by them become
    {{ .Values.environment }}, set in every folder's defaults.yaml next to the
    folder's own 'color';
  * one base/ms1.yaml serves all four ms1 copies, layered base -> defaults ->
    values, and renders back to the raw manifests (--fidelity-strict).
"""
import pathlib
import subprocess
import sys

import yaml

TEST_DIR = pathlib.Path(__file__).resolve().parent
GITOPS_DIR = TEST_DIR.parents[3]
CHART_DIR = GITOPS_DIR.parent
CONVERTER_PY = GITOPS_DIR / "convert_to_universal_chart.py"
FIXTURE = TEST_DIR.parent / "fixture"


def _load(path):
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def test_color_folders_and_environment_values(tmp_path):
    out = tmp_path / "out"
    proc = subprocess.run(
        [sys.executable, str(CONVERTER_PY), "--input", str(FIXTURE), "--output", str(out),
         "--chart", str(CHART_DIR), "--env-group", "color=black,yellow", "--env-suffixes", "dev,prd",
         "--argocd-manifests", "true", "--verify-fidelity", "--fidelity-strict"],
        capture_output=True, text=True, errors="replace", cwd=str(GITOPS_DIR))
    assert proc.returncode == 0, proc.stdout + proc.stderr

    files = sorted(p.relative_to(out).as_posix() for p in out.rglob("*.yaml") if "report" not in p.parts)
    folders = [f"{ns}/{c}/" for ns in ("dev", "prd") for c in ("black", "yellow")]
    assert files == sorted(
        ["base/ms1.yaml", "base/ms2.yaml", "rootApplicationSet.yaml"]
        + [f"{ns}/defaults.yaml" for ns in ("dev", "prd")]
        + [f"{ns}/values/ms2.yaml" for ns in ("dev", "prd")]
        + [f + "defaults.yaml" for f in folders] + [f + "values/ms1.yaml" for f in folders])

    base = _load(out / "base/ms1.yaml")
    assert base["fullnameOverride"] == "ms1-{{ .Values.color }}"
    assert base["env"]["DB_URL"]["value"] == "http://db.{{ .Values.environment }}.svc:5432"
    assert _load(out / "dev/yellow/defaults.yaml") == {"color": "yellow", "environment": "dev"}
    assert _load(out / "prd/defaults.yaml")["environment"] == "prd"
    # The folder carries the group values; the release file does not repeat them.
    assert "color" not in _load(out / "prd/black/values/ms1.yaml")

    assert (out / "rootApplication.yaml.txt").is_file()
    appset = (out / "rootApplicationSet.yaml").read_text(encoding="utf-8")
    assert "path: '**/defaults.yaml'" in appset and "goTemplate: true" in appset
