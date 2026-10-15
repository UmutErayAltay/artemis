"""Windows pencere ve sistem kontrolü: sabit PowerShell betikleri, parametre yalnız ortamda.

Desen `jarvis/perception/screen.py` ile aynıdır (ADR 0031): her betik modül düzeyinde bir
sabittir, `-EncodedCommand` ile gönderilir, argv'de kullanıcı değeri bulunmaz, değerler yalnız
Python'da doğrulandıktan sonra ortam değişkeniyle taşınır ve alt sürecin ortamı
`child_environment()`'dır. Kapatma ve yeniden başlatma `shutdown.exe`'yi sabit bayraklarla
doğrudan çalıştırır. Windows'a özgü hiçbir şey import edilmez; her şey bir alt süreçtir, bu
yüzden modül Linux'ta da içe aktarılabilir ve testler PowerShell çalıştırmaz.

Dürüstlük kuralı (CLAUDE.md 5): ses, parlaklık ve pencere işlemleri sonradan geri okunur;
uyuşmazlık başarı sayılmaz. Kilit, uyku, kapatma ve yeniden başlatmada yalnız komutun
gönderildiği bilinir, bu yüzden `status="sent"` döner.
"""

from __future__ import annotations

import base64
import json
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, get_args

from jarvis.tools.platform import child_environment

WindowAction = Literal["focus", "maximize", "minimize", "restore", "left", "right"]
PowerAction = Literal["lock", "sleep", "shutdown", "restart", "cancel_shutdown"]
Runner = Callable[..., subprocess.CompletedProcess[str]]

WINDOW_LIMIT = 50
WINDOW_TITLE_LIMIT = 120
PROCESS_NAME_LIMIT = 64
MIN_SHUTDOWN_DELAY = 30
MAX_SHUTDOWN_DELAY = 600

_WINDOW_ACTIONS = get_args(WindowAction)
_POWER_ACTIONS = get_args(PowerAction)
_UNAVAILABLE_MESSAGE = "Sistem kontrolü yalnız Windows'ta çalışır."

# Neden: /c metni kullanıcıya Windows'un kapanma penceresinde gösterilir. Sabit bir şablondur,
# yalnız doğrulanmış tamsayı gecikme girer; böylece argv'ye serbest metin sızamaz.
_SHUTDOWN_COMMENT = (
    "Jarvis: bilgisayar {delay} saniye sonra kapanacak. "
    "İptal için Jarvis'e 'kapatmayı iptal et' deyin ya da 'shutdown /a' çalıştırın."
)
_RESTART_COMMENT = (
    "Jarvis: bilgisayar {delay} saniye sonra yeniden başlatılacak. "
    "İptal için Jarvis'e 'kapatmayı iptal et' deyin ya da 'shutdown /a' çalıştırın."
)

