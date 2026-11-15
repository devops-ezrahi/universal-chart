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


def _ctx():
    ctx = engine.MicroserviceContext.__new__(engine.MicroserviceContext)
    ctx.warnings = []
    return ctx


def test_console_garbled_hebrew_is_repaired_and_reported(monkeypatch):
    # What a US Windows console (cp437) makes of UTF-8 Hebrew inside a JSON value.
    layers = '{"layers": ["אזורי התרעה", "צדינות"]}'
    doc = {"data": {"config.json": layers.encode("utf-8").decode("cp437"), "ok": "שלום"}}
    ctx = _ctx()
    monkeypatch.setattr(engine, "REPAIR_MOJIBAKE", True)
    engine._repair_mojibake(doc, ctx, "ConfigMap", "ui")
    assert doc["data"] == {"config.json": layers, "ok": "שלום"}
    assert len(ctx.warnings) == 1 and "ConfigMap/ui data.config.json" in ctx.warnings[0]
    assert "cp437" in ctx.warnings[0] and "repaired" in ctx.warnings[0]


def test_secret_data_is_checked_decoded():
    import base64
    garbled = "סיסמה".encode("utf-8").decode("cp437")
    doc = {"data": {"p": base64.b64encode(garbled.encode("utf-8")).decode()}}
    engine._repair_mojibake(doc, _ctx(), "Secret", "s", fields=("stringData",), b64_fields=("data",))
    assert base64.b64decode(doc["data"]["p"]).decode("utf-8") == "סיסמה"


def test_repair_can_be_turned_off_and_lossy_text_is_only_reported(monkeypatch):
    monkeypatch.setattr(engine, "REPAIR_MOJIBAKE", False)
    garbled = "שלום".encode("utf-8").decode("cp437")
    lossy = "שלום עולם".encode("utf-8").decode("cp1252", "replace")
    doc = {"data": {"a": garbled, "b": lossy}}
    ctx = _ctx()
    engine._repair_mojibake(doc, ctx, "ConfigMap", "texts")
    assert doc["data"] == {"a": garbled, "b": lossy}
    assert len(ctx.warnings) == 2 and "cannot be repaired" in ctx.warnings[1]


def test_a_pull_of_an_already_garbled_configmap_writes_real_hebrew(tmp_path):
    docs = manifests()
    docs[1]["data"] = {"layers.json": '["אזורי התרעה"]'.encode("utf-8").decode("cp437")}
    src = tmp_path / "in" / NS
    src.mkdir(parents=True)
    (src / "web.yaml").write_text(yaml.safe_dump_all(docs, allow_unicode=True), encoding="utf-8")
    out = tmp_path / "out"
    r = subprocess.run([sys.executable, str(REPO / "gitops-factory" / "convert_to_universal_chart.py"),
                        "--input", str(tmp_path / "in"), "--output", str(out), "--skip-verify"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]
    written = "".join(f.read_text(encoding="utf-8") for f in out.rglob("*.yaml"))
    assert "אזורי התרעה" in written and "\u256b" not in written
    report = (out / "report" / "conflicts_and_warnings.txt").read_text(encoding="utf-8")
    assert "ConfigMap/web-texts data.layers.json" in report and "cp437" in report


def test_mojibake_detector():
    assert engine.find_mojibake("שלום עולם") is None
    assert engine.find_mojibake("3×4 × 5") is None
    assert engine.find_mojibake("Ľuboš —") is None
    assert engine.find_mojibake("בית".encode().decode("cp1255")) == ("cp1255", "בית")
    assert engine.find_mojibake("שלום".encode().decode("cp862")) == ("cp862", "שלום")
    assert engine.find_mojibake("שלום עולם".encode().decode("cp1252", "replace"))[0] == "cp1252"
    assert engine.find_mojibake("אזורי התרעה".encode().decode("cp437")) == ("cp437", "אזורי התרעה")
    assert engine.find_mojibake("Café ₪ naïve") is None


if __name__ == "__main__":
    sys.exit(fake_oc_main(sys.argv[1:]))
