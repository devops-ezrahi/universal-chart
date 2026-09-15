"""
Regression tests for ERRORS_ANALYSIS/Workload_Metadata_Labels_Lost.

The single highest-count defect in the report: ~634 label fields / 3,087 labels.
Two independent halves, both real, and the report is right about both:

  A. most converter handlers never read `metadata.labels`;
  B. nearly every chart template rendered `universal-chart.labels` alone, with
     no block for the object's own labels.

Measured before the fix: **19 of the chart's templates** had no own-labels block
at all, and only 5 handlers captured labels.

Two corrections to the report's framing, in STATUS.md §1:

  * It rates this LOW / "cosmetic-metadata-only". `argocd.argoproj.io/instance`
    is in the same bucket, and losing it makes ArgoCD stop managing the object
    while it stays on the cluster. The report's own MEDIUM #12 says so about
    ExternalSecrets. Not cosmetic.
  * It says `service.yaml` has no `$spec.labels` block (issue 33). It did have
    one — but as a SECOND `labels:` entry under the same key, which is a
    duplicate YAML key. That is worse than missing: Helm keeps the last, so a
    values file could silently overwrite `app.kubernetes.io/name` and drift
    metadata away from what the selector matches.

The fix is one shared helper, `universal-chart.objectLabels`, used by every
template, which builds ONE merged map with the chart's identity labels winning.
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
TEMPLATES = CHART_DIR / "templates"
CONVERTER_PY = GITOPS_DIR / "convert_to_universal_chart.py"
FIXTURE_DIR = TEST_DIR / "fixture"
FIXTURE = FIXTURE_DIR / "shop-web" / "api-gateway.yaml"

NS = "shop-web"
RELEASE = "api-gateway"
TEAM = ("team", "storefront")
ARGOCD = ("argocd.argoproj.io/instance", "shop-web-api-gateway")


def _load_converter():
    spec = importlib.util.spec_from_file_location("convert_to_universal_chart", CONVERTER_PY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["convert_to_universal_chart"] = mod
    spec.loader.exec_module(mod)
    return mod


conv = _load_converter()


def _doc(kind, name=None):
    for d in yaml.safe_load_all(FIXTURE.read_text(encoding="utf-8")):
        if d and d.get("kind") == kind and (name is None or d["metadata"]["name"] == name):
            return d
    raise AssertionError(f"no {kind}/{name} in fixture")


def _helm_template(values: dict, release=RELEASE):
    with tempfile.TemporaryDirectory() as tmp:
        vf = pathlib.Path(tmp) / "values.yaml"
        vf.write_text(yaml.safe_dump(values, sort_keys=False), encoding="utf-8")
        proc = subprocess.run(["helm", "template", release, str(CHART_DIR), "-f", str(vf)],
                              capture_output=True, text=True)
    assert proc.returncode == 0, f"helm template failed:\n{proc.stderr}"
    return [d for d in yaml.safe_load_all(proc.stdout) if d]


# --------------------------------------------------------------------------
# Half B — no template may render the bare helper for an object's metadata
# --------------------------------------------------------------------------

def test_no_template_renders_the_bare_identity_helper_for_object_metadata():
    """The structural guard. A new template that copy-pastes the old
    `{{- include "universal-chart.labels" $ }}` line reintroduces the defect for
    its own kind, and nothing else would notice."""
    offenders = []
    for p in sorted(TEMPLATES.glob("*.yaml")):
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if 'include "universal-chart.labels"' in line and "fromYaml" not in line:
                offenders.append(f"{p.name}:{i}: {line.strip()}")
    assert not offenders, (
        "these templates render the identity labels alone, dropping the object's "
        "own metadata.labels:\n" + "\n".join(offenders))


def test_no_template_emits_two_labels_blocks_under_one_key():
    """A duplicate YAML key: some parsers reject it, Helm keeps the last, and a
    values file could overwrite an identity label."""
    offenders = []
    for p in sorted(TEMPLATES.glob("*.yaml")):
        lines = p.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            if "objectLabels" not in line:
                continue
            nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
            if nxt.startswith("{{- with") and ".labels" in nxt:
                offenders.append(f"{p.name}:{i + 2}: {nxt}")
    assert not offenders, "second labels block still present:\n" + "\n".join(offenders)


# --------------------------------------------------------------------------
# Half A — the converter must capture them
# --------------------------------------------------------------------------

def test_raw_labels_keeps_custom_labels_and_drops_chart_owned_ones():
    """SUPERSEDED IN PART. This asserted `argocd.argoproj.io/instance` is carried.
    A later round moved it into CHART_OWNED_LABELS and gave the reason:

        "ArgoCD stamps this on every object it syncs, naming the Application that
         owns it. [...] the chart cannot know which Application will deploy it,
         and rendering a guess would fight ArgoCD's own tracking on the next
         sync."

    The report's own field table agrees — it is the one row there marked
    "Unavoidable — ArgoCD injects at deploy". So the label is now asserted to be
    DROPPED, and the custom governance labels this folder actually exists for are
    still asserted to be carried."""
    got = conv.raw_labels(_doc("Deployment"))
    assert got[TEAM[0]] == TEAM[1]
    assert ARGOCD[0] not in got, (
        "argocd.argoproj.io/instance must not be carried — the chart would be "
        "guessing the Application name")
    assert got["app.kubernetes.io/part-of"] == "shop-web"
    assert "app" not in got, (
        "`app` is the pre-Helm convention for app.kubernetes.io/name and is the "
        "chart's to set; carrying it would contradict selectorLabels")


@pytest.mark.parametrize("kind,name,values_path", [
    ("Deployment", "api-gateway", ("labels",)),
    ("Service", "api-gateway", ("service", "labels")),
    ("ConfigMap", "api-gateway-config", ("configMaps", "api-gateway-config", "labels")),
    ("ServiceAccount", "api-gateway", ("serviceAccount", "labels")),
    ("PersistentVolumeClaim", "api-gateway-cache", ("pvc", "api-gateway-cache", "labels")),
    ("NetworkPolicy", "api-gateway-allow", ("networkPolicies", "api-gateway-allow", "labels")),
    ("Role", "api-gateway-reader", ("rbac", "roles", "api-gateway-reader", "labels")),
    ("CronJob", "api-gateway-rotate", ("cronjobs", "api-gateway-rotate", "labels")),
])
def test_every_handler_captures_metadata_labels(kind, name, values_path):
    ctx = conv.MicroserviceContext(namespace=NS, name=RELEASE)
    doc = _doc(kind, name)
    if kind == "Deployment":
        conv.handle_workload(doc, ctx, "Deployment")
    elif kind == "Service":
        conv.handle_service(doc, ctx, name)
    elif kind == "ConfigMap":
        conv.handle_configmap(doc, ctx, name)
    elif kind == "ServiceAccount":
        conv.handle_serviceaccount(doc, ctx, name)
    elif kind == "PersistentVolumeClaim":
        conv.handle_pvc(doc, ctx, name)
    elif kind == "NetworkPolicy":
        conv.handle_networkpolicy(doc, ctx, name)
    elif kind == "Role":
        conv.handle_role(doc, ctx, name)
    elif kind == "CronJob":
        conv.handle_cronjob(doc, ctx, name)

    cur = ctx.values
    for key in values_path:
        assert isinstance(cur, dict) and key in cur, (
            f"{kind}/{name}: no labels at values path {'.'.join(values_path)}; "
            f"got {sorted(cur) if isinstance(cur, dict) else cur}")
        cur = cur[key]
    assert cur.get(TEAM[0]) == TEAM[1], f"{kind}/{name}: team label lost, got {cur}"


def test_pod_template_labels_still_work():
    """Control: the one path that already worked (podLabels) must not change."""
    ctx = conv.MicroserviceContext(namespace=NS, name=RELEASE)
    conv.handle_workload(_doc("Deployment"), ctx, "Deployment")
    assert ctx.values["podLabels"] == {"team": "storefront"}


# --------------------------------------------------------------------------
# Both halves together
# --------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_identity_labels_win_over_declared_ones():
    values = {"workload": {"type": "deployment"},
              "image": {"repository": "nginx", "tag": "1.27"},
              "labels": {"app.kubernetes.io/name": "hijacked", "team": "storefront"}}
    labels = next(d for d in _helm_template(values)
                  if d["kind"] == "Deployment")["metadata"]["labels"]
    assert labels["app.kubernetes.io/name"] != "hijacked"
    assert labels["team"] == "storefront"


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_round_trip_every_custom_label_reaches_the_rendered_object(tmp_path):
    out = tmp_path / "out"
    proc = subprocess.run(
        [sys.executable, str(CONVERTER_PY),
         "--input", str(FIXTURE_DIR), "--output", str(out),
         "--chart", str(CHART_DIR), "--skip-verify"],
        capture_output=True, text=True, cwd=str(GITOPS_DIR),
    )
    assert proc.returncode == 0, f"converter failed:\n{proc.stdout}\n{proc.stderr}"

    merged: dict = {}
    for vf in (out / NS / "defaults.yaml", out / "base" / f"{RELEASE}.yaml",
               out / NS / "values" / f"{RELEASE}.yaml"):
        if vf.exists():
            merged = conv.deep_merge(merged, yaml.safe_load(vf.read_text(encoding="utf-8")) or {})

    rendered = {(d["kind"], d["metadata"]["name"]): d.get("metadata", {}).get("labels", {})
                for d in _helm_template(merged)}

    missing = []
    for raw in yaml.safe_load_all(FIXTURE.read_text(encoding="utf-8")):
        if not raw:
            continue
        want = conv.raw_labels(raw)
        if not want:
            continue
        key = (raw["kind"], raw["metadata"]["name"])
        got = next((v for k, v in rendered.items() if k[0] == raw["kind"]), None)
        if got is None:
            continue  # kind not rendered by this release; covered elsewhere
        for k, v in want.items():
            if got.get(k) != v:
                missing.append(f"{key}: {k}={v!r} -> {got.get(k)!r}")
    assert not missing, "labels lost end to end:\n" + "\n".join(missing)


def test_fidelity_checker_reports_a_lost_custom_label():
    raw = _doc("Deployment")
    rendered = yaml.safe_load(yaml.safe_dump(raw))
    rendered["metadata"]["labels"].pop("team")
    diffs = conv._fidelity_diff("Deployment", raw, rendered)
    assert any(d[0] == "LOST" and "team" in d[2] + d[1] for d in diffs), f"diffs={diffs}"