_WINDOW_NATIVE_TYPE = "\n".join(
    [
        "Add-Type -TypeDefinition @'",
        "using System;",
        "using System.Collections.Generic;",
        "using System.Runtime.InteropServices;",
        "using System.Text;",
        "public static class JarvisWin {",
        "  [StructLayout(LayoutKind.Sequential)]",
        "  public struct RECT {",
        "    public int Left; public int Top; public int Right; public int Bottom;",
        "  }",
        "  [StructLayout(LayoutKind.Sequential)]",
        "  public struct MONITORINFO {",
        "    public int cbSize; public RECT rcMonitor; public RECT rcWork; public int dwFlags;",
        "  }",
        "  public delegate bool EnumProc(IntPtr h, IntPtr l);",
        '  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc p, IntPtr l);',
        '  [DllImport("user32.dll")] public static extern bool IsWindow(IntPtr h);',
        '  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);',
        '  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);',
        '  [DllImport("user32.dll")] public static extern bool IsZoomed(IntPtr h);',
        '  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int cmd);',
        '  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);',
        '  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();',
        '  [DllImport("user32.dll")] public static extern IntPtr GetWindow(IntPtr h, uint cmd);',
        '  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();',
        '  [DllImport("user32.dll")]',
        "  public static extern bool GetWindowRect(IntPtr h, out RECT r);",
        '  [DllImport("user32.dll")]',
        "  public static extern IntPtr MonitorFromWindow(IntPtr h, uint flags);",
        '  [DllImport("user32.dll")]',
        "  public static extern bool GetMonitorInfo(IntPtr m, ref MONITORINFO i);",
        '  [DllImport("user32.dll")]',
        "  public static extern bool SetWindowPos(IntPtr h, IntPtr after, int x, int y,",
        "    int cx, int cy, uint flags);",
        '  [DllImport("user32.dll", CharSet = CharSet.Unicode)]',
        "  public static extern int GetWindowText(IntPtr h, StringBuilder s, int max);",
        '  [DllImport("user32.dll")]',
        "  public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);",
        '  [DllImport("dwmapi.dll")]',
        "  public static extern int DwmGetWindowAttribute(",
        "    IntPtr h, int attr, out int v, int size);",
        "  public static bool IsCloaked(IntPtr h) {",
        "    int cloaked = 0;",
        "    try {",
        "      if (DwmGetWindowAttribute(h, 14, out cloaked, 4) != 0) return false;",
        "    } catch (Exception) { return false; }",
        "    return cloaked != 0;",
        "  }",
        "  public static string Title(IntPtr h) {",
        "    StringBuilder b = new StringBuilder(512);",
        "    GetWindowText(h, b, 512);",
        "    return b.ToString();",
        "  }",
        "  public static long[] TopLevel() {",
        "    List<long> found = new List<long>();",
        "    EnumProc proc = delegate(IntPtr h, IntPtr l) {",
        "      if (!IsWindowVisible(h)) return true;",
        "      if (GetWindow(h, 4) != IntPtr.Zero) return true;",
        "      if (Title(h).Length == 0) return true;",
        "      if (IsCloaked(h)) return true;",
        "      found.Add(h.ToInt64());",
        "      return true;",
        "    };",
        "    EnumWindows(proc, IntPtr.Zero);",
        "    GC.KeepAlive(proc);",
        "    return found.ToArray();",
        "  }",
        "}",
        "'@",
    ]
)

# Neden: C# yalnız süzgeci yapar; JSON'u PowerShell kurar (el ile kaçış hatasını önler).
# Betik WINDOW_LIMIT+1 pencere toplar: fazlalık varsa `truncated` doğru olur. Çıkış kodu 4:
# beklenmeyen hata.
WINDOW_LIST_SCRIPT = "\n".join(
    [
        "$ErrorActionPreference = 'Stop'",
        "try {",
        "  [Console]::OutputEncoding = [System.Text.Encoding]::UTF8",
        _WINDOW_NATIVE_TYPE,
        "  $foreground = [JarvisWin]::GetForegroundWindow().ToInt64()",
        "  $items = New-Object System.Collections.ArrayList",
        "  $truncated = $false",
        "  foreach ($id in [JarvisWin]::TopLevel()) {",
        f"    if ($items.Count -ge {WINDOW_LIMIT}) {{ $truncated = $true; break }}",
        "    $pidValue = [uint32]0",
        "    [void][JarvisWin]::GetWindowThreadProcessId([IntPtr]$id, [ref]$pidValue)",
        "    $name = ''",
        "    try { $name = (Get-Process -Id $pidValue -ErrorAction Stop).ProcessName }",
        "    catch { $name = '' }",
        "    [void]$items.Add([ordered]@{",
        "      id = $id",
        "      title = [JarvisWin]::Title([IntPtr]$id)",
        "      process = $name",
        "      active = ($id -eq $foreground)",
        "    })",
        "  }",
        "  $payload = [ordered]@{ windows = @($items); truncated = $truncated }",
        "  Write-Output ($payload | ConvertTo-Json -Compress -Depth 4)",
        "} catch { exit 4 }",
        "exit 0",
        "",
    ]
)

