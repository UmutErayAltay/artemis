$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$env:PYTHONUTF8 = '1'
# Yerel proxy anahtarı yalnız bu süreç ortamında tutulur.
if (-not $env:OPENROUTER_API_KEY) {
    $cpaSecret = Read-Host 'EasyCLIProxyAPI access key' -AsSecureString
    $cpaPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($cpaSecret)
    try {
        $env:OPENROUTER_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($cpaPointer)
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($cpaPointer)
    }
}
try {
    & "$PSScriptRoot/.venv/Scripts/python.exe" -m jarvis desktop --ses --config "$PSScriptRoot/config/config.yaml"
} finally {
    Remove-Item Env:OPENROUTER_API_KEY -ErrorAction SilentlyContinue
}
