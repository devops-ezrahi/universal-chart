"""--upgrade: trees written by every era of the converter, brought up to date.

tests/converter/upgrade/<version>/<corpus>/ holds, per version:
  tree/      what that version's converter wrote for examples/converted/<corpus>/input,
             then hand-edited the way a person would (dev ms1 image tag 9.9.9 and
             LOG_LEVEL=trace in that version's own env format, prd ms1 replicas 3,
             ms2 moved into values/team-a/ from v1.0.0 on)
  cluster/   what that tree deploys: rendered with that version's OWN chart, each
             object carrying the Argo CD tracking stamp of the Application that
             applied it — the live cluster fake_oc.py answers from
  expect.json  where the edits are
(built by tools/build_upgrade_fixtures.py).

Live upgrades run everywhere. Offline upgrades need this repo's history (they
render with the chart revision of the tree's time) and skip without it.
"""
import filecmp
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
CMD = REPO / "convert_to_universal_chart.py"
FIX = HERE / "upgrade"
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(HERE))
from converter import engine, upgrade  # noqa: E402
from test_sources import fake_oc  # noqa: E402

CASES = sorted((v.name, c.name) for v in FIX.iterdir() if v.is_dir() for c in v.iterdir() if c.is_dir())
IDS = [f"{v}-{c}" for v, c in CASES]
def flags(version, corpus):
    """Nothing: every option comes from the old tree. (Passing --env-group to the
    September grouped tree would reshape dev/values/ms1-black.yaml into
    dev/black/values/ms1.yaml — a new Argo CD Application name, so a delete and
    recreate; the upgrade keeps the old structure unless asked otherwise.)"""
    return []


def run(*args, env=None, ok=True):
    r = subprocess.run([sys.executable, str(CMD), *map(str, args)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env={**os.environ, "PYTHONIOENCODING": "utf-8", **(env or {})})
    if ok:
        assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-4000:]
    return r


def has_rev(rev):
    return subprocess.run(["git", "-C", str(REPO), "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"],
                          capture_output=True).returncode == 0


def effective(out: Path, folder: str, release: str) -> dict:
    """base -> folder defaults -> values, merged as Argo CD layers them."""
    t = upgrade.OldTree(out)
    vf = t.folders[folder] / "values" / t.releases[folder][release]
    chain = [engine.base_file_for(out / "base", t.folders[folder] / "values", vf), t.folders[folder] / "defaults.yaml", vf]
    merged: dict = {}
    for f in chain:
        if f.exists():
            merged = engine.deep_merge(merged, yaml.safe_load(f.read_text(encoding="utf-8")) or {})
    return merged


def tree_diff(a: Path, b: Path) -> list[str]:
    # rootApplication.yaml.txt's header names its own output path
    cmp = filecmp.dircmp(a, b, ignore=["report", "rootApplication.yaml.txt"])
    out = [f"only in {a.name}: {x}" for x in cmp.left_only] + [f"only in {b.name}: {x}" for x in cmp.right_only]
    out += [f"differs: {x}" for x in cmp.common_files
            if (a / x).read_bytes().replace(b"\r\n", b"\n") != (b / x).read_bytes().replace(b"\r\n", b"\n")]
    for d in cmp.common_dirs:
        out += [f"{d}/{x}" for x in tree_diff(a / d, b / d)]
    return out


def check(out: Path, version: str, corpus: str, r) -> None:
    """What every upgrade must give, whichever way it read the old tree."""
    exp = json.loads((FIX / version / corpus / "expect.json").read_text(encoding="utf-8"))
    report = (out / "report" / "upgrade.txt").read_text(encoding="utf-8")
    assert "Same releases in every folder." in report, report
    # The hand edits survived, each where the old tree had it.
    dev = effective(out, exp["dev_ms1"], exp["dev_rel"])
    assert dev["image"]["tag"] == "9.9.9"
    assert dev["env"]["LOG_LEVEL"] == {"value": "trace"}, dev.get("env")
    assert effective(out, exp["prd_ms1"], exp["prd_rel"])["replicaCount"] == 3
    for moved in exp["moved"]:
        assert (out / moved).is_file(), moved
    if exp["moved"]:
        assert (out / "base" / "team-a" / "ms2.yaml").is_file()
    # Today's format, and nothing Argo CD stamped carried into values.
    assert engine.lint_env_shape(out) == []
    text = "".join(f.read_text(encoding="utf-8") for f in out.rglob("*.yaml") if "report" not in f.parts)
    assert "tracking-id" not in text
    # The new tree deploys what the old one did (--verify-fidelity, on for --upgrade),
    # for every era — the September chart's Helm labels on Ingress objects included.
    fid = (out / "report" / "fidelity_report.txt").read_text(encoding="utf-8")
    lost = [l for l in fid.splitlines() if l.strip().startswith("ERROR ") and "/" in l.split()[1]]
    assert not lost, "\n".join(lost[:10])


