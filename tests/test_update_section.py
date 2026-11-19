"""Güncelleme bölümü indirme arayüzü testleri (ADR 0081). Gerçek ağ kullanılmaz."""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from artemis.channels.desktop.update_section import UpdateSection
from artemis.update_check import UpdateDownloadError, UpdateResult


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _installer_result() -> UpdateResult:
    return UpdateResult(
        status="newer",
        latest="v9.9.9",
        url="https://github.com/UmutErayAltay/artemis/releases/tag/v9.9.9",
        message="Yeni sürüm var: v9.9.9.",
        installer_url=(
            "https://github.com/UmutErayAltay/artemis/releases/download/"
            "v9.9.9/Artemis-Setup-9.9.9.exe"
        ),
        installer_name="Artemis-Setup-9.9.9.exe",
        installer_size=1_000_000,
        installer_sha256="a" * 64,
    )


def test_install_button_hidden_when_not_frozen(qt_app: QApplication) -> None:
    section = UpdateSection(frozen=False)
    section.show()
    section._on_done(_installer_result())
    qt_app.processEvents()

    assert not section.install_button.isVisible()
    assert section.open_button.isVisible()

    section.finish()
    section.deleteLater()


def test_install_button_visible_when_frozen_with_installer(qt_app: QApplication) -> None:
    section = UpdateSection(frozen=True)
    section.show()
    section._on_done(_installer_result())
    qt_app.processEvents()

    assert section.install_button.isVisible()
    assert section.install_button.isEnabled()

    section.finish()
    section.deleteLater()


def test_install_button_downloads_and_emits_install_ready(
    qt_app: QApplication, tmp_path: Path
) -> None:
    target = tmp_path / "Artemis-Setup-9.9.9.exe"

    def fake_download(
        _result: UpdateResult,
        *,
        progress: Callable[[int, int], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> Path:
        assert cancelled is not None and not cancelled()
        if progress is not None:
            progress(500_000, 1_000_000)
            progress(1_000_000, 1_000_000)
        target.write_bytes(b"MZ")
        return target

    section = UpdateSection(download=fake_download, frozen=True)
    section.show()
    section._on_done(_installer_result())

    ready_paths: list[Path] = []
    section.install_ready.connect(ready_paths.append)

    section.install_button.click()
    thread = section._download_thread
    assert thread is not None
    assert thread.wait(3000)
    qt_app.processEvents()

    assert ready_paths == [target]
    assert not section.cancel_button.isVisible()
    assert "SHA-256" in section.result_label.text()

    section.finish()
    section.deleteLater()


def test_cancel_button_stops_download_and_shows_message(qt_app: QApplication) -> None:
    started = threading.Event()

    def cancellable_download(
        _result: UpdateResult,
        *,
        progress: Callable[[int, int], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> Path:
        if progress is not None:
            progress(100_000, 1_000_000)
        started.set()
        for _ in range(300):
            if cancelled is not None and cancelled():
                raise UpdateDownloadError("İndirme iptal edildi.")
            threading.Event().wait(0.01)
        raise AssertionError("İptal bekleniyordu")

    section = UpdateSection(download=cancellable_download, frozen=True)
    section.show()
    section._on_done(_installer_result())

    section.install_button.click()
    assert started.wait(3)
    thread = section._download_thread
    assert thread is not None

    section.cancel_button.click()
    assert thread.wait(3000)
    qt_app.processEvents()

    assert section.result_label.text() == "İndirme iptal edildi."
    assert not section.cancel_button.isVisible()
    assert section.check_button.isEnabled()
    assert section.install_button.isEnabled()

    section.finish()
    section.deleteLater()
