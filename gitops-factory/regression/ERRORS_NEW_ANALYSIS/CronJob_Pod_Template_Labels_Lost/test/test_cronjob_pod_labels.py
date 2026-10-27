"""
Regression tests for ERRORS_NEW_ANALYSIS/CronJob_Pod_Template_Labels_Lost.

30 errors, rated P1, and error.txt is unusually precise about the shape:

  "TWO INDEPENDENT DEFECTS, BOTH MUST BE FIXED — capturing the labels without
   rendering them changes nothing, and vice versa:
     1. CONVERTER: handle_cronjob() must extract
        spec.jobTemplate.spec.template.metadata.labels into a podLabels key.
        handle_workload() already does exactly this — copy that pattern.
        handle_job() has the same gap.
     2. TEMPLATE: the CronJob pod template renders only selectorLabels
        (app.name + app.instance). It needs an objectLabels/podLabels merge."

Both halves were reproduced independently against the real corpus, each
producing the SAME 11 fidelity errors on its own, which is what makes "both
must be fixed" a measured claim rather than an assumption. See STATUS.md §4.

WHY THIS IS NOT COSMETIC. The report rates it P1 with the consequence
"Sidecar injection could crash CronJobs", and that is the right reading:
pod-template labels and annotations are what admission webhooks and sidecar
injectors select on. A CronJob pod stripped of them can miss an injection it
depends on — a vault-agent sidecar that never mounts the secret — or receive
one it was meant to be excluded from. The five lost labels are governance
metadata; the lost `sidecar.istio.io/inject` annotation is a control signal.

The fix is a SEAM, not a third copy: pod_template_labels() /
pod_template_annotations() are now shared by handle_workload, handle_cronjob and
handle_job. The previous round fixed this class once for workloads and left two
handlers behind — a third inline copy would have set up the same recurrence.
"""
import importlib.util
import pathlib
import subprocess
import sys

import pytest
import yaml

TEST_DIR = pathlib.Path(__file__).resolve().parent
GITOPS_DIR = TEST_DIR.parents[3]
CHART_DIR = GITOPS_DIR.parent
CONVERTER_PY = GITOPS_DIR / "convert_to_universal_chart.py"
SHOWCASE = GITOPS_DIR / "namespaces-showcase"


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


conv = _load(CONVERTER_PY, "convert_to_universal_chart")

# The exact five the report names, in its own spelling (note operationalPurpose
# is camelCase here and operational.purpose on Routes — they are different
# labels in the source data, not a transcription slip).
POD_LABELS = {
    "team": "devops-mahan",
    "environment": "dev",
    "critical": "false",
    "operational": "true",
    "operationalPurpose": "backup-cronjob",
}
POD_ANN = {"sidecar.istio.io/inject": "false"}


def _ctx():
    return conv.MicroserviceContext(namespace="background-jobs", name="jenkins-backup")


def _cronjob(labels=None, annotations=None):
    meta = {}
    if labels is not None:
        meta["labels"] = dict(labels)
    if annotations is not None:
        meta["annotations"] = dict(annotations)
    return {
        "apiVersion": "batch/v1", "kind": "CronJob",
        "metadata": {"name": "jenkins-backup-dev"},
        "spec": {"schedule": "0 2 * * *", "jobTemplate": {"spec": {"template": {
            "metadata": meta,
            "spec": {"restartPolicy": "Never",
                     "containers": [{"name": "backup", "image": "busybox:1.36"}]},
        }}}},
    }


def _job(labels=None, annotations=None):
    meta = {}
    if labels is not None:
        meta["labels"] = dict(labels)
    if annotations is not None:
        meta["annotations"] = dict(annotations)
    return {
        "apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": "db-migrate"},
        "spec": {"template": {
            "metadata": meta,
            "spec": {"restartPolicy": "Never",
                     "containers": [{"name": "migrate", "image": "busybox:1.36"}]},
        }},
    }


# ---------------------------------------------------------------------------
# 1. The shared seam
# ---------------------------------------------------------------------------

