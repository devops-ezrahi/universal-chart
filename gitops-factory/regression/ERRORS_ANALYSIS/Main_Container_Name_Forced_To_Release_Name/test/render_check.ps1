# render_check.ps1 - ERRORS_ANALYSIS/Main_Container_Name_Forced_To_Release_Name
#
# DECIDED: Option A, preserve the raw container name (user decision 2026-08-16).
#
# Two parts:
#   A. fixture chain  - converter -> helm template -> assert the raw container
#                       name survives on a Deployment, a Job AND a CronJob.
#   B. whole showcase - compare_all_container_names.py renders every release in
#                       output-showcase and compares container[0] against the
#                       raw manifest. This defect IS exercised by the real data:
#                       12 of 19 workloads were renamed before the fix.
#
# ASCII only: PowerShell 5.1 reads .ps1 as ANSI without a BOM.

$ErrorActionPreference = 'Stop'

$TestDir   = $PSScriptRoot
$GitOps    = (Resolve-Path (Join-Path $TestDir '..\..\..\..')).Path
$Chart     = (Resolve-Path (Join-Path $GitOps '..')).Path
$Converter = Join-Path $GitOps 'convert_to_universal_chart.py'
$Fixture   = Join-Path $TestDir 'fixture'
$Out       = Join-Path ([System.IO.Path]::GetTempPath()) ("cname-" + [guid]::NewGuid().ToString('N'))

Write-Host "== PART A: fixture chain"
Write-Host "-- 1. raw container names"
Write-Host "   shop-web/storefront.yaml       Deployment container: nginx   (release 'storefront')"
Write-Host "   background-jobs/db-migrate.yaml Job       container: migrate (job key 'db-migrate')"
Write-Host "   background-jobs/db-migrate.yaml CronJob   container: report  (cronjob key 'nightly-report')"

Write-Host "-- 2. converter"
python $Converter --input $Fixture --output $Out --chart $Chart --skip-verify | Out-Null
if ($LASTEXITCODE -ne 0) { throw "converter failed" }

Write-Host "-- 3. containerName captured in generated values"
$Hits = Get-ChildItem -Path $Out -Recurse -Filter '*.yaml' |
        ForEach-Object { Select-String -Path $_.FullName -Pattern 'containerName:' }
if (-not $Hits) { throw "FAIL: no generated values file carries containerName" }
$Hits | ForEach-Object { Write-Host "   $(Split-Path $_.Path -Leaf):$($_.LineNumber): $($_.Line.Trim())" }

Write-Host "-- 4. helm template + assert"
$PyFile = Join-Path ([System.IO.Path]::GetTempPath()) ("cnamechk-" + [guid]::NewGuid().ToString('N') + ".py")
Set-Content -Path $PyFile -Encoding utf8 -Value @'
import os, subprocess, sys, yaml, glob

out, chart = sys.argv[1], sys.argv[2]
# Keyed by KIND, not by object name: the chart legitimately prefixes a CronJob's
# own name (nightly-report -> db-migrate-nightly-report, see
# universal-chart.cronJobName). The fixture has exactly one of each kind.
expected = {"Deployment": "nginx", "Job": "migrate", "CronJob": "report"}

def containers(d):
    k = d.get("kind")
    if k in ("Deployment", "StatefulSet", "DaemonSet", "Job"):
        return d["spec"]["template"]["spec"]["containers"]
    if k == "CronJob":
        return d["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"]
    return None

seen, bad = {}, 0
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
        if not d:
            continue
        cs = containers(d)
        if not cs:
            continue
        kind = d["kind"]
        if kind in expected:
            got = cs[0]["name"]
            seen[kind] = got
            ok = "OK" if got == expected[kind] else "MISMATCH"
            if ok == "MISMATCH":
                bad += 1
            print("   %-10s %-26s raw=%-9s rendered=%-9s %s"
                  % (kind, d["metadata"]["name"], expected[kind], got, ok))

missing = set(expected) - set(seen)
if missing:
    print("   FAIL: never rendered:", sorted(missing)); bad += 1
sys.exit(1 if bad else 0)
'@
python $PyFile $Out $Chart
$Code = $LASTEXITCODE
Remove-Item -Force $PyFile
Remove-Item -Recurse -Force $Out
if ($Code -ne 0) { throw "fixture container names did not survive" }

Write-Host ""
Write-Host "== PART B: whole showcase, raw vs rendered for every workload"
Push-Location $GitOps
try {
    python (Join-Path $TestDir 'compare_all_container_names.py')
    $Code = $LASTEXITCODE
} finally {
    Pop-Location
}
if ($Code -ne 0) { throw "at least one showcase workload still renders a renamed container" }

Write-Host ""
Write-Host "PASS: raw container names preserved on Deployment, StatefulSet, DaemonSet, Job and CronJob."
exit 0
