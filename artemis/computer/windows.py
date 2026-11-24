"""Windows arka ucu (ADR 0039, dalga 4): çoklu ekran görüntüsü, fare/klavye, ön plan süreci.

Fare ve ekran koordinatları sanal masaüstünün fiziksel pikselidir; bu nedenle çoklu
ekranlarda monitör köşesi de hesaba katılır.

Yalnız ctypes kullanılır; pywin32/pyautogui/mss/PIL bağımlılığı eklenmez. Her Windows
çağrısı `sys.platform == "win32"` koruması arkasındadır (mypy strict her platformda
geçsin diye) ve Windows dışında işlevler RuntimeError verir. Saf yardımcılar
(`_normalize`, `_order_monitors`, `_key_inputs`, `_unicode_units`, `_vk`) platformdan
bağımsızdır, bu yüzden testler fareye ve klavyeye hiç dokunmadan onları doğrular.
"""

from __future__ import annotations

import contextlib
import ctypes
import functools
import logging
import sys
import time
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

_NOT_WINDOWS = "Bilgisayar kullanımı yalnız Windows'ta çalışır."

_PER_MONITOR_AWARE_V2 = -4
_SM_XVIRTUALSCREEN = 76
_SM_YVIRTUALSCREEN = 77
_SM_CXVIRTUALSCREEN = 78
_SM_CYVIRTUALSCREEN = 79
_SRCCOPY = 0x00CC0020
_CAPTUREBLT = 0x40000000
_BI_RGB = 0
_DIB_RGB_COLORS = 0
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_GA_ROOT = 2
_MONITORINFOF_PRIMARY = 1

_INPUT_MOUSE = 0
_INPUT_KEYBOARD = 1
_MOUSEEVENTF_MOVE = 0x0001
_MOUSEEVENTF_ABSOLUTE = 0x8000
_MOUSEEVENTF_VIRTUALDESK = 0x4000
_MOUSEEVENTF_WHEEL = 0x0800
_BUTTON_FLAGS = {  # düğme -> (aşağı, yukarı)
    "left": (0x0002, 0x0004),
    "right": (0x0008, 0x0010),
    "middle": (0x0020, 0x0040),
}
_KEYEVENTF_EXTENDEDKEY = 0x0001
_KEYEVENTF_KEYUP = 0x0002
_KEYEVENTF_UNICODE = 0x0004
_WHEEL_DELTA = 120

_MODIFIERS = ("ctrl", "alt", "shift", "win")
_VK: dict[str, int] = {
    "shift": 0x10, "ctrl": 0x11, "alt": 0x12, "win": 0x5B,
    "enter": 0x0D, "escape": 0x1B, "tab": 0x09, "backspace": 0x08,
    "delete": 0x2E, "space": 0x20, "up": 0x26, "down": 0x28, "left": 0x25,
    "right": 0x27, "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "insert": 0x2D,
    "media_play_pause": 0xB3, "media_stop": 0xB2, "media_next": 0xB0, "media_prev": 0xB1,
    "volume_mute": 0xAD, "volume_down": 0xAE, "volume_up": 0xAF,
}  # fmt: skip
_VK.update({f"f{n}": 0x70 + n - 1 for n in range(1, 25)})
_VK.update({chr(c): c - 32 for c in range(ord("a"), ord("z") + 1)})  # 'a' -> 0x41
_VK.update({str(d): 0x30 + d for d in range(10)})
# Bu tuşlar genişletilmiş sayılır; bayrak yoksa sayısal tuş takımı olarak yorumlanır.
_EXTENDED = frozenset(
    _VK[n] for n in ("up", "down", "left", "right", "home", "end", "pageup", "pagedown",
                     "insert", "delete")
)  # fmt: skip

_ULONG_PTR = ctypes.c_size_t  # işaretçi genişliğinde: 64-bit'te INPUT 40 bayt olur


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", _ULONG_PTR),
    ]  # fmt: skip


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD), ("dwExtraInfo", _ULONG_PTR),
    ]  # fmt: skip


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD)]  # fmt: skip


class _INPUTUNION(ctypes.Union):
    # Birleşimin en büyük üyesi MOUSEINPUT'tur; HARDWAREINPUT boyutu doğru çıksın diye şart.
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD),
    ]  # fmt: skip


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


# Windows'ta stdcall çağrı düzeni şarttır; diğer platformlarda yalnız içe aktarım güvenli kalsın.
_MONITOR_PROC: Any = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)(
    wintypes.BOOL,
    ctypes.c_void_p,  # HMONITOR
    ctypes.c_void_p,  # HDC
    ctypes.POINTER(wintypes.RECT),
    wintypes.LPARAM,
)
_ENUM_WINDOWS_PROC: Any = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)(
    wintypes.BOOL,
    ctypes.c_void_p,  # HWND
    wintypes.LPARAM,
)


# --- Saf yardımcılar (platformdan bağımsız, testli) ---------------------------------


