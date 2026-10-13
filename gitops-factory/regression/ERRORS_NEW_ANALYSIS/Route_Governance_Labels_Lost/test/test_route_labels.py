"""
Regression tests for ERRORS_NEW_ANALYSIS/Route_Governance_Labels_Lost.

The largest single error class in the report — 613-705 occurrences, more than
all other defects combined — and the report gives THREE incompatible
instructions for it:

  * remediation table: "Fix custom labels on Routes (~613) | -613 | 80 remaining"
  * summary table:     "Route labels | 705 | Suppress in _fidelity_ignored_path()
                        for Routes specifically"
  * a third note:      "Route metadata.labels are NOT suppressed by design"

DECISION: FIX. Both Route templates already render `.labels` through
universal-chart.objectLabels; the converter simply never filled it. Suppressing
would hide a real loss the report's own priority table rates P1 with the
consequence stated plainly — "Breaks ArgoCD sync tracking, team/env filtering".

THE CAUSE IS A HALF-FIX, NOT AN OVERSIGHT IN THE USUAL SENSE. Every other
handler that emits an object calls raw_labels(). build_route_entry carried the
COMMENT "labels handled at the end of this function via raw_labels(doc)" and
never made the call — the note was written, the line was not.

ONE EXCEPTION, and the report identifies it correctly: argocd.argoproj.io/instance
(92) is "Unavoidable — ArgoCD injects at deploy". A chart cannot know which
Application will sync it, and rendering a guess would fight ArgoCD's own
tracking. That single key is suppressed on both sides. "The other ~520 are not."
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

# The exact label set the report names, per its own field-path table.
GOVERNANCE = {
    "team": "identity-auth",
    "environment": "prd",
    "critical": "true",
    "operational": "false",
    "operational.purpose": "policy-decision-endpoint",
    "app.openshift.io/runtime": "golang",
    "opentelemetry.io/enabled": "true",
}
ARGOCD_INJECTED = "argocd.argoproj.io/instance"


def _route(labels=None, name="opa-route"):
    return {
        "apiVersion": "route.openshift.io/v1", "kind": "Route",
        "metadata": {"name": name, "labels": dict(labels if labels is not None else GOVERNANCE)},
        "spec": {"host": "opa.apps.example.com",
                 "to": {"kind": "Service", "name": "opa"},
                 "port": {"targetPort": "http"}},
    }


def _ctx():
    return conv.MicroserviceContext(namespace="identity-auth-prd", name="opa")


# ---------------------------------------------------------------------------
# 1. The converter half — the line that was missing
# ---------------------------------------------------------------------------

def test_build_route_entry_captures_governance_labels():
    entry = conv.build_route_entry(_route(), _ctx())
    assert entry.get("labels") == GOVERNANCE, \
        "build_route_entry dropped the Route's own metadata.labels"


@pytest.mark.parametrize("label", sorted(GOVERNANCE))
def test_each_named_governance_label_survives_individually(label):
    """Parametrised per label because the report's nine per-label triage files
    are nine VIEWS of this one defect. If a future filter drops one key, the
    failure should name that key rather than a whole-dict mismatch."""
    entry = conv.build_route_entry(_route({label: GOVERNANCE[label]}), _ctx())
    assert entry["labels"] == {label: GOVERNANCE[label]}


def test_extra_routes_carry_labels_too_not_just_the_primary():
    """handle_route sends the first Route to .Values.route and the rest to
    .Values.routes. Both templates render labels, so both paths must fill them
    — an extra Route fronting an admin backend is exactly the kind that carries
    distinct governance metadata."""
    ctx = _ctx()
    conv.handle_route(_route(name="opa-route"), ctx, "opa-route")
    conv.handle_route(_route({"team": "sre"}, name="opa-admin-route"), ctx, "opa-admin-route")
    assert ctx.values["route"]["labels"] == GOVERNANCE
    assert ctx.values["routes"]["opa-admin-route"]["labels"] == {"team": "sre"}


def test_a_route_with_no_labels_gets_no_labels_key():
    """Absent, not empty. An invented `labels: {}` would show up as an ADDED
    field on every unlabelled Route."""
    entry = conv.build_route_entry(_route({}), _ctx())
    assert "labels" not in entry


# ---------------------------------------------------------------------------
# 2. The one authorised suppression — scoped to a single key
# ---------------------------------------------------------------------------

def test_argocd_instance_label_is_not_carried_into_the_chart():
    """ArgoCD writes it at sync time. A chart rendering a guessed Application
    name would fight ArgoCD's own tracking on the next sync."""
    entry = conv.build_route_entry(_route({**GOVERNANCE, ARGOCD_INJECTED: "id-auth-opa"}), _ctx())
    assert ARGOCD_INJECTED not in entry["labels"]


def test_argocd_instance_label_is_not_reported_as_a_loss():
    """Both halves are needed. Excluding it from capture without suppressing it
    in the checker would simply move the noise from the chart to the report."""
    diffs = conv._fidelity_diff(
        "Route",
        {"metadata": {"name": "opa-route", "labels": {ARGOCD_INJECTED: "id-auth-opa"}}},
        {"metadata": {"name": "opa-route", "labels": {}}},
    )
    assert diffs == [], f"the ArgoCD tracking label was reported as lost: {diffs}"