# Çıkış kodları: 2 geçersiz girdi, 5 pencere yok/görünmez, 6 Windows odağı engelledi,
# 7 geri okuma tutmadı, 4 beklenmeyen hata. DPI farkındalığı olmadan yüksek DPI'da çalışma
# alanı ile pencere koordinatları uyuşmaz ve yarım ekran yanlış hesaplanır.
WINDOW_CONTROL_SCRIPT = "\n".join(
    [
        "$ErrorActionPreference = 'Stop'",
        "try {",
        _WINDOW_NATIVE_TYPE,
        "  $rawId = $env:JARVIS_WINDOW_ID",
        "  $action = $env:JARVIS_WINDOW_ACTION",
        "  $idValue = [long]0",
        "  if (-not [long]::TryParse($rawId, [ref]$idValue) -or $idValue -le 0) { exit 2 }",
        "  if (@('focus','maximize','minimize','restore','left','right') -notcontains $action) {",
        "    exit 2",
        "  }",
        "  [void][JarvisWin]::SetProcessDPIAware()",
        "  $h = [IntPtr]$idValue",
        "  if (-not [JarvisWin]::IsWindow($h) -or -not [JarvisWin]::IsWindowVisible($h)) {",
        "    exit 5",
        "  }",
        "  switch ($action) {",
        "    'focus' {",
        "      if ([JarvisWin]::IsIconic($h)) { [void][JarvisWin]::ShowWindow($h, 9) }",
        "      [void][JarvisWin]::SetForegroundWindow($h)",
        "      Start-Sleep -Milliseconds 150",
        "      if ([JarvisWin]::GetForegroundWindow() -ne $h) { exit 6 }",
        "    }",
        "    'maximize' {",
        "      [void][JarvisWin]::ShowWindow($h, 3)",
        "      Start-Sleep -Milliseconds 150",
        "      if (-not [JarvisWin]::IsZoomed($h)) { exit 7 }",
        "    }",
        "    'minimize' {",
        "      [void][JarvisWin]::ShowWindow($h, 6)",
        "      Start-Sleep -Milliseconds 150",
        "      if (-not [JarvisWin]::IsIconic($h)) { exit 7 }",
        "    }",
        "    'restore' {",
        "      [void][JarvisWin]::ShowWindow($h, 9)",
        "      Start-Sleep -Milliseconds 150",
        "      if ([JarvisWin]::IsIconic($h) -or [JarvisWin]::IsZoomed($h)) { exit 7 }",
        "    }",
        "    default {",
        "      [void][JarvisWin]::ShowWindow($h, 9)",
        "      $monitor = [JarvisWin]::MonitorFromWindow($h, 2)",
        "      $info = New-Object JarvisWin+MONITORINFO",
        "      $infoType = [type][JarvisWin+MONITORINFO]",
        "      $info.cbSize = [System.Runtime.InteropServices.Marshal]::SizeOf($infoType)",
        "      if (-not [JarvisWin]::GetMonitorInfo($monitor, [ref]$info)) { exit 4 }",
        "      $work = $info.rcWork",
        "      $half = [int][Math]::Floor(($work.Right - $work.Left) / 2)",
        "      $height = $work.Bottom - $work.Top",
        "      $x = $work.Left",
        "      if ($action -eq 'right') { $x = $work.Left + $half }",
        "      $width = $half",
        "      if ($action -eq 'right') { $width = $work.Right - $x }",
        "      [void][JarvisWin]::SetWindowPos($h, [IntPtr]::Zero, $x, $work.Top, "
        "$width, $height, 0x0044)",
        "      Start-Sleep -Milliseconds 150",
        "      $rect = New-Object JarvisWin+RECT",
        "      if (-not [JarvisWin]::GetWindowRect($h, [ref]$rect)) { exit 7 }",
        "      $close = ([Math]::Abs($rect.Left - $x) -le 16) -and",
        "        ([Math]::Abs($rect.Top - $work.Top) -le 16) -and",
        "        ([Math]::Abs(($rect.Right - $rect.Left) - $width) -le 16) -and",
        "        ([Math]::Abs(($rect.Bottom - $rect.Top) - $height) -le 16)",
        "      if (-not $close) { exit 7 }",
        "    }",
        "  }",
        "  Write-Output '{\"ok\":true}'",
        "} catch { exit 4 }",
        "exit 0",
        "",
    ]
)