def _normalize(
    x: int,
    y: int,
    origin_x: int,
    origin_y: int,
    width: int,
    height: int,
) -> tuple[int, int]:
    """Piksel koordinatını SendInput'un 0..65535 mutlak aralığına çevirir.

    Sanal masaüstünün sol-üst köşesi sıfır olmayabilir; bu yüzden önce monitör köşesi
    çıkarılır. `width - 1` paydası son pikseli tam 65535'e düşürür.
    """
    nx = round((x - origin_x) * 65535 / max(width - 1, 1))
    ny = round((y - origin_y) * 65535 / max(height - 1, 1))
    return min(max(nx, 0), 65535), min(max(ny, 0), 65535)


def _order_monitors(
    items: list[tuple[int, int, int, int, bool]],
) -> list[tuple[int, int, int, int]]:
    """Ana monitörü öne alır; diğerlerini masaüstü konumuna göre kararlı sıralar."""

    ordered = sorted(items, key=lambda item: (not item[4], item[0], item[1]))
    return [(left, top, width, height) for left, top, width, height, _ in ordered]


def _vk(name: str) -> int:
    try:
        return _VK[name]
    except KeyError:
        raise ValueError(f"Bilinmeyen tuş adı: {name!r}") from None


def _key_inputs(keys: tuple[str, ...]) -> list[tuple[int, bool]]:
    """(vk, key_up) sırası: değiştiriciler sırayla basılır, son tuş basılıp bırakılır,
    değiştiriciler ters sırayla bırakılır (ör. ctrl+shift+t)."""
    if not keys:
        raise ValueError("Boş tuş birleşimi.")
    codes = [_vk(k) for k in keys]
    *mods, main = codes
    return [(c, False) for c in mods] + [(main, False), (main, True)] + [
        (c, True) for c in reversed(mods)
    ]


def _unicode_units(text: str) -> list[int]:
    """Metni UTF-16 kod birimlerine böler; emoji gibi çiftler iki birim (vekil çifti) olur,
    KEYEVENTF_UNICODE her birimi ayrı ister."""
    raw = text.encode("utf-16-le")
    return [int.from_bytes(raw[i : i + 2], "little") for i in range(0, len(raw), 2)]


# --- Windows çağrıları -----------------------------------------------------------------


def _require_windows() -> None:
    if sys.platform != "win32":
        raise RuntimeError(_NOT_WINDOWS)


