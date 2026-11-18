"""Sürüm denetimi testleri (ADR 0075). Gerçek ağ kullanılmaz."""

from __future__ import annotations

import os

import httpx
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QPushButton

from artemis.channels.desktop.update_section import UpdateSection
from artemis.update_check import (
    UpdateResult,
    check_latest,
    current_version,
    parse_version,
)


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_parse_version() -> None:
    assert parse_version("v1.2.3") == (1, 2, 3)
    assert parse_version("1.2.3rc1") == (1, 2, 3)
    assert parse_version("") == ()
    assert parse_version("   ") == ()
    assert parse_version("garbage") == ()
    assert parse_version("v0.1.0") == (0, 1, 0)
    assert parse_version("1.2.x") == (1, 2)
    assert parse_version("0.10.0") > parse_version("0.9.9")


def test_current_version_returns_non_empty_str() -> None:
    version = current_version()
    assert isinstance(version, str)
    assert len(version) > 0


def test_check_latest_newer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("artemis.update_check.current_version", lambda: "0.1.0")
    payload = {
        "tag_name": "v9.9.9",
        "html_url": "https://github.com/UmutErayAltay/artemis/releases/tag/v9.9.9",
    }
    result = check_latest(fetch=lambda _u, _h, _t: (200, payload))
    assert result.status == "newer"
    assert result.latest == "v9.9.9"
    assert result.url == "https://github.com/UmutErayAltay/artemis/releases/tag/v9.9.9"
    assert "v9.9.9" in result.message


def test_check_latest_same_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("artemis.update_check.current_version", lambda: "1.0.0")
    payload = {
        "tag_name": "v1.0.0",
        "html_url": "https://github.com/UmutErayAltay/artemis/releases/tag/v1.0.0",
    }
    result = check_latest(fetch=lambda _u, _h, _t: (200, payload))
    assert result.status == "current"
    assert result.latest == "v1.0.0"
    assert "1.0.0" in result.message


def test_check_latest_404() -> None:
    result = check_latest(fetch=lambda _u, _h, _t: (404, {"message": "Not Found"}))
    assert result.status == "current"
    assert result.message == "Henüz yayımlanmış sürüm yok."
    assert result.latest == ""
    assert result.url == ""


def test_check_latest_http_500() -> None:
    result = check_latest(fetch=lambda _u, _h, _t: (500, {}))
    assert result.status == "error"
    assert "500" in result.message
    assert result.url == ""


def test_check_latest_network_error() -> None:
    def broken_fetch(_u: str, _h: dict[str, str], _t: float) -> tuple[int, object]:
        raise httpx.ConnectError("Bağlantı hatası")

    result = check_latest(fetch=broken_fetch)
    assert result.status == "error"
    assert "http" not in result.message.lower()
    assert "github.com" not in result.message


def test_check_latest_malformed_body() -> None:
    result = check_latest(fetch=lambda _u, _h, _t: (200, "not a json dict"))
    assert result.status == "error"

    result_missing = check_latest(fetch=lambda _u, _h, _t: (200, {"tag_name": 123}))
    assert result_missing.status == "error"

    def value_error_fetch(_u: str, _h: dict[str, str], _t: float) -> tuple[int, object]:
        raise ValueError("Malformed JSON")

    result_value_err = check_latest(fetch=value_error_fetch)
    assert result_value_err.status == "error"


def test_update_section_ui_smoke(qt_app: QApplication) -> None:
    fake_result = UpdateResult(
        status="newer",
        latest="v9.9.9",
        url="https://example.invalid/r",
        message="Yeni sürüm var: v9.9.9.",
    )
    section = UpdateSection(check=lambda: fake_result)
    section.show()

    btn = section.findChild(QPushButton)
    assert btn is not None
    assert btn.text() == "Güncellemeyi denetle"

    section._on_done(fake_result)
    assert section.result_label.text() == fake_result.message
    assert section.open_button.isVisible()
    assert section.open_button.isEnabled()
    assert section.url_label.text() == "https://example.invalid/r"

    section.check_button.click()
    assert section._thread is not None
    section._thread.wait(3000)
    qt_app.processEvents()
    assert section.result_label.text() == fake_result.message

    section.deleteLater()


def test_update_section_close_detaches_running_thread(
    qt_app: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    from artemis.channels.desktop import update_section as mod

    entered = threading.Event()
    release = threading.Event()

    def slow_check() -> UpdateResult:
        entered.set()
        release.wait(5)
        return UpdateResult(status="current", latest="", url="", message="Güncel.")

    monkeypatch.setattr(mod, "_SHUTDOWN_WAIT_MS", 50)
    section = UpdateSection(check=slow_check)
    section.check_button.click()
    assert entered.wait(3)
    thread = section._thread
    assert thread is not None and thread.isRunning() and thread.parent() is section

    section.close()
    assert thread.parent() is None
    assert thread in mod._DETACHED
    section.deleteLater()
    qt_app.processEvents()

    release.set()
    assert thread.wait(3000)
    qt_app.processEvents()
    assert thread not in mod._DETACHED
