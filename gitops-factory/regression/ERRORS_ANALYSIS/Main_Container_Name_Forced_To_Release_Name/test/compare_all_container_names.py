"""Side-by-side: raw container[0] name vs rendered container[0] name, for every
workload/Job/CronJob in the showcase. Runs helm template per release."""
import glob, os, subprocess, sys, yaml

ROOT = "."
CHART = ".."
OUT = "output-showcase"
IN = "namespaces-showcase"

# raw: (namespace, workload-kind, object-name) -> container[0] name
raw = {}
for path in sorted(glob.glob(os.path.join(IN, "*", "*.yaml"))):
    ns = os.path.basename(os.path.dirname(path))
    for d in yaml.safe_load_all(open(path, encoding="utf-8")):
        if not d:
            continue
        k = d.get("kind")
        try:
            if k in ("Deployment", "StatefulSet", "DaemonSet"):
                cs = d["spec"]["template"]["spec"]["containers"]
            elif k == "Job":
                cs = d["spec"]["template"]["spec"]["containers"]
            elif k == "CronJob":
                cs = d["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"]
            else:
                continue
        except (KeyError, TypeError):
            continue
        raw[(ns, k, d["metadata"]["name"])] = cs[0]["name"]

rows = []
for ns_dir in sorted(d for d in glob.glob(os.path.join(OUT, "*")) if os.path.isdir(d)):
    ns = os.path.basename(ns_dir)
    if ns in ("base", "report", "ERRORS_ANALYSIS", "cluster-shared"):
        continue
    vdir = os.path.join(ns_dir, "values")
    if not os.path.isdir(vdir):
        continue
    for vf in sorted(glob.glob(os.path.join(vdir, "*.yaml"))):
        rel = os.path.splitext(os.path.basename(vf))[0]
        files = []
        for cand in (os.path.join(ns_dir, "defaults.yaml"),
                     os.path.join(OUT, "base", rel + ".yaml"), vf):
            if os.path.exists(cand):
                files.append(cand)
        args = ["helm", "template", rel, CHART]
        for f in files:
            args += ["-f", f]
        p = subprocess.run(args, capture_output=True, text=True)
        if p.returncode != 0:
            print("RENDER FAIL", ns, rel, p.stderr[:200]); continue
        for d in yaml.safe_load_all(p.stdout):
            if not d:
                continue
            k = d.get("kind")
            try:
                if k in ("Deployment", "StatefulSet", "DaemonSet"):
                    cs = d["spec"]["template"]["spec"]["containers"]
                elif k == "Job":
                    cs = d["spec"]["template"]["spec"]["containers"]
                elif k == "CronJob":
                    cs = d["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"]
                else:
                    continue
            except (KeyError, TypeError):
                continue
            rows.append((ns, k, d["metadata"]["name"], cs[0]["name"]))

print("%-18s %-12s %-26s %-14s %-14s %s" % ("NAMESPACE", "KIND", "OBJECT", "RAW c[0]", "RENDERED c[0]", "MATCH"))
print("-" * 100)
bad = 0
for ns, k, name, rendered in rows:
    r = raw.get((ns, k, name))
    if r is None:
        r = "<no raw match>"
        ok = "?"
    else:
        ok = "OK" if r == rendered else "MISMATCH"
        if ok == "MISMATCH":
            bad += 1
    print("%-18s %-12s %-26s %-14s %-14s %s" % (ns, k, name, r, rendered, ok))
print("-" * 100)
print("%d workloads compared, %d mismatches" % (len(rows), bad))
sys.exit(1 if bad else 0)
