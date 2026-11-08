# render_check.ps1 - ERRORS_ANALYSIS/CronJob_Schedule_Value_Corruption
#
# converter -> helm template -> assert that every raw CronJob's schedule
# reaches a rendered CronJob, and that no rendered CronJob carries a schedule
# belonging to a different raw object.
#
# ASCII only: PowerShell 5.1 reads .ps1 as ANSI when there is no BOM, so a
# UTF-8 em-dash turns into a stray quote and breaks string parsing.

$ErrorActionPreference = 'Stop'
$Here    = Split-Path -Parent $MyInvocation.MyCommand.Path
$Gitops  = (Resolve-Path (Join-Path $Here '..\..\..\..')).Path
$Chart   = (Resolve-Path (Join-Path $Gitops '..')).Path
$Fixture = Join-Path $Here 'fixture'
$Out     = Join-Path $env:TEMP ('cronjob-schedule-' + [guid]::NewGuid().ToString('N'))
$Release = 'nightly-report'

Write-Host "== 1. converter =="
python (Join-Path $Gitops 'convert_to_universal_chart.py') `
    --input $Fixture --output $Out --chart $Chart --skip-verify
if ($LASTEXITCODE -ne 0) { throw "converter failed" }

Write-Host ""
Write-Host "== 2. collision warning reached conflicts_and_warnings.txt =="
$WarnFile = Join-Path $Out 'report\conflicts_and_warnings.txt'
$Warnings = Select-String -Path $WarnFile -Pattern 'name collision' -SimpleMatch
if (-not $Warnings) { throw "no collision warning was recorded in $WarnFile" }
$Warnings | ForEach-Object { Write-Host ("   " + $_.Line.Trim()) }

Write-Host ""
Write-Host "== 3. helm template =="
$ValueFiles = @()
Get-ChildItem -Path $Out -Recurse -Filter "$Release.yaml" |
    Where-Object { $_.Directory.Name -ne 'releases' } |
    Sort-Object FullName |
    ForEach-Object { $ValueFiles += @('-f', $_.FullName) }
$Rendered = & helm template $Release $Chart @ValueFiles
if ($LASTEXITCODE -ne 0) { throw "helm template failed" }
$RenderedFile = Join-Path $Out 'rendered.yaml'
($Rendered -join "`n") | Out-File -FilePath $RenderedFile -Encoding utf8

Write-Host ""
Write-Host "== 4. raw vs rendered, side by side =="
$Compare = @'
import sys, yaml, pathlib
fixture, rendered = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
raw = {d["metadata"]["name"]: d["spec"]["schedule"]
       for d in yaml.safe_load_all(fixture.read_text(encoding="utf-8"))
       if d and d.get("kind") == "CronJob"}
got = {d["metadata"]["name"]: d["spec"]["schedule"]
       for d in yaml.safe_load_all(rendered.read_text(encoding="utf-8"))
       if d and d.get("kind") == "CronJob"}
print("  {:<26} {:<14} {:<26} {}".format("RAW NAME", "RAW SCHEDULE", "RENDERED NAME", "RENDERED SCHEDULE"))
bad = 0
for name, sched in sorted(raw.items()):
    match = [n for n, s in got.items() if s == sched]
    mark = "OK" if match else "LOST"
    if not match:
        bad += 1
    print("  {:<26} {:<14} {:<26} {}".format(
        name, sched, match[0] if match else "-", sched if match else "-"), mark)
if len(got) != len(raw):
    print("  FAIL {} raw CronJobs, {} rendered".format(len(raw), len(got)))
    bad += 1
for name, sched in sorted(got.items()):
    if name in raw and raw[name] != sched:
        print("  FAIL {} rendered with {!r}, its raw object says {!r}".format(name, sched, raw[name]))
        bad += 1
sys.exit(1 if bad else 0)
'@
$ComparePy = Join-Path $Out 'compare.py'
$Compare | Out-File -FilePath $ComparePy -Encoding utf8
python $ComparePy (Join-Path $Fixture 'background-jobs\nightly-report.yaml') $RenderedFile
if ($LASTEXITCODE -ne 0) { throw "raw vs rendered schedules do not match" }

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