def test_the_suppression_did_not_widen_to_labels_in_general():
    """The failure mode this whole folder guards against: making the report
    read 0 by blinding the checker rather than by fixing the converter. A
    genuinely lost governance label must still report."""
    diffs = conv._fidelity_diff(
        "Route",
        {"metadata": {"name": "opa-route", "labels": {"team": "identity-auth"}}},
        {"metadata": {"name": "opa-route", "labels": {}}},
    )
    assert [d[1] for d in diffs] == ["metadata.labels.team"]


def test_only_one_argocd_key_is_suppressed():
    """`app.kubernetes.io/applicationSet` appears in the report's field table as
    an "ArgoCD ApplicationSet label", but it is not something ArgoCD injects —
    it is a label the source org's ApplicationSet template sets, i.e. custom
    metadata the chart must carry. Suppressing it because the name looks
    ArgoCD-ish would silently drop 75 real labels."""
    entry = conv.build_route_entry(
        _route({"app.kubernetes.io/applicationSet": "identity-auth"}), _ctx())
    assert entry["labels"] == {"app.kubernetes.io/applicationSet": "identity-auth"}


# ---------------------------------------------------------------------------
# 3. The template half — already correct, pinned so it stays that way
# ---------------------------------------------------------------------------

def _helm_template(values: dict, tmp_path):
    vf = tmp_path / "values.yaml"
    vf.write_text(yaml.safe_dump(values), encoding="utf-8")
    out = subprocess.run(
        ["helm", "template", "opa", str(CHART_DIR), "--namespace", "identity-auth-prd",
         "-f", str(vf)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert out.returncode == 0, out.stderr
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def _route_doc(docs, name):
    for d in docs:
        if d.get("kind") == "Route" and d["metadata"]["name"] == name:
            return d
    raise AssertionError(f"no Route/{name} in render: "
                         f"{[(d.get('kind'), d['metadata'].get('name')) for d in docs]}")


def test_primary_route_template_renders_the_labels(tmp_path):
    docs = _helm_template({
        "workload": {"type": "deployment"},
        "image": {"repository": "openpolicyagent/opa", "tag": "0.60"},
        "service": {"ports": {"http": {"port": 8181}}},
        "route": {"enabled": True, "name": "opa-route", "host": "opa.apps.example.com",
                  "serviceName": "opa", "targetPort": "http", "labels": GOVERNANCE},
    }, tmp_path)
    rendered = _route_doc(docs, "opa-route")["metadata"]["labels"]
    for k, v in GOVERNANCE.items():
        assert rendered.get(k) == v, f"{k} missing from the rendered Route"


def test_extra_routes_template_renders_the_labels(tmp_path):
    docs = _helm_template({
        "workload": {"type": "deployment"},
        "image": {"repository": "openpolicyagent/opa", "tag": "0.60"},
        "service": {"ports": {"http": {"port": 8181}}},
        "routes": {"opa-admin-route": {"enabled": True, "serviceName": "opa",
                                       "targetPort": "http", "labels": {"team": "sre"}}},
    }, tmp_path)
    assert _route_doc(docs, "opa-admin-route")["metadata"]["labels"]["team"] == "sre"


def test_chart_identity_labels_are_not_clobbered_by_custom_ones(tmp_path):
    """objectLabels merges rather than replaces. A custom label set that wiped
    app.kubernetes.io/instance would break selectors elsewhere."""
    docs = _helm_template({
        "workload": {"type": "deployment"}, "chartLabels": True,
        "image": {"repository": "openpolicyagent/opa", "tag": "0.60"},
        "service": {"ports": {"http": {"port": 8181}}},
        "route": {"enabled": True, "name": "opa-route", "serviceName": "opa",
                  "targetPort": "http", "labels": GOVERNANCE},
    }, tmp_path)
    labels = _route_doc(docs, "opa-route")["metadata"]["labels"]
    assert labels["app.kubernetes.io/instance"] == "opa"
    assert labels["app.kubernetes.io/managed-by"] == "Helm"
    assert labels["team"] == "identity-auth"


# ---------------------------------------------------------------------------
# 4. Corpus coverage — the reason this was invisible for a whole round
# ---------------------------------------------------------------------------

def test_the_corpus_now_contains_a_route_at_all():
    """`namespaces-showcase` had ZERO Routes, so the round-trip fidelity check
    reported 0 for the largest defect class in the analysis purely because it
    had nothing to look at. Without this, the fix above is provable only by
    unit test and can silently rot again."""
    routes = [d for f in SHOWCASE.rglob("*.yaml")
              for d in yaml.safe_load_all(f.read_text(encoding="utf-8"))
              if d and d.get("kind") == "Route"]
    assert routes, "no Route in the corpus — the standing fidelity check is blind to this defect"


def test_the_corpus_route_carries_the_labels_the_report_names():
    routes = [d for f in SHOWCASE.rglob("*.yaml")
              for d in yaml.safe_load_all(f.read_text(encoding="utf-8"))
              if d and d.get("kind") == "Route"]
    labels = {k for r in routes for k in (r["metadata"].get("labels") or {})}
    missing = set(GOVERNANCE) - labels
    assert not missing, f"corpus Route(s) no longer exercise: {sorted(missing)}"
    assert ARGOCD_INJECTED in labels, \
        "the corpus no longer proves the ArgoCD suppression is scoped to one key"
