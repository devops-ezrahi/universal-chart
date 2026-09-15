"""
Regression tests for ERRORS_NEW_ANALYSIS/Summary_Vs_Report_Data_Mismatch.

The defect is a PROCESS defect, and error.txt says so: "THIS IS A PROCESS
DEFECT, NOT A CODE DEFECT, AND IT INVALIDATES PLANNING BUILT ON THE SUMMARY."
A hand-maintained error summary and the generated fidelity_report.txt were used
interchangeably to set priorities while describing different converter runs.
Eight error types the summary rated significant — including its own #1 HIGH
(containers[0].resources, 41) and its highest MEDIUM (progressDeadlineSeconds,
68) — appear zero times in the report.

Neither document said which run it came from, so the contradiction was
invisible and every count downstream inherited it. The fix is therefore not a
field fix; it is to make the report state its own identity and generate its own
breakdown, so the two failure modes become impossible:

  1. Comparing counts across runs. Every report now carries a content-derived
     `run id`. Differing ids means the counts are not evidence about each other.
  2. Maintaining a summary by hand. The report now generates its own
     per-category breakdown from its own lines, in the same run.

error.txt's own ACTION line is "re-run the converter on a single known input
set, regenerate the report, and rebuild the summary from it. Do not reconcile
the two documents by hand." These tests pin the machinery that makes that the
only available option.

The last group settles the substantive question error.txt could not: it lists
TWO POSSIBLE READINGS of the eight missing error types — either they were
genuinely fixed between runs, or they are still live in a different input set —
and says "the difference matters enormously". They are asserted fixed, in code,
one test each.
"""
import importlib.util
import pathlib
import shutil
import sys
import tempfile

import pytest
import yaml

TEST_DIR = pathlib.Path(__file__).resolve().parent
GITOPS_DIR = TEST_DIR.parents[3]
CONVERTER_PY = GITOPS_DIR / "convert_to_universal_chart.py"
SANITIZER_PY = GITOPS_DIR / "sanitize_fidelity_report.py"


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


conv = _load(CONVERTER_PY, "convert_to_universal_chart")
san = _load(SANITIZER_PY, "sanitize_fidelity_report")


# ---------------------------------------------------------------------------
# 1. Run identity — two reports can now say whether they describe the same run
# ---------------------------------------------------------------------------

def _corpus(tmp: pathlib.Path, body: str) -> pathlib.Path:
    ns = tmp / "shop-web"
    ns.mkdir(parents=True)
    (ns / "api.yaml").write_text(body, encoding="utf-8")
    return tmp


DOC_A = """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: api
spec:
  replicas: 1
  template:
    spec:
      containers:
        - name: api
          image: nginx:1.25
"""


def test_run_fingerprint_is_stable_for_the_same_input():
    """Same converter, same bytes, same id — otherwise the id is noise and
    nobody would trust it enough to compare two reports by."""
    with tempfile.TemporaryDirectory() as d1, tempfile.TemporaryDirectory() as d2:
        a = conv.run_fingerprint(_corpus(pathlib.Path(d1), DOC_A))
        b = conv.run_fingerprint(_corpus(pathlib.Path(d2), DOC_A))
    assert a == b, "identical input produced different run ids"


def test_run_fingerprint_changes_when_the_input_changes():
    """The whole point: a count that moved between two reports with DIFFERENT
    ids may be more data rather than a regression. If the id did not move, the
    reader would go on comparing them."""
    with tempfile.TemporaryDirectory() as d1, tempfile.TemporaryDirectory() as d2:
        a = conv.run_fingerprint(_corpus(pathlib.Path(d1), DOC_A))
        b = conv.run_fingerprint(_corpus(pathlib.Path(d2), DOC_A.replace("replicas: 1", "replicas: 3")))
    assert a[1] != b[1], "an edited manifest did not change the input hash"
    assert a[2] != b[2], "an edited manifest did not change the run id"


