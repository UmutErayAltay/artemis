"""UIA öğeleri boş veya yetersiz olduğunda Windows OCR tabanlı görsel öğe çıkarıcı.

Pencere görüntüsü yalnız bellekte (`MemoryStream`) işlenir; diske yazılmaz ve
dış servise gönderilmez. Çıktı `UiElement` listesi olarak döner ve `ADR 0078`
yıkıcı öğe kapısından aynı şekilde geçer.
"""

from __future__ import annotations

import ctypes
import json
import logging
import subprocess
import sys
from collections.abc import Callable
from typing import Any

from artemis.computer.windows import UiElement
from artemis.tools.platform import child_environment

_logger = logging.getLogger(__name__)
_DEFAULT_MAX_ITEMS = 200

_VISION_OCR_SCRIPT = "\n".join(
    [
        "$ErrorActionPreference = 'Stop'",
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8",
        "$hwnd = [IntPtr][long]$env:ARTEMIS_VISION_HWND",
        "if ($hwnd -eq [IntPtr]::Zero) { '{\"words\":[]}'; exit 0 }",
        "Add-Type -AssemblyName System.Windows.Forms",
        "Add-Type -AssemblyName System.Drawing",
        "Add-Type -AssemblyName System.Runtime.WindowsRuntime",
        "Add-Type -TypeDefinition @'",
        "using System;",
        "using System.Runtime.InteropServices;",
        "public static class ArtemisVisionNative {",
        "  [StructLayout(LayoutKind.Sequential)]",
        "  public struct RECT { public int Left; public int Top; "
        "public int Right; public int Bottom; }",
        '  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();',
        '  [DllImport("user32.dll")] public static extern bool GetWindowRect('
        "IntPtr h, out RECT r);",
        '  [DllImport("dwmapi.dll")] public static extern int DwmGetWindowAttribute('
        "IntPtr h, int attr, out RECT r, int size);",
        "}",
        "'@",
        "[void][ArtemisVisionNative]::SetProcessDPIAware()",
        "$rect = New-Object ArtemisVisionNative+RECT",
        "$sz = [System.Runtime.InteropServices.Marshal]::SizeOf([type][ArtemisVisionNative+RECT])",
        "if ([ArtemisVisionNative]::DwmGetWindowAttribute($hwnd, 9, [ref]$rect, $sz) -ne 0) {",
        "  if (-not [ArtemisVisionNative]::GetWindowRect($hwnd, [ref]$rect)) "
        "{ '{\"words\":[]}'; exit 0 }",
        "}",
        "$left = $rect.Left; $top = $rect.Top",
        "$width = $rect.Right - $rect.Left; $height = $rect.Bottom - $rect.Top",
        "if ($width -le 4 -or $height -le 4) { '{\"words\":[]}'; exit 0 }",
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
        "function Wait-WinRT($op, [Type]$rt) {",
        "  $t = $asTask.MakeGenericMethod($rt).Invoke($null, @($op))",
        "  $t.Wait(-1) | Out-Null",
        "  return $t.Result",
        "}",
        "$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage("
        "(New-Object Windows.Globalization.Language 'tr-TR'))",
        "if ($null -eq $engine) { $engine = "
        "[Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages() }",
        "if ($null -eq $engine) { '{\"words\":[]}'; exit 0 }",
        "$bitmap = $null; $scaled = $null; $graphics = $null; $stream = $null; $ras = $null",
        "try {",
        "  $bitmap = New-Object System.Drawing.Bitmap $width, $height",
        "  $graphics = [System.Drawing.Graphics]::FromImage($bitmap)",
        "  $graphics.CopyFromScreen($left, $top, 0, 0, $bitmap.Size)",
        "  $image = $bitmap",
        "  $invScale = 1.0",
        "  $maxDim = [Windows.Media.Ocr.OcrEngine]::MaxImageDimension",
        "  $ratio = 1.0",
        "  if ($width -gt $maxDim -or $height -gt $maxDim) {",
        "    $ratio = [Math]::Min($maxDim / $width, $maxDim / $height)",
        "  } elseif ($width * 1.5 -le $maxDim -and $height * 1.5 -le $maxDim) {",
        "    $ratio = 1.5",
        "  }",
        "  if ($ratio -ne 1.0) {",
        "    $invScale = 1.0 / $ratio",
        "    $nw = [Math]::Max(1, [int][Math]::Floor($width * $ratio))",
        "    $nh = [Math]::Max(1, [int][Math]::Floor($height * $ratio))",
        "    $scaled = New-Object System.Drawing.Bitmap $nw, $nh",
        "    $sg = [System.Drawing.Graphics]::FromImage($scaled)",
        "    try {",
        "      $sg.InterpolationMode = "
        "[System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic",
        "      $sg.DrawImage($bitmap, 0, 0, $nw, $nh)",
        "    } finally { $sg.Dispose() }",
        "    $image = $scaled",
        "  }",
        "  $stream = New-Object System.IO.MemoryStream",
        "  $image.Save($stream, [System.Drawing.Imaging.ImageFormat]::Png)",
        "  $stream.Position = 0",
        "  $ras = [System.IO.WindowsRuntimeStreamExtensions]::AsRandomAccessStream($stream)",
        "  $dec = Wait-WinRT ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($ras)) "
        "([Windows.Graphics.Imaging.BitmapDecoder])",
        "  $sw = Wait-WinRT ($dec.GetSoftwareBitmapAsync()) "
        "([Windows.Graphics.Imaging.SoftwareBitmap])",
        "  try { $res = Wait-WinRT ($engine.RecognizeAsync($sw)) ([Windows.Media.Ocr.OcrResult]) } "
        "finally { $sw.Dispose() }",
        "  $linesOut = New-Object System.Collections.Generic.List[object]",
        "  foreach ($line in $res.Lines) {",
        "    $wordsOut = New-Object System.Collections.Generic.List[object]",
        "    foreach ($w in $line.Words) {",
        "      $b = $w.BoundingRect",
        "      $wordsOut.Add([pscustomobject]@{ t = [string]$w.Text; "
        "x = [int][math]::Round($b.X * $invScale); y = [int][math]::Round($b.Y * $invScale); "
        "w = [int][math]::Round($b.Width * $invScale); "
        "h = [int][math]::Round($b.Height * $invScale) })",
        "    }",
        "    if ($wordsOut.Count -gt 0) { $linesOut.Add($wordsOut) }",
        "  }",
        "  $payload = [pscustomobject]@{ left = $left; top = $top; lines = $linesOut }",
        "  Write-Output ($payload | ConvertTo-Json -Compress -Depth 6)",
        "} finally {",
        "  if ($null -ne $ras) { $ras.Dispose() }",
        "  if ($null -ne $stream) { $stream.Dispose() }",
        "  if ($null -ne $graphics) { $graphics.Dispose() }",
        "  if ($null -ne $scaled) { $scaled.Dispose() }",
        "  if ($null -ne $bitmap) { $bitmap.Dispose() }",
        "}",
    ]
)


