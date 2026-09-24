# render_check.ps1 - ERRORS_ANALYSIS/Subtract_Defaults_Removes_Explicit_Zero
#
# converter -> helm template (through the real defaults/base/values layering)
# -> assert every explicitly-declared 0 survives.
#
# ASCII only: PowerShell 5.1 reads .ps1 as ANSI without a BOM.

$ErrorActionPreference = 'Stop'
$Here    = Split-Path -Parent $MyInvocation.MyCommand.Path
$Gitops  = (Resolve-Path (Join-Path $Here '..\..\..\..')).Path
$Chart   = (Resolve-Path (Join-Path $Gitops '..')).Path
$Fixture = Join-Path $Here 'fixture'
$Out     = Join-Path $env:TEMP ('explicit-zero-' + [guid]::NewGuid().ToString('N'))
$Ns      = 'background-jobs'

Write-Host "== 1. converter =="
python (Join-Path $Gitops 'convert_to_universal_chart.py') `
    --input $Fixture --output $Out --chart $Chart --skip-verify
if ($LASTEXITCODE -ne 0) { throw "converter failed" }

Write-Host ""
Write-Host "== 2. where the zeros ended up across the layers =="
foreach ($L in @("$Ns\defaults.yaml", "base\nightly-report.yaml", "base\db-migrate.yaml",
                 "$Ns\values\nightly-report.yaml", "$Ns\values\db-migrate.yaml")) {
    $P = Join-Path $Out $L
    if (-not (Test-Path $P)) { continue }
    $Hits = Select-String -Path $P -Pattern "backoffLimit|JobsHistoryLimit"
    if ($Hits) {
        foreach ($H in $Hits) { Write-Host ("   {0,-36} {1}" -f $L, $H.Line.Trim()) }
    } else {
        Write-Host ("   {0,-36} (none here - supplied by a lower layer)" -f $L)
    }
}

Write-Host ""
Write-Host "== 3. helm template, layered base -> defaults -> values =="
foreach ($Slug in @('nightly-report', 'db-migrate')) {
    $Files = @()
    foreach ($L in @("base\$Slug.yaml", "$Ns\defaults.yaml", "$Ns\values\$Slug.yaml")) {
        $P = Join-Path $Out $L
        if (Test-Path $P) { $Files += @('-f', $P) }
    }
    $Rendered = & helm template $Slug $Chart @Files
    if ($LASTEXITCODE -ne 0) { throw "helm template failed for $Slug" }
    ($Rendered -join "`n") | Out-File -FilePath (Join-Path $Out "$Slug.rendered.yaml") -Encoding utf8
    Write-Host "   rendered $Slug"
}

Write-Host ""
Write-Host "== 4. raw vs rendered =="
$Compare = @'
import sys, yaml, pathlib
out = pathlib.Path(sys.argv[1]); fixture = pathlib.Path(sys.argv[2])

def load(p):
    return [d for d in yaml.safe_load_all(p.read_text(encoding="utf-8")) if d]

def pick(docs, kind):
    for d in docs:
        if d.get("kind") == kind:
            return d
    return None

raw_cj = pick(load(fixture / "background-jobs" / "nightly-report.yaml"), "CronJob")
raw_jb = pick(load(fixture / "background-jobs" / "db-migrate.yaml"), "Job")
got_cj = pick(load(out / "nightly-report.rendered.yaml"), "CronJob")
got_jb = pick(load(out / "db-migrate.rendered.yaml"), "Job")

checks = [
    ("CronJob successfulJobsHistoryLimit",
     raw_cj["spec"]["successfulJobsHistoryLimit"], got_cj["spec"]["successfulJobsHistoryLimit"]),
    ("CronJob failedJobsHistoryLimit",
     raw_cj["spec"]["failedJobsHistoryLimit"], got_cj["spec"]["failedJobsHistoryLimit"]),
    ("CronJob backoffLimit",
     raw_cj["spec"]["jobTemplate"]["spec"]["backoffLimit"],
     got_cj["spec"]["jobTemplate"]["spec"]["backoffLimit"]),
    ("Job backoffLimit", raw_jb["spec"]["backoffLimit"], got_jb["spec"]["backoffLimit"]),
]
bad = 0
print("  {:<34} {:>8} {:>10}".format("FIELD", "RAW", "RENDERED"))
for label, raw, got in checks:
    ok = raw == got
    if not ok:
        bad += 1
    print("  {:<34} {:>8} {:>10}  {}".format(label, raw, got, "OK" if ok else "FAIL"))
sys.exit(1 if bad else 0)
'@
$ComparePy = Join-Path $Out 'compare.py'
$Compare | Out-File -FilePath $ComparePy -Encoding utf8
python $ComparePy $Out $Fixture
if ($LASTEXITCODE -ne 0) { throw "an explicit 0 did not survive" }

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
