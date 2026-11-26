"""Görsel (Windows.Media.Ocr) öğe çıkarıcı: UIA'nın kör kaldığı pencereler için (ADR 0073).

Steam (CEF), oyun başlatıcıları ve tuval tabanlı arayüzler UI Automation ağacına yalnızca
dış çerçeveyi verir; içerideki düğme ve sekme metinleri ağaçta yoktur. Bu modül ön plan
penceresinin görüntüsünü bellekte (diske yazmadan) yakalar, Windows'un yerleşik OCR
motoruyla satır/sözcük kutularını çıkarır ve `UiElement` listesine dönüştürür.

PowerShell işçisi ilk çağrıda bir kez başlatılır (Add-Type C# derlemesi + WinRT OCR hazırlığı)
ve sonraki çağrılarda stdin/stdout üzerinden tek satırlık JSON istek/yanıt protokolüyle
yeniden kullanılır; zaman aşımı veya protokol hatasında işçi öldürülüp bir sonraki çağrıda
temiz başlatılır.

Ham görüntü hiçbir zaman diske veya günlüğe yazılmaz (ADR 0039/3).
"""

from __future__ import annotations

import atexit
import contextlib
import ctypes
import json
import logging
import queue
import subprocess
import sys
import threading
from collections.abc import Callable
from typing import Any

from artemis.computer.windows import UiElement
from artemis.tools.platform import child_environment

logger = logging.getLogger(__name__)
_DEFAULT_MAX_ITEMS = 200

# Kalıcı PowerShell OCR işçisi: başlangıçta bir kez DPI farkındalığı, C# P/Invoke ve WinRT OCR
# türlerini yükler; ardından stdin'den gelen her `{"hwnd": <int>}` satırı için pencereyi
# yalnızca bellekte (MemoryStream) yakalayıp tek satır JSON yanıtı yazar ve stdout'u boşaltır.
_VISION_OCR_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Runtime.WindowsRuntime
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public static class ArtemisVisionNative {
    [StructLayout(LayoutKind.Sequential)]
    public struct RECT { public int Left; public int Top; public int Right; public int Bottom; }
    [DllImport("user32.dll")] public static extern bool SetProcessDpiAwarenessContext(IntPtr value);
    [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
    [DllImport("dwmapi.dll")] public static extern int DwmGetWindowAttribute(
        IntPtr h, int attr, out RECT r, int size);
}
"@
try {
    if (-not [ArtemisVisionNative]::SetProcessDpiAwarenessContext([IntPtr](-4))) {
        [void][ArtemisVisionNative]::SetProcessDPIAware()
    }
} catch {
    [void][ArtemisVisionNative]::SetProcessDPIAware()
}

$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Globalization.Language, Windows.Globalization, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Graphics, ContentType = WindowsRuntime]
$null = [Windows.Media.Ocr.OcrResult, Windows.Foundation, ContentType = WindowsRuntime]

$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
})[0]
function Wait-WinRT($op, [Type]$t) {
    $task = $asTask.MakeGenericMethod($t).Invoke($null, @($op))
    $task.Wait()
    return $task.Result
}

