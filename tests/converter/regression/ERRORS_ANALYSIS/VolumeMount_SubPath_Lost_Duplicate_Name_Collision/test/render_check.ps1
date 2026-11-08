# render_check.ps1 - ERRORS_ANALYSIS/VolumeMount_SubPath_Lost_Duplicate_Name_Collision
#
# Offline chain: raw fixture -> converter -> helm template -> assert ALL FOUR
# volumeMounts survive with the right name/mountPath/subPath, and that every
# rendered mount names a volume that actually exists.
#
# ASCII only: PowerShell 5.1 reads .ps1 as ANSI without a BOM.

$ErrorActionPreference = 'Stop'

$TestDir   = $PSScriptRoot
$GitOps    = (Resolve-Path (Join-Path $TestDir '..\..\..\..')).Path
$Chart     = (Resolve-Path (Join-Path $GitOps '..')).Path
$Converter = Join-Path $GitOps 'convert_to_universal_chart.py'
$Fixture   = Join-Path $TestDir 'fixture'
$RawDep    = Join-Path $Fixture 'shop-web\storefront.yaml'
$Out       = Join-Path ([System.IO.Path]::GetTempPath()) ("subpath-" + [guid]::NewGuid().ToString('N'))

Write-Host "== 1. raw manifest declares these mounts"
Select-String -Path $RawDep -Pattern '^\s+- name: |^\s+mountPath: |^\s+subPath: ' |
    Where-Object { $_.LineNumber -gt 30 -and $_.LineNumber -lt 52 } |
    ForEach-Object { Write-Host "   $($_.Line.Trim())" }

Write-Host "== 2. converter"
python $Converter --input $Fixture --output $Out --chart $Chart --skip-verify | Out-Null
if ($LASTEXITCODE -ne 0) { throw "converter failed" }

$ValuesFiles = Get-ChildItem -Path $Out -Recurse -Filter 'storefront.yaml' | ForEach-Object { $_.FullName }

Write-Host "== 3. generated volumeMounts map keys"
$BaseFile = $ValuesFiles | Where-Object { $_ -match '\\base\\' }
$InBlock = $false
Get-Content $BaseFile | ForEach-Object {
    if ($_ -match '^volumeMounts:') { $InBlock = $true; Write-Host "   $_"; return }
    if ($InBlock) {
        if ($_ -match '^\S') { $InBlock = $false; return }
        Write-Host "   $_"
    }
}

Write-Host "== 4. helm template"
$Args = @('template', 'storefront', $Chart)
foreach ($v in $ValuesFiles) { $Args += @('-f', $v) }
$Rendered = & helm @Args
if ($LASTEXITCODE -ne 0) { throw "helm template failed" }
$Text = $Rendered -join "`n"

Write-Host "== 5/6/7. parse the rendered YAML and compare mount-for-mount with the raw manifest"
# Hand-rolled indentation parsing in PowerShell is fragile; parse the YAML for
# real and assert on the structure. Compares (name, mountPath, subPath) as a
# set, and checks every rendered mount names a volume the pod actually declares
# (a composite key leaking through as `name` would reference nothing).
$RawDep | Out-Null
$PyCheck = @'
import sys, yaml
raw_path, rendered_path = sys.argv[1], sys.argv[2]

raw = next(d for d in yaml.safe_load_all(open(raw_path, encoding="utf-8"))
           if d and d.get("kind") == "Deployment")
rawc = raw["spec"]["template"]["spec"]["containers"][0]
raw_mounts = {(m["name"], m["mountPath"], m.get("subPath")) for m in rawc["volumeMounts"]}

docs = [d for d in yaml.safe_load_all(open(rendered_path, encoding="utf-8")) if d]
dep = next(d for d in docs if d.get("kind") == "Deployment")
pod = dep["spec"]["template"]["spec"]
c = pod["containers"][0]
got = {(m["name"], m["mountPath"], m.get("subPath")) for m in c.get("volumeMounts", [])}

print("   RAW mounts:")
for t in sorted(raw_mounts, key=lambda x: (x[0], x[1])):
    print(f"     name={t[0]:<12} mountPath={t[1]:<32} subPath={t[2]}")
print("   RENDERED mounts:")
for t in sorted(got, key=lambda x: (x[0], x[1])):
    print(f"     name={t[0]:<12} mountPath={t[1]:<32} subPath={t[2]}")

if got != raw_mounts:
    print("FAIL: rendered volumeMounts != raw volumeMounts")
    print("  missing from render:", sorted(raw_mounts - got))
    print("  extra in render:    ", sorted(got - raw_mounts))
    sys.exit(1)
print(f"   OK: all {len(raw_mounts)} mounts round-tripped exactly")

declared = {v["name"] for v in pod.get("volumes", [])}
bad = {n for n, _, _ in got if n not in declared}
if bad:
    print(f"FAIL: mounts reference undeclared volumes {sorted(bad)}; declared={sorted(declared)}")
    sys.exit(1)
print(f"   OK: every mount names a declared volume ({sorted(declared)})")
'@
$PyFile  = Join-Path ([System.IO.Path]::GetTempPath()) ("subpathchk-" + [guid]::NewGuid().ToString('N') + ".py")
$RendFile = Join-Path ([System.IO.Path]::GetTempPath()) ("subpathrender-" + [guid]::NewGuid().ToString('N') + ".yaml")
Set-Content -Path $PyFile -Value $PyCheck -Encoding utf8
Set-Content -Path $RendFile -Value $Text -Encoding utf8
python $PyFile $RawDep $RendFile
$Code = $LASTEXITCODE
Remove-Item -Force $PyFile, $RendFile
if ($Code -ne 0) { throw "rendered volumeMounts did not match the raw manifest" }

Remove-Item -Recurse -Force $Out
Write-Host ""
Write-Host "PASS: all 4 volumeMounts survived, including the 3 sharing volume 'nginx-conf'."
exit 0