def _clean_text(text: str) -> str:
    no_ctrl = "".join(ch if ord(ch) >= 0x20 else " " for ch in text)
    return " ".join(no_ctrl.split())[:80]


def group_ocr_lines_to_elements(
    lines: list[list[dict[str, Any]]],
    *,
    offset_x: int,
    offset_y: int,
    start_id: int = 1,
    max_items: int = _DEFAULT_MAX_ITEMS,
) -> list[UiElement]:
    """OCR satırlarındaki yakın kelimeleri ayrı tıklanabilir görsel öğelere gruplar."""
    elements: list[UiElement] = []
    for line_words in lines:
        if len(elements) >= max_items:
            break
        valid = [
            w for w in line_words
            if isinstance(w, dict) and str(w.get("t") or "").strip()
            and int(w.get("w", 0)) >= 2 and int(w.get("h", 0)) >= 2
        ]
        if not valid:
            continue
        valid.sort(key=lambda w: int(w["x"]))
        groups: list[list[dict[str, Any]]] = [[valid[0]]]
        for word in valid[1:]:
            prev = groups[-1][-1]
            prev_right = int(prev["x"]) + int(prev["w"])
            gap = int(word["x"]) - prev_right
            max_gap = max(18, int(max(int(prev["h"]), int(word["h"])) * 1.15))
            if gap <= max_gap:
                groups[-1].append(word)
            else:
                groups.append([word])

        for group in groups:
            if len(elements) >= max_items:
                break
            name = _clean_text(" ".join(str(w["t"]) for w in group))
            if not name:
                continue
            gx1 = min(int(w["x"]) for w in group)
            gy1 = min(int(w["y"]) for w in group)
            gx2 = max(int(w["x"]) + int(w["w"]) for w in group)
            gy2 = max(int(w["y"]) + int(w["h"]) for w in group)
            elements.append(
                UiElement(
                    id=start_id + len(elements),
                    kind="VisualText",
                    name=name,
                    rect=(offset_x + gx1, offset_y + gy1, gx2 - gx1, gy2 - gy1),
                    interactive=True,
                    focused=False,
                )
            )
    return elements


def vision_elements(
    hwnd: int,
    *,
    # Soğuk ilk çağrı (Add-Type derleme + WinRT yükleme) ölçümde ~6 sn, sıcak ~1 sn.
    timeout: float = 12.0,
    max_items: int = _DEFAULT_MAX_ITEMS,
    start_id: int = 1,
    runner: Callable[..., Any] | None = None,
) -> list[UiElement]:
    """Pencere görüntüsünden Windows OCR ile tıklanabilir öğe listesi çıkarır."""
    if sys.platform != "win32" or hwnd <= 0 or max_items <= 0:
        return []
    if runner is None and not ctypes.windll.user32.IsWindow(hwnd):
        return []
    env = child_environment()
    env["ARTEMIS_VISION_HWND"] = str(hwnd)
    run = runner or subprocess.run
    kwargs: dict[str, Any] = {
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": timeout,
        "env": env,
    }
    if runner is None and hasattr(subprocess, "CREATE_NO_WINDOW"):
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        completed = run(["powershell.exe", "-NoProfile", "-Command", _VISION_OCR_SCRIPT], **kwargs)
    except (OSError, subprocess.TimeoutExpired) as error:
        _logger.warning("vision_elements başarısız (hwnd=%s): %s", hwnd, error)
        return []
    if getattr(completed, "returncode", 1) != 0:
        return []
    raw = (getattr(completed, "stdout", "") or "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, dict):
        return []
    lines = parsed.get("lines")
    if not isinstance(lines, list):
        return []
    try:
        return group_ocr_lines_to_elements(
            lines,
            offset_x=int(parsed.get("left", 0)),
            offset_y=int(parsed.get("top", 0)),
            start_id=start_id,
            max_items=max_items,
        )
    except (TypeError, ValueError, KeyError):
        # Alt süreç çıktısı güvenilmez veridir: null/eksik koordinat oturumu çökertmez.
        return []