def test_run_fingerprint_changes_when_a_manifest_only_MOVES():
    """A manifest that moved is a different input even with identical bytes —
    it can land in another namespace and change which release owns it. Hashing
    content alone would call the two runs the same."""
    with tempfile.TemporaryDirectory() as d1, tempfile.TemporaryDirectory() as d2:
        a = conv.run_fingerprint(_corpus(pathlib.Path(d1), DOC_A))
        root = pathlib.Path(d2) / "platform-ops"
        root.mkdir(parents=True)
        (root / "api.yaml").write_text(DOC_A, encoding="utf-8")
        b = conv.run_fingerprint(pathlib.Path(d2))
    assert a[2] != b[2], "moving a manifest to another namespace kept the same run id"


def test_run_id_names_both_halves_so_a_mismatch_is_attributable():
    """converter hash and input hash are separately visible. A reader seeing
    two differing ids has to know WHICH half moved: same converter + different
    input is 'more data'; different converter + same input is a code change."""
    with tempfile.TemporaryDirectory() as d:
        converter_h, input_h, run_id = conv.run_fingerprint(_corpus(pathlib.Path(d), DOC_A))
    assert run_id == f"{converter_h}-{input_h}"
    assert converter_h != input_h


# ---------------------------------------------------------------------------
# 2. The generated breakdown — so no summary has to be kept by hand
# ---------------------------------------------------------------------------

# Every ERROR shape verify_raw_fidelity actually emits, verbatim in form.
REPORT_LINES = [
    "=== rakia-fac1 — 131 raw objects vs 162 rendered ===",
    "  INFO   ConfigMap/shared-ca-bundle (from shared.yaml): relocated to release 'x' (expected)",
    "  ERROR  Route/midasson-api LOST at metadata.labels.team: raw had 'rakia_app_a'",
    "  ERROR  Route/midasson-web LOST at metadata.labels.team: raw had 'rakia_app_b'",
    "  ERROR  Route/midasson-web LOST at metadata.labels.critical: raw had 'true'",
    "  ERROR  Deployment/b2b LOST at spec.template.spec.containers[0].ports[#port:9125/TCP]: gone",
    "  ERROR  ConfigMap/rakia-zk-config CHANGED at data.navlogback.xml: raw='Ľuboš' -> render='x'",
    "  ERROR  Service/orphan-svc (from shared.yaml): rendered by NO release",
    "  ERROR  Secret/ghost: rendered by 'x' but present in NO raw manifest",
    "RENDER FAILURE cluster-shared: helm exploded",
]


def _breakdown(lines):
    out = conv.fidelity_error_breakdown(lines)
    counts = {}
    for line in out:
        parts = line.split()
        if len(parts) == 2 and parts[1].isdigit():
            counts[parts[0]] = int(parts[1])
    return counts


def test_breakdown_splits_field_errors_by_leaf_not_by_verb():
    """error.txt's root complaint is that categories were lumped. 'Labels/
    annotations lost | 644 | 2,928' is one bucket hiding many defects. Two
    Routes losing `team` and one losing `critical` must be two categories."""
    counts = _breakdown(REPORT_LINES)
    assert counts["field_lost_team"] == 2
    assert counts["field_lost_critical"] == 1


def test_breakdown_counts_every_error_shape_the_checker_emits():
    counts = _breakdown(REPORT_LINES)
    assert counts["missing_object"] == 1
    assert counts["extra_object"] == 1
    assert counts["render_failure"] == 1
    # Leaf of `data.navlogback.xml` is the last dot segment, `xml` — the same
    # convention the sanitizer already uses, whose real index shows
    # `field_changed_crt` for `data.ca-bundle.crt`. Keeping the two in step
    # matters more than a prettier name: they are read side by side.
    assert counts["field_changed_xml"] == 1


def test_breakdown_strips_list_subscripts_from_the_leaf():
    """`ports[#port:9125/TCP]` is one port's synthetic identity, not a category.
    Left in, every single lost port would be its own category of one and the
    breakdown would be as unreadable as the raw report."""
    assert _breakdown(REPORT_LINES)["field_lost_ports"] == 1


def test_breakdown_ignores_INFO_and_namespace_headers():
    counts = _breakdown(REPORT_LINES)
    assert sum(counts.values()) == 8, f"counted non-ERROR lines: {counts}"


