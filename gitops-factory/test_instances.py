"""--instances: one release deployed several times side by side in a namespace
(x-1, x-2, x-3) becomes ONE base/x.yaml plus a small values/x-<id>.yaml per
copy carrying `instance: "<id>"` — the key ms-applicationSet reads to layer
base/x.yaml under it. Run end to end through namespaces_to_chart.py, with the
helm render-verify and the strict round-trip fidelity check on."""
import subprocess
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from convert_to_universal_chart import base_file_for  # noqa: E402


def _copies(namespaces, ids, plain=False):
    docs = []
    for ns in namespaces:
        names = [f"x-{i}" for i in ids] + (["x"] if plain else [])
        for n in names:
            i = n.rpartition("-")[2] if n != "x" else "0"
            sel = {"app.kubernetes.io/name": "x", "app.kubernetes.io/instance": n}
            docs += [
                {"apiVersion": "apps/v1", "kind": "Deployment",
                 "metadata": {"name": n, "namespace": ns, "labels": sel},
                 "spec": {"selector": {"matchLabels": sel}, "template": {"metadata": {"labels": sel}, "spec": {
                     "containers": [{"name": "x", "image": "registry.example.com/x:1.0",
                                     "ports": [{"name": "http", "containerPort": 8080}],
                                     "env": [{"name": "SHARD", "value": i},
                                             {"name": "URL", "value": f"http://{n}.{ns}.svc"}],
                                     "envFrom": [{"configMapRef": {"name": f"{n}-config"}}]}]}}}},
                {"apiVersion": "v1", "kind": "Service", "metadata": {"name": n, "namespace": ns, "labels": sel},
                 "spec": {"selector": sel, "ports": [{"name": "http", "port": 80, "targetPort": "http"}]}},
                {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": f"{n}-config", "namespace": ns},
                 "data": {"MODE": "worker", "SHARD_ID": i}},
            ]
    return yaml.safe_dump_all(docs)


def _run(tmp_path, manifests, *extra):
    src = tmp_path / "in.yaml"
    src.write_text(manifests, encoding="utf-8")
    out = tmp_path / "out"
    r = subprocess.run([sys.executable, str(HERE / "namespaces_to_chart.py"), "--from-yaml", str(src),
                        "-o", str(out), "--chart", str(HERE.parent), "--report", str(tmp_path / "report"),
                        "--verify-fidelity", "--fidelity-strict", *extra],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]
    return out


def _load(p):
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def test_copies_share_one_base(tmp_path):
    envs = ["shop-dev", "shop-prd"]
    out = _run(tmp_path, _copies(envs, [1, 2, 3]), "--instances", "x", "--env-suffixes", "dev,prd")
    assert sorted(p.name for p in (out / "base").iterdir()) == ["x.yaml"]
    base = _load(out / "base" / "x.yaml")
    assert base["fullnameOverride"] == "x-{{ .Values.instance }}"
    assert base["env"]["URL"] == "http://x-{{ .Values.instance }}.{{ .Release.Namespace }}.svc"
    assert base["configMaps"]["x-{{ .Values.instance }}-config"]["data"]["SHARD_ID"] == "{{ .Values.instance }}"
    for ns in envs:
        values_dir = out / ns / "values"
        assert sorted(p.name for p in values_dir.iterdir()) == ["x-1.yaml", "x-2.yaml", "x-3.yaml"]
        for i in ("1", "2", "3"):
            vf = values_dir / f"x-{i}.yaml"
            assert _load(vf)["instance"] == i
            assert base_file_for(out / "base", values_dir, vf) == out / "base" / "x.yaml"


def test_a_plain_release_of_the_base_name_keeps_them_apart(tmp_path):
    out = _run(tmp_path, _copies(["shop-dev"], [1, 2], plain=True), "--instances", "x")
    assert sorted(p.name for p in (out / "base").iterdir()) == ["x-1.yaml", "x-2.yaml", "x.yaml"]
    assert "instance" not in _load(out / "shop-dev" / "values" / "x-1.yaml")
    assert "--instances x: a release named 'x' exists too" in \
        (tmp_path / "report" / "conflicts_and_warnings.txt").read_text(encoding="utf-8")


def test_a_single_copy_is_left_alone(tmp_path):
    out = _run(tmp_path, _copies(["shop-dev"], [1]), "--instances", "x")
    assert sorted(p.name for p in (out / "base").iterdir()) == ["x-1.yaml"]
    assert "instance" not in _load(out / "shop-dev" / "values" / "x-1.yaml")


def test_base_file_for_reads_the_instance_key(tmp_path):
    values_dir = tmp_path / "dev" / "values"
    (values_dir / "g1").mkdir(parents=True)
    cases = {"web.yaml": ({"image": {"tag": "1"}}, "web.yaml"),
             "x-2.yaml": ({"instance": "2"}, "x.yaml"),
             "x-eu.yaml": ({"instance": "eu"}, "x.yaml"),
             "g1/x-3.yaml": ({"instance": 3}, "g1/x.yaml"),     # an unquoted id works too
             "y-1.yaml": ({"instance": "2"}, "y-1.yaml")}       # name doesn't end in -<id>: own name
    for rel, (data, want) in cases.items():
        vf = values_dir / rel
        vf.write_text(yaml.safe_dump(data), encoding="utf-8")
        assert base_file_for(tmp_path / "base", values_dir, vf) == tmp_path / "base" / want, rel
