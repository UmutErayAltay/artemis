"""AboutSection birim testleri."""

from __future__ import annotations

import os
import platform
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel  # noqa: E402

from artemis.channels.desktop.about_section import AboutSection  # noqa: E402
from artemis.update_check import current_version  # noqa: E402


def _app() -> QApplication:
    instance = QApplication.instance()
    if isinstance(instance, QApplication):
        return instance
    return QApplication([])


def test_version_label_contains_current_version(tmp_path: Path) -> None:
    _app()
    section = AboutSection(tmp_path / "config.yaml")
    assert current_version() in section.version_label.text()


def test_github_button_calls_injected_open_url(tmp_path: Path) -> None:
    _app()
    opened: list[str] = []
    section = AboutSection(
        tmp_path / "config.yaml",
        open_url=lambda url: opened.append(url),
    )
    section.github_button.click()
    assert opened == ["https://github.com/UmutErayAltay/artemis"]


def test_license_button_enabled_iff_license_exists(tmp_path: Path) -> None:
    _app()
    repo_root = Path(__file__).resolve().parents[1]
    expected = (repo_root / "LICENSE").is_file()
    section = AboutSection(tmp_path / "config.yaml")
    assert section.license_button.isEnabled() is expected


def test_system_text_contains_python_version(tmp_path: Path) -> None:
    _app()
    cfg = tmp_path / "config.yaml"
    opened: list[str] = []
    section = AboutSection(cfg, open_url=lambda url: opened.append(url))
    all_text = "\n".join(label.text() for label in section.findChildren(QLabel))
    assert platform.python_version() in all_text
    assert str(cfg.parent) in all_text and "config.yaml" not in all_text
    section.open_config_dir_button.click()
    assert len(opened) == 1
