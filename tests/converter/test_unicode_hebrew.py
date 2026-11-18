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
    kinds = {"deployments": "Deployment", "statefulsets": "StatefulSet", "daemonsets": "DaemonSet",
             "cronjobs": "CronJob", "jobs": "Job", "configmaps": "ConfigMap", "secrets": "Secret",
             "services": "Service", "routes": "Route", "ingresses": "Ingress",
             "serviceaccounts": "ServiceAccount", "horizontalpodautoscalers": "HorizontalPodAutoscaler",
             "poddisruptionbudgets": "PodDisruptionBudget", "persistentvolumeclaims": "PersistentVolumeClaim"}
    kinds.update({k[:-1]: v for k, v in list(kinds.items())})  # `oc get configmap <name>`
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



def windows_decode(text: str, cp: str) -> str:
    """UTF-8 text as Windows (and .NET / PowerShell) decodes it with an ANSI
    codepage: a byte the codepage leaves undefined becomes the C1 control
    character of the same number, not an error."""
    out = []
    for b in text.encode("utf-8"):
        try:
            out.append(bytes([b]).decode(cp))
        except UnicodeDecodeError:
            out.append(chr(b))
    return "".join(out)


def test_windows_ansi_garbling_is_repairable():
    # ל ם א מ ן ך sit on bytes cp1255 leaves undefined (א ם for cp1252): every
    # one of them used to read as "characters were lost".
    text = "שלום עולם, אמן ךןם"
    for cp in ("cp1255", "cp1252", "cp437", "cp850", "cp862"):
        assert engine.find_mojibake(windows_decode(text, cp)) == (cp, text), cp


