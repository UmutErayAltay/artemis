"""Windows masaüstüne erişim bu sınırda kalır; testler adaptör enjekte eder."""

import csv
import io
import ntpath
import os
import platform
import subprocess
import time
from typing import Any, Protocol, cast


class UnsupportedPlatformError(RuntimeError):
    """Desteklenmeyen sistemde başarı iddiası yerine açık sonuç üretir."""


class DesktopPlatform(Protocol):
    def open_application(self, executable: str) -> None: ...

    def open_document(self, executable: str, document: str) -> None: ...

    def open_browser(self, url: str) -> None: ...

    def close_application(self, executable: str) -> bool: ...

    def read_clipboard(self) -> str: ...

    def write_clipboard(self, text: str) -> bool: ...


def child_environment() -> dict[str, str]:
    """Çocuklara sağlayıcı/bot sırları yerine yalnız OS çalışma değişkenleri verilir."""
    allowed = {
        "SYSTEMROOT",
        "WINDIR",
        "TEMP",
        "TMP",
        "PATH",
        "PATHEXT",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "HOMEDRIVE",
        "HOMEPATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "DISPLAY",
        "WAYLAND_DISPLAY",
        "XAUTHORITY",
        "XDG_RUNTIME_DIR",
        "DBUS_SESSION_BUS_ADDRESS",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "XDG_CURRENT_DESKTOP",
        "XDG_SESSION_TYPE",
        "DESKTOP_SESSION",
        "SESSION_MANAGER",
    }
    return {key: value for key, value in os.environ.items() if key.upper() in allowed}