@functools.lru_cache(maxsize=1)
def _libs() -> tuple[Any, Any, Any]:
    """user32/gdi32/kernel32 (+ dpi için user32 yeniden kullanılır); argtypes 64-bit'te
    tutamaçların kesilmemesi için açıkça verilir."""
    if sys.platform != "win32":
        raise RuntimeError(_NOT_WINDOWS)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    vp = ctypes.c_void_p
    user32.GetDC.argtypes = [vp]
    user32.GetDC.restype = vp
    user32.ReleaseDC.argtypes = [vp, vp]
    user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    user32.GetSystemMetrics.restype = ctypes.c_int
    user32.EnumDisplayMonitors.argtypes = [vp, vp, _MONITOR_PROC, wintypes.LPARAM]
    user32.EnumDisplayMonitors.restype = wintypes.BOOL
    user32.GetMonitorInfoW.argtypes = [vp, ctypes.POINTER(_MONITORINFO)]
    user32.GetMonitorInfoW.restype = wintypes.BOOL
    user32.GetForegroundWindow.restype = vp
    user32.GetWindowRect.argtypes = [vp, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.WindowFromPoint.argtypes = [wintypes.POINT]
    user32.WindowFromPoint.restype = vp
    user32.GetAncestor.argtypes = [vp, wintypes.UINT]
    user32.GetAncestor.restype = vp
    user32.GetWindowThreadProcessId.argtypes = [vp, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.EnumWindows.argtypes = [_ENUM_WINDOWS_PROC, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [vp]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.IsIconic.argtypes = [vp]
    user32.IsIconic.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = [vp]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [vp, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]
    user32.SendInput.restype = wintypes.UINT
    gdi32.CreateCompatibleDC.argtypes = [vp]
    gdi32.CreateCompatibleDC.restype = vp
    gdi32.CreateCompatibleBitmap.argtypes = [vp, ctypes.c_int, ctypes.c_int]
    gdi32.CreateCompatibleBitmap.restype = vp
    gdi32.SelectObject.argtypes = [vp, vp]
    gdi32.SelectObject.restype = vp
    gdi32.DeleteObject.argtypes = [vp]
    gdi32.DeleteDC.argtypes = [vp]
    gdi32.BitBlt.argtypes = [vp, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                             vp, ctypes.c_int, ctypes.c_int, wintypes.DWORD]  # fmt: skip
    gdi32.BitBlt.restype = wintypes.BOOL
    gdi32.GetDIBits.argtypes = [vp, vp, wintypes.UINT, wintypes.UINT, vp,
                                ctypes.POINTER(_BITMAPINFOHEADER), wintypes.UINT]  # fmt: skip
    gdi32.GetDIBits.restype = ctypes.c_int
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = vp
    kernel32.CloseHandle.argtypes = [vp]
    kernel32.QueryFullProcessImageNameW.argtypes = [
        vp, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
    ]  # fmt: skip
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    return user32, gdi32, kernel32


def set_dpi_aware() -> None:
    """Süreci DPI farkında yapar: aksi hâlde GetSystemMetrics/BitBlt sanal (ölçeklenmiş)
    piksel verir ve modelin gördüğü koordinat tıklananla kayar. Tekrar çağrı zararsızdır;
    "zaten ayarlı" hatası bilerek yok sayılır."""
    _require_windows()
    user32 = _libs()[0]
    # Eski Windows'ta işlev yok (AttributeError); 0 dönerse da (yok ya da "zaten ayarlı")
    # SetProcessDPIAware denenir, ikincisi zararsızdır.
    try:
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(_PER_MONITOR_AWARE_V2)):
            return
    except AttributeError:
        pass
    user32.SetProcessDPIAware()


def monitors() -> list[tuple[int, int, int, int]]:
    """Her monitörü (sol, üst, genişlik, yükseklik) fiziksel sanal masaüstü pikseliyle verir."""
    _require_windows()
    set_dpi_aware()
    user32 = _libs()[0]
    items: list[tuple[int, int, int, int, bool]] = []

    def collect(monitor: Any, _hdc: Any, _rect: Any, _data: Any) -> bool:
        info = _MONITORINFO()
        info.cbSize = ctypes.sizeof(_MONITORINFO)
        # Tek monitörün bilgisi okunamazsa numaralandırmayı bitirmeyiz: o ekran atlanır, geri
        # kalanlar kullanılır (False dönersek EnumDisplayMonitors komple hata verirdi).
        if not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            return True
        rect = info.rcMonitor
        items.append(
            (
                int(rect.left),
                int(rect.top),
                int(rect.right - rect.left),
                int(rect.bottom - rect.top),
                bool(info.dwFlags & _MONITORINFOF_PRIMARY),
            )
        )
        return True

    callback = _MONITOR_PROC(collect)
    if not user32.EnumDisplayMonitors(None, None, callback, 0):
        raise OSError("Monitörler okunamadı (EnumDisplayMonitors).")
    if not items:
        raise OSError("Monitör bulunamadı.")
    return _order_monitors(items)


def capture(left: int, top: int, width: int, height: int) -> tuple[bytes, int, int]:
    """Sanal masaüstündeki verilen dikdörtgeni (BGRA, üstten alta) döndürür; diske yazılmaz
    (ADR 0039/3)."""
    _require_windows()
    if width <= 0 or height <= 0:
        raise OSError("Ekran genişliği ve yüksekliği pozitif olmalıdır.")
    set_dpi_aware()
    user32, gdi32, _ = _libs()
    screen_dc = mem_dc = bitmap = old = None
    try:
        screen_dc = user32.GetDC(None)
        if not screen_dc:
            raise OSError("Ekran cihaz bağlamı (GetDC) alınamadı.")
        mem_dc = gdi32.CreateCompatibleDC(screen_dc)
        if not mem_dc:
            raise OSError("Bellek cihaz bağlamı (CreateCompatibleDC) oluşturulamadı.")
        bitmap = gdi32.CreateCompatibleBitmap(screen_dc, width, height)
        if not bitmap:
            raise OSError("Ekran bitmap'i (CreateCompatibleBitmap) oluşturulamadı.")
        old = gdi32.SelectObject(mem_dc, bitmap)
        if not old:
            raise OSError("Bitmap bellek bağlamına seçilemedi (SelectObject).")
        # CAPTUREBLT: katmanlı (layered) pencereler de görüntüye girsin.
        if not gdi32.BitBlt(
            mem_dc, 0, 0, width, height, screen_dc, left, top, _SRCCOPY | _CAPTUREBLT
        ):
            raise OSError("Ekran kopyalanamadı (BitBlt başarısız).")
        header = _BITMAPINFOHEADER()
        header.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        header.biWidth = width
        header.biHeight = -height  # negatif: üstten alta satır sırası
        header.biPlanes = 1
        header.biBitCount = 32
        header.biCompression = _BI_RGB
        buffer = ctypes.create_string_buffer(width * height * 4)
        # Bitmap, GetDIBits sırasında hiçbir DC'ye seçili olmamalıdır.
        gdi32.SelectObject(mem_dc, old)
        old = None
        lines = gdi32.GetDIBits(
            mem_dc, bitmap, 0, height, buffer, ctypes.byref(header), _DIB_RGB_COLORS
        )
        if lines != height:
            raise OSError("Ekran pikselleri okunamadı (GetDIBits başarısız).")
        return bytes(buffer.raw), width, height
    finally:
        if old:
            gdi32.SelectObject(mem_dc, old)
        if bitmap:
            gdi32.DeleteObject(bitmap)
        if mem_dc:
            gdi32.DeleteDC(mem_dc)
        if screen_dc:
            user32.ReleaseDC(None, screen_dc)


def foreground_process() -> tuple[str, int]:
    """Ön plandaki pencerenin (exe tam yolu, pid). Yol okunamazsa ("", pid veya 0) döner;
    çağıran boş yolu engelli sayar (okunamayan pencereye güvenilmez)."""
    _require_windows()
    return _window_process(_libs()[0].GetForegroundWindow())


def foreground_window() -> tuple[int, tuple[int, int, int, int]]:
    """Ön plandaki pencerenin (hwnd, (sol, üst, sağ, alt)); yakalama ile girdi arasında
    pencerenin değişip değişmediğini anlamak için (session._stale_reason)."""
    _require_windows()
    user32 = _libs()[0]
    hwnd = user32.GetForegroundWindow()
    rect = wintypes.RECT()
    if not hwnd or not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise OSError("Ön plandaki pencere okunamadı (GetForegroundWindow/GetWindowRect).")
    return int(hwnd), (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))


def window_rect(hwnd: int) -> tuple[int, int, int, int]:
    """Verilen pencere kimliğinin güncel (sol, üst, sağ, alt) dikdörtgenini döndürür;
    onay sırasında taşınma/yeniden boyutlanma denetimi için (session._stale_reason)."""
    _require_windows()
    user32 = _libs()[0]
    rect = wintypes.RECT()
    if hwnd <= 0 or not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise OSError("Pencere dikdörtgeni okunamadı (GetWindowRect).")
    return int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)