def test_breakdown_never_absorbs_an_unrecognised_ERROR_into_a_neighbour():
    """A shape the parser does not know is the one most likely to be genuinely
    new. Counting it under its own name keeps it visible; folding it into the
    nearest bucket is how a novel defect becomes invisible."""
    counts = _breakdown(["  ERROR  Sensor/foo went sideways in an unforeseen way"])
    assert counts == {"other_error": 1}


def test_breakdown_totals_agree_with_the_lines_it_summarises():
    """The defect in one sentence: a summary whose numbers did not come from
    the report it summarised. This is the invariant that makes that impossible."""
    counts = _breakdown(REPORT_LINES)
    actual = sum(1 for line in REPORT_LINES
                 if line.strip().startswith(("ERROR", "RENDER FAILURE")))
    assert sum(counts.values()) == actual


def test_breakdown_says_none_rather_than_nothing_on_a_clean_run():
    """A clean run must still print the block. An absent breakdown reads as
    'not generated' and invites someone to write one by hand — which is the
    defect."""
    assert any("none" in line for line in conv.fidelity_error_breakdown([]))


# ---------------------------------------------------------------------------
# 3. The report header stays machine-readable — the sanitizer must not
#    silently start dropping it
# ---------------------------------------------------------------------------

def test_sanitizer_treats_the_run_identity_block_as_preamble():
    """sanitize_fidelity_report OMITS lines it cannot parse (by design — see
    Sanitizer_Drops_Unparsed_Lines). Adding 12 header lines the parser did not
    recognise would have quietly raised `lines unparsed` from 3 to 15 and made
    every shareable artifact less trustworthy, which is the same class of defect
    this folder is about."""
    header = [
        "RUN IDENTITY — counts below describe THIS run and no other.",
        "  run id:         c919e0fb0308-e1073701d07c",
        "  converter:      c919e0fb0308  (convert_to_universal_chart.py)",
        "  input:          e1073701d07c  (namespaces-showcase)",
        "  output:         /tmp/out",
        "  Compare two fidelity reports ONLY when their run ids match. A differing",
        "  run id means the converter, the input, or both changed — so a count that",
        "  moved between them may be more data rather than a regression, and the two",
        "  numbers are not evidence about each other. Do not maintain a summary of",
        "  this report by hand; the breakdown below is generated from these same",
        "  lines, in this same run, and is the only summary that cannot drift.",
        "ERROR BREAKDOWN BY CATEGORY (generated from the lines below):",
        "  field_lost_team  76",
        "  extra_object  16",
        "  render_failure  1",
    ]
    result = san.parse("\n".join(header))
    assert result.unparsed == [], f"sanitizer could not parse: {result.unparsed}"
    assert result.records == [], "header lines were mistaken for error records"


# ---------------------------------------------------------------------------
# 4. Settling the eight — error.txt's "TWO POSSIBLE READINGS"
# ---------------------------------------------------------------------------
#
# Reading 1: the summary is older, describing a pre-fix run, and these eight
#            were genuinely fixed between runs.
# Reading 2: the summary is from a different input set, and they are still live.
#
# The previous analysis round documented every one of the eight as a real defect
# and marked each FIXED. These tests assert the fix is present in the code
# TODAY, which is what makes Reading 1 the correct one. They are guard tests:
# each fails the moment one of those eight regresses, so this question cannot
# come back a third time as an open question.

def _values(doc, kind="Deployment"):
    ctx = conv.MicroserviceContext(namespace="shop-web", name="api")
    conv.handle_workload(doc, ctx, kind)
    return ctx.values


def _wl(pod_spec=None, spec_extra=None, container=None):
    return {
        "apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "api"},
        "spec": {**(spec_extra or {}), "template": {"metadata": {}, "spec": {
            **(pod_spec or {}),
            "containers": [{"name": "api", "image": "nginx:1.25", **(container or {})}],
        }}},
    }


def test_eight_1_imagePullSecrets_still_captured():
    v = _values(_wl(pod_spec={"imagePullSecrets": [{"name": "ghcr-pull"}]}))
    assert v.get("imagePullSecrets") == [{"name": "ghcr-pull"}]


