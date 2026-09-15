"""
Regression tests for ERRORS_ANALYSIS/CronJob_Schedule_Value_Corruption.

Reported symptom: a CronJob whose raw manifest says `schedule: "46 2 * * 6"`
renders as `"6 2 * * 1"`. error.txt offers two competing hypotheses and says
both must be checked:

  1. a microservice-merge collision — two CronJobs reduced to the same values
     map key and one overwrote the other;
  2. subtract_defaults bleeding the value 6 (successfulJobsHistoryLimit) into
     the schedule string.

Hypothesis 2 is disproved below (test_subtract_defaults_never_bleeds_*).
Hypothesis 1 is confirmed: `handle_cronjob` ends with

    ctx.values.setdefault("cronjobs", {})[sub_name] = entry

STRICT NAMING UPDATE: this defect is now UNREACHABLE. `short_subresource_name`
returns the raw `metadata.name` verbatim and the chart renders the map key
verbatim, so the key IS the object name — unique per kind per namespace by API
rule. Two CronJobs can no longer land on one key. The tests below assert that
stronger guarantee; the history is kept because the reasoning is what justifies
the current design.

The original defect, for the record: `sub_name` used to be a name with a leading
"<microservice>-". Two CronJobs in one microservice file that differ only by
that prefix land on the same key and the second silently replaces the first.
The survivor is then rendered under the FIRST object's Kubernetes name — the
chart re-adds the release prefix — while carrying the SECOND object's schedule.
That is exactly "46 2 * * 6 -> 6 2 * * 1": not a corrupted string, two objects
collapsed into one.
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
FIXTURE = FIXTURE_DIR / "background-jobs" / "nightly-report.yaml"

NS = "background-jobs"
RELEASE = "nightly-report"

# The two schedules named in error.txt, plus the control.
PREFIXED_SCHEDULE = "46 2 * * 6"      # raw CronJob/nightly-report-cleanup
BARE_SCHEDULE = "6 2 * * 1"           # raw CronJob/cleanup
ARCHIVE_SCHEDULE = "*/15 * * * *"     # raw CronJob/nightly-report-archive


def _load_converter():
    spec = importlib.util.spec_from_file_location("convert_to_universal_chart", CONVERTER_PY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["convert_to_universal_chart"] = mod
    spec.loader.exec_module(mod)
    return mod


conv = _load_converter()


def _docs():
    return [d for d in yaml.safe_load_all(FIXTURE.read_text(encoding="utf-8")) if d]


def _doc(kind, name):
    for d in _docs():
        if d.get("kind") == kind and d["metadata"]["name"] == name:
            return d
    raise AssertionError(f"no {kind}/{name} in fixture")


def _convert_fixture():
    """Run handle_cronjob/handle_job over the fixture in file order, exactly as
    build_microservice does, and return the resulting context."""
    ctx = conv.MicroserviceContext(namespace=NS, name=RELEASE)
    for d in _docs():
        name = d["metadata"]["name"]
        if d["kind"] == "CronJob":
            conv.handle_cronjob(d, ctx, name)
        elif d["kind"] == "Job":
            conv.handle_job(d, ctx, name)
    return ctx


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


def _rendered_schedules(docs):
    return {d["metadata"]["name"]: d["spec"]["schedule"]
            for d in docs if d.get("kind") == "CronJob"}


# --------------------------------------------------------------------------
# Hypothesis 2 — subtract_defaults bleeding a number into the schedule.
# Checked first because error.txt says both must be checked; it does not hold.
# --------------------------------------------------------------------------

def test_subtract_defaults_never_bleeds_a_history_limit_into_the_schedule():
    a = {"cronjobs": {"cleanup": {"schedule": PREFIXED_SCHEDULE,
                                  "successfulJobsHistoryLimit": 6}}}
    b = {"cronjobs": {"cleanup": {"schedule": BARE_SCHEDULE,
                                  "successfulJobsHistoryLimit": 6}}}
    common = conv.common_subtree([a, b])
    assert "schedule" not in common["cronjobs"]["cleanup"], (
        "common_subtree hoisted a schedule the two instances do NOT share")
    assert conv.subtract_defaults(a, common)["cronjobs"]["cleanup"]["schedule"] == PREFIXED_SCHEDULE
    assert conv.subtract_defaults(b, common)["cronjobs"]["cleanup"]["schedule"] == BARE_SCHEDULE


def test_yaml_round_trip_preserves_every_schedule_form():
    """A schedule is a space-separated string full of YAML metacharacters —
    `*` opens an alias, `@` is reserved, `1:30` looks sexagesimal. Prove the
    converter's own dumper cannot be the corrupting step."""
    for s in (PREFIXED_SCHEDULE, BARE_SCHEDULE, ARCHIVE_SCHEDULE,
              "@daily", "5 4 * * sun", "0 0 1 1 *", "1:30 * * * *"):
        assert yaml.safe_load(conv.dump_yaml({"schedule": s}))["schedule"] == s