def process_at(x: int, y: int) -> tuple[str, int]:
    """Fiziksel (x, y) noktasındaki üst düzey pencerenin (exe yolu, pid).

    Tıklama ön plandaki pencereye değil o noktadaki pencereye gider: arkadaki bir terminal
    ya da Artemis paneli ön plan denetiminden kaçmasın diye nokta ayrıca denetlenir.
    """
    _require_windows()
    user32 = _libs()[0]
    hwnd = user32.WindowFromPoint(wintypes.POINT(x, y))
    return _window_process(user32.GetAncestor(hwnd, _GA_ROOT) if hwnd else None)


def window_at(x: int, y: int) -> int:
    """Fiziksel (x, y) noktasındaki üst düzey pencerenin kimliği (hwnd).

    Tıklama onayı bu pencereye bağlanır (ADR 0056): aynı süreçteki başka bir pencere ya da
    araya giren bir pencere onaylanan hedef sayılmaz. Okunamazsa OSError.
    """
    _require_windows()
    user32 = _libs()[0]
    hwnd = user32.WindowFromPoint(wintypes.POINT(x, y))
    root = user32.GetAncestor(hwnd, _GA_ROOT) if hwnd else None
    if not root:
        raise OSError("Noktadaki pencere okunamadı (WindowFromPoint).")
    return int(root)


def _window_process(hwnd: Any) -> tuple[str, int]:
    user32, _, kernel32 = _libs()
    if not hwnd:
        return "", 0
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return "", 0
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not handle:
        return "", int(pid.value)
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return "", int(pid.value)
        return buf.value, int(pid.value)
    finally:
        kernel32.CloseHandle(handle)


def _send(inputs: list[_INPUT]) -> None:
    """SendInput; eksik enjekte edilirse (ör. UIPI yüksek yetkili pencereyi engeller) hata."""
    user32 = _libs()[0]
    array = (_INPUT * len(inputs))(*inputs)
    sent = int(user32.SendInput(len(inputs), array, ctypes.sizeof(_INPUT)))
    if sent != len(inputs):
        raise OSError(
            f"Girdi enjekte edilemedi ({sent}/{len(inputs)}); pencere yönetici yetkisinde "
            "ya da girdi engellenmiş olabilir."
        )


def _mouse(dx: int, dy: int, data: int, flags: int) -> _INPUT:
    item = _INPUT(type=_INPUT_MOUSE)
    item.mi = _MOUSEINPUT(dx, dy, data & 0xFFFFFFFF, flags, 0, 0)
    return item


def _key(vk: int, scan: int, flags: int) -> _INPUT:
    item = _INPUT(type=_INPUT_KEYBOARD)
    item.ki = _KEYBDINPUT(vk, scan, flags, 0, 0)
    return item


def _move_event(x: int, y: int) -> _INPUT:
    set_dpi_aware()
    user32 = _libs()[0]
    origin_x = int(user32.GetSystemMetrics(_SM_XVIRTUALSCREEN))
    origin_y = int(user32.GetSystemMetrics(_SM_YVIRTUALSCREEN))
    width = int(user32.GetSystemMetrics(_SM_CXVIRTUALSCREEN))
    height = int(user32.GetSystemMetrics(_SM_CYVIRTUALSCREEN))
    nx, ny = _normalize(x, y, origin_x, origin_y, width, height)
    flags = _MOUSEEVENTF_VIRTUALDESK | _MOUSEEVENTF_MOVE | _MOUSEEVENTF_ABSOLUTE
    return _mouse(nx, ny, 0, flags)


