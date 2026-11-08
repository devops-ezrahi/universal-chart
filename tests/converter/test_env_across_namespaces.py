"""Every namespace gets exactly its own env vars back.

env is a NAME: value map split per var between base/ and each folder's values/
file. This builds releases that run in several namespaces with env that
differs between them (values changed, vars dropped, vars added, Secret refs,
cronjob/job env), converts them with --prevent-restart, then rebuilds each
namespace's env from base -> defaults -> values and compares it var by var
with the raw manifests. Where helm is installed the converter also renders
every release through the chart (--verify-fidelity --fidelity-strict).
"""
import copy
import json
import random
import re
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest
import yaml

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]  # the universal-chart checkout
CHART = REPO
HELM = shutil.which("helm") is not None


# --------------------------------------------------------------------------
# raw env vs converted env, per namespace
# --------------------------------------------------------------------------

def _load(p: Path) -> dict:
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def _merge(a, b):
    out = dict(a or {})
    for k, v in (b or {}).items():
        if v is None:
            out.pop(k, None)
        elif isinstance(out.get(k), dict) and isinstance(v, dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _norm(v) -> str:
    if isinstance(v, dict) and "valueFrom" in v:
        v = v["valueFrom"]
    elif isinstance(v, dict) and "value" in v:
        v = v["value"]
    elif v == {}:
        v = ""
    return json.dumps("" if v is None else v, sort_keys=True)


def _pairs(env) -> list:
    if isinstance(env, list):
        return [(e["name"], _norm({k: x for k, x in e.items() if k != "name"})) for e in env]
    if isinstance(env, dict):
        return [(k, _norm(v)) for k, v in env.items() if v is not None]
    return []


def raw_env(ns_dir: Path) -> Counter:
    c = Counter()
    for f in ns_dir.rglob("*.yaml"):
        for d in yaml.safe_load_all(f.read_text(encoding="utf-8")):
            if not isinstance(d, dict):
                continue
            spec = d.get("spec") or {}
            jt = ((spec.get("jobTemplate") or {}).get("spec") or {}).get("template")
            for tpl in (spec.get("template"), jt):
                for key in ("containers", "initContainers"):
                    for ct in ((tpl or {}).get("spec") or {}).get(key) or []:
                        c.update(_pairs(ct.get("env")))
    return c


def _tpl(node, vals, ns):
    if isinstance(node, str):
        s = node.replace('{{ "{{" }}', "\x00")
        s = re.sub(r"\{\{\s*\.Release\.Namespace\s*\}\}", ns, s)
        s = re.sub(r"\{\{\s*\.Values\.(\w+)\s*\}\}", lambda m: str(vals.get(m.group(1), m.group(0))), s)
        return s.replace("\x00", "{{")
    if isinstance(node, dict):
        return {_tpl(k, vals, ns): _tpl(v, vals, ns) for k, v in node.items()}
    if isinstance(node, list):
        return [_tpl(x, vals, ns) for x in node]
    return node


def converted_env(out: Path, ns: str) -> Counter:
    c = Counter()
    for vf in sorted((out / ns).rglob("*.yaml")):
        if "values" not in vf.relative_to(out / ns).parts:
            continue
        folder = vf.parent
        while folder.name != "values":
            folder = folder.parent
        rel, folder = vf.relative_to(folder), folder.parent
        base = out / "base" / rel
        chain, f = [], folder
        while f != out:
            if (f / "defaults.yaml").exists():
                chain.insert(0, _load(f / "defaults.yaml"))
            f = f.parent
        eff = {}
        for layer in [_load(base) if base.exists() else {}] + chain + [_load(vf)]:
            eff = _merge(eff, layer)
        eff = _tpl(eff, eff, ns)
        c.update(_pairs(eff.get("env")))
        for j in (eff.get("cronjobs") or {}).values():
            c.update(_pairs((j.get("jobTemplate") or {}).get("env")))
            for s in ((j.get("jobTemplate") or {}).get("initContainers") or {}).values():
                c.update(_pairs((s or {}).get("env")))
            for s in ((j.get("jobTemplate") or {}).get("sidecars") or {}).values():
                c.update(_pairs((s or {}).get("env")))
        for j in (eff.get("jobs") or {}).values():
            c.update(_pairs(j.get("env")))
            for group in ("initContainers", "sidecars"):
                for s in (j.get(group) or {}).values():
                    c.update(_pairs((s or {}).get("env")))
        for group in ("sidecars", "initContainers"):
            for s in (eff.get(group) or {}).values():
                c.update(_pairs((s or {}).get("env")))
    return c


def assert_every_namespace_keeps_its_env(inp: Path, out: Path) -> int:
    problems, total = [], 0
    for ns_dir in sorted(p for p in inp.iterdir() if p.is_dir()):
        r, c = raw_env(ns_dir), converted_env(out, ns_dir.name)
        total += sum(r.values())
        problems += [f"{ns_dir.name}: MISSING {n}={v}" for (n, v) in (r - c)]
        problems += [f"{ns_dir.name}: EXTRA {n}={v}" for (n, v) in (c - r)]
    assert not problems, "\n".join(problems[:50])
    return total


def convert(inp: Path, out: Path, *flags) -> None:
    verify = ["--verify-fidelity", "--fidelity-strict"] if HELM else ["--skip-verify"]
    r = subprocess.run([sys.executable, str(REPO / "convert_to_universal_chart.py"), "--input", str(inp),
                        "--output", str(out), "--chart", str(CHART), *verify, *flags],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(HERE))
    report_path = out / "report" / "fidelity_report.txt"
    if r.returncode != 0 and report_path.exists():
        lines = report_path.read_text(encoding="utf-8").splitlines()
        start = next((i for i, l in enumerate(lines) if l.startswith("ERROR BREAKDOWN")), 0)
        issues = [l for l in lines if l.strip().startswith(("ERROR", "LOST", "CHANGED")) or " LOST " in l or " CHANGED " in l]
        raise AssertionError("fidelity failed:\n" + "\n".join(lines[start:start + 40])
                             + "\n--- first issue lines ---\n" + "\n".join(issues[:80]))
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-4000:]
    if HELM:
        report = (out / "report" / "fidelity_report.txt").read_text(encoding="utf-8")
        assert "fields LOST (raw -> render): 0" in report and "fields CHANGED:             0" in report, report[:3000]


