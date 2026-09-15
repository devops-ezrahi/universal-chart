"""
Regression tests for ERRORS_ANALYSIS/CronJob_Merge_Overwrites_Command_And_Env.

Two claims in error.txt, and they need different answers.

  Issue 9 — "Two cronjobs have the same template name jenkins-backup [with
  different commands]. The values YAML has neither."
  Issue 10 — "Two cronjobs have different env values for STAGE. Values has only
  one."

The *overwrite* is real and is the same single line as folder 13
(CronJob_Schedule_Value_Corruption): `handle_cronjob` keyed
ctx.values["cronjobs"] by short_subresource_name(), so two CronJobs in one
microservice file differing only by the "<microservice>-" prefix landed on the
same key and the second replaced the first — the whole entry, command and env
included. `claim_subresource_key()` fixed that; these tests hold it fixed from
the command/env side, which folder 13 only guarded in passing.

The "has NEITHER" half of issue 9 is a different thing and is **not** an
overwrite. When one microservice runs in more than one namespace with differing
commands, common_subtree() declines to promote `command` into the shared
base/<slug>.yaml, and it lands in each namespace's own values/<ns>/<slug>.yaml.
Reading base/ alone shows neither command while both are present one layer up.
test_command_moves_out_of_base_when_namespaces_disagree pins exactly that, in
both files at once, so the layering can never quietly become a real loss.

RED evidence: these tests were re-run with claim_subresource_key() reverted to
the original `ctx.values.setdefault("cronjobs", {})[sub_name] = entry` — see
STATUS.md §3.
"""
import importlib.util
import pathlib
import shutil
import subprocess
import sys
import tempfile

import pytest
import yaml

TEST_DIR = pathlib.Path(__file__).resolve().parent
GITOPS_DIR = TEST_DIR.parents[3]
CHART_DIR = GITOPS_DIR.parent
CONVERTER_PY = GITOPS_DIR / "convert_to_universal_chart.py"
FIXTURE_DIR = TEST_DIR / "fixture"
FIXTURE = FIXTURE_DIR / "background-jobs" / "db-migrate.yaml"

NS = "background-jobs"
RELEASE = "db-migrate"

CMD_1 = ["/bin/bash", "-c", "echo 'Hello from jenkins-backup-1!'"]
CMD_2 = ["/bin/bash", "-c", "echo 'Hello from jenkins-backup-2!'"]
CMD_1_PRD = ["/bin/bash", "-c", "echo 'Hello from jenkins-backup-1-prd!'"]
CMD_2_PRD = ["/bin/bash", "-c", "echo 'Hello from jenkins-backup-2-prd!'"]
SECRET_REF = {"secretKeyRef": {"name": "db-migrate-credentials", "key": "api-key"}}