def click(x: int, y: int, button: str = "left", double: bool = False) -> None:
    _require_windows()
    if button not in _BUTTON_FLAGS:
        raise ValueError(f"Bilinmeyen fare düğmesi: {button!r}")
    down, up = _BUTTON_FLAGS[button]
    _send([_move_event(x, y)])
    time.sleep(0.01)
    events: list[_INPUT] = []
    for _ in range(2 if double else 1):
        events += [_mouse(0, 0, 0, down), _mouse(0, 0, 0, up)]
    _send(events)


def hover(x: int, y: int) -> None:
    """Fare imlecini tıklamadan belirtilen piksele taşır (otomatik gizlenen kontroller için)."""
    _require_windows()
    _send([_move_event(x, y)])


def scroll(x: int, y: int, notches: int) -> None:
    """Pozitif = aşağı (tekerlek deltası negatif), WHEEL_DELTA = 120."""
    _require_windows()
    _send([_move_event(x, y)])
    time.sleep(0.01)
    _send([_mouse(0, 0, -_WHEEL_DELTA * notches, _MOUSEEVENTF_WHEEL)])


# Windows 11 Not Defteri (RichEditD2DPT) toplu/hızlı VK_PACKET girdisini bozuyor: 2026-11-05
# ölçümü, toplu gönderim 0/6 ("merhaba dünya ttttttttttt"), 15 ms 5/6, 30 ms 10/10; Tk toplu 8/8.
# ponytail: sabit hız, 500 karakter ≈ 15 sn; yavaş kalırsa uygulamaya göre hız ya da yapıştırma.
_TYPE_DELAY_SECONDS = 0.03


def type_text(text: str, should_continue: Callable[[], bool] | None = None) -> int:
    """KEYEVENTF_UNICODE ile yazar: klavye düzenine bağlı değildir (ğüşıöç çalışır).

    Her kod noktasından önce `should_continue` sorulur (iptal, mutlak süre, hedef pencere);
    False dönerse kalan metin gönderilmez (ADR 0056). Bir kod noktasının bütün birimleri (vekil
    çifti dahil) basma/bırakma olaylarıyla tek SendInput'tadır: yarım karakter ya da basılı
    kalan tuş olmaz. Gönderilen kod noktası sayısı döner; gönderilen geri alınmaz.
    """
    _require_windows()
    sent = 0
    for char in text:
        if should_continue is not None and not should_continue():
            break
        up = _KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP
        events: list[_INPUT] = []
        for unit in _unicode_units(char):
            events += [_key(0, unit, _KEYEVENTF_UNICODE), _key(0, unit, up)]
        _send(events)
        sent += 1
        time.sleep(_TYPE_DELAY_SECONDS)
    return sent


def modifiers_down() -> bool:
    """Ctrl/Alt/Shift/Win'den biri hâlâ basılı mı. Kısayolun kendi tuşları bırakılmadan gönderilen
    Ctrl+C hedefte Ctrl+Alt+C gibi okunur; çağıran bırakılmasını sınırlı süre bekler."""
    _require_windows()
    user32 = _libs()[0]
    user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    user32.GetAsyncKeyState.restype = ctypes.c_short
    return any(user32.GetAsyncKeyState(vk) & 0x8000 for vk in (0x10, 0x11, 0x12, 0x5B, 0x5C))


def press_keys(keys: tuple[str, ...]) -> None:
    """Tuş birleşimi; hata olsa bile basılı kalan tuşlar finally'de bırakılır, aksi hâlde
    Ctrl/Shift takılı kalıp kullanıcının klavyesini bozar."""
    _require_windows()
    plan = _key_inputs(keys)  # geçersiz ad, hiçbir tuşa basmadan ValueError verir
    held: list[int] = []
    try:
        for vk, is_up in plan:
            flags = (_KEYEVENTF_EXTENDEDKEY if vk in _EXTENDED else 0) | (
                _KEYEVENTF_KEYUP if is_up else 0
            )
            _send([_key(vk, 0, flags)])
            if is_up:
                held.remove(vk)
            else:
                held.append(vk)
    finally:
        for vk in reversed(held):
            flags = _KEYEVENTF_KEYUP | (_KEYEVENTF_EXTENDEDKEY if vk in _EXTENDED else 0)
            with contextlib.suppress(OSError):  # asıl hatayı gölgelemesin
                _send([_key(vk, 0, flags)])


def focus_window(hwnd: int) -> bool:
    """Pencereyi ön plana getirmeyi dener; gerçekten ön planda olduğu çağıranca ayrıca doğrulanır
    (SetForegroundWindow'un True dönmesi odağın geçtiğini garanti etmez)."""
    _require_windows()
    user32 = _libs()[0]
    user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    return bool(user32.SetForegroundWindow(hwnd))


