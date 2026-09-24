"""Whole-showcase sweep: for every release, compare each rendered Service /
Route / Ingress name -- and every reference to the primary Service -- against
the raw manifests. Exits non-zero on any mismatch.

Run from universal/gitops-factory.
"""
import glob, os, subprocess, sys, yaml

CHART = os.environ.get("CHART_OVERRIDE", "..")
OUT = "output-showcase"
IN = "namespaces-showcase"
NAMED_KINDS = ("Service", "Route", "Ingress")

raw = {}          # (namespace, kind, name) -> doc
raw_refs = {}     # (namespace, kind, name) -> referenced service name
for path in sorted(glob.glob(os.path.join(IN, "*", "*.yaml"))):
    ns = os.path.basename(os.path.dirname(path))
    for d in yaml.safe_load_all(open(path, encoding="utf-8")):
        if not d or d.get("kind") not in NAMED_KINDS:
            continue
        name = d["metadata"]["name"]
        raw[(ns, d["kind"], name)] = d
        if d["kind"] == "Route":
            raw_refs[(ns, "Route", name)] = ((d.get("spec") or {}).get("to") or {}).get("name")
        elif d["kind"] == "Ingress":
            for rule in (d.get("spec") or {}).get("rules") or []:
                for p in (rule.get("http") or {}).get("paths") or []:
                    svc = ((p.get("backend") or {}).get("service") or {}).get("name")
                    if svc:
                        raw_refs[(ns, "Ingress", name)] = svc
                        break

rows, bad = [], 0
for ns_dir in sorted(d for d in glob.glob(os.path.join(OUT, "*")) if os.path.isdir(d)):
    ns = os.path.basename(ns_dir)
    if ns in ("base", "report", "ERRORS_ANALYSIS", "cluster-shared"):
        continue
    vdir = os.path.join(ns_dir, "values")
    if not os.path.isdir(vdir):
        continue
    for vf in sorted(glob.glob(os.path.join(vdir, "*.yaml"))):
        rel = os.path.splitext(os.path.basename(vf))[0]
        # The ms-applicationSet chart layers the SAME three files for every
        # release the namespace's values/ directory holds, 'shared' included:
        # base/<slug>.yaml -> <ns>/defaults.yaml -> <ns>/values/<slug>.yaml.
        # That is safe for 'shared' because the defaults file is computed with
        # the shared release in the pool, so it can never supply a key shared
        # did not already declare identically.
        chain = [os.path.join(OUT, "base", rel + ".yaml"),
                 os.path.join(ns_dir, "defaults.yaml"), vf]
        files = [f for f in chain if os.path.exists(f)]
        args = ["helm", "template", rel, CHART]
        for f in files:
            args += ["-f", f]
        p = subprocess.run(args, capture_output=True, text=True)
        if p.returncode != 0:
            print("RENDER FAIL", ns, rel, p.stderr[:200]); bad += 1; continue
        for d in yaml.safe_load_all(p.stdout):
            if not d or d.get("kind") not in NAMED_KINDS:
                continue
            name = d["metadata"]["name"]
            present = (ns, d["kind"], name) in raw
            ref = None
            if d["kind"] == "Route":
                ref = ((d.get("spec") or {}).get("to") or {}).get("name")
            elif d["kind"] == "Ingress":
                for rule in (d.get("spec") or {}).get("rules") or []:
                    for pp in (rule.get("http") or {}).get("paths") or []:
                        s = ((pp.get("backend") or {}).get("service") or {}).get("name")
                        if s:
                            ref = s
                            break
                    if ref:
                        break
            want_ref = raw_refs.get((ns, d["kind"], name))
            ok = "OK" if present else "NO RAW OBJECT OF THIS NAME"
            if not present:
                bad += 1
            if want_ref is not None and ref != want_ref:
                ok = "REF MISMATCH raw=%s rendered=%s" % (want_ref, ref)
                bad += 1
            rows.append((ns, d["kind"], name, ref or "-", ok))

print("%-20s %-9s %-34s %-24s %s" % ("NAMESPACE", "KIND", "RENDERED NAME", "-> SERVICE REF", "STATUS"))
print("-" * 118)
for r in rows:
    print("%-20s %-9s %-34s %-24s %s" % r)
print("-" * 118)
print("%d objects compared, %d problems" % (len(rows), bad))
sys.exit(1 if bad else 0)