# --------------------------------------------------------------------------
# Hypothesis 1 — the map-key collision. Layer 1: converter capture.
# --------------------------------------------------------------------------

def test_every_raw_cronjob_survives_conversion():
    cronjobs = _convert_fixture().values.get("cronjobs", {})
    assert len(cronjobs) == 3, (
        "a raw CronJob was silently dropped: 3 CronJobs in, "
        f"{len(cronjobs)} values entries out -> {sorted(cronjobs)}"
    )


def test_the_prefixed_cronjob_keeps_its_own_schedule():
    """The headline assertion, now under strict naming: the map key IS the raw
    `metadata.name`, so `nightly-report-cleanup` has its own entry and cannot
    carry `cleanup`'s schedule."""
    cronjobs = _convert_fixture().values.get("cronjobs", {})
    entry = cronjobs.get("nightly-report-cleanup")
    assert entry is not None, (
        f"no 'nightly-report-cleanup' entry; keys={sorted(cronjobs)}")
    assert entry["schedule"] == PREFIXED_SCHEDULE, (
        f"CronJob/nightly-report-cleanup carries {entry['schedule']!r}, "
        f"expected {PREFIXED_SCHEDULE!r}")


def test_the_bare_cronjob_keeps_its_own_schedule():
    cronjobs = _convert_fixture().values.get("cronjobs", {})
    schedules = {k: v["schedule"] for k, v in cronjobs.items()}
    assert BARE_SCHEDULE in schedules.values(), (
        f"CronJob/cleanup's schedule {BARE_SCHEDULE!r} is nowhere in the values; "
        f"got {schedules}"
    )


def test_a_non_colliding_cronjob_is_untouched():
    """Control: the normal path must not change."""
    entry = _convert_fixture().values["cronjobs"]["nightly-report-archive"]
    assert entry["schedule"] == ARCHIVE_SCHEDULE


def test_each_cronjob_keeps_its_own_command_history_limit_and_args():
    """The schedule is not the only field the overwrite takes with it — the
    whole entry is replaced. Pinned so a fix that only special-cased `schedule`
    would still fail."""
    cronjobs = _convert_fixture().values.get("cronjobs", {})
    by_schedule = {v["schedule"]: v for v in cronjobs.values()}
    assert by_schedule[PREFIXED_SCHEDULE]["successfulJobsHistoryLimit"] == 6
    assert by_schedule[BARE_SCHEDULE]["successfulJobsHistoryLimit"] == 3
    assert by_schedule[PREFIXED_SCHEDULE]["jobTemplate"]["args"] == \
        ["echo weekly cleanup; sleep 2; echo done"]
    assert by_schedule[BARE_SCHEDULE]["jobTemplate"]["args"] == \
        ["echo monday cleanup; sleep 2; echo done"]


def test_no_collision_is_possible_under_strict_naming():
    """The defect is now unreachable rather than handled.

    The map key is the raw `metadata.name`, which Kubernetes guarantees unique
    per kind per namespace — so two CronJobs CANNOT land on one key, no
    disambiguation runs, and no warning is needed. This is a stronger guarantee
    than the collision resolver it replaced: that survived the clash, this makes
    it impossible."""
    ctx = _convert_fixture()
    keys = sorted(ctx.values.get("cronjobs", {}))
    assert keys == ["cleanup", "nightly-report-archive", "nightly-report-cleanup"], keys
    assert not [w for w in ctx.warnings if "collision" in w.lower()], (
        f"a collision was reported where none is possible: {ctx.warnings}")