# Odaktaki denetimin değerini UI Automation ile okur ve beklenen metinle karşılaştırır. Betik
# sabittir; beklenen metin (özel, ekran içeriği sayılır) yalnız ortam değişkeniyle çocuğa gider,
# argv'de ve logda yoktur. Yalnız `1`/`0`/`?` basılır: okunan değer süreçten çıkmaz.
_FOCUS_SCRIPT = "\n".join(
    [
        "$ErrorActionPreference = 'Stop'",
        "try {",
        "  Add-Type -AssemblyName UIAutomationClient",
        "  Add-Type -AssemblyName UIAutomationTypes",
        "  $e = [System.Windows.Automation.AutomationElement]::FocusedElement",
        # Parola alanlarının içeriği hiçbir koşulda okunmaz; sözleşme gereği bilinmiyor ('?') döner.
        "  if ($null -eq $e -or $e.Current.IsPassword) { '?'; exit 0 }",
        "  $p = $null",
        "  $vp = [System.Windows.Automation.ValuePattern]::Pattern",
        "  $tp = [System.Windows.Automation.TextPattern]::Pattern",
        "  if ($e.TryGetCurrentPattern($vp, [ref]$p)) {",
        "    $v = $p.Current.Value",
        "  } elseif ($e.TryGetCurrentPattern($tp, [ref]$p)) {",
        "    $v = $p.DocumentRange.GetText(65536)",
        "  } else { '?'; exit 0 }",
        "  $x = $env:ARTEMIS_EXPECTED_TEXT",
        "  $v = $v -replace \"`r`n\", \"`n\"; $x = $x -replace \"`r`n\", \"`n\"",
        "  if ($v.Contains($x)) { '1' } else { '0' }",
        "} catch { '?' }",
    ]
)
_FOCUS_TEXT_LIMIT = 8000  # ortam değişkeni sınırı (32 KB) altında kalmak için


def focused_value_contains(
    expected: str, timeout: float, runner: Callable[..., Any] | None = None
) -> bool | None:
    """Odaktaki alanın değeri `expected`i içeriyor mu; her başarısızlıkta None (ADR 0070)."""
    # Ortam değişkeninde NUL subprocess'te ValueError fırlatır; sözleşme gereği None döner.
    if (
        sys.platform != "win32"
        or not expected
        or len(expected) > _FOCUS_TEXT_LIMIT
        or "\x00" in expected
    ):
        return None
    import base64
    import subprocess

    from artemis.tools.platform import child_environment

    encoded = base64.b64encode(_FOCUS_SCRIPT.encode("utf-16-le")).decode("ascii")
    command = [
        "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
        "-EncodedCommand", encoded,
    ]
    env = {**child_environment(), "ARTEMIS_EXPECTED_TEXT": expected}
    try:
        completed = (runner or subprocess.run)(
            command, env=env, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    answer = (completed.stdout or "").strip()
    return {"1": True, "0": False}.get(answer) if completed.returncode == 0 else None


# Odaktaki UI Automation öğesinin yalnız adını (Name) okur; değer/içerik okunmaz, betik
# sabittir ve kullanıcı girdisi taşımaz (ADR 0078 screen kipi tavanını kapatmak için).
_FOCUS_NAME_SCRIPT = "\n".join(
    [
        "$ErrorActionPreference = 'Stop'",
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8",
        "try {",
        "  Add-Type -AssemblyName UIAutomationClient",
        "  Add-Type -AssemblyName UIAutomationTypes",
        "  $e = [System.Windows.Automation.AutomationElement]::FocusedElement",
        "  if ($null -eq $e) { exit 0 }",
        "  $n = [string]$e.Current.Name",
        "  if ([string]::IsNullOrWhiteSpace($n)) { exit 0 }",
        "  $n = ($n -replace \"[\\r\\n]+\", \" \").Trim()",
        "  if ($n.Length -gt 200) { $n = $n.Substring(0, 200) }",
        "  $n",
        "} catch { exit 4 }",
    ]
)


def focused_name(
    timeout: float, runner: Callable[..., Any] | None = None
) -> str | None:
    """Odaktaki denetimin UI Automation adını döndürür; okunamazsa veya boşsa None (ADR 0078)."""
    if sys.platform != "win32":
        return None
    import base64
    import subprocess

    from artemis.tools.platform import child_environment

    encoded = base64.b64encode(_FOCUS_NAME_SCRIPT.encode("utf-16-le")).decode("ascii")
    command = [
        "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
        "-EncodedCommand", encoded,
    ]
    try:
        completed = (runner or subprocess.run)(
            command, env=child_environment(), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    name = (completed.stdout or "").replace("\r", " ").replace("\n", " ").strip()[:200].strip()
    return name or None


@dataclass(frozen=True)
class WindowInfo:
    """Görünür üst düzey pencerenin kimliği, başlığı, süreci ve sanal masaüstü konumu."""

    hwnd: int
    title: str
    exe: str
    pid: int
    rect: tuple[int, int, int, int]  # (left, top, right, bottom)


def _sanitize_text(text: str, limit: int) -> str:
    """Kontrol karakterlerini boşluğa çevirip boşlukları tekler ve uzunluğu sınırlar."""
    clean = "".join(c if c.isprintable() else " " for c in text)
    return " ".join(clean.split())[:limit]


def visible_windows(max_items: int = 12) -> list[WindowInfo]:
    """Görünür, simge durumuna küçültülmemiş üst düzey pencereleri döndürür; hatada `[]`."""
    if sys.platform != "win32" or max_items <= 0:
        return []
    try:
        set_dpi_aware()
        user32 = _libs()[0]
    except (OSError, RuntimeError):
        return []
    windows: list[WindowInfo] = []

    def collect(hwnd: Any, _lparam: Any) -> bool:
        if len(windows) >= max_items or not hwnd:
            return True
        try:
            if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
                return True
            length = int(user32.GetWindowTextLengthW(hwnd))
            if length <= 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 2)
            if int(user32.GetWindowTextW(hwnd, buf, length + 2)) <= 0:
                return True
            title = _sanitize_text(buf.value, 80)
            if not title:
                return True
            rect = wintypes.RECT()
            if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return True
            left, top, right, bottom = (
                int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)
            )
            if right - left < 40 or bottom - top < 40:
                return True
            exe, pid = _window_process(hwnd)
            windows.append(
                WindowInfo(
                    hwnd=int(hwnd),
                    title=title,
                    exe=exe,
                    pid=pid,
                    rect=(left, top, right, bottom),
                )
            )
        except (OSError, RuntimeError, ValueError):
            return True
        return True

    try:
        callback = _ENUM_WINDOWS_PROC(collect)
        user32.EnumWindows(callback, 0)
    except (OSError, RuntimeError, ValueError):
        return windows
    return windows


