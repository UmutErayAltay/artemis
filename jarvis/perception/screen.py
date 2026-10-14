"""Windows ekran OCR çalıştırıcısı: sabit PowerShell betiği, kapsam yalnız ortam değişkeniyle.

Desen `jarvis/tools/toast.py` ile aynıdır (ADR 0029): betik sabittir, argv'de kapsam/metin
yoktur, alt sürecin ortamı `child_environment()`'dır ve sonuç dürüst bir `ScreenOutcome`'dur.
Görüntü yalnız bellekte yaşar (`MemoryStream`); diske hiçbir dosya yazılmaz ve modele/buluta
görüntü gitmez, yalnız OCR metni döner. Windows'a özgü hiçbir şey import edilmez; her şey bir
`powershell.exe` alt sürecidir, bu yüzden modül Linux'ta da içe aktarılabilir.
"""

from __future__ import annotations

import base64
import json
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from jarvis.tools.platform import child_environment

SCREEN_TEXT_LIMIT = 16000
SCREEN_TITLE_LIMIT = 200
_ALLOWED_SCOPES = ("active_window", "screen")

# Kapsam kapısı ve P/Invoke tanımı. Tek tırnaklı here-string: PowerShell içinde `$` genişlemesi
# olmaz, C# kodu olduğu gibi derlenir. DPI farkındalığı olmadan yüksek DPI'da (%150 ölçekleme)
# pencere koordinatları sanal piksellerle uyuşmaz ve yanlış bölge yakalanır.
_NATIVE_TYPE = "\n".join(
    [
        "Add-Type -TypeDefinition @'",
        "using System;",
        "using System.Runtime.InteropServices;",
        "using System.Text;",
        "public static class JarvisNative {",
        "  [StructLayout(LayoutKind.Sequential)]",
        "  public struct RECT {",
        "    public int Left; public int Top; public int Right; public int Bottom;",
        "  }",
        '  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();',
        '  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();',
        '  [DllImport("user32.dll")]',
        "  public static extern bool GetWindowRect(IntPtr h, out RECT r);",
        '  [DllImport("user32.dll", CharSet = CharSet.Unicode)]',
        "  public static extern int GetWindowText(IntPtr h, StringBuilder s, int max);",
        '  [DllImport("dwmapi.dll")]',
        "  public static extern int DwmGetWindowAttribute(",
        "    IntPtr h, int attr, out RECT r, int size);",
        "}",
        "'@",
    ]
)

