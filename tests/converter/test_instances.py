"""--instances: one release deployed several times side by side in a namespace
(x-1, x-2, x-3, or alpha, beta, gamma) becomes ONE base/x.yaml plus a small
values file per copy carrying `BaseFile: x` (the key ms-applicationSet reads to
layer base/x.yaml under it) and `instance: <id>` (what base/x.yaml fills in).
Run end to end through namespaces_to_chart.py, with the helm render-verify and
the strict round-trip fidelity check on."""
import subprocess
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]  # the universal-chart checkout
sys.path.insert(0, str(REPO))
from converter.engine import base_file_for  # noqa: E402


def _copies(namespaces, ids, plain=False, names=None):
    docs = []
    for ns in namespaces:
        for n in (names or [f"x-{i}" for i in ids]) + (["x"] if plain else []):
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
    r = subprocess.run([sys.executable, str(REPO / "converter" / "sources.py"), "--from-yaml", str(src),
                        "-o", str(out), "--chart", str(REPO), "--report", str(tmp_path / "report"),
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
            assert _load(vf)["BaseFile"] == "x"
            assert _load(vf)["instance"] == i
            assert base_file_for(out / "base", values_dir, vf) == out / "base" / "x.yaml"


def test_copies_with_unrelated_names_share_one_base(tmp_path):
    """Names that are not x-<id> at all: `--instances x=alpha,beta,gamma`. Each
    copy's instance is its whole name, so `alpha-config` becomes
    `{{ .Values.instance }}-config`; a release not listed (web) is untouched."""
    names = ["alpha", "beta", "gamma"]
    web = yaml.safe_dump({"apiVersion": "apps/v1", "kind": "Deployment",
                          "metadata": {"name": "web", "namespace": "shop-dev"},
                          "spec": {"selector": {"matchLabels": {"app": "web"}}, "template": {
                              "metadata": {"labels": {"app": "web"}},
                              "spec": {"containers": [{"name": "web", "image": "nginx:1.27"}]}}}})
    out = _run(tmp_path, _copies(["shop-dev"], [], names=names) + "---\n" + web,
               "--instances", "x=alpha,beta,gamma")
    assert sorted(p.name for p in (out / "base").iterdir()) == ["web.yaml", "x.yaml"]
    base = _load(out / "base" / "x.yaml")
    assert base["fullnameOverride"] == "{{ .Values.instance }}"
    assert "{{ .Values.instance }}-config" in base["configMaps"]
    values_dir = out / "shop-dev" / "values"
    assert sorted(p.name for p in values_dir.iterdir()) == ["alpha.yaml", "beta.yaml", "gamma.yaml", "web.yaml"]
    for n in names:
        vf = values_dir / f"{n}.yaml"
        assert (_load(vf)["BaseFile"], _load(vf)["instance"]) == ("x", n)
        assert base_file_for(out / "base", values_dir, vf) == out / "base" / "x.yaml"
    assert "BaseFile" not in _load(values_dir / "web.yaml")


def test_a_named_copy_that_does_not_exist_is_reported(tmp_path):
    _run(tmp_path, _copies(["shop-dev"], [], names=["alpha", "beta"]), "--instances", "x=alpha,beta,nope")
    assert "no release named nope" in (tmp_path / "report" / "conflicts_and_warnings.txt").read_text(encoding="utf-8")


def test_a_plain_release_of_the_base_name_keeps_them_apart(tmp_path):
    out = _run(tmp_path, _copies(["shop-dev"], [1, 2], plain=True), "--instances", "x")
    assert sorted(p.name for p in (out / "base").iterdir()) == ["x-1.yaml", "x-2.yaml", "x.yaml"]
    assert "BaseFile" not in _load(out / "shop-dev" / "values" / "x-1.yaml")
    assert "--instances x: a release named 'x' exists too" in \
        (tmp_path / "report" / "conflicts_and_warnings.txt").read_text(encoding="utf-8")


def test_a_single_copy_is_left_alone(tmp_path):
    out = _run(tmp_path, _copies(["shop-dev"], [1]), "--instances", "x")
    assert sorted(p.name for p in (out / "base").iterdir()) == ["x-1.yaml"]
    assert "BaseFile" not in _load(out / "shop-dev" / "values" / "x-1.yaml")


def test_base_file_for_reads_basefile(tmp_path):
    values_dir = tmp_path / "dev" / "values"
    (values_dir / "g1").mkdir(parents=True)
    cases = {"web.yaml": ({"image": {"tag": "1"}}, "web.yaml"),
             "x-2.yaml": ({"BaseFile": "x", "instance": "2"}, "x.yaml"),
             "alpha.yaml": ({"BaseFile": "x"}, "x.yaml"),
             "beta.yaml": ({"BaseFile": "x.yaml"}, "x.yaml"),       # with the extension too
             "g1/gamma.yaml": ({"BaseFile": "x"}, "g1/x.yaml"),     # base/ mirrors the sub-folder
             "y-1.yaml": ({"instance": "1"}, "y-1.yaml")}           # instance alone wires nothing
    for rel, (data, want) in cases.items():
        vf = values_dir / rel
        vf.write_text(yaml.safe_dump(data), encoding="utf-8")
        assert base_file_for(tmp_path / "base", values_dir, vf) == tmp_path / "base" / want, rel
    assert base_file_for(tmp_path / "base", values_dir, values_dir / "missing.yaml") == tmp_path / "base" / "missing.yaml"
