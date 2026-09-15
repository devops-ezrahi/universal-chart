# render_check.ps1 - ERRORS_ANALYSIS/CronJob_Merge_Overwrites_Command_And_Env
#
# converter -> helm template -> assert that each rendered CronJob carries its
# OWN command, args and STAGE, and prints where each command actually lives
# across the base/ + values/ layers (the "the values YAML has neither" claim).
#
# ASCII only: PowerShell 5.1 reads .ps1 as ANSI without a BOM.

$ErrorActionPreference = 'Stop'
$Here    = Split-Path -Parent $MyInvocation.MyCommand.Path
$Gitops  = (Resolve-Path (Join-Path $Here '..\..\..\..')).Path
$Chart   = (Resolve-Path (Join-Path $Gitops '..')).Path
$Fixture = Join-Path $Here 'fixture'
$Out     = Join-Path $env:TEMP ('cronjob-cmdenv-' + [guid]::NewGuid().ToString('N'))
$Release = 'db-migrate'
$Ns      = 'background-jobs'

Write-Host "== 1. converter =="
python (Join-Path $Gitops 'convert_to_universal_chart.py') `
    --input $Fixture --output $Out --chart $Chart --skip-verify
if ($LASTEXITCODE -ne 0) { throw "converter failed" }

Write-Host ""
Write-Host "== 2. which layer holds each command =="
$Layers = @(
    (Join-Path $Out "base\$Release.yaml"),
    (Join-Path $Out "$Ns\values\$Release.yaml"),
    (Join-Path $Out "$Ns-prd\values\$Release.yaml")
)
foreach ($L in $Layers) {
    $Hits = Select-String -Path $L -Pattern "Hello from" -SimpleMatch
    $Rel  = $L.Substring($Out.Length + 1)
    if ($Hits) {
        foreach ($H in $Hits) { Write-Host ("   {0,-42} {1}" -f $Rel, $H.Line.Trim()) }
    } else {
        Write-Host ("   {0,-42} (no command here)" -f $Rel)
    }
}

Write-Host ""
Write-Host "== 3. helm template ($Ns) =="
$ValueFiles = @('-f', (Join-Path $Out "base\$Release.yaml"),
                '-f', (Join-Path $Out "$Ns\values\$Release.yaml"))
$Rendered = & helm template $Release $Chart @ValueFiles
if ($LASTEXITCODE -ne 0) { throw "helm template failed" }
$RenderedFile = Join-Path $Out 'rendered.yaml'
($Rendered -join "`n") | Out-File -FilePath $RenderedFile -Encoding utf8

Write-Host ""
Write-Host "== 4. raw vs rendered, per CronJob =="
$Compare = @'
import sys, yaml, pathlib
fixture, rendered = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])

def cj(path):
    return [d for d in yaml.safe_load_all(path.read_text(encoding="utf-8"))
            if d and d.get("kind") == "CronJob"]

raw = {}
for d in cj(fixture):
    c = d["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]
    raw[d["metadata"]["name"]] = (c.get("command"), c.get("args"),
                                  {e["name"]: e.get("value") for e in c.get("env", [])})
got = {}
for d in cj(rendered):
    c = d["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]
    got[d["metadata"]["name"]] = (c.get("command"), c.get("args"),
                                  {e["name"]: e.get("value") for e in c.get("env", [])})

bad = 0
if len(got) != len(raw):
    print("  FAIL {} raw CronJobs, {} rendered".format(len(raw), len(got)))
    bad += 1
for name, (cmd, args, env) in sorted(raw.items()):
    match = [n for n, v in got.items() if v[0] == cmd]
    if not match:
        print("  LOST {:<20} command {} appears on no rendered CronJob".format(name, cmd))
        bad += 1
        continue
    n = match[0]
    rcmd, rargs, renv = got[n]
    ok = (rargs == args and renv.get("STAGE") == env.get("STAGE"))
    print("  {:<4} raw {:<18} -> rendered {:<22} STAGE={} args={}".format(
        "OK" if ok else "FAIL", name, n, renv.get("STAGE"), rargs))
    if not ok:
        print("       raw said STAGE={} args={}".format(env.get("STAGE"), args))
        bad += 1
sys.exit(1 if bad else 0)
'@
$ComparePy = Join-Path $Out 'compare.py'
$Compare | Out-File -FilePath $ComparePy -Encoding utf8
python $ComparePy (Join-Path $Fixture "$Ns\$Release.yaml") $RenderedFile
if ($LASTEXITCODE -ne 0) { throw "raw vs rendered command/env do not match" }

Write-Host ""
Write-Host "== 5. helm lint =="
& helm lint $Chart
if ($LASTEXITCODE -ne 0) { throw "helm lint failed" }

Write-Host ""
Write-Host "== 6. kubectl --dry-run=client =="
Write-Host "   SKIPPED - needs an API server for the OpenAPI schema; no cluster access here."

Remove-Item -Recurse -Force $Out
Write-Host ""
Write-Host "render_check: PASS"
