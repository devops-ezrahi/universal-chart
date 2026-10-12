"""--include over sibling namespaces (goldenbell-ui / goldenbell-next) whose
app is named after each namespace: one base/ file, one small values/ file per
namespace. The namespace's last word ('ui', 'next') is taken as an environment
word on its own (sibling_env_suffixes), and the fidelity check does not count
what --include left out as lost."""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import convert_to_universal_chart as conv  # noqa: E402

NAMESPACES = {"goldenbell-ui": ("1.0", 2), "goldenbell-next": ("2.0", 3)}


def _app(ns, name, tag, replicas):
    labels = {"app": name}
    return [
        {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": name, "labels": labels},
         "spec": {"replicas": replicas, "selector": {"matchLabels": labels}, "template": {
             "metadata": {"labels": labels},
             "spec": {"containers": [{"name": name, "image": f"reg.io/goldenbell:{tag}",
                                      "ports": [{"containerPort": 8080, "protocol": "TCP"}],
                                      "env": [{"name": "LOG_LEVEL", "value": "info"}],
                                      "envFrom": [{"configMapRef": {"name": f"{name}-config"}}]}]}}}},
        {"apiVersion": "v1", "kind": "Service", "metadata": {"name": name, "labels": labels},
         "spec": {"selector": labels, "ports": [{"name": "http", "port": 80, "targetPort": 8080,
                                                 "protocol": "TCP"}]}},
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": f"{name}-config"},
         "data": {"A": "1", "URL": f"http://{ns}.example.org"}},
    ]


def _write(root: Path, files: dict) -> Path:
    """files: {namespace: {file stem: object name}}"""
    for ns, stems in files.items():
        (root / ns).mkdir(parents=True, exist_ok=True)
        tag, replicas = NAMESPACES.get(ns, ("1.0", 1))
        for stem, name in stems.items():
            (root / ns / f"{stem}.yaml").write_text(
                yaml.safe_dump_all(_app(ns, name, tag, replicas)), encoding="utf-8")
    return root


def _named_after_namespace(root: Path) -> Path:
    return _write(root, {ns: {ns: ns, "other": "other"} for ns in NAMESPACES})


def _convert(src: Path, out: Path, *flags, check=True):
    r = subprocess.run([sys.executable, str(HERE / "convert_to_universal_chart.py"), "--input", str(src),
                        "--output", str(out), "--chart", str(HERE.parent), *flags],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check:
        assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]
    return r


def _load(p: Path):
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def test_app_named_after_its_namespace_is_one_base(tmp_path):
    src = _named_after_namespace(tmp_path / "in")
    out = tmp_path / "out"
    _convert(src, out, "--skip-verify", "--include", "goldenbell")
    assert sorted(p.name for p in (out / "base").iterdir()) == ["goldenbell.yaml"]
    base = _load(out / "base" / "goldenbell.yaml")
    assert base["fullnameOverride"] == "{{ .Release.Namespace }}"
    assert base["configMaps"] == {"{{ .Release.Namespace }}-config": {"data": {
        "A": "1", "URL": "http://{{ .Release.Namespace }}.example.org"}}}
    for ns, (tag, replicas) in NAMESPACES.items():
        assert sorted(p.name for p in (out / ns / "values").iterdir()) == ["goldenbell.yaml"]
        # Only what really differs: the image tag and the replica count.
        assert _load(out / ns / "values" / "goldenbell.yaml") == {
            "image": {"repository": "reg.io/goldenbell", "tag": tag}, "replicaCount": replicas}
        assert _load(out / ns / "defaults.yaml") == {"environment": ns.rsplit("-", 1)[1]}
    report = (out / "report" / "conflicts_and_warnings.txt").read_text(encoding="utf-8")
    assert "environment words taken from the namespace names: next, ui" in report


def test_suffixed_file_names_fold_too(tmp_path):
    """web-ui / web-next: same app, the namespace's word in the name."""
    src = _write(tmp_path / "in", {"goldenbell-ui": {"web-ui": "web-ui"},
                                   "goldenbell-next": {"web-next": "web-next"}})
    out = tmp_path / "out"
    _convert(src, out, "--skip-verify", "--include", "web")
    assert sorted(p.name for p in (out / "base").iterdir()) == ["web.yaml"]
    for ns in NAMESPACES:
        assert sorted(p.name for p in (out / ns / "values").iterdir()) == ["web.yaml"]