_AUDIO_NATIVE_TYPE = "\n".join(
    [
        "Add-Type -TypeDefinition @'",
        "using System;",
        "using System.Runtime.InteropServices;",
        '[ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")]',
        "public class MMDeviceEnumeratorCom { }",
        '[Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"),',
        " InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]",
        "public interface IMMDeviceEnumerator {",
        "  int EnumAudioEndpoints(int dataFlow, int stateMask, out IntPtr devices);",
        "  int GetDefaultAudioEndpoint(int dataFlow, int role, out IMMDevice device);",
        "}",
        '[Guid("D666063F-1587-4E43-81F1-B948E807363F"),',
        " InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]",
        "public interface IMMDevice {",
        "  int Activate(ref Guid iid, int clsCtx, IntPtr activationParams,",
        "    [MarshalAs(UnmanagedType.IUnknown)] out object iface);",
        "}",
        '[Guid("5CDF2C82-841E-4546-9722-0CF74078229A"),',
        " InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]",
        "public interface IAudioEndpointVolume {",
        "  int RegisterControlChangeNotify(IntPtr notify);",
        "  int UnregisterControlChangeNotify(IntPtr notify);",
        "  int GetChannelCount(out uint count);",
        "  int SetMasterVolumeLevel(float level, ref Guid context);",
        "  int SetMasterVolumeLevelScalar(float level, ref Guid context);",
        "  int GetMasterVolumeLevel(out float level);",
        "  int GetMasterVolumeLevelScalar(out float level);",
        "  int SetChannelVolumeLevel(uint channel, float level, ref Guid context);",
        "  int SetChannelVolumeLevelScalar(uint channel, float level, ref Guid context);",
        "  int GetChannelVolumeLevel(uint channel, out float level);",
        "  int GetChannelVolumeLevelScalar(uint channel, out float level);",
        "  int SetMute([MarshalAs(UnmanagedType.Bool)] bool mute, ref Guid context);",
        "  int GetMute([MarshalAs(UnmanagedType.Bool)] out bool mute);",
        "}",
        "public static class JarvisAudio {",
        "  public static IAudioEndpointVolume Open() {",
        "    IMMDeviceEnumerator enumerator = (IMMDeviceEnumerator)(new MMDeviceEnumeratorCom());",
        "    IMMDevice device;",
        "    if (enumerator.GetDefaultAudioEndpoint(0, 1, out device) != 0) return null;",
        '    Guid iid = new Guid("5CDF2C82-841E-4546-9722-0CF74078229A");',
        "    object endpoint;",
        "    if (device.Activate(ref iid, 23, IntPtr.Zero, out endpoint) != 0) return null;",
        "    return (IAudioEndpointVolume)endpoint;",
        "  }",
        "}",
        "'@",
    ]
)

# Çıkış kodları: 2 geçersiz girdi, 3 varsayılan ses aygıtı yok, 4 beklenmeyen hata.
# Düzey geri okunup 0-100'e yuvarlanır; Python bunu istenenle karşılaştırır.
VOLUME_SCRIPT = "\n".join(
    [
        "$ErrorActionPreference = 'Stop'",
        "try {",
        _AUDIO_NATIVE_TYPE,
        "  [Console]::OutputEncoding = [System.Text.Encoding]::UTF8",
        "  $rawLevel = $env:JARVIS_VOLUME_LEVEL",
        "  $rawMute = $env:JARVIS_VOLUME_MUTE",
        "  $level = -1",
        "  if ($rawLevel) {",
        "    $parsed = 0",
        "    if (-not [int]::TryParse($rawLevel, [ref]$parsed)) { exit 2 }",
        "    if ($parsed -lt 0 -or $parsed -gt 100) { exit 2 }",
        "    $level = $parsed",
        "  }",
        "  if ($rawMute -and $rawMute -ne '1' -and $rawMute -ne '0') { exit 2 }",
        "  if ($level -lt 0 -and -not $rawMute) { exit 2 }",
        "  $endpoint = [JarvisAudio]::Open()",
        "  if ($null -eq $endpoint) { exit 3 }",
        "  $context = [Guid]::Empty",
        "  if ($level -ge 0) {",
        "    [void]$endpoint.SetMasterVolumeLevelScalar([single]($level / 100.0), [ref]$context)",
        "  }",
        "  if ($rawMute) { [void]$endpoint.SetMute(($rawMute -eq '1'), [ref]$context) }",
        "  $current = [single]0",
        "  [void]$endpoint.GetMasterVolumeLevelScalar([ref]$current)",
        "  $muted = $false",
        "  [void]$endpoint.GetMute([ref]$muted)",
        "  $payload = [ordered]@{",
        "    level = [int][Math]::Round($current * 100)",
        "    muted = [bool]$muted",
        "  }",
        "  Write-Output ($payload | ConvertTo-Json -Compress)",
        "} catch { exit 4 }",
        "exit 0",
        "",
    ]
)