@pytest.mark.parametrize("version,corpus", CASES, ids=IDS)
def test_live_upgrade(tmp_path, version, corpus):
    d = FIX / version / corpus
    out = tmp_path / "out"
    r = run("--upgrade", d / "tree", "--oc", fake_oc(tmp_path), "-o", out, *flags(version, corpus),
            "--fidelity-strict",
            env={"FAKE_OC_DIR": str(d / "cluster")})
    check(out, version, corpus, r)


@pytest.mark.parametrize("version,corpus", CASES, ids=IDS)
def test_offline_upgrade_matches_the_live_one(tmp_path, version, corpus):
    d = FIX / version / corpus
    rev = json.loads((d / "expect.json").read_text(encoding="utf-8"))["rev"]
    if not has_rev(rev):
        pytest.skip(f"no {rev} in this checkout (shallow clone)")
    off = tmp_path / "offline"
    r = run("--upgrade", d / "tree", "--upgrade-offline", "--upgrade-chart-rev", rev, "-o", off,
            *flags(version, corpus), "--fidelity-strict")
    check(off, version, corpus, r)
    live = tmp_path / "live"
    run("--upgrade", d / "tree", "--oc", fake_oc(tmp_path), "-o", live, *flags(version, corpus),
        env={"FAKE_OC_DIR": str(d / "cluster")})
    assert tree_diff(live, off) == []


def test_offline_reads_the_chart_revision_from_the_trees_git_date(tmp_path):
    """No --upgrade-chart-rev: the tree's own last commit date picks the chart."""
    if not has_rev("v2.0.0"):
        pytest.skip("needs this repo's history")
    tree = tmp_path / "values-repo"
    shutil.copytree(FIX / "v2.0.0" / "flat" / "tree", tree)
    date = subprocess.run(["git", "-C", str(REPO), "log", "-1", "--format=%cI", "v2.0.0"],
                          capture_output=True, text=True).stdout.strip()
    git = ["git", "-C", str(tree), "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run(git[:3] + ["init", "-q"], check=True)
    subprocess.run(git + ["add", "-A"], check=True)
    subprocess.run(git + ["commit", "-q", "-m", "old tree"], check=True,
                   env={**os.environ, "GIT_COMMITTER_DATE": date, "GIT_AUTHOR_DATE": date})
    r = run("--upgrade", tree, "--upgrade-offline", "-o", tmp_path / "out", "--fidelity-strict")
    assert "rendering the old tree with chart commit" in r.stderr
    check(tmp_path / "out", "v2.0.0", "flat", r)


def test_in_place_upgrade_rewrites_the_tree_and_prunes_the_old_layout(tmp_path):
    """The September layout had root-application*.yaml; in place, the new tree
    replaces it and nothing of the old layout is left behind."""
    if not has_rev("e7abf677"):
        pytest.skip("needs this repo's history")
    tree = tmp_path / "tree"
    shutil.copytree(FIX / "pre-2026-09-20" / "grouped" / "tree", tree)
    assert (tree / "root-application.yaml").exists()
    run("--upgrade", tree, "--upgrade-offline", "--upgrade-chart-rev", "e7abf677", "-o", tree)
    assert not (tree / "root-application.yaml").exists() and not (tree / "root-applicationSet.yaml").exists()
    assert (tree / "rootApplicationSet.yaml").exists()
    check(tree, "pre-2026-09-20", "grouped", None)


def test_upgrading_an_upgraded_tree_changes_nothing(tmp_path):
    d = FIX / "v3.1.0" / "grouped"
    once, twice = tmp_path / "once", tmp_path / "twice"
    run("--upgrade", d / "tree", "--oc", fake_oc(tmp_path), "-o", once, env={"FAKE_OC_DIR": str(d / "cluster")})
    # Deploy `once` (render it with today's chart, stamped as Argo CD would) and upgrade again.
    cluster = tmp_path / "cluster"
    tree = upgrade.OldTree(once)
    assert upgrade.render_tree(tree, REPO, cluster) == []
    for folder, rels in tree.releases.items():
        for stem in rels:
            f = cluster / folder / f"{stem}.yaml"
            docs = [x for x in yaml.safe_load_all(f.read_text(encoding="utf-8")) if x]
            for x in docs:
                m = x.setdefault("metadata", {})
                m["namespace"] = m.get("namespace") or engine.k8s_ns(folder)
                m.setdefault("annotations", {})[upgrade.TRACKING_ANNOTATION] = \
                    f"{engine.argo_release_name(folder, stem)}:/{x['kind']}:{m['namespace']}/{m['name']}"
            dest = cluster / engine.k8s_ns(folder) / ("-".join(Path(folder).parts[1:] + (f"{stem}.yaml",)))
            f.unlink()
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(yaml.safe_dump_all(docs, sort_keys=False), encoding="utf-8")
    run("--upgrade", once, "--oc", fake_oc(tmp_path), "-o", twice, "--fidelity-strict",
        env={"FAKE_OC_DIR": str(cluster)})
    assert tree_diff(once, twice) == []


def test_settings_the_old_tree_recorded_are_taken_over_and_flags_win(tmp_path):
    tree = REPO / "examples" / "converted" / "grouped" / "output"
    rec = upgrade.recorded_settings(upgrade.OldTree(tree))
    assert rec["values_repo_url"] == "https://github.com/devops-ezrahi/universal-chart-example-grouped.git"
    assert rec["root_set_name"] == "example-grouped"
    assert rec["argocd_manifests"] is True
    assert rec["env_group"] == ["color=black,yellow"]
    assert rec["env_suffixes"] == "dev,prd"
    ap = engine.build_parser()
    from converter import sources
    sources.add_arguments(ap)
    args = ap.parse_args(["--root-set-name", "mine"])
    upgrade.apply_settings(args, ap, rec)
    assert args.root_set_name == "mine" and args.values_repo_url == rec["values_repo_url"]


def test_tracking_puts_objects_back_in_the_release_that_deployed_them(tmp_path):
    """The importer would make the ms1 CronJob a release of its own; Argo CD's
    stamp says the ms1 Application deployed it, so it stays in ms1."""
    tree = upgrade.OldTree(FIX / "v2.0.0" / "grouped" / "tree")
    dump = tmp_path / "dump" / "dev"
    dump.mkdir(parents=True)
    cron = {"apiVersion": "batch/v1", "kind": "CronJob", "metadata": {"name": "ms1-report", "annotations": {
        upgrade.TRACKING_ANNOTATION: "ms1-dev-black:batch/CronJob:dev/ms1-report"}}}
    stray = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "other"}}
    (dump / "ms1-report.yaml").write_text(yaml.safe_dump_all([cron, stray]), encoding="utf-8")
    lines = upgrade.regroup_by_owner(tree, tmp_path / "dump")
    assert [d["metadata"]["name"] for d in yaml.safe_load_all((dump / "black" / "ms1.yaml").read_text())] == ["ms1-report"]
    assert [d["metadata"]["name"] for d in yaml.safe_load_all((dump / "ms1-report.yaml").read_text())] == ["other"]
    assert any("ConfigMap/other" in l for l in lines)