def test_eight_2_serviceaccount_pull_secrets_resolved_as_decided_last_round():
    """Not "still captured" — this one was resolved by DECIDING what to carry,
    and the decision is the thing to pin.

    ERRORS_ANALYSIS/ServiceAccount_Secrets_And_ImagePullSecrets_Lost split the
    report's single row in two: a hand-added registry credential must survive
    (without it every pod on this SA fails ImagePullBackOff), while `secrets:`
    and OpenShift's auto-stamped `<sa>-dockercfg-XXXXX` must NOT — Kubernetes
    creates and rotates those itself, and on a vanilla cluster the referenced
    Secret never exists at all.

    This round's K8s_Runtime_Defaults_Benign_Suppress independently reaches the
    same verdict ("ServiceAccount imagePullSecrets/secrets (50 instances) —
    OpenShift auto-provisions them"), so the two rounds agree and the absence
    of these from the report is correct behaviour rather than a missing fix."""
    ctx = conv.MicroserviceContext(namespace="shop-web", name="api")
    conv.handle_serviceaccount({
        "apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": "api"},
        "secrets": [{"name": "api-token-abcde"}],
        "imagePullSecrets": [{"name": "ghcr-pull"}, {"name": "api-dockercfg-x7k2p"}],
    }, ctx, "api")
    sa = ctx.values["serviceAccount"]
    assert sa.get("imagePullSecrets") == [{"name": "ghcr-pull"}], \
        "the hand-added registry credential must survive"
    assert not any(s["name"].endswith("-dockercfg-x7k2p")
                   for s in sa.get("imagePullSecrets", [])), \
        "an OpenShift auto-stamped dockercfg reference was carried into the chart"
    assert "secrets" not in sa, "SA token references must not be declared by the chart"


def test_eight_3_container_resources_still_captured():
    res = {"limits": {"cpu": "500m", "memory": "1Gi"}}
    v = _values(_wl(container={"resources": res}))
    assert v.get("resources") == res


def test_eight_4_progressDeadlineSeconds_still_captured():
    v = _values(_wl(spec_extra={"progressDeadlineSeconds": 120}))
    assert v.get("progressDeadlineSeconds") == 120


def test_eight_5_pod_securityContext_still_captured():
    sc = {"runAsUser": 1000, "fsGroup": 2000}
    v = _values(_wl(pod_spec={"securityContext": sc}))
    assert v.get("podSecurityContext") == sc


def test_eight_6_service_sessionAffinity_still_captured():
    entry = conv.build_service_entry({
        "apiVersion": "v1", "kind": "Service", "metadata": {"name": "api"},
        "spec": {"type": "ClusterIP", "sessionAffinity": "ClientIP",
                 "ports": [{"name": "http", "port": 80}]},
    })
    assert entry.get("sessionAffinity") == "ClientIP"


def test_eight_7_cronjob_config_still_captured():
    ctx = conv.MicroserviceContext(namespace="shop-web", name="api")
    conv.handle_cronjob({
        "apiVersion": "batch/v1", "kind": "CronJob", "metadata": {"name": "api-backup"},
        "spec": {"schedule": "17 3 * * *", "concurrencyPolicy": "Forbid",
                 "jobTemplate": {"spec": {"backoffLimit": 4, "template": {"spec": {
                     "serviceAccountName": "backup-sa", "restartPolicy": "OnFailure",
                     "containers": [{"name": "backup", "image": "b:1"}]}}}}},
    }, ctx, "api-backup")
    cj = ctx.values["cronjobs"]["api-backup"]
    assert cj["schedule"] == "17 3 * * *"
    assert cj["concurrencyPolicy"] == "Forbid"
    assert cj["jobTemplate"]["backoffLimit"] == 4
    assert cj["jobTemplate"]["serviceAccountName"] == "backup-sa"


def test_eight_8_volumeMount_subPath_still_survives_a_duplicate_name():
    """The previous round's data-loss bug: two mounts sharing a volume name and
    differing only by subPath collapsed onto one key and one was lost."""
    mounts = [
        {"name": "config", "mountPath": "/etc/a.conf", "subPath": "a.conf"},
        {"name": "config", "mountPath": "/etc/b.conf", "subPath": "b.conf"},
    ]
    out = conv.named_list_to_map(mounts)
    assert len(out) == 2, f"a subPath mount was dropped: {out}"
    assert {e.get("subPath") for e in out.values()} == {"a.conf", "b.conf"}
