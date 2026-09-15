"""
Regression tests for ERRORS_ANALYSIS/Annotations_Lost.

The report rates this LOW — "deployment.kubernetes.io/revision, OpenShift
annotations - informational only" — but error.txt flags its own wording as the
warning sign: the pattern table says *"Mixed: revisions, OpenShift-specific"*,
and **"Mixed" means the bucket was never triaged.** It lists what must be
checked before accepting LOW: `argocd.argoproj.io/*` (a lost sync-wave reorders
the entire deploy sequence), ingress/route behaviour annotations,
`prometheus.io/scrape` (losing it silently stops metrics collection), and
`checksum/config` (losing it stops pods restarting on a config change).

Triage done (STATUS.md §1). Of 2,411 annotations across all three manifest
trees:

    2405  kubectl.kubernetes.io/last-applied-configuration   server-generated
       3  traefik.ingress.kubernetes.io/router.entrypoints   NOT SAFE
       3  traefik.ingress.kubernetes.io/router.tls           NOT SAFE

Zero `argocd.argoproj.io/*`, zero `prometheus.io/*`, zero `checksum/*`. So the
LOW rating holds for this repo — but only because the six that are NOT safe
happen to sit on Ingresses, the one kind whose handler already captured
annotations. Eight other handlers captured none at all, and two templates had no
annotations block, so the same six on a NetworkPolicy or an HPA would have
vanished.

That asymmetry is the defect: whether an annotation survived depended on which
kind it was attached to, and nothing said so.
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
RAW_DIRS = ["namespaces", "namespaces-showcase", "namespaces-live-demo"]
WAVE = ("argocd.argoproj.io/sync-wave", "2")
NOT_SAFE_PREFIXES = ("argocd.argoproj.io/", "prometheus.io/", "checksum/",
                     "kubernetes.io/ingress.class", "haproxy.router.openshift.io/",
                     "nginx.ingress.kubernetes.io/", "traefik.ingress.kubernetes.io/",
                     "cert-manager.io/", "reloader.stakater.com/")


def _load_converter():
    spec = importlib.util.spec_from_file_location("convert_to_universal_chart", CONVERTER_PY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["convert_to_universal_chart"] = mod
    spec.loader.exec_module(mod)
    return mod


conv = _load_converter()


def _doc(kind):
    for d in yaml.safe_load_all(FIXTURE.read_text(encoding="utf-8")):
        if d and d.get("kind") == kind:
            return d
    raise AssertionError(f"no {kind} in fixture")


def _helm_template(values: dict, release=RELEASE):
    with tempfile.TemporaryDirectory() as tmp:
        vf = pathlib.Path(tmp) / "values.yaml"
        vf.write_text(yaml.safe_dump(values, sort_keys=False), encoding="utf-8")
        proc = subprocess.run(["helm", "template", release, str(CHART_DIR), "-f", str(vf)],
                              capture_output=True, text=True)
    assert proc.returncode == 0, f"helm template failed:\n{proc.stderr}"
    return [d for d in yaml.safe_load_all(proc.stdout) if d]


# --------------------------------------------------------------------------
# The triage the report never did
# --------------------------------------------------------------------------

def test_the_not_safe_annotations_in_this_repo_are_the_known_router_ones():
    """Re-runs the triage on every test run. A new `argocd.argoproj.io/sync-wave`
    or `prometheus.io/scrape` landing in the raw manifests fails here, which is
    when the LOW rating has to be revisited.

    IT FIRED, AND THE RE-TRIAGE WAS DONE. The new-analysis round added the
    corpus' first OpenShift Route (ERRORS_NEW_ANALYSIS/Route_Governance_Labels_Lost),
    which carries `haproxy.router.openshift.io/balance` — a behaviour-changing
    router annotation, and exactly the class this tripwire watches for. Traced
    end to end before widening the expected set:

      raw       namespaces-showcase/identity-auth-prd/opa-prd.yaml:124
      values    output-showcase/base/opa.yaml:43        <- carried
      render    fidelity report: 0 lost / 0 changed     <- renders

    It is preserved, so the LOW rating stands. The set is widened by ONE named
    annotation with that evidence attached — not relaxed."""
    found = {}
    for d in RAW_DIRS:
        root = GITOPS_DIR / d
        if not root.is_dir():
            continue
        for f in root.rglob("*.yaml"):
            for doc in yaml.safe_load_all(f.read_text(encoding="utf-8")):
                if not doc:
                    continue
                for k in ((doc.get("metadata") or {}).get("annotations") or {}):
                    if k.startswith(NOT_SAFE_PREFIXES):
                        found.setdefault(k, set()).add(doc.get("kind"))
    # kind each known router annotation is allowed to appear on
    KNOWN = {"traefik.ingress.kubernetes.io/": "Ingress",
             "haproxy.router.openshift.io/": "Route"}
    unexpected = {k: sorted(v) for k, v in found.items()
                  if not k.startswith(tuple(KNOWN))}
    assert not unexpected, (
        "behaviour-changing annotations beyond the known router ones now exist; "
        f"re-triage this folder: {unexpected}")
    misplaced = {k: sorted(v) for k, v in found.items()
                 if v != {next(host for pre, host in KNOWN.items() if k.startswith(pre))}}
    assert not misplaced, f"a router annotation moved off its own kind: {misplaced}"


def test_server_generated_annotations_are_dropped():
    ann = conv.clean_annotations(_doc("Deployment")["metadata"]["annotations"])
    assert "kubectl.kubernetes.io/last-applied-configuration" not in ann
    assert "deployment.kubernetes.io/revision" not in ann, (
        "a rollout counter from the SOURCE cluster is meaningless on the target "
        "and produces a permanent ArgoCD diff")


def test_behaviour_changing_annotations_are_kept():
    ann = conv.clean_annotations(_doc("Deployment")["metadata"]["annotations"])
    assert ann[WAVE[0]] == WAVE[1]
    assert ann["prometheus.io/scrape"] == "true"
    assert ann["checksum/config"] == "9f2c1e4a"


# --------------------------------------------------------------------------
# Capture — the eight handlers that read none
# --------------------------------------------------------------------------

@pytest.mark.parametrize("kind,values_path", [
    ("Deployment", ("annotations",)),
    ("NetworkPolicy", ("networkPolicies", "api-gateway-allow", "annotations")),
    ("Role", ("rbac", "roles", "api-gateway-reader", "annotations")),
    ("RoleBinding", ("rbac", "roleBindings", "api-gateway-reader", "annotations")),
    ("HorizontalPodAutoscaler", ("hpa", "annotations")),
    ("Ingress", ("ingress", "annotations")),
])
def test_every_handler_captures_annotations(kind, values_path):
    ctx = conv.MicroserviceContext(namespace=NS, name=RELEASE)
    doc = _doc(kind)
    name = doc["metadata"]["name"]
    {"Deployment": lambda: conv.handle_workload(doc, ctx, "Deployment"),
     "NetworkPolicy": lambda: conv.handle_networkpolicy(doc, ctx, name),
     "Role": lambda: conv.handle_role(doc, ctx, name),
     "RoleBinding": lambda: conv.handle_rolebinding(doc, ctx, name),
     "HorizontalPodAutoscaler": lambda: conv.handle_hpa(doc, ctx),
     "Ingress": lambda: conv.handle_ingress(doc, ctx)}[kind]()

    cur = ctx.values
    for key in values_path:
        assert isinstance(cur, dict) and key in cur, (
            f"{kind}: no annotations at values path {'.'.join(values_path)}; "
            f"got {sorted(cur) if isinstance(cur, dict) else cur}")
        cur = cur[key]
    expected = conv.clean_annotations(doc["metadata"]["annotations"])
    assert cur == expected, f"{kind}: {cur} != {expected}"


# --------------------------------------------------------------------------
# Render — the two templates that had no annotations block
# --------------------------------------------------------------------------

def test_every_template_has_an_annotations_block():
    """Structural guard, the same shape as folder 20's label test. `rbac.yaml`
    and `servicemonitor.yaml` had none, so an annotation captured for those kinds
    would have sat in values.yaml and rendered nowhere."""
    missing = [p.name for p in sorted(TEMPLATES.glob("*.yaml"))
               if "annotations" not in p.read_text(encoding="utf-8")
               and p.name != "extra-deploy.yaml"]
    assert not missing, f"templates with no annotations block: {missing}"


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_rbac_and_servicemonitor_render_their_annotations():
    values = {
        "workload": {"type": "none"}, "service": {"enabled": False},
        "rbac": {"roles": {"r": {"rules": [], "annotations": {WAVE[0]: WAVE[1]}}},
                 "roleBindings": {"rb": {"roleRef": "r", "subjects": [],
                                         "annotations": {WAVE[0]: WAVE[1]}}}},
        "serviceMonitor": {"enabled": True, "port": "http",
                           "annotations": {WAVE[0]: WAVE[1]}},
    }
    got = {d["kind"]: d["metadata"].get("annotations", {}) for d in _helm_template(values)}
    for kind in ("Role", "RoleBinding", "ServiceMonitor"):
        assert got.get(kind, {}).get(WAVE[0]) == WAVE[1], (
            f"{kind} rendered no annotations; got {got.get(kind)}")


# --------------------------------------------------------------------------
# End to end
# --------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_round_trip_keeps_every_behaviour_changing_annotation(tmp_path):
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

    rendered = {d["kind"]: d["metadata"].get("annotations", {}) for d in _helm_template(merged)}
    missing = []
    for raw in yaml.safe_load_all(FIXTURE.read_text(encoding="utf-8")):
        if not raw:
            continue
        want = conv.clean_annotations(raw["metadata"].get("annotations"))
        got = rendered.get(raw["kind"])
        if got is None:
            continue
        for k, v in want.items():
            if got.get(k) != v:
                missing.append(f"{raw['kind']}: {k}={v!r} -> {got.get(k)!r}")
    assert not missing, "annotations lost end to end:\n" + "\n".join(missing)

    assert "deployment.kubernetes.io/revision" not in rendered.get("Deployment", {})


def test_fidelity_checker_reports_a_lost_annotation():
    raw = _doc("Deployment")
    rendered = yaml.safe_load(yaml.safe_dump(raw))
    rendered["metadata"]["annotations"].pop(WAVE[0])
    diffs = conv._fidelity_diff("Deployment", raw, rendered)
    assert any(d[0] == "LOST" and "sync-wave" in d[1] for d in diffs), f"diffs={diffs}"