def test_layer_order_of_each_era():
    assert upgrade.layer_order(REPO) == ["base", "defaults", "values"]
    if has_rev("e7abf677"):
        dest = Path(os.environ.get("TMP", "/tmp")) / "ucc-layer-e7abf677"
        shutil.rmtree(dest, ignore_errors=True)
        upgrade.chart_at("e7abf677", is_date=False, dest=dest)
        assert upgrade.layer_order(dest) == ["defaults", "base", "values"]
        shutil.rmtree(dest, ignore_errors=True)


@pytest.mark.parametrize("args,msg", [
    (["--upgrade", "{fix}/v3.1.0/flat/tree", "-n", "dev"], "--upgrade reads its namespaces from the old tree"),
    (["--upgrade-offline"], "need --upgrade TREE"),
    (["--upgrade", "{fix}/v3.1.0/flat/tree", "--upgrade-chart-rev", "v1.0.0"], "choose the chart for --upgrade-offline"),
    (["--upgrade", str(REPO / "examples" / "showcase")], "not a converter tree"),
    (["--upgrade", "{fix}/does-not-exist"], "is not a folder"),
    (["--upgrade", "{fix}/v3.1.0/flat/tree", "--upgrade-offline", "--upgrade-chart-rev", "no-such-rev"],
     "is not a revision"),
])
def test_bad_upgrades_are_refused(tmp_path, args, msg):
    r = run(*[a.replace("{fix}", str(FIX)) for a in args], "-o", tmp_path / "out", ok=False)
    assert r.returncode != 0 and msg in r.stderr, r.stderr[-2000:]


def test_offline_without_git_or_revision_says_what_to_pass(tmp_path):
    tree = tmp_path / "tree"
    shutil.copytree(FIX / "v3.1.0" / "flat" / "tree", tree)
    r = run("--upgrade", tree, "--upgrade-offline", "-o", tmp_path / "out", ok=False,
            env={"GIT_CEILING_DIRECTORIES": str(tmp_path)})
    assert r.returncode != 0 and "pass --upgrade-chart-rev REV" in r.stderr


def test_a_release_the_old_chart_cannot_render_is_reported_not_skipped(tmp_path):
    if not has_rev("v3.1.0"):
        pytest.skip("needs this repo's history")
    tree = tmp_path / "tree"
    shutil.copytree(FIX / "v3.1.0" / "flat" / "tree", tree)
    (tree / "dev" / "values" / "ms1.yaml").write_text("workload: {type: nope}\n", encoding="utf-8")
    r = run("--upgrade", tree, "--upgrade-offline", "--upgrade-chart-rev", "v3.1.0", "-o", tmp_path / "out", ok=False)
    assert "dev/values/ms1.yaml: does not render with the chart of its time" in r.stderr
