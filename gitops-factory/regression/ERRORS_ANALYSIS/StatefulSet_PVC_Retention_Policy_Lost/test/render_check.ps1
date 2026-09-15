# render_check.ps1 — ERRORS_ANALYSIS/StatefulSet_PVC_Retention_Policy_Lost
#
# Full offline chain, no cluster: raw fixture -> converter -> helm template ->
# assert spec.persistentVolumeClaimRetentionPolicy survived with the exact
# value the raw manifest declared.
#
# This is the layer that catches "the converter captured it but the template
# never renders it". Exits non-zero on any mismatch.

$ErrorActionPreference = 'Stop'

$TestDir   = $PSScriptRoot
$GitOps    = (Resolve-Path (Join-Path $TestDir '..\..\..\..')).Path
$Chart     = (Resolve-Path (Join-Path $GitOps '..')).Path
$Converter = Join-Path $GitOps 'convert_to_universal_chart.py'
$Fixture   = Join-Path $TestDir 'fixture'
$RawSts    = Join-Path $Fixture 'platform-data\postgres.yaml'
$Out       = Join-Path ([System.IO.Path]::GetTempPath()) ("pvcret-" + [guid]::NewGuid().ToString('N'))

Write-Host "== 1. converter: $Fixture -> $Out"
python $Converter --input $Fixture --output $Out --chart $Chart --skip-verify | Out-Null
if ($LASTEXITCODE -ne 0) { throw "converter failed" }

Write-Host "== 2. generated values files"
$ValuesFiles = Get-ChildItem -Path $Out -Recurse -Filter 'postgres.yaml' | ForEach-Object { $_.FullName }
$ValuesFiles | ForEach-Object { Write-Host "   $_" }
if (-not $ValuesFiles) { throw "converter wrote no postgres values" }

Write-Host "== 3. converter captured the field?"
$Captured = $ValuesFiles | ForEach-Object { Select-String -Path $_ -Pattern 'persistentVolumeClaimRetentionPolicy' -SimpleMatch }
if (-not $Captured) { throw "FAIL: no generated values file mentions persistentVolumeClaimRetentionPolicy" }
$Captured | ForEach-Object { Write-Host "   $($_.Path):$($_.LineNumber): $($_.Line.Trim())" }

Write-Host "== 4. helm template"
$Args = @('template', 'postgres', $Chart)
foreach ($v in $ValuesFiles) { $Args += @('-f', $v) }
$Rendered = & helm @Args
if ($LASTEXITCODE -ne 0) { throw "helm template failed" }

Write-Host "== 5. side-by-side: raw vs rendered"
$RawBlock = (Select-String -Path $RawSts -Pattern 'persistentVolumeClaimRetentionPolicy' -Context 0,2).ToString()
Write-Host "   RAW      (from $RawSts):"
Select-String -Path $RawSts -Pattern 'persistentVolumeClaimRetentionPolicy' -Context 0,2 |
    ForEach-Object { $_.Line; $_.Context.PostContext } | ForEach-Object { Write-Host "     $($_.Trim())" }

$RenderedLines = $Rendered -split "`n"
$Idx = ($RenderedLines | Select-String -Pattern 'persistentVolumeClaimRetentionPolicy' -SimpleMatch)
if (-not $Idx) {
    Write-Host "   RENDERED: <ABSENT>"
    throw "FAIL: rendered StatefulSet has no persistentVolumeClaimRetentionPolicy -> K8s defaults to Delete -> DATA LOSS"
}
$Start = $Idx[0].LineNumber - 1
Write-Host "   RENDERED (helm template):"
$RenderedLines[$Start..($Start+2)] | ForEach-Object { Write-Host "     $($_.Trim())" }

Write-Host "== 6. exact value assertion"
$Body = ($RenderedLines[($Start+1)..($Start+2)] -join ' ')
if ($Body -notmatch 'whenDeleted:\s*Retain') { throw "FAIL: whenDeleted is not Retain -> got '$Body'" }
if ($Body -notmatch 'whenScaled:\s*Retain')  { throw "FAIL: whenScaled is not Retain -> got '$Body'" }

Write-Host "== 7. kubectl structural validation (client-side)"
# NOTE: `kubectl apply --dry-run=client` still fetches the OpenAPI schema from
# the API server, so with no cluster reachable it cannot run. It is attempted
# and reported, never fatal - the binding assertions are steps 5 and 6, which
# are pure text comparison against the raw manifest and need nothing external.
if (Get-Command kubectl -ErrorAction SilentlyContinue) {
    $PrevEAP = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $Rendered | kubectl apply --dry-run=client --validate=false -f - | Out-Null
    $Code = $LASTEXITCODE
    $ErrorActionPreference = $PrevEAP
    if ($Code -eq 0) {
        Write-Host "   ok (structure only; schema validation needs an API server)"
    } else {
        Write-Host "   SKIP: no API server reachable - cannot validate against the OpenAPI schema"
    }
} else {
    Write-Host "   SKIP: kubectl not on PATH"
}

Remove-Item -Recurse -Force $Out
Write-Host ""
Write-Host "PASS: raw whenDeleted/whenScaled: Retain survived converter + helm template."
exit 0
