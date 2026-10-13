"""Windows Başlat menüsündeki kurulu uygulamalar; serbest shell girdisi alınmaz."""

from __future__ import annotations

import json
import os
import platform
import subprocess
from functools import lru_cache
from pathlib import Path

from jarvis.tools.platform import child_environment

_SCRIPT = r"""
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$targets = @{}
$shell = New-Object -ComObject WScript.Shell
$roots = @([Environment]::GetFolderPath('Programs'), [Environment]::GetFolderPath('CommonPrograms'))
foreach ($root in $roots) {
  Get-ChildItem -LiteralPath $root -Filter *.lnk -Recurse -ErrorAction SilentlyContinue |
    ForEach-Object {
    try {
      $link = $shell.CreateShortcut($_.FullName)
      $exe = [IO.Path]::GetFileName($link.TargetPath)
      if ($link.Arguments -match '--processStart\s+"?([^"\s]+\.exe)') { $exe = $Matches[1] }
      if ($exe -like '*.exe') { $targets[$_.BaseName] = $exe }
    } catch [System.Runtime.InteropServices.COMException] { }
  }
}
$packages = @{}
Get-AppxPackage | ForEach-Object {
  $package = $_
  try {
    $manifest = Get-AppxPackageManifest -Package $package
    foreach ($app in $manifest.Package.Applications.Application) {
      $id = $package.PackageFamilyName + '!' + $app.Id
      $packages[$id] = [IO.Path]::GetFileName($app.Executable)
    }
  } catch [System.Exception] { }
}
$apps = @(Get-StartApps | ForEach-Object {
  $exe = $targets[$_.Name]
  if ($packages.ContainsKey($_.AppID)) { $exe = $packages[$_.AppID] }
  if (-not $exe -and $_.AppID -match '([A-Za-z0-9_+.-]+\.exe)') { $exe = $Matches[1] }
  [pscustomobject]@{name=$_.Name; id=$_.AppID; process=$exe}
})
ConvertTo-Json -InputObject $apps -Compress
"""


@lru_cache(maxsize=1)
def installed_applications() -> dict[str, str]:
    if platform.system() != "Windows":
        return {}
    powershell = (
        Path(os.environ.get("SYSTEMROOT", r"C:\Windows"))
        / "System32/WindowsPowerShell/v1.0/powershell.exe"
    )
    result = subprocess.run(
        [str(powershell), "-NoProfile", "-NonInteractive", "-Command", _SCRIPT],
        capture_output=True, env=child_environment(), timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode != 0:
        raise OSError("Kurulu uygulama kataloğu alınamadı")
    records = json.loads(result.stdout.decode("utf-8-sig"))
    if not isinstance(records, list):
        raise ValueError("Kurulu uygulama kataloğu geçersiz")
    apps: dict[str, str] = {}
    for item in records:
        if not isinstance(item, dict):
            raise ValueError("Kurulu uygulama kaydı geçersiz")
        name, app_id, process = item.get("name"), item.get("id"), item.get("process") or ""
        if isinstance(name, str) and isinstance(app_id, str) and isinstance(process, str):
            apps[name] = f"startapp:{app_id}|{process}"
    return apps