def test_pod_template_labels_excludes_only_the_replicaset_hash():
    """SUPERSEDED: name/instance/app used to be dropped and re-derived from
    Release.Name, which rewrote the live labels. Only the ReplicaSet
    controller's pod-template-hash is dropped now."""
    out = conv.pod_template_labels({"labels": {
        **POD_LABELS,
        "app": "jenkins-backup",
        "app.kubernetes.io/name": "jenkins-backup",
        "app.kubernetes.io/instance": "jenkins-backup",
        "pod-template-hash": "7d9f8b6c4",
    }})
    assert out == {**POD_LABELS, "app": "jenkins-backup",
                   "app.kubernetes.io/name": "jenkins-backup",
                   "app.kubernetes.io/instance": "jenkins-backup"}


def test_pod_template_annotations_drops_only_checksum_keys():
    out = conv.pod_template_annotations({"annotations": {
        **POD_ANN,
        "checksum/config": "abc123",
        "prometheus.io/scrape": "true",
    }})
    assert out == {**POD_ANN, "prometheus.io/scrape": "true"}


def test_the_seam_is_actually_shared_by_all_three_handlers():
    """The point of extracting it. If a handler goes back to an inline filter,
    this fails — which is the recurrence this folder exists to stop."""
    src = CONVERTER_PY.read_text(encoding="utf-8")
    assert src.count("pod_template_labels(") >= 4, \
        "pod_template_labels is no longer called from all three handlers"
    assert 'if k not in ("pod-template-hash"' not in src, \
        "an inline copy of the pod-label filter reappeared"


# ---------------------------------------------------------------------------
# 2. Converter half — the gap error.txt names
# ---------------------------------------------------------------------------

def test_handle_cronjob_captures_pod_template_labels():
    ctx = _ctx()
    conv.handle_cronjob(_cronjob(POD_LABELS), ctx, "jenkins-backup-dev")
    assert ctx.values["cronjobs"]["jenkins-backup-dev"]["jobTemplate"]["podLabels"] == POD_LABELS


def test_handle_cronjob_captures_pod_template_annotations():
    ctx = _ctx()
    conv.handle_cronjob(_cronjob(POD_LABELS, POD_ANN), ctx, "jenkins-backup-dev")
    jt = ctx.values["cronjobs"]["jenkins-backup-dev"]["jobTemplate"]
    assert jt["podAnnotations"] == POD_ANN


def test_handle_job_captures_pod_template_labels():
    """error.txt: "handle_job() has the same gap." """
    ctx = _ctx()
    conv.handle_job(_job(POD_LABELS), ctx, "db-migrate")
    assert ctx.values["jobs"]["db-migrate"]["podLabels"] == POD_LABELS


@pytest.mark.parametrize("label", sorted(POD_LABELS))
def test_each_of_the_five_named_labels_survives_individually(label):
    """The report's five per-label triage files are five views of this one
    defect. Parametrised so a future filter dropping one key names that key."""
    ctx = _ctx()
    conv.handle_cronjob(_cronjob({label: POD_LABELS[label]}), ctx, "jenkins-backup-dev")
    assert ctx.values["cronjobs"]["jenkins-backup-dev"]["jobTemplate"]["podLabels"] == \
        {label: POD_LABELS[label]}


def test_pod_labels_are_distinct_from_the_cronjob_objects_own_labels():
    """Two different label sets at two different paths. handle_cronjob already
    read the object's own metadata.labels — which is precisely why the pod-level
    loss was easy to miss: `labels` was present in the values, just not these."""
    doc = _cronjob(POD_LABELS)
    doc["metadata"]["labels"] = {"team": "platform-owned-object"}
    ctx = _ctx()
    conv.handle_cronjob(doc, ctx, "jenkins-backup-dev")
    entry = ctx.values["cronjobs"]["jenkins-backup-dev"]
    assert entry["labels"] == {"team": "platform-owned-object"}
    assert entry["jobTemplate"]["podLabels"]["team"] == "devops-mahan"


def test_absent_pod_metadata_produces_no_keys():
    ctx = _ctx()
    conv.handle_cronjob(_cronjob(), ctx, "jenkins-backup-dev")
    jt = ctx.values["cronjobs"]["jenkins-backup-dev"]["jobTemplate"]
    assert "podLabels" not in jt and "podAnnotations" not in jt


# ---------------------------------------------------------------------------
# 3. Template half — "renders only selectorLabels"
# ---------------------------------------------------------------------------

