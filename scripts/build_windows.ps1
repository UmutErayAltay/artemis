# Artemis Windows onedir paketi ve Inno Setup kurucusu derleme betiği (PowerShell 5.1 uyumlu).
$ErrorActionPreference = 'Stop'

$pyproject = Get-Content -Path "pyproject.toml" -Raw -Encoding UTF8
if ($pyproject -notmatch '(?m)^version = "(.+)"') {
    throw "pyproject.toml içinde sürüm bilgisi bulunamadı."
}
$version = $Matches[1].Trim()
Write-Host "Artemis sürümü: $version"

uv pip install --python .venv\Scripts\python.exe "pyinstaller==6.11.1"
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller kurulumu başarısız oldu."
}

.venv\Scripts\python.exe -m PyInstaller packaging\artemis.spec --noconfirm --clean --distpath dist --workpath build\pyinstaller
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller derlemesi başarısız oldu."
}

$isccPath = $null
$isccCommand = Get-Command ISCC.exe -ErrorAction SilentlyContinue
if ($null -ne $isccCommand) {
    $isccPath = $isccCommand.Source
} else {
    $candidates = @(
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -Path $candidate) {
            $isccPath = $candidate
            break
        }
    }
}

if ($null -ne $isccPath) {
    Write-Host "Inno Setup ile kurucu oluşturuluyor: $isccPath"
    & $isccPath "/DMyAppVersion=$version" "packaging\artemis.iss"
    if ($LASTEXITCODE -ne 0) {
        throw "Inno Setup kurucu derlemesi başarısız oldu."
    }
} else {
    Write-Host "Inno Setup (ISCC.exe) bulunamadı. Kurmak için: winget install JRSoftware.InnoSetup"
}
