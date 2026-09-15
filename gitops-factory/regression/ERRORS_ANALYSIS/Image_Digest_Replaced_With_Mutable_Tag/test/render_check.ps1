# render_check.ps1 - ERRORS_ANALYSIS/Image_Digest_Replaced_With_Mutable_Tag
#
# Offline chain: raw fixture -> converter -> helm template -> assert every image
# reference in the rendered pod is byte-identical to the raw one, and that
# Chart.AppVersion (:2.0.0) never leaks in as a substitute tag.
#
# ASCII only: PowerShell 5.1 reads .ps1 as ANSI without a BOM.

$ErrorActionPreference = 'Stop'

$TestDir   = $PSScriptRoot
$GitOps    = (Resolve-Path (Join-Path $TestDir '..\..\..\..')).Path
$Chart     = (Resolve-Path (Join-Path $GitOps '..')).Path
$Converter = Join-Path $GitOps 'convert_to_universal_chart.py'
$Fixture   = Join-Path $TestDir 'fixture'
$RawFile   = Join-Path $Fixture 'platform-data\postgres.yaml'
$Out       = Join-Path ([System.IO.Path]::GetTempPath()) ("digest-" + [guid]::NewGuid().ToString('N'))

Write-Host "== 1. raw image references (all digest-pinned)"
Select-String -Path $RawFile -Pattern '^\s+image: ' | ForEach-Object { Write-Host "   $($_.Line.Trim())" }

Write-Host "== 2. converter"
python $Converter --input $Fixture --output $Out --chart $Chart --skip-verify | Out-Null
if ($LASTEXITCODE -ne 0) { throw "converter failed" }

Write-Host "== 3. digest captured into values?"
$Hits = Get-ChildItem -Path $Out -Recurse -Filter '*.yaml' |
        ForEach-Object { Select-String -Path $_.FullName -Pattern 'digest:|sha256:' }
if (-not $Hits) { throw "FAIL: no generated values file mentions a digest" }
$Hits | ForEach-Object { Write-Host "   $(Split-Path $_.Path -Leaf):$($_.LineNumber): $($_.Line.Trim())" }

Write-Host "== 4. helm template + compare image sets"
$PyFile = Join-Path ([System.IO.Path]::GetTempPath()) ("digchk-" + [guid]::NewGuid().ToString('N') + ".py")
Set-Content -Path $PyFile -Encoding utf8 -Value @'
import glob, os, subprocess, sys, yaml

out, chart, raw_file = sys.argv[1], sys.argv[2], sys.argv[3]

raw = next(d for d in yaml.safe_load_all(open(raw_file, encoding="utf-8"))
           if d and d.get("kind") == "StatefulSet")
rp = raw["spec"]["template"]["spec"]
raw_images = sorted({c["image"] for c in rp["containers"]} |
                    {c["image"] for c in rp.get("initContainers", [])})

merged = {}
def deep_merge(a, b):
    o = dict(a)
    for k, v in b.items():
        o[k] = deep_merge(o[k], v) if k in o and isinstance(o[k], dict) and isinstance(v, dict) else v
    return o

for vf in sorted(glob.glob(os.path.join(out, "**", "*.yaml"), recursive=True)):
    if os.sep + "releases" + os.sep in vf or "applicationset" in vf or "application.yaml" in vf:
        continue
    if os.path.basename(vf) != "postgres.yaml":
        continue
    merged = deep_merge(merged, yaml.safe_load(open(vf, encoding="utf-8")) or {})

import tempfile
with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8") as f:
    yaml.safe_dump(merged, f)
    vpath = f.name
p = subprocess.run(["helm", "template", "postgres", chart, "-f", vpath],
                   capture_output=True, text=True)
os.unlink(vpath)
if p.returncode != 0:
    print("   RENDER FAIL", p.stderr[:400]); sys.exit(1)

sts = next(d for d in yaml.safe_load_all(p.stdout) if d and d.get("kind") == "StatefulSet")
sp = sts["spec"]["template"]["spec"]
got = sorted({c["image"] for c in sp["containers"]} |
             {c["image"] for c in sp.get("initContainers", [])})

print("   RAW:")
for i in raw_images:
    print("     " + i)
print("   RENDERED:")
for i in got:
    print("     " + i)

bad = 0
if got != raw_images:
    print("   FAIL: image sets differ")
    print("     missing:", sorted(set(raw_images) - set(got)))
    print("     extra:  ", sorted(set(got) - set(raw_images)))
    bad = 1
leaked = [i for i in got if ":2.0.0" in i]
if leaked:
    print("   FAIL: Chart.AppVersion leaked in as a tag:", leaked)
    bad = 1
if not bad:
    print("   OK: all %d image references round-tripped byte-identically" % len(got))
sys.exit(bad)
'@
python $PyFile $Out $Chart $RawFile
$Code = $LASTEXITCODE
Remove-Item -Force $PyFile
Remove-Item -Recurse -Force $Out
if ($Code -ne 0) { throw "FAIL: an immutable digest was replaced" }

Write-Host ""
Write-Host "PASS: every @sha256 digest survived converter + helm template."
exit 0
