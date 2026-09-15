"""
Regression tests for ERRORS_NEW_ANALYSIS/Service_AllocateLoadBalancerNodePorts_Lost.

The report splits this field into two populations and proposes suppressing the
larger one:

  "1. THE 62 ON REDIS 'ANNOUNCE' SERVICES — marked 'Suppressible or explicitly
      set'. Redis announce Services are headless/internal; the field is
      irrelevant there.
   2. THE 3 REAL ONES — Service/data-receiver-paamon and
      Service/zookeeper-external lost allocateLoadBalancerNodePorts: true.
      These are actual LoadBalancer Services where the field controls behaviour.
      FIX THESE.
   Suppressing the whole field path silences the 62 AND the 3 together."

DECISION: carry it for every Service; suppress nothing. The report's own field
table offers this as the alternative — "Suppressible **or explicitly set**" —
and it is strictly the better half of that choice. One rule, no kind-scoping to
get wrong, no suppression that a later reader has to re-litigate, and the 62
inert copies cost one rendered line each while staying byte-accurate to the
source. Splitting a rule by *which Service it is* would have been a per-instance
judgement encoded in code, which is how the "Route labels: fix or suppress?"
mess in folder 3 came about.

THE ONE THING THAT ACTUALLY MATTERS HERE is the truthiness trap, and error.txt
asks for it directly:

  "CHECK FIRST: does any affected Service set it to FALSE? Losing an explicit
   false while the default is true would silently flip behaviour and IS a
   production break. The report only ever cites true — confirm that holds."

The Kubernetes default is TRUE. So losing an explicit `true` is invisible and
harmless, and losing an explicit `false` opens a node port on every node that
the Service was configured not to open. `if spec.get(...)` would have preserved
exactly the harmless case and dropped the dangerous one — on both the converter
and the template side. Both use existence checks, and both are pinned below.
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

FIELD = "allocateLoadBalancerNodePorts"


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


conv = _load(CONVERTER_PY, "convert_to_universal_chart")


def _svc(**spec_extra):
    return {
        "apiVersion": "v1", "kind": "Service", "metadata": {"name": "zookeeper-external"},
        "spec": {"type": "LoadBalancer", "selector": {"app": "zk"},
                 "ports": [{"name": "client", "port": 2181}], **spec_extra},
    }


# ---------------------------------------------------------------------------
# 1. Converter — and the truthiness trap
# ---------------------------------------------------------------------------

def test_explicit_false_is_captured():
    """THE test. `false` is the only value of this field whose loss changes
    behaviour, and it is the one a truthy check drops."""
    assert conv.build_service_entry(_svc(**{FIELD: False}))[FIELD] is False


def test_explicit_true_is_captured_too():
    """Harmless to lose, but carrying it costs nothing and keeps the report at
    zero rather than at 62 suppressed lines."""
    assert conv.build_service_entry(_svc(**{FIELD: True}))[FIELD] is True


def test_absent_stays_absent():
    """No invented field on the ~everything that does not declare it."""
    assert FIELD not in conv.build_service_entry(_svc())


def test_it_is_carried_regardless_of_service_type():
    """Deliberately not scoped to type LoadBalancer. The 62 benign occurrences
    are on headless/internal Services where the field is inert — carrying it
    there is byte-accurate and costs a line; scoping the rule by Service type
    would encode a per-instance judgement in code."""
    entry = conv.build_service_entry({
        "apiVersion": "v1", "kind": "Service", "metadata": {"name": "redis-announce"},
        "spec": {"type": "ClusterIP", "clusterIP": "None", FIELD: True,
                 "ports": [{"name": "redis", "port": 6379}]},
    })
    assert entry[FIELD] is True


def test_the_neighbouring_fields_did_not_regress():
    """This whitelist is where appProtocol, sessionAffinity and
    externalTrafficPolicy were each added by a previous folder — the same
    one-field-at-a-time pattern that left this one behind."""
    entry = conv.build_service_entry(_svc(
        externalTrafficPolicy="Local", sessionAffinity="ClientIP",
        publishNotReadyAddresses=False, **{FIELD: False}))
    assert entry["externalTrafficPolicy"] == "Local"
    assert entry["sessionAffinity"] == "ClientIP"
    assert entry["publishNotReadyAddresses"] is False
    assert entry[FIELD] is False


# ---------------------------------------------------------------------------
# 2. Template — both the primary and the extra-Services path
# ---------------------------------------------------------------------------

def _render(values, tmp_path):
    vf = tmp_path / "values.yaml"
    vf.write_text(yaml.safe_dump(values), encoding="utf-8")
    out = subprocess.run(
        ["helm", "template", "zk", str(CHART_DIR), "--namespace", "platform-data", "-f", str(vf)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert out.returncode == 0, out.stderr
    return [d for d in yaml.safe_load_all(out.stdout) if d]


BASE = {"workload": {"type": "deployment"}, "image": {"repository": "zookeeper", "tag": "3.9"}}


@pytest.mark.parametrize("value", [True, False])
def test_primary_service_template_renders_both_values(value, tmp_path):
    docs = _render({**BASE, "service": {
        "type": "LoadBalancer", "ports": {"client": {"port": 2181}}, FIELD: value}}, tmp_path)
    svc = next(d for d in docs if d["kind"] == "Service")
    assert svc["spec"][FIELD] is value


def test_primary_service_template_omits_it_when_unset(tmp_path):
    docs = _render({**BASE, "service": {"type": "LoadBalancer",
                                        "ports": {"client": {"port": 2181}}}}, tmp_path)
    svc = next(d for d in docs if d["kind"] == "Service")
    assert FIELD not in svc["spec"]


@pytest.mark.parametrize("value", [True, False])
def test_extra_services_template_renders_both_values(value, tmp_path):
    """The two real LoadBalancers the report names are a data-receiver and an
    externally exposed ZooKeeper — a second Service beside the primary, which is
    exactly what lands in services.yaml."""
    docs = _render({**BASE,
                    "service": {"ports": {"client": {"port": 2181}}},
                    "services": {"zookeeper-external": {
                        "enabled": True,  # services.yaml gates every extra Service on this
                        "type": "LoadBalancer", "ports": {"client": {"port": 2181}},
                        FIELD: value}}}, tmp_path)
    svc = next(d for d in docs if d["kind"] == "Service"
               and d["metadata"]["name"] == "zookeeper-external")
    assert svc["spec"][FIELD] is value


def test_the_template_uses_hasKey_not_with(tmp_path):
    """A `with` block treats `false` as empty and skips it. Asserted through
    behaviour rather than by reading the template: render false, expect false."""
    docs = _render({**BASE, "service": {
        "type": "LoadBalancer", "ports": {"client": {"port": 2181}}, FIELD: False}}, tmp_path)
    svc = next(d for d in docs if d["kind"] == "Service")
    assert FIELD in svc["spec"], "an explicit false was skipped — `with` semantics leaked in"
    assert svc["spec"][FIELD] is False


def test_schema_rejects_a_stringified_boolean():
    """`"false"` is truthy everywhere in Go templates and would render as the
    string false, which the API server may or may not coerce. Typed in
    values.schema.json so it fails at lint time instead."""
    out = subprocess.run(
        ["helm", "template", "zk", str(CHART_DIR), "--namespace", "platform-data",
         "--set", "workload.type=deployment", "--set", "image.repository=zookeeper",
         "--set-string", f"service.{FIELD}=false"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert out.returncode != 0, "schema accepted a string where a boolean is required"


# ---------------------------------------------------------------------------
# 3. Corpus coverage
# ---------------------------------------------------------------------------

def test_the_corpus_carries_an_explicit_false_on_a_real_loadbalancer():
    """error.txt asks for exactly this case and notes the report never observed
    it. A `true` in the corpus would prove nothing — losing it is undetectable
    from behaviour, since true is the default."""
    svcs = [d for f in SHOWCASE.rglob("*.yaml")
            for d in yaml.safe_load_all(f.read_text(encoding="utf-8"))
            if d and d.get("kind") == "Service"]
    hits = [d for d in svcs if d["spec"].get(FIELD) is False]
    assert hits, f"no Service in the corpus sets {FIELD}: false"
    assert any(d["spec"].get("type") == "LoadBalancer" for d in hits), \
        "the false is not on a LoadBalancer, where the field actually applies"