function Invoke-OcrRequest([long]$hwndVal) {
    if ($hwndVal -le 0) { return '{"words":[]}' }
    $hwnd = [IntPtr]$hwndVal
    $rect = New-Object ArtemisVisionNative+RECT
    $hr = [ArtemisVisionNative]::DwmGetWindowAttribute(
        $hwnd, 9, [ref]$rect, [System.Runtime.InteropServices.Marshal]::SizeOf($rect))
    if ($hr -ne 0 -or ($rect.Right - $rect.Left) -lt 40) {
        if (-not [ArtemisVisionNative]::GetWindowRect($hwnd, [ref]$rect)) {
            return '{"words":[]}'
        }
    }
    $left = $rect.Left; $top = $rect.Top
    $width = $rect.Right - $rect.Left; $height = $rect.Bottom - $rect.Top
    if ($width -le 4 -or $height -le 4) { return '{"words":[]}' }

    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage((New-Object Windows.Globalization.Language 'tr-TR'))
    if ($null -eq $engine) { $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages() }
    if ($null -eq $engine) { return '{"words":[]}' }

    $bitmap = $null; $scaled = $null; $graphics = $null; $stream = $null; $ras = $null
    try {
        $bitmap = New-Object System.Drawing.Bitmap $width, $height
        $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
        $graphics.CopyFromScreen($left, $top, 0, 0, $bitmap.Size)
        $image = $bitmap
        $invScale = 1.0
        $maxDim = [Windows.Media.Ocr.OcrEngine]::MaxImageDimension
        $ratio = 1.0
        if ($width -gt $maxDim -or $height -gt $maxDim) {
            $ratio = [Math]::Min($maxDim / $width, $maxDim / $height)
        } elseif ($width * 1.5 -le $maxDim -and $height * 1.5 -le $maxDim) {
            $ratio = 1.5
        }
        if ($ratio -ne 1.0) {
            $invScale = 1.0 / $ratio
            $nw = [Math]::Max(1, [int][Math]::Floor($width * $ratio))
            $nh = [Math]::Max(1, [int][Math]::Floor($height * $ratio))
            $scaled = New-Object System.Drawing.Bitmap $nw, $nh
            $sg = [System.Drawing.Graphics]::FromImage($scaled)
            try {
                $sg.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
                $sg.DrawImage($bitmap, 0, 0, $nw, $nh)
            } finally { $sg.Dispose() }
            $image = $scaled
        }
        $stream = New-Object System.IO.MemoryStream
        $image.Save($stream, [System.Drawing.Imaging.ImageFormat]::Png)
        $stream.Position = 0
        $ras = [System.IO.WindowsRuntimeStreamExtensions]::AsRandomAccessStream($stream)
        $dec = Wait-WinRT ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($ras)) ([Windows.Graphics.Imaging.BitmapDecoder])
        $sw = Wait-WinRT ($dec.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
        try { $res = Wait-WinRT ($engine.RecognizeAsync($sw)) ([Windows.Media.Ocr.OcrResult]) } finally { $sw.Dispose() }
        $linesOut = New-Object System.Collections.Generic.List[object]
        foreach ($line in $res.Lines) {
            $wordsOut = New-Object System.Collections.Generic.List[object]
            foreach ($w in $line.Words) {
                $b = $w.BoundingRect
                $wordsOut.Add([pscustomobject]@{
                    t = [string]$w.Text
                    x = [int][math]::Round($b.X * $invScale)
                    y = [int][math]::Round($b.Y * $invScale)
                    w = [int][math]::Round($b.Width * $invScale)
                    h = [int][math]::Round($b.Height * $invScale)
                })
            }
            if ($wordsOut.Count -gt 0) { $linesOut.Add($wordsOut) }
        }
        $payload = [pscustomobject]@{ left = $left; top = $top; lines = $linesOut }
        return ($payload | ConvertTo-Json -Compress -Depth 6)
    } finally {
        if ($null -ne $ras) { $ras.Dispose() }
        if ($null -ne $stream) { $stream.Dispose() }
        if ($null -ne $graphics) { $graphics.Dispose() }
        if ($null -ne $scaled) { $scaled.Dispose() }
        if ($null -ne $bitmap) { $bitmap.Dispose() }
    }
}