class WindowsDesktop:
    """Windows API'lerini yalnız çağrı anında yükler; Linux import'u güvenlidir."""

    @staticmethod
    def _require_windows() -> None:
        if platform.system() != "Windows":
            raise UnsupportedPlatformError("Bu masaüstü işlemi yalnız Windows'ta destekleniyor.")

    @staticmethod
    def _win_dll(name: str) -> Any:
        """Windows ctypes bağını yalnız platform kontrolünden sonra edinir."""
        import ctypes

        loader_name = "WinDLL"
        win_dll: Any = getattr(ctypes, loader_name)
        return win_dll(name, use_last_error=True)

    def open_application(self, executable: str) -> None:
        self._require_windows()
        if executable.startswith("startapp:"):
            app_id = executable[len("startapp:"):].split("|", 1)[0]
            command = [
                ntpath.join(os.environ.get("SYSTEMROOT", r"C:\Windows"), "explorer.exe"),
                "shell:AppsFolder\\" + app_id,
            ]
        else:
            command = [executable]
        # Popen yalnız isteğin OS tarafından alındığını gösterir; araç 'sent' döndürür.
        subprocess.Popen(
            command,
            shell=False,
            env=child_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def open_browser(self, url: str) -> None:
        """Tarayıcı isteğini gizli ortam değişkenlerini aktarmadan gönderir."""
        system = platform.system()
        if system == "Windows":
            system_root = os.environ.get("SYSTEMROOT", r"C:\Windows")
            command = [
                ntpath.join(system_root, "System32", "rundll32.exe"),
                "url.dll,FileProtocolHandler",
                url,
            ]
        elif system == "Linux":
            command = ["xdg-open", url]
        else:
            raise UnsupportedPlatformError("Tarayıcı başlatma Windows ve Linux'ta destekleniyor.")
        # Sürecin başlaması yalnız isteğin OS tarafından kabul edildiğini gösterir.
        subprocess.Popen(
            command,
            shell=False,
            env=child_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def open_document(self, executable: str, document: str) -> None:
        """Not Defteri'ne düz dosya argümanı verir; shell veya klavye otomasyonu yoktur."""
        self._require_windows()
        if (
            executable.startswith("startapp:")
            and self._application_process_name(executable).casefold() == "notepad.exe"
        ):
            executable = ntpath.join(
                os.environ.get("SYSTEMROOT", r"C:\Windows"), "System32", "notepad.exe"
            )
        if ntpath.basename(executable).casefold() != "notepad.exe":
            raise ValueError("Metin dosyası yalnız Not Defteri ile açılabilir")
        subprocess.Popen(
            [executable, document], shell=False, env=child_environment(),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    def close_application(self, executable: str) -> bool:
        self._require_windows()
        name = self._application_process_name(executable)
        if not name.lower().endswith(".exe") or any(char in name for char in '*?"\r\n'):
            raise ValueError("Kapatma için açık bir .exe dosya adı gerekli.")
        system_root = os.environ.get("SYSTEMROOT", r"C:\Windows")
        taskkill = ntpath.join(system_root, "System32", "taskkill.exe")
        subprocess.run(
            # /F zorlamalı sonlandırmadır; onay metni kaydedilmemiş işleri
            # kaybettirebilecek bu etkiyi açıkça anlatır, ancak varsayılanı nazik kapatmadır.
            [taskkill, "/IM", name],
            shell=False,
            check=False,
            capture_output=True,
            env=child_environment(),
            timeout=10,
        )
        for attempt in range(6):
            if not self._application_pids(name):
                return True
            if attempt < 5:
                time.sleep(0.2)
        return False

    def _application_pids(self, name: str) -> set[int]:
        system_root = os.environ.get("SYSTEMROOT", r"C:\Windows")
        tasklist = ntpath.join(system_root, "System32", "tasklist.exe")
        check = subprocess.run(
            [tasklist, "/FI", f"IMAGENAME eq {name}", "/FO", "CSV", "/NH"],
            shell=False,
            check=False,
            capture_output=True,
            env=child_environment(),
            timeout=10,
        )
        if check.returncode != 0:
            raise OSError("Uygulama süreçleri sorgulanamadı")
        rows = csv.reader(io.StringIO(check.stdout.decode("utf-8", errors="replace")))
        # taskkill başarısız dönebilir çünkü süreç zaten yoktur. Başarı için
        # tasklist doğrulamasında aynı ada sahip çalışan süreç kalmaması yeterlidir.
        return {
            int(row[1]) for row in rows
            if len(row) > 1 and row[0].casefold() == name.casefold()
        }

    def application_windows_open(self, executable: str) -> bool:
        """Arka plan süreçlerini görünen uygulama pencerelerinden ayırır."""
        self._require_windows()
        import ctypes
        from ctypes import wintypes

        pids = self._application_pids(self._application_process_name(executable))
        user32 = self._win_dll("user32")
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
        user32.EnumWindows.restype = wintypes.BOOL
        user32.EnumChildWindows.argtypes = [wintypes.HWND, callback_type, wintypes.LPARAM]
        user32.EnumChildWindows.restype = wintypes.BOOL
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetClassNameW.restype = ctypes.c_int
        found = False
        query_failed = False

        def inspect_window(hwnd: Any, _lparam: Any) -> bool:
            nonlocal found, query_failed
            if user32.IsWindowVisible(hwnd):
                pid = wintypes.DWORD()
                if not user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)):
                    query_failed = True
                elif pid.value in pids:
                    found = True
            return True

        child_callback = callback_type(inspect_window)

        def visit(hwnd: Any, lparam: Any) -> bool:
            # Explorer'ın masaüstü ve görev çubuğu bir klasör açma başarısı değildir.
            window_class = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, window_class, len(window_class))
            if window_class.value in {
                "Shell_TrayWnd", "Shell_SecondaryTrayWnd", "Progman", "WorkerW"
            }:
                return True
            inspect_window(hwnd, lparam)
            if user32.IsWindowVisible(hwnd):
                # Paketli uygulamalarda üst pencere ApplicationFrameHost'a aittir.
                user32.EnumChildWindows(hwnd, child_callback, 0)
            return True

        callback = callback_type(visit)
        if not user32.EnumWindows(callback, 0):
            raise OSError("Uygulama pencereleri sorgulanamadı")
        if query_failed and not found:
            raise OSError("Pencerenin uygulama kimliği doğrulanamadı")
        return found

    @staticmethod
    def _application_process_name(executable: str) -> str:
        if executable.startswith("startapp:"):
            _app_id, separator, process = executable.partition("|")
            if not separator or not process.casefold().endswith(".exe"):
                raise ValueError("Kurulu uygulamanın süreç kimliği bilinmiyor")
            return process
        name = ntpath.basename(executable)
        # Windows Hesap Makinesi başlatıcısı ayrı paket sürecini açar.
        if name.casefold() == "calc.exe":
            return "CalculatorApp.exe"
        return name

    def wait_for_application_window(self, executable: str) -> bool:
        """Başlatma isteğinden sonra pencereyi sınırlı süre gerçek OS ile doğrular."""
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if self.application_windows_open(executable):
                return True
            time.sleep(0.3)
        return False

    def read_clipboard(self) -> str:
        self._require_windows()
        import ctypes
        from ctypes import wintypes

        user32 = self._win_dll("user32")
        kernel32 = self._win_dll("kernel32")
        user32.OpenClipboard.argtypes = [wintypes.HWND]
        user32.OpenClipboard.restype = wintypes.BOOL
        user32.GetClipboardData.argtypes = [wintypes.UINT]
        user32.GetClipboardData.restype = wintypes.HANDLE
        user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
        user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
        user32.CloseClipboard.restype = wintypes.BOOL
        kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalUnlock.restype = wintypes.BOOL
        if not user32.OpenClipboard(None):
            raise OSError("Pano başka bir uygulama tarafından kullanılıyor.")
        try:
            if not user32.IsClipboardFormatAvailable(13):
                return ""
            handle = user32.GetClipboardData(13)
            pointer = kernel32.GlobalLock(handle)
            if not pointer:
                raise OSError("Pano metni okunamadı.")
            try:
                return ctypes.wstring_at(pointer)
            finally:
                kernel32.GlobalUnlock(handle)
        finally:
            if not user32.CloseClipboard():
                raise OSError("Pano erişimi kapatılamadı.")

    def write_clipboard(self, text: str) -> bool:
        self._require_windows()
        import ctypes
        from ctypes import wintypes

        if "\x00" in text:
            raise ValueError("Pano metni NUL karakteri içeremez.")
        user32 = self._win_dll("user32")
        kernel32 = self._win_dll("kernel32")
        user32.OpenClipboard.argtypes = [wintypes.HWND]
        user32.OpenClipboard.restype = wintypes.BOOL
        user32.EmptyClipboard.restype = wintypes.BOOL
        user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
        user32.SetClipboardData.restype = wintypes.HANDLE
        user32.CloseClipboard.restype = wintypes.BOOL
        user32.CreateWindowExW.argtypes = [
            wintypes.DWORD,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HWND,
            wintypes.HMENU,
            wintypes.HINSTANCE,
            ctypes.c_void_p,
        ]
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.DestroyWindow.argtypes = [wintypes.HWND]
        user32.DestroyWindow.restype = wintypes.BOOL
        kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
        kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalUnlock.restype = wintypes.BOOL
        kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalFree.restype = wintypes.HGLOBAL
        data = text.encode("utf-16-le") + b"\x00\x00"
        handle = kernel32.GlobalAlloc(0x0002, len(data))
        if not handle:
            raise OSError("Pano belleği ayrılamadı.")
        transferred = False
        window = None
        try:
            pointer = kernel32.GlobalLock(handle)
            if not pointer:
                raise OSError("Pano belleği kilitlenemedi.")
            ctypes.memmove(pointer, data, len(data))
            kernel32.GlobalUnlock(handle)
            # Yazarken NULL sahip, EmptyClipboard sonrası SetClipboardData'yı bozar.
            # Mesaj penceresi görünmezdir ve pano sahipliğini bu sürece verir.
            window = user32.CreateWindowExW(
                0, "STATIC", "Jarvis Clipboard", 0, 0, 0, 0, 0, -3, None, None, None
            )
            if not window:
                raise OSError("Pano sahipliği için pencere oluşturulamadı.")
            if not user32.OpenClipboard(window):
                raise OSError("Pano başka bir uygulama tarafından kullanılıyor.")
            try:
                if not user32.EmptyClipboard():
                    raise OSError("Pano yazmaya hazırlanamadı.")
                if not user32.SetClipboardData(13, handle):
                    raise OSError("Pano metni yazılamadı.")
                transferred = True
            finally:
                if not user32.CloseClipboard():
                    raise OSError("Pano erişimi kapatılamadı.")
        finally:
            if not transferred:
                kernel32.GlobalFree(handle)
            if window and not user32.DestroyWindow(window):
                raise OSError("Pano sahiplik penceresi kapatılamadı.")
        return self.read_clipboard() == text


def desktop_service(services: dict[str, object]) -> DesktopPlatform:
    """Varsayılan adaptör ile izole test servisi aynı sözleşmeyi uygular."""
    return cast(DesktopPlatform, services.get("desktop", WindowsDesktop()))
