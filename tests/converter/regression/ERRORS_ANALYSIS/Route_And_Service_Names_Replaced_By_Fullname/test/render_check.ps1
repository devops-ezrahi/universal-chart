# render_check.ps1 - ERRORS_ANALYSIS/Route_And_Service_Names_Replaced_By_Fullname
#
# PART A: fixture chain - the '-service' / '-route' suffixed names must survive
#         converter -> helm template, and every reference to the Service must
#         follow it (Route spec.to.name, Ingress backend.service.name).
# PART B: whole-showcase sweep - every rendered Service/Route/Ingress must
#         correspond to a raw object of that exact name, and every reference
#         must match the raw reference.
#
# ASCII only: PowerShell 5.1 reads .ps1 as ANSI without a BOM.

$ErrorActionPreference = 'Stop'

$TestDir   = $PSScriptRoot
$GitOps    = (Resolve-Path (Join-Path $TestDir '..\..\..\..')).Path
$Chart     = (Resolve-Path (Join-Path $GitOps '..')).Path
$Converter = Join-Path $GitOps 'convert_to_universal_chart.py'
$Fixture   = Join-Path $TestDir 'fixture'
$Out       = Join-Path ([System.IO.Path]::GetTempPath()) ("objname-" + [guid]::NewGuid().ToString('N'))

Write-Host "== PART A: fixture chain"
Write-Host "-- raw object names"
Get-ChildItem -Path $Fixture -Recurse -Filter '*.yaml' | ForEach-Object {
    $f = $_.FullName
    Select-String -Path $f -Pattern '^kind: (Deployment|Service|Route|Ingress)$' -Context 0,3 |
        ForEach-Object {
            $nm = ($_.Context.PostContext | Where-Object { $_ -match '^\s+name: ' } | Select-Object -First 1)
            Write-Host ("   {0,-10} {1}" -f ($_.Line -replace 'kind: ',''), $nm.Trim())
        }
}

Write-Host "-- converter"
python $Converter --input $Fixture --output $Out --chart $Chart --skip-verify | Out-Null
if ($LASTEXITCODE -ne 0) { throw "converter failed" }

Write-Host "-- names captured into values"
Get-ChildItem -Path $Out -Recurse -Filter '*.yaml' | Where-Object { $_.FullName -match '\\base\\' } | ForEach-Object {
    Select-String -Path $_.FullName -Pattern '^\s+name: |^\s+serviceName: ' |
        ForEach-Object { Write-Host "   $(Split-Path $_.Path -Leaf):$($_.LineNumber): $($_.Line.Trim())" }
}

Write-Host "-- helm template + assert"
$PyFile = Join-Path ([System.IO.Path]::GetTempPath()) ("objchk-" + [guid]::NewGuid().ToString('N') + ".py")
Set-Content -Path $PyFile -Encoding utf8 -Value @'
import glob, os, subprocess, sys, yaml

out, chart, fixture = sys.argv[1], sys.argv[2], sys.argv[3]

raw = {}
refs = {}
for path in sorted(glob.glob(os.path.join(fixture, "*", "*.yaml"))):
    for d in yaml.safe_load_all(open(path, encoding="utf-8")):
        if not d or d.get("kind") not in ("Service", "Route", "Ingress"):
            continue
        raw[(d["kind"], d["metadata"]["name"])] = True
        if d["kind"] == "Route":
            refs[(d["kind"], d["metadata"]["name"])] = d["spec"]["to"]["name"]
        else:
            for rule in (d.get("spec") or {}).get("rules") or []:
                for p in (rule.get("http") or {}).get("paths") or []:
                    s = ((p.get("backend") or {}).get("service") or {}).get("name")
                    if s:
                        refs[(d["kind"], d["metadata"]["name"])] = s

bad = 0
for vf in sorted(glob.glob(os.path.join(out, "*", "values", "*.yaml"))):
    rel = os.path.splitext(os.path.basename(vf))[0]
    files = [f for f in (os.path.join(out, "base", rel + ".yaml"), vf) if os.path.exists(f)]
    args = ["helm", "template", rel, chart]
    for f in files:
        args += ["-f", f]
    p = subprocess.run(args, capture_output=True, text=True)
    if p.returncode != 0:
        print("   RENDER FAIL", rel, p.stderr[:300]); bad += 1; continue
    for d in yaml.safe_load_all(p.stdout):
        if not d or d.get("kind") not in ("Service", "Route", "Ingress"):
            continue
        key = (d["kind"], d["metadata"]["name"])
        got_ref = None
        if d["kind"] == "Route":
            got_ref = d["spec"]["to"]["name"]
        else:
            for rule in (d.get("spec") or {}).get("rules") or []:
                for pp in (rule.get("http") or {}).get("paths") or []:
                    s = ((pp.get("backend") or {}).get("service") or {}).get("name")
                    if s:
                        got_ref = s
        status = "OK"
        if key not in raw:
            status = "FAIL: no raw object named %r" % (key[1],); bad += 1
        want = refs.get(key)
        if want and got_ref != want:
            status = "FAIL: ref raw=%s rendered=%s" % (want, got_ref); bad += 1
        print("   %-8s %-32s -> %-32s %s" % (key[0], key[1], got_ref or "-", status))
sys.exit(1 if bad else 0)
'@
python $PyFile $Out $Chart $Fixture
$Code = $LASTEXITCODE
Remove-Item -Force $PyFile
Remove-Item -Recurse -Force $Out
if ($Code -ne 0) { throw "fixture object names/references did not survive" }

Write-Host ""
Write-Host "== PART B: whole showcase"
Push-Location $GitOps
try {
    python (Join-Path $TestDir 'compare_all_object_names.py')
    $Code = $LASTEXITCODE
} finally {
    Pop-Location
}
if ($Code -ne 0) { throw "a showcase Service/Route/Ingress name or reference does not match its raw manifest" }

Write-Host ""
Write-Host "PASS: Service/Route names preserved and every reference follows them."
exit 0