# Harici monitörlerde `WmiMonitorBrightnessMethods` sınıfı yoktur: bu \"desteklenmiyor\" (3)
# demektir, hata değil. Çıkış kodları: 2 geçersiz girdi, 3 destek yok, 4 beklenmeyen hata.
BRIGHTNESS_SCRIPT = "\n".join(
    [
        "$ErrorActionPreference = 'Stop'",
        "try {",
        "  [Console]::OutputEncoding = [System.Text.Encoding]::UTF8",
        "  $parsed = 0",
        "  if (-not [int]::TryParse($env:JARVIS_BRIGHTNESS_LEVEL, [ref]$parsed)) { exit 2 }",
        "  if ($parsed -lt 0 -or $parsed -gt 100) { exit 2 }",
        "  $methods = Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightnessMethods "
        "-ErrorAction SilentlyContinue | Select-Object -First 1",
        "  if ($null -eq $methods) { exit 3 }",
        "  [void](Invoke-CimMethod -InputObject $methods -MethodName WmiSetBrightness "
        "-Arguments @{ Timeout = [uint32]1; Brightness = [byte]$parsed })",
        "  $state = Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightness "
        "-ErrorAction SilentlyContinue | Select-Object -First 1",
        "  if ($null -eq $state) { exit 3 }",
        "  $payload = [ordered]@{ level = [int]$state.CurrentBrightness }",
        "  Write-Output ($payload | ConvertTo-Json -Compress)",
        "} catch { exit 4 }",
        "exit 0",
        "",
    ]
)

# Neden: LockWorkStation sıfır dönerse kilit gerçekten istenmemiştir; bu başarı sayılmaz (4).
LOCK_SCRIPT = "\n".join(
    [
        "$ErrorActionPreference = 'Stop'",
        "try {",
        "  Add-Type -TypeDefinition @'",
        "using System.Runtime.InteropServices;",
        "public static class JarvisLock {",
        '  [DllImport("user32.dll")] public static extern bool LockWorkStation();',
        "}",
        "'@",
        "  if (-not [JarvisLock]::LockWorkStation()) { exit 4 }",
        "} catch { exit 4 }",
        "exit 0",
        "",
    ]
)

# Neden: SetSuspendState uyanana dek döner; doğrudan çağırsak araç bloke olur ve zaman aşımına
# düşerdi. Bu yüzden ayrı, gizli bir süreç 2 sn sonra uykuya alır ve bu betik hemen çıkar.
SLEEP_SCRIPT = "\n".join(
    [
        "$ErrorActionPreference = 'Stop'",
        "try {",
        "  $inner = 'Start-Sleep -Seconds 2; Add-Type -AssemblyName System.Windows.Forms; "
        "[System.Windows.Forms.Application]::SetSuspendState(''Suspend'', $false, $false)'",
        "  Start-Process -FilePath 'powershell.exe' -WindowStyle Hidden -ArgumentList "
        "@('-NoProfile', '-NonInteractive', '-Command', $inner)",
        "} catch { exit 4 }",
        "exit 0",
        "",
    ]
)


@dataclass(frozen=True)
class WindowInfo:
    id: int
    title: str
    process: str
    active: bool


@dataclass(frozen=True)
class SystemOutcome:
    status: Literal["ok", "sent", "unavailable", "error"]
    message: str
    windows: tuple[WindowInfo, ...] = ()
    truncated: bool = False
    level: int | None = None
    muted: bool | None = None