def test_same_name_everywhere_is_unchanged(tmp_path):
    src = _write(tmp_path / "in", {ns: {"web": "web", "api": "api"} for ns in NAMESPACES})
    out = tmp_path / "out"
    _convert(src, out, "--skip-verify", "--include", "=web")
    assert sorted(p.name for p in (out / "base").iterdir()) == ["web.yaml"]
    for ns in NAMESPACES:
        assert sorted(p.name for p in (out / ns / "values").iterdir()) == ["web.yaml"]
        assert _load(out / ns / "defaults.yaml") == {}  # no evidence, no environment word


def test_auto_env_suffixes_false_keeps_them_apart(tmp_path):
    src = _named_after_namespace(tmp_path / "in")
    out = tmp_path / "out"
    _convert(src, out, "--skip-verify", "--include", "goldenbell", "--auto-env-suffixes", "false")
    assert sorted(p.name for p in (out / "base").iterdir()) == ["goldenbell-next.yaml", "goldenbell-ui.yaml"]


def test_explicit_env_suffixes_replace_the_guess(tmp_path):
    src = _named_after_namespace(tmp_path / "in")
    out = tmp_path / "out"
    r = _convert(src, out, "--skip-verify", "--env-suffixes", "prd")
    assert "Environment words from the namespace names" not in r.stdout
    assert "goldenbell-ui.yaml" in {p.name for p in (out / "base").iterdir()}


def test_lone_suffixed_file_keeps_its_name(tmp_path):
    """admin-ui only in goldenbell-ui: nothing else is 'admin', so no fold."""
    src = _named_after_namespace(tmp_path / "in")
    _write(src, {"goldenbell-ui": {"admin-ui": "admin-ui"}})
    out = tmp_path / "out"
    _convert(src, out, "--skip-verify")
    assert sorted(p.name for p in (out / "base").iterdir()) == ["admin-ui.yaml", "goldenbell.yaml", "other.yaml"]


def test_include_matching_nothing_fails_and_writes_nothing(tmp_path):
    src = _named_after_namespace(tmp_path / "in")
    out = tmp_path / "out"
    r = _convert(src, out, "--skip-verify", "--include", "goldenbell-prd-nothing", check=False)
    assert r.returncode != 0
    assert "no object" in r.stderr and "matches" in r.stderr
    assert not out.exists()


@pytest.mark.parametrize("files, expected", [
    ({"goldenbell-ui": ["goldenbell-ui"], "goldenbell-next": ["goldenbell-next"]}, {"ui", "next"}),
    ({"shop-dev": ["pay-dev"], "shop-prd": ["pay-prd"]}, {"dev", "prd"}),
    # Different apps: platform-data's postgres and platform-ops' log-agent.
    ({"platform-data": ["postgres"], "platform-ops": ["log-agent"]}, set()),
    # The word in only one namespace's file names: no second copy to share with.
    ({"goldenbell-ui": ["admin-ui"], "goldenbell-next": ["web"]}, set()),
    ({"plain": ["plain"], "other": ["other"]}, set()),
])
def test_sibling_env_suffixes(tmp_path, files, expected):
    for ns, stems in files.items():
        (tmp_path / ns).mkdir()
        for stem in stems:
            (tmp_path / ns / f"{stem}.yaml").write_text("{}\n", encoding="utf-8")
    dirs = [(ns, tmp_path / ns) for ns in files]
    assert conv.sibling_env_suffixes(dirs) == expected


@pytest.mark.skipif(shutil.which("helm") is None, reason="needs helm")
@pytest.mark.parametrize("flags", [["--include", "goldenbell"], ["--include", "=goldenbell-ui,=goldenbell-next"],
                                   ["--exclude", "=other"], []])
def test_selection_round_trips_strictly(tmp_path, flags):
    """Rendered back, the selected app is byte-for-byte the source, and the
    files --include left out are not reported as lost."""
    src = _named_after_namespace(tmp_path / "in")
    out = tmp_path / "out"
    _convert(src, out, "--verify-fidelity", "--fidelity-strict", *flags)
    report = (out / "report" / "fidelity_report.txt").read_text(encoding="utf-8")
    assert "fields LOST (raw -> render): 0" in report
    assert "objects in raw but rendered by nothing:      0" in report
    if flags:
        assert "left out by --include / --exclude — not checked" in report