# --------------------------------------------------------------------------
# inputs
# --------------------------------------------------------------------------

def _mutate_env(env, rnd):
    out = []
    for e in env or []:
        r = rnd.random()
        if r < 0.2:
            continue  # dropped in this namespace
        e = copy.deepcopy(e)
        if r < 0.5 and "value" in e:
            e["value"] = f"{e['value']}-prd"  # different value here
        out.append(e)
    for i in range(rnd.randint(1, 2)):  # only this namespace has it
        out.insert(rnd.randint(0, len(out)), {"name": f"PRD_ONLY_{i}_{rnd.randint(0, 999)}", "value": "p"})
    return out


def _mutate(doc, rnd):
    spec = doc.get("spec") or {}
    jt = ((spec.get("jobTemplate") or {}).get("spec") or {}).get("template")
    for tpl in (spec.get("template"), jt):
        for key in ("containers", "initContainers"):
            for ct in ((tpl or {}).get("spec") or {}).get(key) or []:
                if ct.get("env") or rnd.random() < 0.3:
                    ct["env"] = _mutate_env(ct.get("env"), rnd)


def mock_with_prd_copies(tmp: Path, namespaces: list, seed: "int | None") -> Path:
    """The mock environment's namespaces, each next to a -prd copy whose env
    differs from it."""
    mock = tmp / "mock"
    subprocess.run([sys.executable, str(REPO / "tools" / "generate_mock_environment.py"), "--output", str(mock)],
                   check=True, capture_output=True)
    rnd, inp = random.Random(seed or 0), tmp / "in"
    for ns in namespaces:
        for f in sorted((mock / ns).rglob("*.yaml")):
            docs = list(yaml.safe_load_all(f.read_text(encoding="utf-8")))
            for name, mutate in ((ns, False), (f"{ns}-prd", True)):
                out_docs = copy.deepcopy(docs)
                for d in out_docs:
                    if isinstance(d, dict) and mutate:
                        if (d.get("metadata") or {}).get("namespace"):
                            d["metadata"]["namespace"] = name
                        if seed is not None:  # None: an identical copy (baseline)
                            _mutate(d, rnd)
                dst = inp / name / f.relative_to(mock / ns)
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_text(yaml.safe_dump_all(out_docs, sort_keys=False), encoding="utf-8")
    return inp


