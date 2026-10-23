"""Açılışta başlama (ADR 0030): gerçek kayıt defterine dokunulmaz, sahte depo kullanılır."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from artemis.channels.autostart import (
    LEGACY_VALUE_NAME,
    VALUE_NAME,
    Autostart,
    autostart_command,
    gui_python,
)
from artemis.channels.cli import autostart as autostart_cli
from artemis.tools.registry import discover_tools


class FakeStore:
    def __init__(self, value: str | None = None, legacy: str | None = None) -> None:
        self.value = value
        self.legacy = legacy
        self.calls: list[str] = []

    def read(self, name: str = VALUE_NAME) -> str | None:
        self.calls.append("read")
        return self.legacy if name == LEGACY_VALUE_NAME else self.value

    def write(self, command: str) -> None:
        self.calls.append("write")
        self.value = command

    def delete(self, name: str = VALUE_NAME) -> None:
        self.calls.append("delete")
        if name == LEGACY_VALUE_NAME:
            self.legacy = None
        else:
            self.value = None


class ForbiddenStore(FakeStore):
    def read(self, name: str = VALUE_NAME) -> str | None:
        raise AssertionError("Windows dışında kayıt deposu çağrılmamalı")

    write = delete = read  # type: ignore[assignment]


def _manager(
    tmp_path: Path, store: FakeStore, events: list[dict[str, Any]] | None = None
) -> Autostart:
    def emit(event: str, **fields: Any) -> None:
        if events is not None:
            events.append({"event": event, **fields})

    python = tmp_path / "pythonw.exe"
    python.write_text("")
    return Autostart(
        tmp_path / "config.yaml",
        store=store,
        platform="win32",
        python=str(python),
        emit=emit,
    )


def test_command_is_fixed_quoted_absolute_and_never_opens_microphone(tmp_path: Path) -> None:
    config = tmp_path / "boşluklu klasör" / "config.yaml"
    command = autostart_command(config, python=r"C:\Program Files\Py\pythonw.exe")
    assert command.startswith('"C:\\Program Files\\Py\\pythonw.exe" -m artemis desktop --arka-plan')
    assert f'--config "{config.resolve()}"' in command
    assert "--ses" not in command
    assert "voice" not in command


def test_gui_python_prefers_windowed_interpreter(tmp_path: Path) -> None:
    console = tmp_path / "python.exe"
    console.write_text("")
    assert gui_python(str(console)) == str(console)
    (tmp_path / "pythonw.exe").write_text("")
    assert gui_python(str(console)) == str(tmp_path / "pythonw.exe")


def test_enable_writes_reads_back_and_audits(tmp_path: Path) -> None:
    store, events = FakeStore(), []
    manager = _manager(tmp_path, store, events)
    result = manager.enable()
    assert result.success and result.state == "on"
    assert store.value == manager.expected
    assert store.calls == ["write", "delete", "read"]
    assert events == [{"event": "autostart", "action": "enable", "success": True, "state": "on"}]
    assert manager.status().state == "on"


def test_disable_removes_value(tmp_path: Path) -> None:
    store = FakeStore()
    manager = _manager(tmp_path, store)
    manager.enable()
    result = manager.disable()
    assert result.success and result.state == "off"
    assert store.value is None
    assert manager.status().state == "off"


def test_status_reports_stale_entry_from_another_installation(tmp_path: Path) -> None:
    manager = _manager(tmp_path, FakeStore(r'"C:\eski\pythonw.exe" -m artemis desktop'))
    assert manager.status().state == "stale"


def test_enable_fails_when_read_back_differs(tmp_path: Path) -> None:
    class Mangling(FakeStore):
        def write(self, command: str) -> None:
            self.value = command + " --ses"

    events: list[dict[str, Any]] = []
    result = _manager(tmp_path, Mangling(), events).enable()
    assert not result.success
    assert events[0]["success"] is False


def test_registry_errors_are_reported_not_raised(tmp_path: Path) -> None:
    class Broken(FakeStore):
        def write(self, command: str) -> None:
            raise PermissionError("erişim")

        def delete(self, name: str = VALUE_NAME) -> None:
            raise PermissionError("erişim")

    assert not _manager(tmp_path, Broken()).enable().success
    assert not _manager(tmp_path, Broken()).disable().success


def test_non_windows_is_honest_and_never_touches_store(tmp_path: Path) -> None:
    manager = Autostart(tmp_path / "c.yaml", store=ForbiddenStore(), platform="linux")
    for result in (manager.enable(), manager.disable(), manager.status()):
        assert not result.success
        assert "Windows" in result.message


def test_cli_status_on_linux_reports_windows_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("artemis.channels.autostart.sys.platform", "linux")
    config = tmp_path / "config.yaml"
    config.write_text(Path("config/config.example.yaml").read_text(encoding="utf-8"), "utf-8")
    assert autostart_cli(config, "ac") == 1
    assert "yalnız Windows" in capsys.readouterr().out


def test_status_is_stale_when_interpreter_is_gone(tmp_path: Path) -> None:
    store = FakeStore()
    manager = _manager(tmp_path, store)
    manager.enable()
    (tmp_path / "pythonw.exe").unlink()
    assert manager.status().state == "stale"


def test_audit_failure_does_not_hide_the_registry_change(tmp_path: Path) -> None:
    def broken_emit(event: str, **fields: Any) -> None:
        raise OSError("disk dolu")

    python = tmp_path / "pythonw.exe"
    python.write_text("")
    store = FakeStore()
    manager = Autostart(
        tmp_path / "c.yaml", store=store, platform="win32", python=str(python), emit=broken_emit
    )
    result = manager.enable()
    assert result.success and result.state == "on"
    assert "Denetim kaydı yazılamadı" in result.message
    assert store.value == manager.expected


def test_model_has_no_tool_that_touches_autostart_or_registry() -> None:
    names = [definition.name for definition in discover_tools().definitions()]
    assert names
    assert not [name for name in names if "autostart" in name or "registry" in name]
    plugins = Path("artemis/tools/plugins")
    for source in plugins.glob("*.py"):
        text = source.read_text(encoding="utf-8")
        assert "artemis.channels.autostart" not in text, source
        assert "winreg" not in text, source


def test_enable_removes_legacy_value(tmp_path: Path) -> None:
    store = FakeStore(legacy=r'"C:\eski\pythonw.exe" -m jarvis desktop')
    result = _manager(tmp_path, store).enable()
    assert result.success and result.state == "on"
    assert store.legacy is None


def test_status_flags_legacy_value_as_stale(tmp_path: Path) -> None:
    manager = _manager(tmp_path, FakeStore(legacy="eski"))
    status = manager.status()
    assert status.state == "stale" and "yeniden" in status.message
    manager.enable()
    assert manager.status().state == "on"


def test_disable_removes_both_values(tmp_path: Path) -> None:
    store = FakeStore("yeni", legacy="eski")
    result = _manager(tmp_path, store).disable()
    assert result.success and result.state == "off"
    assert store.value is None and store.legacy is None