def _clean_line(text: str, limit: int) -> str:
    """Kontrol karakterlerini atar, tek satıra indirir, kısaltır.

    Pencere başlıkları e-posta konusu veya belge adı taşıyabilir: NUL/ESC gibi karakterler
    JSONL denetim kaydını ve terminali bozabilir, satır sonları da istem enjeksiyonuna yarar.
    """
    spaced = "".join(" " if ch in "\t\r\n" else ch for ch in text)
    cleaned = "".join(ch for ch in spaced if ord(ch) >= 32 and ord(ch) != 127)
    return " ".join(cleaned.split())[:limit]


def _parse_payload(stdout: str) -> dict[str, object] | None:
    lines = [line for line in stdout.splitlines() if line.strip()]
    if not lines:
        return None
    try:
        payload = json.loads(lines[-1])
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _is_int(value: object) -> bool:
    # bool, int'in alt sınıfıdır; `true` bir düzey/kimlik olarak kabul edilmemeli.
    return isinstance(value, int) and not isinstance(value, bool)


def _parse_windows(raw: object) -> tuple[WindowInfo, ...]:
    if not isinstance(raw, list):
        return ()
    windows: list[WindowInfo] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        window_id = entry.get("id")
        title = entry.get("title")
        process = entry.get("process")
        active = entry.get("active")
        if not (_is_int(window_id) and isinstance(window_id, int) and window_id > 0):
            continue
        if not isinstance(title, str) or not isinstance(process, str):
            continue
        if not isinstance(active, bool):
            continue
        clean_title = _clean_line(title, WINDOW_TITLE_LIMIT)
        if not clean_title:
            continue
        windows.append(
            WindowInfo(
                id=window_id,
                title=clean_title,
                process=_clean_line(process, PROCESS_NAME_LIMIT),
                active=active,
            )
        )
    return tuple(windows)


def _encoded_command(script: str) -> list[str]:
    # `-EncodedCommand`: betik UTF-16-LE + base64; değerler argv'de değil ortamda gider.
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-EncodedCommand",
        encoded,
    ]