# Neden bu yapı: `SetProcessDPIAware` yakalamadan ÖNCE çağrılır. Pencere sınırları için DWM'in
# "extended frame bounds"u (9) tercih edilir çünkü `GetWindowRect` Windows 10/11'de görünmez
# gölge kenarlıkları da kapsar; DWM başarısızsa (0 dönmezse) GetWindowRect'e düşülür.
# WinRT `IAsyncOperation` PowerShell 5.1'de doğrudan beklenemez: `AsTask` genel yansıtma
# yardımcısı (tek parametreli, `IAsyncOperation`1` alan aşırı yükleme) ile Task'a çevrilir.
# Çıkış kodları: 2 geçersiz kapsam, 3 OCR dil paketi yok, 4 boyut alınamadı.
SCREEN_SCRIPT = "\n".join(
    [
        "$ErrorActionPreference = 'Stop'",
        "$scope = $env:JARVIS_SCREEN_SCOPE",
        "if ($scope -ne 'active_window' -and $scope -ne 'screen') { exit 2 }",
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8",
        "Add-Type -AssemblyName System.Windows.Forms",
        "Add-Type -AssemblyName System.Drawing",
        "Add-Type -AssemblyName System.Runtime.WindowsRuntime",
        _NATIVE_TYPE,
        "[void][JarvisNative]::SetProcessDPIAware()",
        "$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]",
        "$null = [Windows.Globalization.Language, Windows.Globalization, "
        "ContentType = WindowsRuntime]",
        "$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics, "
        "ContentType = WindowsRuntime]",
        "$null = [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Graphics, "
        "ContentType = WindowsRuntime]",
        "$null = [Windows.Media.Ocr.OcrResult, Windows.Foundation, ContentType = WindowsRuntime]",
        "$asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | "
        "Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and "
        "$_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' } | "
        "Select-Object -First 1",
        "function Wait-WinRT($operation, [Type]$resultType) {",
        "  $task = $asTask.MakeGenericMethod($resultType).Invoke($null, @($operation))",
        "  $task.Wait(-1) | Out-Null",
        "  return $task.Result",
        "}",
        "$title = ''",
        "if ($scope -eq 'active_window') {",
        "  $hwnd = [JarvisNative]::GetForegroundWindow()",
        "  if ($hwnd -eq [IntPtr]::Zero) { exit 4 }",
        "  $rect = New-Object JarvisNative+RECT",
        "  $size = [System.Runtime.InteropServices.Marshal]::SizeOf([type][JarvisNative+RECT])",
        "  if ([JarvisNative]::DwmGetWindowAttribute($hwnd, 9, [ref]$rect, $size) -ne 0) {",
        "    if (-not [JarvisNative]::GetWindowRect($hwnd, [ref]$rect)) { exit 4 }",
        "  }",
        "  $builder = New-Object System.Text.StringBuilder 512",
        "  [void][JarvisNative]::GetWindowText($hwnd, $builder, 512)",
        "  $title = $builder.ToString()",
        "  $left = $rect.Left; $top = $rect.Top",
        "  $width = $rect.Right - $rect.Left; $height = $rect.Bottom - $rect.Top",
        "} else {",
        "  $virtual = [System.Windows.Forms.SystemInformation]::VirtualScreen",
        "  $left = $virtual.Left; $top = $virtual.Top",
        "  $width = $virtual.Width; $height = $virtual.Height",
        "}",
        "if ($width -le 0 -or $height -le 0) { exit 4 }",
        "$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage("
        "(New-Object Windows.Globalization.Language 'tr-TR'))",
        "if ($null -eq $engine) { $engine = "
        "[Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages() }",
        "if ($null -eq $engine) { exit 3 }",
        "$bitmap = $null; $scaled = $null; $graphics = $null; $stream = $null; $ras = $null",
        "try {",
        "  $bitmap = New-Object System.Drawing.Bitmap $width, $height",
        "  $graphics = [System.Drawing.Graphics]::FromImage($bitmap)",
        "  $graphics.CopyFromScreen($left, $top, 0, 0, $bitmap.Size)",
        "  $image = $bitmap",
        "  $maxDim = [Windows.Media.Ocr.OcrEngine]::MaxImageDimension",
        "  if ($width -gt $maxDim -or $height -gt $maxDim) {",
        "    $ratio = [Math]::Min($maxDim / $width, $maxDim / $height)",
        "    $newWidth = [Math]::Max(1, [int][Math]::Floor($width * $ratio))",
        "    $newHeight = [Math]::Max(1, [int][Math]::Floor($height * $ratio))",
        "    $scaled = New-Object System.Drawing.Bitmap $newWidth, $newHeight",
        "    $scaledGraphics = [System.Drawing.Graphics]::FromImage($scaled)",
        "    try {",
        "      $scaledGraphics.InterpolationMode = "
        "[System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic",
        "      $scaledGraphics.DrawImage($bitmap, 0, 0, $newWidth, $newHeight)",
        "    } finally { $scaledGraphics.Dispose() }",
        "    $image = $scaled",
        "  }",
        "  $stream = New-Object System.IO.MemoryStream",
        "  $image.Save($stream, [System.Drawing.Imaging.ImageFormat]::Png)",
        "  $stream.Position = 0",
        "  $ras = [System.IO.WindowsRuntimeStreamExtensions]::AsRandomAccessStream($stream)",
        "  $decoder = Wait-WinRT ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($ras)) "
        "([Windows.Graphics.Imaging.BitmapDecoder])",
        "  $software = Wait-WinRT ($decoder.GetSoftwareBitmapAsync()) "
        "([Windows.Graphics.Imaging.SoftwareBitmap])",
        "  try {",
        "    $result = Wait-WinRT ($engine.RecognizeAsync($software)) "
        "([Windows.Media.Ocr.OcrResult])",
        "  } finally { $software.Dispose() }",
        "  $lines = @($result.Lines | ForEach-Object { $_.Text })",
        "  $payload = [ordered]@{",
        "    text = [string]::Join(\"`n\", $lines)",
        "    language = $engine.RecognizerLanguage.LanguageTag",
        "    title = $title",
        "  }",
        "  Write-Output ($payload | ConvertTo-Json -Compress)",
        "} finally {",
        "  if ($null -ne $ras) { $ras.Dispose() }",
        "  if ($null -ne $stream) { $stream.Dispose() }",
        "  if ($null -ne $graphics) { $graphics.Dispose() }",
        "  if ($null -ne $scaled) { $scaled.Dispose() }",
        "  if ($null -ne $bitmap) { $bitmap.Dispose() }",
        "}",
        "exit 0",
        "",
    ]
)