def test_garbled_text_outside_configmap_data_is_repaired(tmp_path):
    docs = manifests()
    ctr = docs[0]["spec"]["template"]["spec"]["containers"][0]
    ctr["env"] = [{"name": "GREETING", "value": windows_decode("שלום עולם", "cp1255")}]
    ctr["args"] = [windows_decode("--title=כותרת", "cp1252")]
    docs[0]["metadata"]["annotations"] = {"description": windows_decode("שירות הזמנות", "cp437")}
    docs[1]["data"] = {"a": windows_decode("מלון ים", "cp1255")}
    src = tmp_path / "manifests.yaml"
    # As Windows PowerShell 5.1 saves `oc get ... -o yaml > file`: UTF-16 with a BOM.
    src.write_text(yaml.safe_dump_all(docs, allow_unicode=True), encoding="utf-16")
    out = tmp_path / "out"
    r = subprocess.run([sys.executable, str(REPO / "gitops-factory" / "convert_to_universal_chart.py"),
                        "--from-yaml", str(src), "--output", str(out), "--skip-verify"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]
    written = "".join(f.read_text(encoding="utf-8") for f in out.rglob("*.yaml"))
    for text in ("שלום עולם", "--title=כותרת", "שירות הזמנות", "מלון ים"):
        assert text in written, text
    assert engine.find_mojibake(written) is None
    report = (out / "report" / "conflicts_and_warnings.txt").read_text(encoding="utf-8")
    assert "Deployment/web spec.template.spec.containers[0].env[0].value" in report
    assert "Deployment/web metadata.annotations.description" in report


def test_manifest_files_with_a_bom_are_read(tmp_path):
    f = tmp_path / "m.yaml"
    for enc in ("utf-8-sig", "utf-16", "utf-8"):
        f.write_text("kind: ConfigMap\ndata:\n  a: שלום\n", encoding=enc)
        assert engine.load_multidoc(f) == [{"kind": "ConfigMap", "data": {"a": "שלום"}}], enc


def _b64(text):
    import base64
    return base64.b64encode(text.encode("utf-8")).decode()


def every_kind_manifests():
    """Hebrew (niqqud, RTL marks, ₪, emoji, multi-line) in every kind the
    converter handles, and in every field a person writes text into."""
    def pod(app, ctr=None, spec=None):
        return {"metadata": {"labels": {"app": app}, "annotations": {"pod-note": f"הערת פוד {app}"}},
                "spec": {"containers": [{"name": app, "image": "registry.example.com/x:1",
                                         "command": ["sh", "-c", f"echo 'שלום מ-{app}'"],
                                         "args": [f"--title=כותרת {app}"],
                                         "env": [{"name": "GREETING", "value": f"שלום עולם {app}"}],
                                         **(ctr or {})}], **(spec or {})}}
    meta = lambda name, note: {"name": name, "namespace": NS, "annotations": {"note": note}}
    multi = "כותרת=מערכת הזמנות\nתיאור=שָׁלוֹם עֲלֵיכֶם\nמחיר=100 ₪ 🙂\nrtl=שם:‏משה‏\n"
    return [
        {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": meta("web", "שירות הזמנות"),
         "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "web"}}, "template": pod("web", {
             "envFrom": [{"configMapRef": {"name": "web-texts"}}, {"secretRef": {"name": "web-secret"}}],
             "volumeMounts": [{"name": "cfg", "mountPath": "/cfg"}], "ports": [{"containerPort": 8080}]},
             {"serviceAccountName": "web-sa", "volumes": [{"name": "cfg", "configMap": {"name": "web-files"}}]})}},
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": meta("web-texts", "צוות אינטרנט"),
         "data": {"greeting": "שלום עולם", "mixed": "Price: 100 ₪ — מחיר", "niqqud": "בְּרֵאשִׁית"}},
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": meta("web-files", "קבצים"),
         "data": {"app.properties": multi,
                  "layers.json": json.dumps({"layers": ["אזורי התרעה", "צדינות"]}, ensure_ascii=False)},
         "binaryData": {"hello.txt": _b64("שלום בינארי")}},
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": meta("orphan-cm", "יתום"), "data": {"x": "קובץ יתום"}},
        {"apiVersion": "v1", "kind": "Secret", "metadata": meta("web-secret", "סוד"), "type": "Opaque",
         "data": {"PASSWORD": _b64("סיסמה סודית")}, "stringData": {"USER": "משתמש"}},
        {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": meta("web-sa", "חשבון שירות")},
        {"apiVersion": "v1", "kind": "Service", "metadata": meta("web", "שירות רשת"),
         "spec": {"selector": {"app": "web"}, "ports": [{"name": "http", "port": 80, "targetPort": 8080}]}},
        {"apiVersion": "route.openshift.io/v1", "kind": "Route", "metadata": meta("web", "נתיב חיצוני"),
         "spec": {"host": "web.example.com", "to": {"kind": "Service", "name": "web"}, "port": {"targetPort": "http"}}},
        {"apiVersion": "v1", "kind": "Service", "metadata": meta("admin", "ניהול"),
         "spec": {"selector": {"app": "migrate"}, "ports": [{"name": "http", "port": 80, "targetPort": 8080}]}},
        {"apiVersion": "networking.k8s.io/v1", "kind": "Ingress", "metadata": meta("admin", "כניסה"),
         "spec": {"rules": [{"host": "admin.example.com", "http": {"paths": [{"path": "/", "pathType": "Prefix",
                  "backend": {"service": {"name": "admin", "port": {"number": 80}}}}]}}]}},
        {"apiVersion": "autoscaling/v2", "kind": "HorizontalPodAutoscaler", "metadata": meta("web", "הרחבה"),
         "spec": {"scaleTargetRef": {"apiVersion": "apps/v1", "kind": "Deployment", "name": "web"},
                  "minReplicas": 1, "maxReplicas": 3}},
        {"apiVersion": "policy/v1", "kind": "PodDisruptionBudget", "metadata": meta("web", "תקציב"),
         "spec": {"minAvailable": 1, "selector": {"matchLabels": {"app": "web"}}}},
        {"apiVersion": "apps/v1", "kind": "StatefulSet", "metadata": meta("db", "מסד נתונים"),
         "spec": {"serviceName": "db", "replicas": 1, "selector": {"matchLabels": {"app": "db"}},
                  "template": pod("db", {"volumeMounts": [{"name": "data", "mountPath": "/data"}]}),
                  "volumeClaimTemplates": [{"metadata": {"name": "data", "annotations": {"note": "אחסון"}},
                      "spec": {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": "1Gi"}}}}]}},
        {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": meta("shared-files", "קבצים משותפים"),
         "spec": {"accessModes": ["ReadWriteMany"], "resources": {"requests": {"storage": "1Gi"}}}},
        {"apiVersion": "batch/v1", "kind": "CronJob", "metadata": meta("nightly", "ניקוי לילי"),
         "spec": {"schedule": "0 2 * * *", "jobTemplate": {"spec": {"template": pod("nightly",
             {"volumeMounts": [{"name": "f", "mountPath": "/f"}]},
             {"restartPolicy": "OnFailure", "volumes": [{"name": "f", "persistentVolumeClaim": {"claimName": "shared-files"}}]})}}}},
        {"apiVersion": "batch/v1", "kind": "Job", "metadata": meta("migrate", "הגירה"),
         "spec": {"template": pod("migrate", spec={"restartPolicy": "Never"})}},
    ]


def _hebrew(node, out):
    """Every string holding Hebrew, keys included, base64 decoded too."""
    import base64
    if isinstance(node, dict):
        for k, v in node.items():
            _hebrew(k, out)
            _hebrew(v, out)
    elif isinstance(node, list):
        for v in node:
            _hebrew(v, out)
    elif isinstance(node, str):
        if any("֐" <= c <= "׿" for c in node):
            out.add(node)
        else:
            try:
                text = base64.b64decode(node, validate=True).decode("utf-8")
            except ValueError:
                return out
            if any("֐" <= c <= "׿" for c in text):
                out.add(text)
    return out


def _render_tree(out):
    docs = []
    ns = out / NS
    for vf in sorted((ns / "values").glob("*.yaml")):
        files = [f for f in (out / "base" / vf.name, ns / "defaults.yaml", vf) if f.exists()]
        docs += engine.helm_template(REPO, engine.argo_release_name(NS, vf.stem), NS, files)
    for cf in sorted((ns / "configMaps").glob("*.yaml")):
        r = subprocess.run(["helm", "template", f"{cf.stem}-cm", str(REPO), "-n", NS, "-f", str(cf),
                            "--set", "workload.type=none", "--set-string", f"fullnameOverride={cf.stem}"],
                           capture_output=True, encoding="utf-8")
        assert r.returncode == 0, r.stderr
        docs += [d for d in yaml.safe_load_all(r.stdout) if d]
    return docs


def _convert(args, out):
    env = dict(os.environ, PYTHONIOENCODING="cp1252")  # a non-UTF-8 console, like Windows
    r = subprocess.run([sys.executable, str(REPO / "gitops-factory" / "convert_to_universal_chart.py"),
                        *args, "--output", str(out), "--verify-fidelity"], capture_output=True, env=env)
    assert r.returncode == 0, (r.stdout + r.stderr).decode("utf-8", "replace")[-4000:]


def _assert_hebrew_copied(out, want):
    trees = [f for f in out.rglob("*.yaml") if "report" not in f.parts]
    text = "".join(f.read_text(encoding="utf-8") for f in trees)
    written = set()
    for f in trees:
        for d in yaml.safe_load_all(f.read_text(encoding="utf-8")):
            _hebrew(d, written)
    # In the generated files, literally (a PDB is an extraDeploy template: text).
    assert sorted(w for w in want - written if w not in text) == []
    assert "\\u05" not in text and engine.find_mojibake(text) is None
    # And byte-identical in what helm (Argo CD) renders.
    assert sorted(want - _hebrew(_render_tree(out), set())) == []


def test_hebrew_in_every_kind_is_copied_exactly(tmp_path):
    src = tmp_path / "manifests.yaml"
    src.write_text(yaml.safe_dump_all(every_kind_manifests(), allow_unicode=True), encoding="utf-8")
    want = _hebrew(every_kind_manifests(), set())
    assert len(want) > 35
    _convert(["--from-yaml", str(src)], tmp_path / "out")
    _assert_hebrew_copied(tmp_path / "out", want)


def test_hebrew_in_every_kind_survives_a_live_pull(tmp_path):
    src = tmp_path / "manifests.json"
    src.write_text(json.dumps(every_kind_manifests(), ensure_ascii=False), encoding="utf-8")
    oc = tmp_path / "oc"
    oc.write_text(f"#!{sys.executable}\nimport sys\nsys.path.insert(0, {str(HERE)!r})\n"
                  "import test_unicode_hebrew as t\nsys.exit(t.fake_oc_main(sys.argv[1:]))\n", encoding="utf-8")
    oc.chmod(0o755)
    os.environ["FAKE_OC_MANIFESTS"] = str(src)
    try:
        _convert(["-n", NS, "--oc", str(oc)], tmp_path / "out")
    finally:
        del os.environ["FAKE_OC_MANIFESTS"]
    want = _hebrew(every_kind_manifests(), set())
    want.discard("קובץ יתום")  # a live pull takes only the ConfigMaps a workload references
    want.discard("יתום")
    _assert_hebrew_copied(tmp_path / "out", want)


def test_hand_written_hebrew_config_map_files_are_kept_in_any_encoding(tmp_path):
    src = tmp_path / "manifests.yaml"
    src.write_text(yaml.safe_dump_all(manifests(), allow_unicode=True), encoding="utf-8")
    out = tmp_path / "out"
    _convert(["--from-yaml", str(src)], out)
    cms = out / NS / "configMaps"
    cms.mkdir()
    body = "# הערה\ndata:\n  greeting: {}\n  app.properties: |\n    כותרת=מערכת\n    מחיר=100 ₪\n"
    files = {"plain": ("utf-8", "שלום"), "bom": ("utf-8-sig", "בום"), "utf16": ("utf-16", "שש-עשרה")}
    for name, (enc, word) in files.items():
        (cms / f"{name}.yaml").write_text(body.format(word), encoding=enc)
    # The old wrapped shape, saved by Windows PowerShell 5.1: migrated in place.
    (cms / "legacy.yaml").write_text(yaml.safe_dump(
        {"configMaps": {"legacy": {"data": {"greeting": "שלום ישן"}, "annotations": {"o": "צוות"}}}},
        allow_unicode=True), encoding="utf-16")
    (out / NS / "values" / "configMaps.yaml").write_text("configMapFiles: [legacy]\n", encoding="utf-8")
    before = {n: (cms / f"{n}.yaml").read_bytes() for n in files}
    _convert(["--from-yaml", str(src)], out)
    # UTF-8 (BOM or not) is left byte for byte. UTF-16, which Helm 4 (and so
    # Argo CD) cannot read, is rewritten as UTF-8 with the same text.
    after = {n: (cms / f"{n}.yaml").read_bytes() for n in files}
    assert after["plain"] == before["plain"] and after["bom"] == before["bom"]
    assert after["utf16"] == before["utf16"].decode("utf-16").encode("utf-8")
    report = (out / "report" / "config_maps_migration.txt").read_text(encoding="utf-8")
    assert "configMaps/utf16.yaml was UTF-16" in report
    assert yaml.safe_load((cms / "legacy.yaml").read_text(encoding="utf-8")) == {
        "data": {"greeting": "שלום ישן"}, "commonAnnotations": {"o": "צוות"}}
    rendered = {d["metadata"]["name"]: d["data"] for d in _render_tree(out)
                if d["kind"] == "ConfigMap" and d["metadata"]["name"] in {*files, "legacy"}}
    assert rendered["legacy"] == {"greeting": "שלום ישן"}
    for name, (_, word) in files.items():
        assert rendered[name] == {"greeting": word, "app.properties": "כותרת=מערכת\nמחיר=100 ₪\n"}

if __name__ == "__main__":
    sys.exit(fake_oc_main(sys.argv[1:]))