@dataclass(frozen=True)
class UiElement:
    """Ön plan penceresinin tek bir UI Automation denetimi (ADR 0073)."""

    id: int  # 1-based, order of the list
    kind: str  # ControlType programmatic name without "ControlType." (Button, Edit, ...)
    name: str  # max 80 chars, whitespace collapsed; "<parola>" for password fields
    rect: tuple[int, int, int, int]  # x, y, width, height in virtual-desktop pixels
    interactive: bool  # Invoke / Value / Toggle pattern available
    focused: bool


# Ön plan penceresinin erişilebilirlik ağacını tek geçişte (CacheRequest + genişlik öncelikli)
# okur. Pencere kimliği ve sınır argv'ye değil ortama gider; ValuePattern değeri ASLA okunmaz,
# parola alanının adı `<parola>` olarak döner (ADR 0073).
# ponytail: breadth-first 200 cap may cut web page content; upgrade = interactive-first ordering.
_UIA_SCRIPT = "\n".join(
    [
        "$ErrorActionPreference = 'Stop'",
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8",
        "try {",
        # Karma DPI çoklu monitörlerde BoundingRectangle kaymasını önlemek için UIA'dan önce
        # Per-Monitor V2 DPI farkındalığı kurulur; eski Windows'ta SetProcessDPIAware'e düşülür.
        "  Add-Type -TypeDefinition @'",
        "using System;",
        "using System.Runtime.InteropServices;",
        "public static class ArtemisUiaNative {",
        '  [DllImport("user32.dll")] public static extern bool '
        "SetProcessDpiAwarenessContext(IntPtr value);",
        '  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();',
        "}",
        "'@",
        "  try { if (-not [ArtemisUiaNative]::SetProcessDpiAwarenessContext([IntPtr](-4))) "
        "{ [void][ArtemisUiaNative]::SetProcessDPIAware() } } "
        "catch { [void][ArtemisUiaNative]::SetProcessDPIAware() }",
        "  Add-Type -AssemblyName UIAutomationClient",
        "  Add-Type -AssemblyName UIAutomationTypes",
        "  $hwnd = [IntPtr][long]$env:ARTEMIS_UIA_HWND",
        "  $max = [int]$env:ARTEMIS_UIA_MAX",
        "  $ae = [System.Windows.Automation.AutomationElement]",
        "  $cr = New-Object System.Windows.Automation.CacheRequest",
        "  $cr.TreeScope = [System.Windows.Automation.TreeScope]::Subtree",
        "  $cr.TreeFilter = [System.Windows.Automation.Automation]::ControlViewCondition",
        "  foreach ($p in @(",
        "    $ae::NameProperty, $ae::ControlTypeProperty, $ae::BoundingRectangleProperty,",
        "    $ae::IsOffscreenProperty, $ae::IsPasswordProperty, $ae::HasKeyboardFocusProperty,",
        "    $ae::IsInvokePatternAvailableProperty, $ae::IsValuePatternAvailableProperty,",
        "    $ae::IsTogglePatternAvailableProperty",
        "  )) { [void]$cr.Add($p) }",
        "  $cr.Push()",
        "  try { $root = $ae::FromHandle($hwnd) } finally { [void]$cr.Pop() }",
        "  if ($null -eq $root) { '[]'; exit 0 }",
        "  $q = New-Object 'System.Collections.Generic.Queue[System.Windows.Automation."
        "AutomationElement]'",
        "  $ch = $root.CachedChildren",
        "  if ($null -ne $ch) { foreach ($c in $ch) { $q.Enqueue($c) } }",
        "  $out = New-Object System.Collections.Generic.List[object]",
        "  while ($q.Count -gt 0 -and $out.Count -lt $max) {",
        "    $el = $q.Dequeue()",
        "    $kids = $el.CachedChildren",
        "    if ($null -ne $kids) { foreach ($k in $kids) { $q.Enqueue($k) } }",
        "    $c = $el.Cached",
        "    if ($c.IsOffscreen) { continue }",
        "    $r = $c.BoundingRectangle",
        "    if ($r.IsEmpty -or [double]::IsInfinity($r.Width) -or [double]::IsInfinity($r.Height)"
        " -or $r.Width -lt 2 -or $r.Height -lt 2) { continue }",
        "    $inv = [bool]$el.GetCachedPropertyValue($ae::IsInvokePatternAvailableProperty)",
        "    $val = [bool]$el.GetCachedPropertyValue($ae::IsValuePatternAvailableProperty)",
        "    $tog = [bool]$el.GetCachedPropertyValue($ae::IsTogglePatternAvailableProperty)",
        "    $inter = $inv -or $val -or $tog",
        "    $name = if ($c.IsPassword) { '<parola>' } else { [string]$c.Name }",
        "    $name = ($name -replace '\\s+', ' ').Trim()",
        "    if (-not $name -and -not $inter) { continue }",
        "    if ($name.Length -gt 80) { $name = $name.Substring(0, 80) }",
        "    $kind = ($c.ControlType.ProgrammaticName -replace '^ControlType\\.', '')",
        "    $rect = @([int][math]::Round($r.Left), [int][math]::Round($r.Top),"
        " [int][math]::Round($r.Width), [int][math]::Round($r.Height))",
        "    $out.Add([pscustomobject]@{ k = $kind; n = $name; r = $rect;"
        " i = $inter; f = [bool]$c.HasKeyboardFocus })",
        "  }",
        "  if ($out.Count -eq 0) { '[]' } else {"
        " ConvertTo-Json -InputObject $out.ToArray() -Compress -Depth 4 }",
        "} catch { exit 4 }",
    ]
)