def _render(values, tmp_path):
    vf = tmp_path / "values.yaml"
    vf.write_text(yaml.safe_dump(values), encoding="utf-8")
    out = subprocess.run(
        ["helm", "template", "jenkins-backup", str(CHART_DIR), "--namespace", "background-jobs",
         "-f", str(vf)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert out.returncode == 0, out.stderr
    return [d for d in yaml.safe_load_all(out.stdout) if d]


BASE = {"workload": {"type": "deployment"}, "image": {"repository": "busybox", "tag": "1.36"}}


def test_cronjob_template_renders_pod_labels_alongside_the_selector_labels(tmp_path):
    docs = _render({**BASE, "chartLabels": True, "cronjobs": {"backup": {
        "schedule": "0 2 * * *",
        "jobTemplate": {"image": {"repository": "busybox"}, "podLabels": POD_LABELS},
    }}}, tmp_path)
    cj = next(d for d in docs if d["kind"] == "CronJob")
    rendered = cj["spec"]["jobTemplate"]["spec"]["template"]["metadata"]["labels"]
    for k, v in POD_LABELS.items():
        assert rendered.get(k) == v, f"{k} missing from the rendered CronJob pod template"
    # ...and the selector labels are still there, unclobbered.
    assert rendered["app.kubernetes.io/name"] == "jenkins-backup"
    assert rendered["app.kubernetes.io/instance"] == "jenkins-backup"


def test_cronjob_template_renders_pod_annotations(tmp_path):
    docs = _render({**BASE, "cronjobs": {"backup": {
        "schedule": "0 2 * * *",
        "jobTemplate": {"image": {"repository": "busybox"}, "podAnnotations": POD_ANN},
    }}}, tmp_path)
    cj = next(d for d in docs if d["kind"] == "CronJob")
    meta = cj["spec"]["jobTemplate"]["spec"]["template"]["metadata"]
    assert meta["annotations"] == POD_ANN


def test_job_template_renders_pod_labels(tmp_path):
    docs = _render({**BASE, "jobs": {"migrate": {
        "image": {"repository": "busybox"}, "podLabels": POD_LABELS,
    }}}, tmp_path)
    job = next(d for d in docs if d["kind"] == "Job")
    rendered = job["spec"]["template"]["metadata"]["labels"]
    for k, v in POD_LABELS.items():
        assert rendered.get(k) == v


def test_no_annotations_key_is_emitted_when_there_are_none(tmp_path):
    """An empty `annotations: {}` on every CronJob pod template would be an
    invented field on every diff."""
    docs = _render({**BASE, "cronjobs": {"backup": {
        "schedule": "0 2 * * *", "jobTemplate": {"image": {"repository": "busybox"}},
    }}}, tmp_path)
    cj = next(d for d in docs if d["kind"] == "CronJob")
    assert "annotations" not in cj["spec"]["jobTemplate"]["spec"]["template"]["metadata"]


# ---------------------------------------------------------------------------
# 4. Corpus coverage
# ---------------------------------------------------------------------------

def _corpus_docs(kind):
    return [d for f in SHOWCASE.rglob("*.yaml")
            for d in yaml.safe_load_all(f.read_text(encoding="utf-8"))
            if d and d.get("kind") == kind]


def test_a_corpus_cronjob_carries_pod_template_labels():
    metas = [(d["spec"]["jobTemplate"]["spec"]["template"].get("metadata") or {})
             for d in _corpus_docs("CronJob")]
    assert any(m.get("labels") for m in metas), \
        "no CronJob in the corpus carries pod-template labels — the check is blind again"


def test_a_corpus_job_carries_pod_template_labels():
    metas = [(d["spec"]["template"].get("metadata") or {}) for d in _corpus_docs("Job")]
    assert any(m.get("labels") for m in metas), \
        "no Job in the corpus carries pod-template labels"


def test_a_corpus_cronjob_carries_an_injector_annotation():
    """The P1 half. Without an annotation in the corpus, only the metadata loss
    is guarded and the sidecar-injection risk stays untested."""
    metas = [(d["spec"]["jobTemplate"]["spec"]["template"].get("metadata") or {})
             for d in _corpus_docs("CronJob")]
    assert any(m.get("annotations") for m in metas)