def _load_converter():
    spec = importlib.util.spec_from_file_location("convert_to_universal_chart", CONVERTER_PY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["convert_to_universal_chart"] = mod
    spec.loader.exec_module(mod)
    return mod


conv = _load_converter()


def _docs(path=FIXTURE):
    return [d for d in yaml.safe_load_all(path.read_text(encoding="utf-8")) if d]


def _convert_fixture():
    ctx = conv.MicroserviceContext(namespace=NS, name=RELEASE)
    for d in _docs():
        if d["kind"] == "CronJob":
            conv.handle_cronjob(d, ctx, d["metadata"]["name"])
    return ctx


def _run_converter(out: pathlib.Path):
    proc = subprocess.run(
        [sys.executable, str(CONVERTER_PY),
         "--input", str(FIXTURE_DIR), "--output", str(out),
         "--chart", str(CHART_DIR), "--skip-verify"],
        capture_output=True, text=True, cwd=str(GITOPS_DIR),
    )
    assert proc.returncode == 0, f"converter failed:\n{proc.stdout}\n{proc.stderr}"


def _helm_template(values: dict, release=RELEASE):
    with tempfile.TemporaryDirectory() as tmp:
        vf = pathlib.Path(tmp) / "values.yaml"
        vf.write_text(yaml.safe_dump(values, sort_keys=False), encoding="utf-8")
        proc = subprocess.run(
            ["helm", "template", release, str(CHART_DIR), "-f", str(vf)],
            capture_output=True, text=True,
        )
    assert proc.returncode == 0, f"helm template failed:\n{proc.stderr}"
    return [d for d in yaml.safe_load_all(proc.stdout) if d]


def _cronjob_containers(docs):
    """rendered CronJob name -> its first container."""
    return {d["metadata"]["name"]:
            d["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]
            for d in docs if d.get("kind") == "CronJob"}


# --------------------------------------------------------------------------
# Issue 9 — command. Layer 1: capture.
# --------------------------------------------------------------------------

def test_both_commands_survive_capture():
    cronjobs = _convert_fixture().values.get("cronjobs", {})
    commands = [e["jobTemplate"].get("command") for e in cronjobs.values()]
    assert CMD_1 in commands, f"CronJob/db-migrate-backup's command is gone; got {commands}"
    assert CMD_2 in commands, f"CronJob/backup's command is gone; got {commands}"


def test_each_cronjob_keeps_the_command_that_belongs_to_it():
    """Not just 'both are somewhere' — each must be on the right object. The
    entries are identified by schedule, which is unique per raw CronJob."""
    cronjobs = _convert_fixture().values.get("cronjobs", {})
    by_schedule = {e["schedule"]: e["jobTemplate"] for e in cronjobs.values()}
    assert by_schedule["0 1 * * *"].get("command") == CMD_1
    assert by_schedule["0 3 * * *"].get("command") == CMD_2
    assert by_schedule["0 1 * * *"].get("args") is None
    assert by_schedule["0 3 * * *"].get("args") == ["--verbose"]


# --------------------------------------------------------------------------
# Issue 10 — env. Structural env stays in base/, literal env goes to minimal.
# --------------------------------------------------------------------------

def test_both_stage_values_survive_capture():
    ctx = _convert_fixture()
    stages = sorted(
        env["STAGE"]["value"]
        for env in (v for (top, _sub), v in ctx.sub_full_env.items() if top == "cronjobs")
        if "STAGE" in env
    )
    assert stages == ["dev", "prod"], (
        f"one CronJob's STAGE was lost during the merge; got {stages}")


def test_the_secret_ref_env_stays_structural_and_on_the_right_cronjob():
    cronjobs = _convert_fixture().values.get("cronjobs", {})
    by_schedule = {e["schedule"]: e["jobTemplate"] for e in cronjobs.values()}
    assert by_schedule["0 1 * * *"]["env"]["DB_PASSWORD"]["valueFrom"] == SECRET_REF
    # The literal STAGE is deliberately NOT in the structural values — it goes
    # to the namespace's minimal override (build_minimal_values). Pinned so a
    # future change cannot start duplicating it into both files.
    assert "STAGE" not in by_schedule["0 1 * * *"]["env"]
    assert "env" not in by_schedule["0 3 * * *"], (
        "the second CronJob has only a literal env var, so it should carry no "
        "structural env block at all")


def test_literal_env_reaches_the_minimal_override_for_both_cronjobs():
    minimal = conv.build_minimal_values(_convert_fixture())
    stages = sorted(v["jobTemplate"]["env"]["STAGE"]["value"]
                    for v in minimal["cronjobs"].values()
                    if "env" in v.get("jobTemplate", {}))
    assert stages == ["dev", "prod"], f"minimal override lost a STAGE; got {stages}"


# --------------------------------------------------------------------------
# Issue 9's "the values YAML has NEITHER" — the layering, not a loss.
# --------------------------------------------------------------------------

def test_command_moves_out_of_base_when_namespaces_disagree(tmp_path):
    """base/ holds only what EVERY namespace running the microservice agrees on.
    Both namespaces here disagree on both commands, so base/ carries neither —
    and each namespace's own values file carries its own two."""
    out = tmp_path / "out"
    _run_converter(out)

    base = yaml.safe_load((out / "base" / f"{RELEASE}.yaml").read_text(encoding="utf-8"))
    base_cmds = [e.get("jobTemplate", {}).get("command") for e in base["cronjobs"].values()]
    assert base_cmds == [None, None], (
        f"base/{RELEASE}.yaml should hold no command when the namespaces disagree; "
        f"got {base_cmds}")

    found = {}
    for ns, expected in ((NS, [CMD_1, CMD_2]),
                         (f"{NS}-prd", [CMD_1_PRD, CMD_2_PRD])):
        vals = yaml.safe_load(
            (out / ns / "values" / f"{RELEASE}.yaml").read_text(encoding="utf-8"))
        got = [e["jobTemplate"].get("command") for e in vals["cronjobs"].values()]
        found[ns] = got
        for cmd in expected:
            assert cmd in got, (
                f"{ns}/values/{RELEASE}.yaml is missing {cmd}; it holds {got}. "
                f"base/ has none either, so this command is lost outright.")
    assert found[NS] != found[f"{NS}-prd"]


# --------------------------------------------------------------------------
# Layer 2/3 — end to end, the only view that proves the pod would be right.
# --------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_round_trip_each_rendered_cronjob_has_its_own_command_and_stage(tmp_path):
    out = tmp_path / "out"
    _run_converter(out)

    merged: dict = {}
    for vf in sorted((out / NS).rglob(f"{RELEASE}.yaml")):
        if vf.parent.name == "releases":
            continue
        merged = conv.deep_merge(merged, yaml.safe_load(vf.read_text(encoding="utf-8")) or {})
    base = yaml.safe_load((out / "base" / f"{RELEASE}.yaml").read_text(encoding="utf-8"))
    merged = conv.deep_merge(base, merged)

    containers = _cronjob_containers(_helm_template(merged))
    assert len(containers) == 2, f"2 raw CronJobs, {len(containers)} rendered: {list(containers)}"

    by_cmd = {}
    for name, c in containers.items():
        by_cmd[tuple(c["command"])] = (name, c)

    assert tuple(CMD_1) in by_cmd, (
        f"no rendered CronJob runs {CMD_1}; rendered commands = {list(by_cmd)}")
    assert tuple(CMD_2) in by_cmd, (
        f"no rendered CronJob runs {CMD_2}; rendered commands = {list(by_cmd)}")

    def env_of(container):
        return {e["name"]: e for e in container.get("env", [])}

    _, c1 = by_cmd[tuple(CMD_1)]
    _, c2 = by_cmd[tuple(CMD_2)]
    assert env_of(c1)["STAGE"]["value"] == "dev"
    assert env_of(c2)["STAGE"]["value"] == "prod"
    assert env_of(c1)["DB_PASSWORD"]["valueFrom"] == SECRET_REF
    assert "DB_PASSWORD" not in env_of(c2), (
        "the second CronJob never declared DB_PASSWORD; it must not inherit the first's")
    assert c1.get("args") is None
    assert c2.get("args") == ["--verbose"]


# --------------------------------------------------------------------------
# The fidelity checker must be able to see a swapped command.
# --------------------------------------------------------------------------

def test_fidelity_checker_reports_a_changed_command():
    raw = next(d for d in _docs()
               if d["kind"] == "CronJob" and d["metadata"]["name"] == "db-migrate-backup")
    rendered = yaml.safe_load(yaml.safe_dump(raw))
    rendered["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["command"] = CMD_2
    diffs = conv._fidelity_diff("CronJob", raw, rendered)
    assert any(d[0] == "CHANGED" and "command" in d[1] for d in diffs), f"diffs={diffs}"