@dataclass(frozen=True)
class ScreenOutcome:
    status: Literal["ok", "empty", "unavailable", "error"]
    message: str
    text: str = ""
    language: str = ""
    window_title: str = ""
    truncated: bool = False


def _clean(text: str) -> str:
    """Kontrol karakterlerini (\\t ve \\n hariç) atar.

    OCR çıktısı modele ve denetim kaydına gider; NUL/ESC gibi karakterler JSONL'i ve terminali
    bozabilir, ekran içeriği de güvenilmez dış veridir.
    """
    return "".join(ch for ch in text if ch in "\t\n" or (ord(ch) >= 32 and ord(ch) != 127))


def _parse_payload(stdout: str) -> dict[str, object] | None:
    lines = [line for line in stdout.splitlines() if line.strip()]
    if not lines:
        return None
    try:
        payload = json.loads(lines[-1])
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def read_screen(
    scope: Literal["active_window", "screen"],
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    platform: str | None = None,
    timeout: float = 20.0,
) -> ScreenOutcome:
    if scope not in _ALLOWED_SCOPES:
        # Araç Literal ile zaten doğrular; bu yalnız savunma. Değer betiğe ASLA geçmez.
        raise ValueError(f"Geçersiz ekran kapsamı: {scope!r}")
    current = sys.platform if platform is None else platform
    if current != "win32":
        return ScreenOutcome("unavailable", "Ekran okuma yalnız Windows'ta çalışır.")
    # `-EncodedCommand`: betik UTF-16-LE + base64; kapsam argv'de değil ortamda gider.
    encoded = base64.b64encode(SCREEN_SCRIPT.encode("utf-16-le")).decode("ascii")
    command = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-EncodedCommand",
        encoded,
    ]
    env = {**child_environment(), "JARVIS_SCREEN_SCOPE": scope}
    try:
        completed = runner(
            command,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        return ScreenOutcome("error", "Ekran okuma zaman aşımına uğradı.")
    except (OSError, ValueError) as error:
        # İstisna metni yol/ortam ayrıntısı taşıyabilir; yalnız tür adı yazılır.
        return ScreenOutcome("error", f"Ekran okuma başlatılamadı ({type(error).__name__}).")
    # stderr bilerek mesaja alınmaz: ekran içeriği veya yol bilgisi taşıyabilir.
    code = completed.returncode
    if code == 3:
        return ScreenOutcome(
            "unavailable",
            "Windows OCR dil paketi bulunamadı; Ayarlar'dan bir OCR dil paketi kurun.",
        )
    if code == 4:
        return ScreenOutcome("error", "Pencere/ekran boyutu alınamadı.")
    if code == 2:
        return ScreenOutcome("error", "Ekran okuma kapsamı geçersiz.")
    if code != 0:
        return ScreenOutcome("error", f"Ekran okunamadı (çıkış kodu {code}).")

    payload = _parse_payload(completed.stdout or "")
    if payload is None or not isinstance(payload.get("text"), str):
        return ScreenOutcome("error", "Ekran okuma çıktısı anlaşılamadı.")
    text = _clean(str(payload["text"])).strip()
    language = _clean(str(payload.get("language") or ""))
    title = _clean(str(payload.get("title") or "")).replace("\n", " ").replace("\t", " ")
    title = title.strip()[:SCREEN_TITLE_LIMIT]
    if not text:
        # Boş metin asla "ok" değildir: "ekranda bir şey yok" demek ancak bir iddiadır.
        return ScreenOutcome(
            "empty", "Ekranda okunabilir metin bulunamadı.", language=language, window_title=title
        )
    truncated = len(text) > SCREEN_TEXT_LIMIT
    if truncated:
        text = text[:SCREEN_TEXT_LIMIT]
    return ScreenOutcome(
        "ok",
        "Ekran metni okundu." if not truncated else "Ekran metni okundu (kısaltıldı).",
        text=text,
        language=language,
        window_title=title,
        truncated=truncated,
    )
