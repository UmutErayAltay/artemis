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
import sys
from ctypes import wintypes
from typing import Any

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
_MONITOR_PROC: Any
if sys.platform == "win32":
    _MONITOR_PROC = ctypes.WINFUNCTYPE(
        wintypes.BOOL,
        ctypes.c_void_p,  # HMONITOR
        ctypes.c_void_p,  # HDC
        ctypes.POINTER(wintypes.RECT),
        wintypes.LPARAM,
    )
else:
    _MONITOR_PROC = ctypes.CFUNCTYPE(
        wintypes.BOOL,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.RECT),
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
    user32.WindowFromPoint.argtypes = [wintypes.POINT]
    user32.WindowFromPoint.restype = vp
    user32.GetAncestor.argtypes = [vp, wintypes.UINT]
    user32.GetAncestor.restype = vp
    user32.GetWindowThreadProcessId.argtypes = [vp, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
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


def process_at(x: int, y: int) -> tuple[str, int]:
    """Fiziksel (x, y) noktasındaki üst düzey pencerenin (exe yolu, pid).

    Tıklama ön plandaki pencereye değil o noktadaki pencereye gider: arkadaki bir terminal
    ya da Jarvis paneli ön plan denetiminden kaçmasın diye nokta ayrıca denetlenir.
    """
    _require_windows()
    user32 = _libs()[0]
    hwnd = user32.WindowFromPoint(wintypes.POINT(x, y))
    return _window_process(user32.GetAncestor(hwnd, _GA_ROOT) if hwnd else None)


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
    events = [_move_event(x, y)]
    for _ in range(2 if double else 1):
        events += [_mouse(0, 0, 0, down), _mouse(0, 0, 0, up)]
    _send(events)


def scroll(x: int, y: int, notches: int) -> None:
    """Pozitif = aşağı (tekerlek deltası negatif), WHEEL_DELTA = 120."""
    _require_windows()
    _send([_move_event(x, y), _mouse(0, 0, -_WHEEL_DELTA * notches, _MOUSEEVENTF_WHEEL)])


def type_text(text: str) -> None:
    """KEYEVENTF_UNICODE ile yazar: klavye düzenine bağlı değildir (ğüşıöç çalışır)."""
    _require_windows()
    events: list[_INPUT] = []
    for unit in _unicode_units(text):
        events.append(_key(0, unit, _KEYEVENTF_UNICODE))
        events.append(_key(0, unit, _KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP))
    if events:
        _send(events)


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
