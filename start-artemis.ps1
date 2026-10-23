$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$env:PYTHONUTF8 = '1'
# Anahtarlar Ayarlar > Model sağlayıcıları'ndan Kimlik Bilgisi Yöneticisi'ne yazılır ve çağrı
# anında oradan okunur (ADR 0034); burada sorulmaz. Eksikse açılıştaki sağlayıcı kontrolü söyler.
& "$PSScriptRoot/.venv/Scripts/python.exe" -m artemis desktop --ses --config "$PSScriptRoot/config/config.yaml"
