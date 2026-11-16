"""Dondurulmuş giriş noktası ve Windows paket dosyaları için testler."""

import importlib.util
import runpy
import sys
from pathlib import Path
from types import ModuleType

import pytest

from artemis.channels import cli

ROOT = Path(__file__).resolve().parents[1]


def _load_entry_module() -> ModuleType:
    # PyPI `packaging` paketiyle çakışmamak için dosya yolundan benzersiz adla yüklenir.
    entry_path = ROOT / "packaging" / "entry.py"
    spec = importlib.util.spec_from_file_location("artemis_packaging_entry", entry_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_entry_no_args_starts_desktop(monkeypatch: pytest.MonkeyPatch) -> None:
    entry = _load_entry_module()
    captured: list[list[str]] = []

    def fake_cli_main() -> None:
        captured.append(list(sys.argv))

    monkeypatch.setattr(cli, "main", fake_cli_main)
    monkeypatch.setattr(sys, "argv", ["Artemis.exe"])

    entry.main([])

    assert captured == [["Artemis", "desktop"]]


def test_entry_emulates_dash_m_for_artemis_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    entry = _load_entry_module()
    calls: list[tuple[str, str, bool, list[str]]] = []

    def fake_run_module(
        mod_name: str, run_name: str | None = None, alter_sys: bool = False
    ) -> dict[str, object]:
        calls.append((mod_name, str(run_name), alter_sys, list(sys.argv)))
        return {}

    monkeypatch.setattr(runpy, "run_module", fake_run_module)
    monkeypatch.setattr(sys, "argv", ["Artemis.exe"])

    entry.main(["-m", "artemis.x", "--bayrak", "deger"])

    assert calls == [("artemis.x", "__main__", True, ["artemis.x", "--bayrak", "deger"])]


def test_entry_rejects_non_artemis_module() -> None:
    entry = _load_entry_module()

    with pytest.raises(SystemExit):
        entry.main(["-m", "os"])

    with pytest.raises(SystemExit):
        entry.main(["-m", "artemis"])


def test_entry_passes_other_args_through(monkeypatch: pytest.MonkeyPatch) -> None:
    entry = _load_entry_module()
    captured: list[list[str]] = []

    def fake_cli_main() -> None:
        captured.append(list(sys.argv))

    monkeypatch.setattr(cli, "main", fake_cli_main)
    monkeypatch.setattr(sys, "argv", ["Artemis.exe"])

    entry.main(["chat", "--config", "ozel.yaml"])

    assert captured == [["Artemis", "chat", "--config", "ozel.yaml"]]


def test_spec_contains_required_packaging_settings() -> None:
    spec_text = (ROOT / "packaging" / "artemis.spec").read_text(encoding="utf-8")

    assert "config.example.yaml" in spec_text
    assert "console=False" in spec_text
    assert 'collect_submodules("artemis")' in spec_text


def test_iss_uninstall_does_not_touch_userappdata() -> None:
    iss_text = (ROOT / "packaging" / "artemis.iss").read_text(encoding="utf-8")
    _, _, uninstall_section = iss_text.partition("[UninstallDelete]")
    assert uninstall_section
    assert "{userappdata}" not in uninstall_section.casefold()
