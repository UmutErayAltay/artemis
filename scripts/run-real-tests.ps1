# Gerçek bağımlılık isteyen testler (varsayılan `pytest` bunları atlar).
#
# UYARI: `disruptive` testlerin görünür yan etkisi vardır: gerçek toast, gerçek Chromium,
# ekran OCR'ı ve Windows giriş arka ucu. Açık işleri kaydedin; test sırasında fare/klavyeye
# dokunmayın. Ücretli model ÇAĞRILMAZ.
#
# Çıktı test edilen commit ile birlikte reports/real-tests-<commit>.txt dosyasına yazılır;
# sonuç RELIABILITY_STATUS.md'ye bu dosyaya bağlanarak işlenir.
#
# Kullanım:
#   .\scripts\run-real-tests.ps1
#   .\scripts\run-real-tests.ps1 -DfnModelDir C:\models\dfn3 -DfnAssets C:\dfn\assets -DfnGolden C:\dfn\golden.wav
param(
    [string]$DfnModelDir,
    [string]$DfnAssets,
    [string]$DfnGolden
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$python = Join-Path $root ".venv\Scripts\python.exe"
$commit = (git rev-parse --short HEAD).Trim()
$dirty = if (git status --porcelain) { "-dirty" } else { "" }
New-Item -ItemType Directory -Force reports | Out-Null
$report = "reports\real-tests-$commit$dirty.txt"

"commit: $commit$dirty`ndate: $(Get-Date -Format o)`nhost: Windows $([Environment]::OSVersion.Version)`n" |
    Set-Content -Encoding utf8 $report

# -rs: her atlanan testin nedeni rapora girer; "geçti" sanılan atlama görünür olur.
& $python -m pytest -m disruptive -rs -q -p no:cacheprovider 2>&1 | Tee-Object -Append $report
$disruptive = $LASTEXITCODE

$denoise = 0
if ($DfnModelDir) {
    $env:ARTEMIS_DFN_MODEL_DIR = $DfnModelDir
    if ($DfnAssets) { $env:ARTEMIS_DFN_ASSETS = $DfnAssets }
    if ($DfnGolden) { $env:ARTEMIS_DFN_GOLDEN_WAV = $DfnGolden }
    & $python -m pytest tests/test_voice_denoise.py -rs -q -p no:cacheprovider 2>&1 |
        Tee-Object -Append $report
    $denoise = $LASTEXITCODE
}

Write-Host "Rapor: $report"
exit [Math]::Max($disruptive, $denoise)
