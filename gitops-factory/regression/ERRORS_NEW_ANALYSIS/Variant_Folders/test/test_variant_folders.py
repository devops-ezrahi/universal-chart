"""
Variant folders already in the INPUT (detected on their own): the tree is
<namespace>/<variant>/ — prd/yellow and prd/black both deploy into namespace
'prd' and share one base/ms1.yaml — and values/ may hold sub-folders purely for
grouping (values/group1/ms1.yaml).

fixture/: ms1 in prd and dev, a yellow and a black copy each, named ms1-yellow /
ms1-black in the cluster. --verify-fidelity --fidelity-strict renders every
release through the chart and diffs it against the fixture.
"""
import pathlib
import shutil
import subprocess
import sys

import yaml

TEST_DIR = pathlib.Path(__file__).resolve().parent
GITOPS_DIR = TEST_DIR.parents[3]
CHART_DIR = GITOPS_DIR.parent
CONVERTER_PY = GITOPS_DIR / "convert_to_universal_chart.py"
FIXTURE = TEST_DIR.parent / "fixture"


def _convert(inp, out):
    return subprocess.run(
        [sys.executable, str(CONVERTER_PY), "--input", str(inp), "--output", str(out),
         "--chart", str(CHART_DIR), "--env-group", "variant=yellow,black",
         "--argocd-manifests", "true", "--verify-fidelity", "--fidelity-strict"],
        capture_output=True, text=True, errors="replace", cwd=str(GITOPS_DIR))


def test_variant_tree_shares_one_base(tmp_path):
    out = tmp_path / "out"
    proc = _convert(FIXTURE, out)
    assert proc.returncode == 0, proc.stdout + proc.stderr

    files = sorted(p.relative_to(out).as_posix() for p in out.rglob("*.yaml") if "report" not in p.parts)
    assert files == [
        "base/ms1.yaml",
        "dev/black/defaults.yaml", "dev/black/values/ms1.yaml",
        "dev/yellow/defaults.yaml", "dev/yellow/values/ms1.yaml",
        "prd/black/defaults.yaml", "prd/black/values/ms1.yaml",
        "prd/yellow/defaults.yaml", "prd/yellow/values/ms1.yaml",
        "rootApplicationSet.yaml",
    ]
    base = yaml.safe_load((out / "base/ms1.yaml").read_text(encoding="utf-8"))
    assert base["fullnameOverride"] == "ms1-{{ .Values.variant }}"
    db_url = next(e for e in base["env"] if e["name"] == "DB_URL")
    assert db_url["value"] == "http://db.{{ .Release.Namespace }}.svc:5432"
    # The folder's defaults.yaml carries the group value, not the release file.
    assert yaml.safe_load((out / "prd/black/defaults.yaml").read_text(encoding="utf-8"))["variant"] == "black"

    # The root ApplicationSet walks <namespace>/<variant>/ and deploys into the namespace.
    assert (out / "rootApplication.yaml.txt").is_file()
    appset = (out / "rootApplicationSet.yaml").read_text(encoding="utf-8")
    assert "path: '**/defaults.yaml'" in appset and "value: '{{ index .path.segments 0 }}'" in appset


def test_values_subfolders_survive_a_rerun(tmp_path):
    out = tmp_path / "out"
    assert _convert(FIXTURE, out).returncode == 0
    group = out / "prd/yellow/values/group1"
    group.mkdir()
    (out / "prd/yellow/values/ms1.yaml").rename(group / "ms1.yaml")
    # base/ mirrors the values/ sub-folder, for every folder deploying ms1.
    for f in [p for p in sorted(out.rglob("ms1.yaml")) if p.parent.name != "group1"]:
        (f.parent / "group1").mkdir(exist_ok=True)
        f.rename(f.parent / "group1" / f.name)

    proc = _convert(FIXTURE, out)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert sorted(p.relative_to(out / "prd/yellow/values").as_posix()
                  for p in (out / "prd/yellow/values").rglob("*.yaml")) == ["group1/ms1.yaml"]

    # The same file name in two group folders is two Applications with one name.
    (out / "prd/yellow/values/group2").mkdir()
    shutil.copytree(out / "base/group1", out / "base/group2")
    shutil.copy(group / "ms1.yaml", out / "prd/yellow/values/group2/ms1.yaml")
    proc = _convert(FIXTURE, out)
    assert proc.returncode != 0 and "DUPLICATE RELEASE in 'prd/yellow'" in proc.stdout


def test_variants_in_one_namespace_are_checked_against_each_other(tmp_path):
    inp = tmp_path / "in"
    shutil.copytree(FIXTURE, inp)
    # black now declares yellow's object names: both land in namespace prd.
    black = inp / "prd/black/ms1.yaml"
    black.write_text(black.read_text(encoding="utf-8").replace("ms1-black", "ms1-yellow"), encoding="utf-8")
    proc = _convert(inp, tmp_path / "out")
    assert proc.returncode != 0
    assert "NAMESPACE CONFLICT in 'prd'" in proc.stdout
