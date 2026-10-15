"""Windows açılışında başlama (ADR 0030): yalnız kullanıcı eylemiyle, HKCU Run kaydı.

Bu modül bir LLM aracı DEĞİLDİR ve öyle yapılmamalıdır: model kendini kalıcı hale
getiremez. Giriş noktaları `jarvis autostart` komutu ve tepsi menüsüdür. Kayıt defteri
erişimi `RunKeyStore` arkasındadır; testler sahte depo enjekte eder, gerçek kayda dokunmaz.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "Jarvis"

AutostartState = Literal["on", "off", "stale"]


class RunKeyStore(Protocol):
    """HKCU Run anahtarındaki tek değerin okunup yazılması."""

    def read(self) -> str | None: ...

    def write(self, command: str) -> None: ...

    def delete(self) -> None: ...


class WindowsRunKeyStore:
    """Gerçek kayıt defteri; `winreg` yalnız Windows'ta ve çağrı anında yüklenir."""

    def read(self) -> str | None:
        if sys.platform != "win32":  # mypy Linux'ta kalanını erişilmez sayar
            raise OSError("winreg yalnız Windows'ta var")
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                value, kind = winreg.QueryValueEx(key, VALUE_NAME)
        except FileNotFoundError:
            return None
        if kind not in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) or not isinstance(value, str):
            return None
        return value

    def write(self, command: str) -> None:
        if sys.platform != "win32":  # mypy Linux'ta kalanını erişilmez sayar
            raise OSError("winreg yalnız Windows'ta var")
        import winreg

        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, command)

    def delete(self) -> None:
        if sys.platform != "win32":  # mypy Linux'ta kalanını erişilmez sayar
            raise OSError("winreg yalnız Windows'ta var")
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, VALUE_NAME)
        except FileNotFoundError:
            return


@dataclass(frozen=True)
class AutostartResult:
    success: bool
    state: AutostartState
    message: str


def gui_python(executable: str | None = None) -> str:
    """Konsol penceresi açmamak için `pythonw.exe` tercih edilir; yoksa mevcut yorumlayıcı."""
    current = Path(executable or sys.executable)
    windowed = current.with_name("pythonw.exe")
    return str(windowed) if windowed.is_file() else str(current)


def autostart_command(config_path: Path, *, python: str | None = None) -> str:
    """Kayda yazılacak sabit komut. `--ses` bilerek yoktur: mikrofon kendiliğinden açılmaz."""
    arguments = [
        python if python is not None else gui_python(),
        "-m",
        "jarvis",
        "desktop",
        "--arka-plan",
        "--config",
        str(config_path.resolve()),
    ]
    return subprocess.list2cmdline(arguments)


class Autostart:
    def __init__(
        self,
        config_path: Path,
        *,
        store: RunKeyStore | None = None,
        platform: str | None = None,
        python: str | None = None,
        emit: Callable[..., None] | None = None,
    ) -> None:
        self.platform = platform if platform is not None else sys.platform
        self.python = python if python is not None else gui_python()
        self.expected = autostart_command(config_path, python=self.python)
        self._store = store
        self._emit = emit

    def _windows_store(self) -> RunKeyStore | None:
        if self.platform != "win32":
            return None
        if self._store is None:
            self._store = WindowsRunKeyStore()
        return self._store

    def _audit(self, action: str, result: AutostartResult) -> AutostartResult:
        if self._emit is None:
            return result
        try:
            self._emit("autostart", action=action, success=result.success, state=result.state)
        except OSError:
            # Kayıt değişti ama denetime yazılamadı: sonuç bozulmaz, kullanıcıya söylenir.
            return AutostartResult(
                result.success, result.state, result.message + " (Denetim kaydı yazılamadı.)"
            )
        return result

    def status(self) -> AutostartResult:
        store = self._windows_store()
        if store is None:
            return AutostartResult(False, "off", "Açılışta başlama yalnız Windows'ta var.")
        try:
            current = store.read()
        except OSError:
            return AutostartResult(False, "off", "Kayıt defteri okunamadı.")
        if current is None:
            return AutostartResult(True, "off", "Açılışta başlama kapalı.")
        if current == self.expected and not Path(self.python).is_file():
            return AutostartResult(
                True, "stale", "Açılış kaydındaki Python bulunamadı; ortamı kontrol edin."
            )
        if current != self.expected:
            # Ortam taşınmış ya da elle değiştirilmiş: kayıt artık bu kurulumu başlatmıyor.
            return AutostartResult(
                True, "stale", "Açılış kaydı bu kurulumla eşleşmiyor; yeniden açın."
            )
        return AutostartResult(True, "on", "Açılışta başlama açık.")

    def enable(self) -> AutostartResult:
        store = self._windows_store()
        if store is None:
            return AutostartResult(False, "off", "Açılışta başlama yalnız Windows'ta var.")
        try:
            store.write(self.expected)
            written = store.read()
        except OSError:
            return self._audit(
                "enable", AutostartResult(False, "off", "Kayıt defterine yazılamadı.")
            )
        if written != self.expected:
            return self._audit(
                "enable",
                AutostartResult(False, "stale", "Yazılan açılış kaydı doğrulanamadı."),
            )
        return self._audit(
            "enable", AutostartResult(True, "on", "Jarvis Windows açılışında tepside başlayacak.")
        )

    def disable(self) -> AutostartResult:
        store = self._windows_store()
        if store is None:
            return AutostartResult(False, "off", "Açılışta başlama yalnız Windows'ta var.")
        try:
            store.delete()
            remaining = store.read()
        except OSError:
            return self._audit(
                "disable", AutostartResult(False, "on", "Kayıt defterinden silinemedi.")
            )
        if remaining is not None:
            return self._audit("disable", AutostartResult(False, "on", "Açılış kaydı silinemedi."))
        return self._audit("disable", AutostartResult(True, "off", "Açılışta başlama kapatıldı."))