while ($null -ne ($rawLine = [Console]::In.ReadLine())) {
    $trimmed = $rawLine.Trim()
    if (-not $trimmed) { continue }
    try {
        $req = $trimmed | ConvertFrom-Json
        $out = Invoke-OcrRequest ([long]$req.hwnd)
    } catch {
        $msg = [string]$_.Exception.Message
        $out = (@{ error = $msg } | ConvertTo-Json -Compress)
    }
    [Console]::Out.WriteLine($out)
    [Console]::Out.Flush()
}
"""  # noqa: E501


class _OcrWorker:
    """Tek bir kalıcı PowerShell OCR alt süreci ve satır okuma kuyruğu."""

    def __init__(self, proc: Any) -> None:
        self.proc = proc
        self.lines: queue.Queue[str | None] = queue.Queue()
        self._thread = threading.Thread(
            target=self._reader, name="artemis-vision-ocr-reader", daemon=True
        )
        self._thread.start()

    def _reader(self) -> None:
        stdout = self.proc.stdout
        try:
            while stdout is not None:
                line = stdout.readline()
                if not line:
                    break
                self.lines.put(line)
        except (OSError, ValueError):
            # İşçi süreci öldü ya da pipe kapandı: okuma biter, finally sonlandırıcıyı koyar.
            pass
        finally:
            self.lines.put(None)

    def kill(self) -> None:
        with contextlib.suppress(Exception):
            self.proc.kill()
        with contextlib.suppress(Exception):
            self.proc.wait(timeout=1.0)


# ponytail: global lock, tek OCR isteği aynı anda çalışır; paralel oturumda havuza geçilir.
_WORKER_LOCK = threading.Lock()
_WORKER: _OcrWorker | None = None
_WORKER_FACTORY: Callable[..., Any] | None = None


def _spawn_default_process() -> subprocess.Popen[str]:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.Popen(
        ["powershell.exe", "-NoProfile", "-Command", _VISION_OCR_SCRIPT],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=child_environment(),
        creationflags=flags,
    )


def _kill_locked_worker() -> None:
    global _WORKER
    worker = _WORKER
    _WORKER = None
    if worker is not None:
        worker.kill()


def _shutdown_worker() -> None:
    """Canlı OCR işçisini sonlandırır ve tutucuyu sıfırlar (atexit ve testler için)."""
    with _WORKER_LOCK:
        _kill_locked_worker()


atexit.register(_shutdown_worker)


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


def _request_ocr_line(
    hwnd: int,
    timeout: float,
    runner: Callable[..., Any] | None,
) -> str | None:
    """Kalıcı işçiye tek bir `hwnd` isteği gönderir ve yanıt satırını döndürür.

    Zaman aşımı, EOF, süreç çıkışı veya G/Ç hatasında işçi öldürülür ve `None` döner.
    """
    global _WORKER, _WORKER_FACTORY
    with _WORKER_LOCK:
        factory = runner if runner is not None else _spawn_default_process
        if _WORKER is None or _WORKER.proc.poll() is not None or _WORKER_FACTORY is not factory:
            _kill_locked_worker()
            try:
                proc = factory()
            except OSError as exc:
                logger.warning("Görsel OCR işçisi başlatılamadı: %s", exc)
                return None
            _WORKER = _OcrWorker(proc)
            _WORKER_FACTORY = factory

        worker = _WORKER
        payload = json.dumps({"hwnd": int(hwnd)}) + "\n"
        try:
            if worker.proc.stdin is None:
                raise OSError("OCR işçisi stdin kapalı.")
            worker.proc.stdin.write(payload)
            worker.proc.stdin.flush()
        except (OSError, ValueError) as exc:
            logger.warning("Görsel OCR işçisine yazılamadı: %s", exc)
            _kill_locked_worker()
            return None

        try:
            line = worker.lines.get(timeout=timeout)
        except queue.Empty:
            logger.warning("Görsel OCR zaman aşımı (%.1f sn).", timeout)
            _kill_locked_worker()
            return None

        if not line:
            logger.warning("Görsel OCR işçisi beklenmedik şekilde kapandı (EOF).")
            _kill_locked_worker()
            return None
        return line


def vision_elements(
    hwnd: int,
    *,
    timeout: float = 12.0,
    max_items: int = 200,
    start_id: int = 1,
    runner: Callable[..., Any] | None = None,
) -> list[UiElement]:
    """Pencere görüntüsünden Windows.Media.Ocr ile tıklanabilir metin kutularını çıkarır."""
    if sys.platform != "win32" or hwnd <= 0 or max_items <= 0:
        return []
    if runner is None:
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        user32.IsWindow.argtypes = [wintypes.HWND]
        user32.IsWindow.restype = wintypes.BOOL
        if not user32.IsWindow(hwnd):
            return []

    raw_line = _request_ocr_line(hwnd, timeout, runner)
    if raw_line is None:
        return []
    raw = raw_line.strip()
    if not raw:
        with _WORKER_LOCK:
            _kill_locked_worker()
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        logger.warning("Görsel OCR geçersiz JSON döndürdü.")
        with _WORKER_LOCK:
            _kill_locked_worker()
        return []
    if not isinstance(data, dict):
        with _WORKER_LOCK:
            _kill_locked_worker()
        return []
    if "error" in data:
        logger.warning("Görsel OCR istek hatası: %s", data.get("error"))
        return []
    lines = data.get("lines")
    if not isinstance(lines, list):
        return []
    try:
        return group_ocr_lines_to_elements(
            lines,
            offset_x=int(data.get("left", 0)),
            offset_y=int(data.get("top", 0)),
            start_id=start_id,
            max_items=max_items,
        )
    except (TypeError, ValueError, KeyError):
        return []