def test_the_key_assignment_does_not_depend_on_document_order():
    """Order-independence, which used to need an entitlement rule to achieve and
    is now a property of using the raw name."""
    forward = _convert_fixture().values["cronjobs"]["nightly-report-cleanup"]["schedule"]

    ctx = conv.MicroserviceContext(namespace=NS, name=RELEASE)
    conv.handle_cronjob(_doc("CronJob", "cleanup"), ctx, "cleanup")
    conv.handle_cronjob(_doc("CronJob", "nightly-report-cleanup"), ctx, "nightly-report-cleanup")
    reverse = ctx.values["cronjobs"]["nightly-report-cleanup"]["schedule"]

    assert forward == reverse == PREFIXED_SCHEDULE
    assert ctx.values["cronjobs"]["cleanup"]["schedule"] == BARE_SCHEDULE


def test_jobs_are_keyed_the_same_way():
    """Guard: handle_job uses the same helper, so Jobs get full-name keys too
    and are equally collision-proof."""
    jobs = _convert_fixture().values.get("jobs", {})
    assert len(jobs) == 2, f"a raw Job was silently dropped; keys={sorted(jobs)}"


# --------------------------------------------------------------------------
# Layer 2 — the chart must render each schedule verbatim.
# --------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_chart_renders_each_cronjob_schedule_verbatim():
    values = {
        "workload": {"type": "none"},
        "image": {"repository": "busybox", "tag": "1.36"},
        "cronjobs": {
            "nightly-report-cleanup": {"schedule": PREFIXED_SCHEDULE, "jobTemplate": {}},
            "nightly-report-archive": {"schedule": ARCHIVE_SCHEDULE, "jobTemplate": {}},
        },
    }
    got = _rendered_schedules(_helm_template(values))
    assert got == {"nightly-report-cleanup": PREFIXED_SCHEDULE,
                   "nightly-report-archive": ARCHIVE_SCHEDULE}, got


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_a_star_schedule_renders_as_a_string_not_a_yaml_alias():
    values = {
        "workload": {"type": "none"},
        "image": {"repository": "busybox", "tag": "1.36"},
        "cronjobs": {"nightly-report-archive": {"schedule": ARCHIVE_SCHEDULE,
                                                "jobTemplate": {}}},
    }
    docs = _helm_template(values)
    sched = _rendered_schedules(docs)["nightly-report-archive"]
    assert isinstance(sched, str) and sched == ARCHIVE_SCHEDULE


# --------------------------------------------------------------------------
# Layer 3 — end to end: converter over the fixture, then helm template.
# --------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_round_trip_no_rendered_cronjob_carries_another_ones_schedule(tmp_path):
    out = tmp_path / "out"
    proc = subprocess.run(
        [sys.executable, str(CONVERTER_PY),
         "--input", str(FIXTURE_DIR), "--output", str(out),
         "--chart", str(CHART_DIR), "--skip-verify"],
        capture_output=True, text=True, cwd=str(GITOPS_DIR),
    )
    assert proc.returncode == 0, f"converter failed:\n{proc.stdout}\n{proc.stderr}"

    merged: dict = {}
    for vf in sorted(out.rglob(f"{RELEASE}.yaml")):
        if vf.parent.name == "releases":
            continue
        merged = conv.deep_merge(merged, yaml.safe_load(vf.read_text(encoding="utf-8")) or {})

    rendered = _rendered_schedules(_helm_template(merged))
    raw = sorted(d["spec"]["schedule"] for d in _docs() if d["kind"] == "CronJob")

    assert len(rendered) == 3, (
        f"3 raw CronJobs, {len(rendered)} rendered: {rendered}")
    assert sorted(rendered.values()) == raw, (
        f"rendered schedules {sorted(rendered.values())} != raw {raw}")
    assert rendered.get("nightly-report-cleanup") == PREFIXED_SCHEDULE, (
        f"CronJob/nightly-report-cleanup rendered with schedule "
        f"{rendered.get('nightly-report-cleanup')!r}, raw says {PREFIXED_SCHEDULE!r}"
    )


# --------------------------------------------------------------------------
# The fidelity checker must be able to see this if it ever happens again.
# --------------------------------------------------------------------------

def test_fidelity_checker_reports_a_changed_schedule():
    rawcj = _doc("CronJob", "nightly-report-cleanup")
    rendered = yaml.safe_load(yaml.safe_dump(rawcj))
    rendered["spec"]["schedule"] = BARE_SCHEDULE
    diffs = conv._fidelity_diff("CronJob", rawcj, rendered)
    assert any(d[0] == "CHANGED" and "schedule" in d[1] for d in diffs), f"diffs={diffs}"
