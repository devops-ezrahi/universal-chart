"""
Regression tests for ERRORS_NEW_ANALYSIS/Missing_Container_Port_9125.

The one defect in the batch whose root cause the report records as UNKNOWN:

  "5. Missing Container Port - 1 LOST error: Deployment/b2b is missing port
   9125/TCP in render. Root cause unknown — needs investigation into
   ports_list_to_map or Helm template mapToList helper."
  "Root cause is stated as UNKNOWN in the source. This folder is an
   INVESTIGATION TASK, not a known fix — expect to spend time here."

SOLVED, and it was the first of the two suspects error.txt lists.

ports_list_to_map keyed an unnamed port on the port NUMBER alone:

    out[f"port-{p['containerPort']}"] = entry

_item_name — the fidelity checker's half of the same question — keys on the
number AND the protocol, and states the rule in its own docstring: "a container
may not declare the same containerPort/protocol pair twice". The checker is
right. A container declaring 9125/TCP and 9125/UDP is legal Kubernetes and is
the standard statsd-exporter shape; both collapsed onto `port-9125` and, by
dict insertion order, the UDP entry overwrote the TCP one. Hence the report's
`ports[#port:9125/TCP]` — the TCP half, specifically, every time.

The two halves of the pipeline disagreed about what identifies an unnamed port.
Fixed by making the converter adopt the checker's identity, not the reverse:
the checker's is the one Kubernetes itself uses. The synthetic key never reaches
a rendered manifest (universal-chart.mapToList omits both `unnamed` and `name`),
so it is free to be as specific as correctness requires.

error.txt's severity note is worth keeping in view: "Kubernetes does not require
a containerPort be declared for traffic to reach it, so a Service targeting 9125
BY NUMBER keeps working. What breaks is anything resolving it BY NAME (Service
targetPort, probes), NetworkPolicy port references, and port-based discovery."
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

# The exact shape from the report: same number, both protocols, neither named.
STATSD = [
    {"containerPort": 9125, "protocol": "TCP"},
    {"containerPort": 9125, "protocol": "UDP"},
]


# ---------------------------------------------------------------------------
# 1. The defect itself
# ---------------------------------------------------------------------------

def test_both_protocols_on_one_port_number_survive():
    """THE defect, in one assertion. Two ports in, two ports out."""
    out = conv.ports_list_to_map(STATSD)
    assert len(out) == 2, f"a port was dropped: {out}"
    assert {(e["containerPort"], e["protocol"]) for e in out.values()} == \
        {(9125, "TCP"), (9125, "UDP")}


def test_the_tcp_half_is_the_one_that_used_to_vanish():
    """Specifically TCP, because the UDP entry came later and overwrote it —
    which is why the report says `#port:9125/TCP` and not `/UDP`."""
    out = conv.ports_list_to_map(STATSD)
    assert any(e["protocol"] == "TCP" for e in out.values())


def test_converter_key_matches_the_checkers_identity():
    """The actual root cause: two halves of the pipeline disagreeing about what
    identifies an unnamed port. This asserts they now agree — one key per
    distinct _item_name."""
    ports = STATSD + [{"containerPort": 9102, "protocol": "TCP"}]
    assert len(conv.ports_list_to_map(ports)) == len({conv._item_name(p) for p in ports})


def test_protocol_defaults_to_TCP_in_the_key_like_kubernetes_does():
    """A port with no explicit protocol IS TCP, so it must collide with an
    explicit TCP entry rather than sitting beside it under a different key."""
    keyed = conv.ports_list_to_map([{"containerPort": 8080}])
    assert list(keyed) == ["port-8080-tcp"]


# ---------------------------------------------------------------------------
# 2. Nothing else about ports regressed
# ---------------------------------------------------------------------------

def test_named_ports_still_key_on_their_name():
    out = conv.ports_list_to_map([{"name": "metrics", "containerPort": 9102}])
    assert list(out) == ["metrics"]
    assert "unnamed" not in out["metrics"]


def test_unnamed_ports_are_still_marked_unnamed():
    """The marker is what stops mapToList inventing a `name:` the raw manifest
    never had — the defect the previous round fixed in this same function."""
    out = conv.ports_list_to_map([{"containerPort": 9125, "protocol": "UDP"}])
    assert out["port-9125-udp"]["unnamed"] is True


def test_a_named_and_an_unnamed_port_on_the_same_number_coexist():
    out = conv.ports_list_to_map([
        {"name": "metrics", "containerPort": 9125, "protocol": "TCP"},
        {"containerPort": 9125, "protocol": "UDP"},
    ])
    assert set(out) == {"metrics", "port-9125-udp"}


def test_a_genuinely_duplicate_port_raises_instead_of_dropping_one():
    """Kubernetes forbids a repeated containerPort/protocol pair, so this input
    is already invalid. Raising beats the old behaviour — keeping the last one —
    because a port vanishing between raw and rendered is precisely the defect
    this function used to cause."""
    with pytest.raises(ValueError, match="duplicate container port key"):
        conv.ports_list_to_map([
            {"containerPort": 9125, "protocol": "TCP"},
            {"containerPort": 9125, "protocol": "TCP"},
        ])


def test_duplicate_named_ports_also_raise():
    with pytest.raises(ValueError, match="duplicate container port key"):
        conv.ports_list_to_map([
            {"name": "http", "containerPort": 80},
            {"name": "http", "containerPort": 8080},
        ])


def test_empty_and_missing_port_lists_are_fine():
    assert conv.ports_list_to_map([]) == {}
    assert conv.ports_list_to_map(None) == {}


# ---------------------------------------------------------------------------
# 3. Through the chart — the second suspect error.txt names
# ---------------------------------------------------------------------------

def test_mapToList_renders_both_ports_and_invents_no_names(tmp_path):
    """error.txt's other suspect was "the mapToList helper in _helpers.tpl".
    It was not the cause, but it is the half that has to survive the new key —
    so this pins that the key stays bookkeeping and never reaches the manifest."""
    vf = tmp_path / "values.yaml"
    vf.write_text(yaml.safe_dump({
        "workload": {"type": "deployment"},
        "image": {"repository": "prom/statsd-exporter", "tag": "v0.26"},
        "ports": {
            "metrics": {"containerPort": 9102},
            "port-9125-tcp": {"containerPort": 9125, "protocol": "TCP", "unnamed": True},
            "port-9125-udp": {"containerPort": 9125, "protocol": "UDP", "unnamed": True},
        },
    }), encoding="utf-8")
    out = subprocess.run(
        ["helm", "template", "b2b", str(CHART_DIR), "--namespace", "default", "-f", str(vf)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert out.returncode == 0, out.stderr
    dep = next(d for d in yaml.safe_load_all(out.stdout) if d and d.get("kind") == "Deployment")
    ports = dep["spec"]["template"]["spec"]["containers"][0]["ports"]

    assert {(p["containerPort"], p.get("protocol")) for p in ports} == \
        {(9102, None), (9125, "TCP"), (9125, "UDP")}
    for p in ports:
        if p["containerPort"] == 9125:
            assert "name" not in p, f"mapToList invented a name for an unnamed port: {p}"


# ---------------------------------------------------------------------------
# 4. Corpus coverage
# ---------------------------------------------------------------------------

def test_the_corpus_contains_the_colliding_port_shape():
    """Without it the round-trip check cannot see this class at all — the
    collision needs two unnamed ports on one number to happen."""
    found = False
    for f in SHOWCASE.rglob("*.yaml"):
        for d in yaml.safe_load_all(f.read_text(encoding="utf-8")):
            if not d or d.get("kind") not in ("Deployment", "StatefulSet", "DaemonSet"):
                continue
            for c in (d["spec"]["template"]["spec"].get("containers") or []):
                seen = {}
                for p in (c.get("ports") or []):
                    if p.get("name"):
                        continue
                    seen.setdefault(p["containerPort"], set()).add(p.get("protocol", "TCP"))
                if any(len(v) > 1 for v in seen.values()):
                    found = True
    assert found, "no unnamed same-number multi-protocol port pair in the corpus"