SUFFIX_APP = """\
apiVersion: apps/v1
kind: Deployment
metadata: {{name: app-{ns}, namespace: {ns}}}
spec:
  selector: {{matchLabels: {{app: app-{ns}}}}}
  template:
    metadata: {{labels: {{app: app-{ns}}}}}
    spec:
      containers:
        - name: app
          image: registry.example.org/app:1.0
          env:
{env}
---
apiVersion: batch/v1
kind: CronJob
metadata: {{name: job-{ns}, namespace: {ns}}}
spec:
  schedule: "0 1 * * *"
  jobTemplate:
    spec:
      template:
        spec:
          restartPolicy: OnFailure
          containers:
            - name: job
              image: registry.example.org/job:1.0
              env:
{job_env}
"""


def suffix_namespaces(tmp: Path) -> Path:
    """dev/tst/prd, names carrying the environment word, env that differs by
    value, by presence and by Secret name."""
    envs = {
        "dev": (["{name: SHARED, value: same}", "{name: URL, value: http://db.dev.svc}",
                 "{name: DEV_ONLY, value: x}", "{name: PW, valueFrom: {secretKeyRef: {name: db-dev, key: pw}}}"],
                ["{name: STAGE, value: dev}", "{name: J_DEV_ONLY, value: d}"]),
        "tst": (["{name: SHARED, value: same}", "{name: URL, value: http://db.tst.svc}",
                 "{name: TWO_OF_THREE, value: y}"],
                ["{name: STAGE, value: tst}"]),
        "prd": (["{name: SHARED, value: same}", "{name: URL, value: http://db.prd.svc}",
                 "{name: TWO_OF_THREE, value: y}", "{name: PRD_ONLY, value: z}",
                 "{name: PW, valueFrom: {secretKeyRef: {name: db-prd, key: pw}}}",
                 "{name: LINK, value: 'jdbc://$(URL)'}"],
                ["{name: STAGE, value: prd}", "{name: J_PRD_ONLY, value: p}"]),
    }
    inp = tmp / "in"
    for ns, (env, job_env) in envs.items():
        (inp / ns).mkdir(parents=True)
        (inp / ns / f"app-{ns}.yaml").write_text(SUFFIX_APP.format(
            ns=ns, env="\n".join(f"            - {e}" for e in env),
            job_env="\n".join(f"                - {e}" for e in job_env)), encoding="utf-8")
    return inp


# --------------------------------------------------------------------------
# tests
# --------------------------------------------------------------------------

def test_mock_namespaces_with_identical_prd_copies(tmp_path):
    """Baseline: the same mock apps with NO env change between the copies."""
    inp = mock_with_prd_copies(tmp_path, ["payments-svc", "platform-ops"], None)
    out = tmp_path / "out"
    convert(inp, out, "--prevent-restart", "--cluster-scoped")
    assert assert_every_namespace_keeps_its_env(inp, out) > 100


@pytest.mark.parametrize("seed", [1, 2])
def test_mock_namespaces_with_differing_prd_copies(tmp_path, seed):
    inp = mock_with_prd_copies(tmp_path, ["payments-svc", "platform-ops"], seed)
    out = tmp_path / "out"
    convert(inp, out, "--prevent-restart", "--cluster-scoped")
    assert assert_every_namespace_keeps_its_env(inp, out) > 100
    # The point of the split: a var that differs lands in that namespace's
    # values/, not in base/ — so each prd copy has values of its own.
    warnings = (out / "report" / "conflicts_and_warnings.txt").read_text(encoding="utf-8")
    assert "env.PRD_ONLY_" in warnings


@pytest.mark.parametrize("flags", [[], ["--env-in-values"]])
def test_env_suffix_namespaces(tmp_path, flags):
    inp = suffix_namespaces(tmp_path)
    out = tmp_path / "out"
    convert(inp, out, "--prevent-restart", "--env-suffixes", "dev,tst,prd", *flags)
    assert assert_every_namespace_keeps_its_env(inp, out) == 18  # 13 app + 5 cronjob
