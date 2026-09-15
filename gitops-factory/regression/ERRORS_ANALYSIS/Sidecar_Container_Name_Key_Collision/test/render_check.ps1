# render_check.ps1 - ERRORS_ANALYSIS/Sidecar_Container_Name_Key_Collision
#
# This folder's finding did NOT reproduce, so this script exists to produce the
# raw-vs-rendered evidence for that claim rather than to prove a fix.
#
# Two fixture namespaces share the microservice file name 'api-gateway' and
# therefore share base/api-gateway.yaml - the exact mechanism error.txt blames.
# For EACH namespace it prints the raw container list next to the rendered one
# and asserts they match (allowing for the known, separate main-container
# rename), with no container leaking between namespaces.
#
# ASCII only: PowerShell 5.1 reads .ps1 as ANSI without a BOM.

$ErrorActionPreference = 'Stop'

$TestDir   = $PSScriptRoot
$GitOps    = (Resolve-Path (Join-Path $TestDir '..\..\..\..')).Path
$Chart     = (Resolve-Path (Join-Path $GitOps '..')).Path
$Converter = Join-Path $GitOps 'convert_to_universal_chart.py'
$Fixture   = Join-Path $TestDir 'fixture'
$Out       = Join-Path ([System.IO.Path]::GetTempPath()) ("sidecar-" + [guid]::NewGuid().ToString('N'))

Write-Host "== 1. converter over BOTH fixture namespaces"
python $Converter --input $Fixture --output $Out --chart $Chart --skip-verify | Out-Null
if ($LASTEXITCODE -ne 0) { throw "converter failed" }

Write-Host "== 2. confirm both namespaces really do share one base file"
$Base = Join-Path $Out 'base\api-gateway.yaml'
if (-not (Test-Path $Base)) { throw "FAIL: no shared base/api-gateway.yaml - the premise of the test is gone" }
Write-Host "   shared base: $Base"
Write-Host "   sidecars hoisted into the shared base:"
$InBlock = $false
Get-Content $Base | ForEach-Object {
    if ($_ -match '^sidecars:') { $InBlock = $true; Write-Host "     $_"; return }
    if ($InBlock) { if ($_ -match '^\S') { $InBlock = $false; return }; Write-Host "     $_" }
}

$Failed = $false
foreach ($ns in @('shop-web','shop-web-eu')) {
    Write-Host ""
    Write-Host "== 3. namespace: $ns"
    $Vals = @(
        (Join-Path $Out 'base\api-gateway.yaml'),
        (Join-Path $Out "$ns\values\api-gateway.yaml")
    )
    $Args = @('template','api-gateway',$Chart)
    foreach ($v in $Vals) { $Args += @('-f',$v) }
    $Rendered = & helm @Args
    if ($LASTEXITCODE -ne 0) { throw "helm template failed for $ns" }

    $RawFile  = Join-Path $Fixture "$ns\api-gateway.yaml"
    $RendFile = Join-Path ([System.IO.Path]::GetTempPath()) ("rend-$ns-" + [guid]::NewGuid().ToString('N') + ".yaml")
    Set-Content -Path $RendFile -Value ($Rendered -join "`n") -Encoding utf8

    $PyFile = Join-Path ([System.IO.Path]::GetTempPath()) ("chk-" + [guid]::NewGuid().ToString('N') + ".py")
    Set-Content -Path $PyFile -Encoding utf8 -Value @'
import sys, yaml
raw_path, rend_path, ns = sys.argv[1], sys.argv[2], sys.argv[3]
raw = next(d for d in yaml.safe_load_all(open(raw_path, encoding="utf-8"))
           if d and d.get("kind") == "Deployment")
raw_cs = [c["name"] for c in raw["spec"]["template"]["spec"]["containers"]]
docs = [d for d in yaml.safe_load_all(open(rend_path, encoding="utf-8")) if d]
dep = next(d for d in docs if d.get("kind") == "Deployment")
rend_cs = [c["name"] for c in dep["spec"]["template"]["spec"]["containers"]]
print("   RAW      containers: %s" % raw_cs)
print("   RENDERED containers: %s" % rend_cs)
# containers[0] is renamed to the release by the chart - a separate, known
# defect (Main_Container_Name_Forced_To_Release_Name). Compare the sidecars.
raw_side  = set(raw_cs[1:])
rend_side = set(rend_cs) - {"api-gateway", raw_cs[0]}
missing = raw_side - rend_side
extra   = rend_side - raw_side
if missing:
    print("   FAIL: sidecars in raw but ABSENT from render: %s" % sorted(missing)); sys.exit(1)
if extra:
    print("   FAIL: containers rendered that %s never declared (leaked via shared base): %s"
          % (ns, sorted(extra))); sys.exit(1)
print("   OK: all %d sidecars present, none leaked from the other namespace" % len(raw_side))
'@
    python $PyFile $RawFile $RendFile $ns
    if ($LASTEXITCODE -ne 0) { $Failed = $true }
    Remove-Item -Force $PyFile, $RendFile
}

Remove-Item -Recurse -Force $Out
if ($Failed) { throw "sidecar collision REPRODUCED - error.txt's finding is live" }
Write-Host ""
Write-Host "PASS: no sidecar loss and no cross-namespace leak. error.txt's finding does NOT reproduce here."
exit 0
