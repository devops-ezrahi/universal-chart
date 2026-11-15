"""Hebrew (and any non-ASCII text) must come out of the converter exactly as it
went in: readable in the values files (no \\u escapes, no mojibake) and
byte-identical in what helm renders.

The live pull is exercised for real: a fake `oc` (this file, run as a script)
answers the converter's `oc get ... -o yaml` calls with raw UTF-8 bytes, so
the subprocess decoding is under test, not bypassed."""
import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO))
from converter import engine  # noqa: E402

NS = "shop-dev"
HEBREW = {
    "greeting": "שלום עולם",
    "rtl-marks": "שם:‏משה‏ (id 7)",
    "app.properties": "title=מערכת הזמנות  \nfooter=כל הזכויות שמורות\n",
    "mixed": "Price: 100 ₪ — מחיר",
    "שם-מפתח": "value",
}


def manifests():
    return [
        {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "web", "namespace": NS},
         "spec": {"selector": {"matchLabels": {"app": "web"}}, "template": {
             "metadata": {"labels": {"app": "web"}},
             "spec": {"containers": [{"name": "web", "image": "registry.example.com/web:1.0",
                                      "envFrom": [{"configMapRef": {"name": "web-texts"}}]}]}}}},
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "web-texts", "namespace": NS},
         "data": HEBREW},
    ]


def fake_oc_main(argv):
    """`oc get <type>[.<group>] [name] -n NS -o yaml`, answered from MANIFESTS."""
    docs = json.loads(Path(os.environ["FAKE_OC_MANIFESTS"]).read_text(encoding="utf-8"))
    if not argv or argv[0] != "get":
        return 0
    plural = argv[1].split(".")[0]
    kinds = {"deployments": "Deployment", "configmaps": "ConfigMap"}
    name = argv[2] if len(argv) > 2 and not argv[2].startswith("-") else None
    hits = [d for d in docs if d["kind"] == kinds.get(plural)
            and (name is None or d["metadata"]["name"] == name)]
    if name is not None and not hits:
        sys.stderr.write(f'Error from server (NotFound): {plural} "{name}" not found\n')
        return 1
    out = hits[0] if name is not None else {"apiVersion": "v1", "kind": "List", "items": hits}
    # Raw UTF-8 bytes, as oc writes them on every platform.
    sys.stdout.buffer.write(yaml.safe_dump(out, allow_unicode=True).encode("utf-8"))
    return 0


def test_hebrew_survives_a_live_pull_end_to_end(tmp_path):
    src = tmp_path / "manifests.json"
    src.write_text(json.dumps(manifests(), ensure_ascii=False), encoding="utf-8")
    oc = tmp_path / "oc"
    oc.write_text(f"#!{sys.executable}\nimport sys\nsys.path.insert(0, {str(HERE)!r})\n"
                  "import test_unicode_hebrew as t\nsys.exit(t.fake_oc_main(sys.argv[1:]))\n", encoding="utf-8")
    oc.chmod(0o755)
    out = tmp_path / "out"
    env = dict(os.environ, FAKE_OC_MANIFESTS=str(src))
    # A non-UTF-8 locale for the converter's own streams, like a Windows console.
    env["PYTHONIOENCODING"] = "cp1252"
    r = subprocess.run([sys.executable, str(REPO / "gitops-factory" / "convert_to_universal_chart.py"),
                        "-n", NS, "--oc", str(oc), "--output", str(out)],
                       capture_output=True, env=env)
    log = r.stdout.decode("utf-8", "replace") + r.stderr.decode("utf-8", "replace")
    assert r.returncode == 0, log[-4000:]

    written = "".join(f.read_text(encoding="utf-8") for f in sorted(out.rglob("*.yaml"))
                      if "configMaps" in f.read_text(encoding="utf-8"))
    for text in ("שלום עולם", "מערכת הזמנות", "₪ — מחיר", "שם-מפתח"):
        assert text in written, f"{text!r} not written literally"
    assert "\\u05" not in written and "\\u200F" not in written
    assert engine.find_mojibake(written) is None
    assert "garbled" not in log

    rendered, conflicts = engine.verify_no_conflicts(REPO, out)
    assert conflicts == [], conflicts
    files = [out / "base" / "web.yaml", out / NS / "defaults.yaml", out / NS / "values" / "web.yaml"]
    docs = engine.helm_template(REPO, engine.argo_release_name(NS, "web"), NS, files)
    cm = next(d for d in docs if d["kind"] == "ConfigMap")
    assert cm["data"] == HEBREW


def test_garbled_text_from_the_cluster_is_flagged_not_rewritten():
    broken = "שלום עולם".encode("utf-8").decode("cp1255", "replace")
    ctx = engine.MicroserviceContext.__new__(engine.MicroserviceContext)
    ctx.warnings = []
    engine._flag_mojibake({"data": {"msg": broken, "ok": "שלום"}}, ctx, "ConfigMap", "texts")
    assert len(ctx.warnings) == 1 and "ConfigMap/texts data.msg" in ctx.warnings[0]
    assert "cp1255" in ctx.warnings[0]


def test_mojibake_detector():
    assert engine.find_mojibake("שלום עולם") is None
    assert engine.find_mojibake("3×4 × 5") is None
    assert engine.find_mojibake("Ľuboš —") is None
    assert engine.find_mojibake("בית".encode().decode("cp1255")) == ("cp1255", "בית")
    assert engine.find_mojibake("שלום".encode().decode("cp862")) == ("cp862", "שלום")
    assert engine.find_mojibake("שלום עולם".encode().decode("cp1252", "replace"))[0] == "cp1252"


if __name__ == "__main__":
    sys.exit(fake_oc_main(sys.argv[1:]))
