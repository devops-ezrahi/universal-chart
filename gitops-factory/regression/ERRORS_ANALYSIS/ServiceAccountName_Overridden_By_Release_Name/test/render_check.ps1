# render_check.ps1 - ERRORS_ANALYSIS/ServiceAccountName_Overridden_By_Release_Name
#
# Offline chain: raw fixture -> converter -> helm template -> assert every
# Job/CronJob pod runs as the ServiceAccount the RAW manifest named, and that a
# job naming no SA still falls back to the release-level one.
#
# ASCII only: PowerShell 5.1 reads .ps1 as ANSI without a BOM.

$ErrorActionPreference = 'Stop'

$TestDir   = $PSScriptRoot
$GitOps    = (Resolve-Path (Join-Path $TestDir '..\..\..\..')).Path
$Chart     = (Resolve-Path (Join-Path $GitOps '..')).Path
$Converter = Join-Path $GitOps 'convert_to_universal_chart.py'
$Fixture   = Join-Path $TestDir 'fixture'
$RawFile   = Join-Path $Fixture 'background-jobs\nexus-backup.yaml'
$Out       = Join-Path ([System.IO.Path]::GetTempPath()) ("saname-" + [guid]::NewGuid().ToString('N'))

Write-Host "== 1. raw serviceAccountName declarations"
Select-String -Path $RawFile -Pattern 'kind:|serviceAccountName:' |
    ForEach-Object { Write-Host "   line $($_.LineNumber): $($_.Line.Trim())" }

Write-Host "== 2. converter (release slug is 'nexus-backup', raw SA is 'nexus')"
python $Converter --input $Fixture --output $Out --chart $Chart --skip-verify | Out-Null
if ($LASTEXITCODE -ne 0) { throw "converter failed" }

Write-Host "== 3. per-job serviceAccountName captured?"
$Hits = Get-ChildItem -Path $Out -Recurse -Filter '*.yaml' |
        ForEach-Object { Select-String -Path $_.FullName -Pattern 'serviceAccountName:' }
if (-not $Hits) { throw "FAIL: no generated values file carries a per-job serviceAccountName" }
$Hits | ForEach-Object { Write-Host "   $(Split-Path $_.Path -Leaf):$($_.LineNumber): $($_.Line.Trim())" }

Write-Host "== 4. helm template + assert"
$PyFile = Join-Path ([System.IO.Path]::GetTempPath()) ("sachk-" + [guid]::NewGuid().ToString('N') + ".py")
Set-Content -Path $PyFile -Encoding utf8 -Value @'
import glob, os, subprocess, sys, yaml

out, chart, raw_file = sys.argv[1], sys.argv[2], sys.argv[3]

# raw: (kind, object-name) -> serviceAccountName (or None)
raw = {}
for d in yaml.safe_load_all(open(raw_file, encoding="utf-8")):
    if not d:
        continue
    k = d.get("kind")
    if k == "Job":
        ps = d["spec"]["template"]["spec"]
    elif k == "CronJob":
        ps = d["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    else:
        continue
    raw[(k, d["metadata"]["name"])] = ps.get("serviceAccountName")

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
        if not d:
            continue
        k = d.get("kind")
        if k == "Job":
            ps = d["spec"]["template"]["spec"]
        elif k == "CronJob":
            ps = d["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        else:
            continue
        got = ps.get("serviceAccountName")
        name = d["metadata"]["name"]
        # Match the raw object this came from: the chart may prefix a CronJob's
        # own name, so match on suffix.
        match = [rk for rk in raw if rk[0] == k and name.endswith(rk[1])]
        if not match:
            print("   %-8s %-26s rendered SA=%-14s (no raw counterpart)" % (k, name, got))
            continue
        want = raw[match[0]]
        if want is None:
            ok = "OK (falls back to release SA)" if got == rel else "MISMATCH"
            if got != rel:
                bad += 1
            print("   %-8s %-26s raw SA=<unset>  rendered=%-14s %s" % (k, name, got, ok))
        else:
            ok = "OK" if got == want else "MISMATCH"
            if got != want:
                bad += 1
            print("   %-8s %-26s raw SA=%-9s rendered=%-14s %s" % (k, name, want, got, ok))
sys.exit(1 if bad else 0)
'@
python $PyFile $Out $Chart $RawFile
$Code = $LASTEXITCODE
Remove-Item -Force $PyFile
Remove-Item -Recurse -Force $Out
if ($Code -ne 0) { throw "FAIL: a pod references the wrong ServiceAccount" }

Write-Host ""
Write-Host "PASS: per-job ServiceAccounts preserved; unset ones still fall back to the release SA."
exit 0