def ui_elements(
    hwnd: int,
    timeout: float = 5.0,
    runner: Callable[..., Any] | None = None,
    max_items: int = 200,
) -> list[UiElement]:
    """Verilen pencerenin görünür UI Automation öğelerini döndürür; hatada `[]` (ADR 0073)."""
    if sys.platform != "win32" or hwnd <= 0 or max_items <= 0:
        return []
    import base64
    import json
    import subprocess

    from artemis.tools.platform import child_environment

    encoded = base64.b64encode(_UIA_SCRIPT.encode("utf-16-le")).decode("ascii")
    command = [
        "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
        "-EncodedCommand", encoded,
    ]
    env = {
        **child_environment(),
        "ARTEMIS_UIA_HWND": str(int(hwnd)),
        "ARTEMIS_UIA_MAX": str(int(max_items)),
    }
    try:
        completed = (runner or subprocess.run)(
            command, env=env, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        logger.warning("UIA okuma başarısız (%s).", "zaman_asimi")
        return []
    if completed.returncode != 0:
        logger.warning("UIA okuma başarısız (%s).", "cikis_kodu")
        return []
    raw = (completed.stdout or "").strip()
    if not raw:
        return []
    try:
        parsed: object = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("UIA okuma başarısız (%s).", "json")
        return []
    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list):
        return []
    result: list[UiElement] = []
    for item in parsed:
        if len(result) >= max_items:
            break
        if not isinstance(item, dict):
            continue
        kind = item.get("k")
        name = item.get("n")
        rect = item.get("r")
        interactive = item.get("i")
        focused = item.get("f")
        if (
            not isinstance(kind, str)
            or not kind
            or not isinstance(name, str)
            or not isinstance(interactive, bool)
            or not isinstance(focused, bool)
            or not isinstance(rect, list)
            or len(rect) != 4
            or any(isinstance(v, bool) or not isinstance(v, int) for v in rect)
        ):
            continue
        w, h = rect[2], rect[3]
        if w < 2 or h < 2:
            continue
        # Ekrandan gelen metin kontrol karakterleri taşıyamaz: her tüketicide (istem, onay
        # penceresi, günlük) satır bölme/biçim kaçamağı olmasın diye burada sıyırılır.
        clean_name = _sanitize_text(name, 80)
        kind = _sanitize_text(kind, 40)
        if not kind:
            continue
        result.append(
            UiElement(
                id=len(result) + 1,
                kind=kind,
                name=clean_name,
                rect=(rect[0], rect[1], w, h),
                interactive=interactive,
                focused=focused,
            )
        )
    return result
