# render_check.ps1 - ERRORS_ANALYSIS/StatefulSet_ServiceName_Hardcoded_To_Fullname
#
# Offline chain, no cluster: raw fixture -> converter -> helm template ->
# assert spec.serviceName still names the HEADLESS Service, and that a headless
# Service by that exact name is actually rendered.
#
# ASCII only on purpose: PowerShell 5.1 reads .ps1 as ANSI when there is no BOM,
# so a UTF-8 em-dash breaks string parsing.

$ErrorActionPreference = 'Stop'

$TestDir   = $PSScriptRoot
$GitOps    = (Resolve-Path (Join-Path $TestDir '..\..\..\..')).Path
$Chart     = (Resolve-Path (Join-Path $GitOps '..')).Path
$Converter = Join-Path $GitOps 'convert_to_universal_chart.py'
$Fixture   = Join-Path $TestDir 'fixture'
$RawSts    = Join-Path $Fixture 'platform-data\redis.yaml'
$Headless  = 'redis-cache-headless'
$Out       = Join-Path ([System.IO.Path]::GetTempPath()) ("svcname-" + [guid]::NewGuid().ToString('N'))

Write-Host "== 1. converter"
python $Converter --input $Fixture --output $Out --chart $Chart --skip-verify | Out-Null
if ($LASTEXITCODE -ne 0) { throw "converter failed" }

Write-Host "== 2. converter captured serviceName?"
$ValuesFiles = Get-ChildItem -Path $Out -Recurse -Filter 'redis.yaml' | ForEach-Object { $_.FullName }
$Hit = $ValuesFiles | ForEach-Object { Select-String -Path $_ -Pattern '^serviceName:' }
if (-not $Hit) { throw "FAIL: no generated values file carries serviceName" }
$Hit | ForEach-Object { Write-Host "   $($_.Path):$($_.LineNumber): $($_.Line.Trim())" }

Write-Host "== 3. helm template"
$Args = @('template', 'redis', $Chart)
foreach ($v in $ValuesFiles) { $Args += @('-f', $v) }
$Rendered = & helm @Args
if ($LASTEXITCODE -ne 0) { throw "helm template failed" }

Write-Host "== 4. side-by-side: raw vs rendered serviceName"
$RawVal  = ((Select-String -Path $RawSts -Pattern '^\s*serviceName:\s*(.+)$').Matches[0].Groups[1].Value).Trim()
$RendVal = ((($Rendered -split "`n") | Select-String -Pattern '^\s*serviceName:\s*(.+)$').Matches[0].Groups[1].Value).Trim()
Write-Host "   RAW      spec.serviceName = $RawVal"
Write-Host "   RENDERED spec.serviceName = $RendVal"
if ($RawVal -ne $RendVal) { throw "FAIL: serviceName changed: '$RawVal' -> '$RendVal'" }
if ($RendVal -ne $Headless) { throw "FAIL: expected the headless Service '$Headless', got '$RendVal'" }

Write-Host "== 5. does that Service actually exist, and is it headless?"
# A serviceName pointing at a non-existent object is just as broken as one
# pointing at the wrong object, so verify the target renders too.
# `& helm` returns an ARRAY of lines; -split would then apply per-line and no
# single element could ever hold both `kind:` and `name:`. Join first.
$Docs = ($Rendered -join "`n") -split "(?m)^---\s*$"
$Target = $Docs | Where-Object { $_ -match 'kind:\s*Service' -and $_ -match "name:\s*$Headless\b" }
if (-not $Target) { throw "FAIL: no Service named $Headless was rendered" }
Write-Host "   Service/$Headless rendered"
if ($Target -notmatch 'clusterIP:\s*None') { throw "FAIL: $Headless is not headless (clusterIP: None missing) - no per-pod DNS records" }
Write-Host "   clusterIP: None confirmed - per-pod DNS records will be published"

Remove-Item -Recurse -Force $Out
Write-Host ""
Write-Host "PASS: serviceName survived as '$Headless' and that headless Service is rendered."
exit 0