class SystemControl:
    def __init__(
        self,
        *,
        runner: Runner = subprocess.run,
        platform: str | None = None,
        timeout: float = 15.0,
    ) -> None:
        self._runner = runner
        self._platform = sys.platform if platform is None else platform
        self._timeout = timeout

    def _available(self) -> bool:
        return self._platform == "win32"

    @staticmethod
    def _unavailable() -> SystemOutcome:
        return SystemOutcome("unavailable", _UNAVAILABLE_MESSAGE)

    def _run(
        self, command: list[str], env_extra: dict[str, str], label: str
    ) -> subprocess.CompletedProcess[str] | SystemOutcome:
        env = {**child_environment(), **env_extra}
        try:
            return self._runner(
                command,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self._timeout,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired:
            return SystemOutcome("error", f"{label} zaman aşımına uğradı.")
        except (OSError, ValueError) as error:
            # İstisna metni yol/ortam ayrıntısı taşıyabilir; yalnız tür adı yazılır.
            return SystemOutcome("error", f"{label} başlatılamadı ({type(error).__name__}).")

    def _run_script(
        self, script: str, env_extra: dict[str, str], label: str
    ) -> subprocess.CompletedProcess[str] | SystemOutcome:
        return self._run(_encoded_command(script), env_extra, label)

    def list_windows(self) -> SystemOutcome:
        if not self._available():
            return self._unavailable()
        completed = self._run_script(WINDOW_LIST_SCRIPT, {}, "Pencere listesi")
        if isinstance(completed, SystemOutcome):
            return completed
        # stderr bilerek mesaja alınmaz: pencere başlığı veya yol bilgisi taşıyabilir.
        if completed.returncode != 0:
            return SystemOutcome(
                "error", f"Pencereler listelenemedi (çıkış kodu {completed.returncode})."
            )
        payload = _parse_payload(completed.stdout or "")
        if payload is None or not isinstance(payload.get("windows"), list):
            return SystemOutcome("error", "Pencere listesi çıktısı anlaşılamadı.")
        windows = _parse_windows(payload["windows"])
        truncated = payload.get("truncated") is True
        if len(windows) > WINDOW_LIMIT:
            windows = windows[:WINDOW_LIMIT]
            truncated = True
        if not windows:
            return SystemOutcome("ok", "Açık pencere bulunamadı.", truncated=truncated)
        message = f"{len(windows)} pencere bulundu."
        if truncated:
            message = f"{len(windows)} pencere gösteriliyor (liste kısaltıldı)."
        return SystemOutcome("ok", message, windows=windows, truncated=truncated)

    def control_window(self, window_id: int, action: WindowAction) -> SystemOutcome:
        if not _is_int(window_id) or window_id <= 0:
            raise ValueError(f"Geçersiz pencere kimliği: {window_id!r}")
        if action not in _WINDOW_ACTIONS:
            raise ValueError(f"Geçersiz pencere işlemi: {action!r}")
        if not self._available():
            return self._unavailable()
        env = {"JARVIS_WINDOW_ID": str(window_id), "JARVIS_WINDOW_ACTION": action}
        completed = self._run_script(WINDOW_CONTROL_SCRIPT, env, "Pencere işlemi")
        if isinstance(completed, SystemOutcome):
            return completed
        code = completed.returncode
        if code == 5:
            return SystemOutcome("error", "Pencere bulunamadı.")
        if code == 6:
            return SystemOutcome("error", "Windows odağı engelledi; pencere öne getirilemedi.")
        if code == 7:
            return SystemOutcome("error", "Pencere işlemi uygulandı ama sonuç doğrulanamadı.")
        if code != 0:
            return SystemOutcome("error", f"Pencere işlemi başarısız (çıkış kodu {code}).")
        payload = _parse_payload(completed.stdout or "")
        if payload is None or payload.get("ok") is not True:
            return SystemOutcome("error", "Pencere işlemi çıktısı anlaşılamadı.")
        return SystemOutcome("ok", "Pencere işlemi tamamlandı ve doğrulandı.")

    def set_volume(self, level: int | None, muted: bool | None) -> SystemOutcome:
        if level is None and muted is None:
            raise ValueError("Ses için düzey ya da sessiz değeri verilmeli.")
        if level is not None and (not _is_int(level) or not 0 <= level <= 100):
            raise ValueError(f"Geçersiz ses düzeyi: {level!r}")
        if muted is not None and not isinstance(muted, bool):
            raise ValueError(f"Geçersiz sessiz değeri: {muted!r}")
        if not self._available():
            return self._unavailable()
        env = {
            "JARVIS_VOLUME_LEVEL": "" if level is None else str(level),
            "JARVIS_VOLUME_MUTE": "" if muted is None else ("1" if muted else "0"),
        }
        completed = self._run_script(VOLUME_SCRIPT, env, "Ses ayarı")
        if isinstance(completed, SystemOutcome):
            return completed
        code = completed.returncode
        if code == 3:
            return SystemOutcome("unavailable", "Ses aygıtı bulunamadı.")
        if code != 0:
            return SystemOutcome("error", f"Ses ayarlanamadı (çıkış kodu {code}).")
        payload = _parse_payload(completed.stdout or "")
        read_level = None if payload is None else payload.get("level")
        read_muted = None if payload is None else payload.get("muted")
        if (
            not _is_int(read_level)
            or not isinstance(read_level, int)
            or not isinstance(read_muted, bool)
            or not 0 <= read_level <= 100
        ):
            return SystemOutcome("error", "Ses ayarı çıktısı anlaşılamadı.")
        if level is not None and abs(read_level - level) > 1:
            return SystemOutcome(
                "error",
                f"Ses düzeyi doğrulanamadı: istenen {level}, okunan {read_level}.",
                level=read_level,
                muted=read_muted,
            )
        if muted is not None and read_muted != muted:
            return SystemOutcome(
                "error",
                "Sessiz durumu doğrulanamadı.",
                level=read_level,
                muted=read_muted,
            )
        state = "sessiz" if read_muted else "açık"
        return SystemOutcome(
            "ok",
            f"Ses düzeyi %{read_level}, ses {state}.",
            level=read_level,
            muted=read_muted,
        )

    def set_brightness(self, level: int) -> SystemOutcome:
        if not _is_int(level) or not 0 <= level <= 100:
            raise ValueError(f"Geçersiz parlaklık düzeyi: {level!r}")
        if not self._available():
            return self._unavailable()
        env = {"JARVIS_BRIGHTNESS_LEVEL": str(level)}
        completed = self._run_script(BRIGHTNESS_SCRIPT, env, "Parlaklık ayarı")
        if isinstance(completed, SystemOutcome):
            return completed
        code = completed.returncode
        if code == 3:
            return SystemOutcome("unavailable", "Bu ekran parlaklık ayarını desteklemiyor.")
        if code != 0:
            return SystemOutcome("error", f"Parlaklık ayarlanamadı (çıkış kodu {code}).")
        payload = _parse_payload(completed.stdout or "")
        read_level = None if payload is None else payload.get("level")
        if not _is_int(read_level) or not isinstance(read_level, int):
            return SystemOutcome("error", "Parlaklık ayarı çıktısı anlaşılamadı.")
        if abs(read_level - level) > 1:
            return SystemOutcome(
                "error",
                f"Parlaklık doğrulanamadı: istenen {level}, okunan {read_level}.",
                level=read_level,
            )
        return SystemOutcome("ok", f"Parlaklık %{read_level}.", level=read_level)

    def power(self, action: PowerAction, *, delay_seconds: int = 60) -> SystemOutcome:
        if action not in _POWER_ACTIONS:
            raise ValueError(f"Geçersiz güç işlemi: {action!r}")
        if action in ("shutdown", "restart") and (
            not _is_int(delay_seconds)
            or not MIN_SHUTDOWN_DELAY <= delay_seconds <= MAX_SHUTDOWN_DELAY
        ):
            raise ValueError(
                f"Gecikme {MIN_SHUTDOWN_DELAY}-{MAX_SHUTDOWN_DELAY} saniye olmalı: "
                f"{delay_seconds!r}"
            )
        if not self._available():
            return self._unavailable()
        if action == "lock":
            return self._lock_or_sleep(LOCK_SCRIPT, "Kilitleme", "Kilitleme komutu gönderildi.")
        if action == "sleep":
            return self._lock_or_sleep(SLEEP_SCRIPT, "Uyku", "Uyku komutu gönderildi.")
        if action == "cancel_shutdown":
            return self._cancel_shutdown()
        return self._schedule_shutdown(action, delay_seconds)

    def _lock_or_sleep(self, script: str, label: str, sent_message: str) -> SystemOutcome:
        completed = self._run_script(script, {}, label)
        if isinstance(completed, SystemOutcome):
            return completed
        if completed.returncode != 0:
            return SystemOutcome(
                "error", f"{label} komutu başarısız (çıkış kodu {completed.returncode})."
            )
        # Komutun verildiği bilinir; gerçekleştiği (kilit ekranı, uyku) doğrulanmaz.
        return SystemOutcome("sent", sent_message)

    def _schedule_shutdown(self, action: str, delay_seconds: int) -> SystemOutcome:
        restart = action == "restart"
        template = _RESTART_COMMENT if restart else _SHUTDOWN_COMMENT
        command = [
            "shutdown.exe",
            "/r" if restart else "/s",
            "/t",
            str(delay_seconds),
            "/c",
            template.format(delay=delay_seconds),
        ]
        completed = self._run(command, {}, "Kapatma komutu")
        if isinstance(completed, SystemOutcome):
            return completed
        code = completed.returncode
        if code == 1190:
            return SystemOutcome("error", "Zaten planlanmış bir kapatma var.")
        if code != 0:
            return SystemOutcome("error", f"Kapatma komutu başarısız (çıkış kodu {code}).")
        what = "Yeniden başlatma" if restart else "Kapatma"
        return SystemOutcome(
            "sent",
            f"{what} komutu gönderildi: {delay_seconds} saniye sonra gerçekleşecek. "
            "İptal etmek için kapatmayı iptal edebilirsiniz.",
        )

    def _cancel_shutdown(self) -> SystemOutcome:
        completed = self._run(["shutdown.exe", "/a"], {}, "Kapatma iptali")
        if isinstance(completed, SystemOutcome):
            return completed
        code = completed.returncode
        if code == 1116:
            return SystemOutcome("error", "İptal edilecek planlanmış kapatma yok.")
        if code != 0:
            return SystemOutcome("error", f"Kapatma iptal edilemedi (çıkış kodu {code}).")
        return SystemOutcome("ok", "Planlanan kapatma iptal edildi.")
