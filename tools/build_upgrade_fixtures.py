"""Build tests/converter/upgrade/<version>/<corpus>/{tree,cluster}: the tree an
old converter wrote, hand-edited in that version's own env format, and what it
deploys — rendered with that version's own chart, stamped as Argo CD stamps.

    python tools/build_upgrade_fixtures.py HISTORY

HISTORY holds src/<version>/ (a `git archive` of this repo at that version) and
trees/<version>/<corpus>/ (that version's converter run over
examples/converted/<corpus>/input, with --env-suffixes dev,prd, --env-group
color=black,yellow for grouped, and --argocd-manifests)."""
import json
import shutil
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from converter import engine, upgrade  # noqa: E402

H = Path(sys.argv[1]) if len(sys.argv) > 1 else sys.exit(__doc__)
OUT = REPO / "tests" / "converter" / "upgrade"
VERSIONS = {"pre-2026-09-20": "e7abf677", "v1.0.0": "v1.0.0", "v1.4.0": "v1.4.0", "v2.0.0": "v2.0.0",
            "v2.4.0": "v2.4.0", "v3.0.0": "v3.0.0", "v3.1.0": "v3.1.0"}


def load(p):
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def dump(p, d):
    p.write_text(yaml.safe_dump(d, sort_keys=False, allow_unicode=True), encoding="utf-8")


def set_env(values: dict, base_env, name: str, value: str):
    """LOG_LEVEL override in the tree's own env format."""
    if isinstance(base_env, list):  # v2.x: a list replaces the lower one whole
        lst = [dict(e) for e in base_env]
        for e in lst:
            if e.get("name") == name:
                e.clear(); e.update({"name": name, "value": value})
        if not any(e.get("name") == name for e in lst):
            lst.append({"name": name, "value": value})
        values["env"] = lst
    elif isinstance(base_env, dict) and any(isinstance(v, dict) and "value" in v for v in base_env.values()):
        values.setdefault("env", {})[name] = {"value": value}  # v1.x explicit map
    else:
        values.setdefault("env", {})[name] = value  # v3.x shorthand


def edit(tree: Path, version: str) -> dict:
    """Hand edits a person would make after the conversion. Returns what to expect."""
    t = upgrade.OldTree(tree)

    def ms1_of(prefix):
        for f in t.folders:
            if f.startswith(prefix):
                rel = next((r for r in sorted(t.releases[f]) if r.startswith("ms1")), None)
                if rel:
                    return f, rel
        raise SystemExit(f"no ms1 release under {prefix}")

    dev_ms1, dev_rel = ms1_of("dev")
    prd_ms1, prd_rel = ms1_of("prd")
    vf = t.folders[dev_ms1] / "values" / t.releases[dev_ms1][dev_rel]
    v = load(vf)
    v.setdefault("image", {})["tag"] = "9.9.9"
    # the format of the env this file already has, else of base/'s
    bf = tree / "base" / f"{dev_rel}.yaml"
    base_env = v.get("env") if v.get("env") else (load(bf).get("env") if bf.exists() else None)
    set_env(v, base_env, "LOG_LEVEL", "trace")
    dump(vf, v)
    pf = t.folders[prd_ms1] / "values" / t.releases[prd_ms1][prd_rel]
    p = load(pf)
    p["replicaCount"] = 3
    dump(pf, p)
    moved = []
    if version != "pre-2026-09-20":  # values/ sub-folders arrived with v1.0.0
        for folder in t.folders:
            if "ms2" in t.releases[folder]:
                src = t.folders[folder] / "values" / "ms2.yaml"
                (src.parent / "team-a").mkdir(exist_ok=True)
                shutil.move(str(src), str(src.parent / "team-a" / "ms2.yaml"))
                moved.append(f"{folder}/values/team-a/ms2.yaml")
        (tree / "base" / "team-a").mkdir(exist_ok=True)
        shutil.move(str(tree / "base" / "ms2.yaml"), str(tree / "base" / "team-a" / "ms2.yaml"))
    return {"dev_ms1": dev_ms1, "dev_rel": dev_rel, "prd_ms1": prd_ms1, "prd_rel": prd_rel, "moved": moved}


if OUT.exists():
    shutil.rmtree(OUT)
for version, rev in VERSIONS.items():
    for corpus in ("flat", "grouped"):
        src = H / "trees" / version / corpus
        d = OUT / version / corpus
        shutil.copytree(src, d / "tree", ignore=shutil.ignore_patterns("report"))
        expect = edit(d / "tree", version)
        cluster = d / "cluster"
        cluster.mkdir()
        old = upgrade.OldTree(d / "tree")
        warnings = upgrade.render_tree(old, H / "src" / version, cluster)
        assert not warnings, warnings
        # What Argo CD stamps on each object it applies (annotation tracking,
        # the Argo CD 3 default): the owning Application, which is how the live
        # upgrade keeps every object in the release that deployed it.
        for folder, rels in old.releases.items():
            for stem in rels:
                f = cluster / folder / f"{stem}.yaml"
                app = engine.argo_release_name(folder, stem)
                docs = [x for x in yaml.safe_load_all(f.read_text(encoding="utf-8")) if x]
                for x in docs:
                    m = x.setdefault("metadata", {})
                    group = (x.get("apiVersion", "").split("/")[0] if "/" in x.get("apiVersion", "") else "")
                    m.setdefault("annotations", {})["argocd.argoproj.io/tracking-id"] =                         f"{app}:{group}/{x['kind']}:{m.get('namespace') or engine.k8s_ns(folder)}/{m['name']}"
                    m["namespace"] = m.get("namespace") or engine.k8s_ns(folder)
                f.write_text(yaml.safe_dump_all(docs, sort_keys=False, allow_unicode=True), encoding="utf-8")
        # The fake oc reads <namespace>/*.yaml: variant folders deploy into their namespace.
        for f in sorted(cluster.rglob("*.yaml")):
            rel = f.relative_to(cluster)
            if len(rel.parts) > 2:
                dst = cluster / rel.parts[0] / ("-".join(rel.parts[1:-1]) + "-" + rel.name)
                shutil.move(str(f), str(dst))
        for x in sorted(cluster.rglob("*"), key=lambda p: -len(p.parts)):
            if x.is_dir() and not any(x.iterdir()):
                x.rmdir()
        expect["rev"] = rev
        (d / "expect.json").write_text(json.dumps(expect, indent=1), encoding="utf-8")
        n = sum(1 for _ in d.rglob("*") if _.is_file())
        print(f"{version} {corpus}: {n} files, dev={expect['dev_ms1']} prd={expect['prd_ms1']} moved={len(expect['moved'])}")
