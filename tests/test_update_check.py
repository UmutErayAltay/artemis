"""Sürüm denetimi testleri (ADR 0075). Gerçek ağ kullanılmaz."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterable
from pathlib import Path

import httpx
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QPushButton

from artemis.channels.desktop.update_section import UpdateSection
from artemis.update_check import (
    UpdateDownloadError,
    UpdateResult,
    check_latest,
    current_version,
    download_installer,
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

    monkeypatch.setattr("artemis.channels.desktop.thread_shutdown.DEFAULT_WAIT_MS", 50)
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


def test_check_latest_selects_valid_installer_asset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("artemis.update_check.current_version", lambda: "0.1.0")
    digest_upper = "A" * 64
    installer_url = (
        "https://github.com/UmutErayAltay/artemis/releases/download/v9.9.9/Artemis-Setup-9.9.9.exe"
    )
    payload = {
        "tag_name": "v9.9.9",
        "html_url": "https://github.com/UmutErayAltay/artemis/releases/tag/v9.9.9",
        "assets": [
            {
                "name": "artemis-v9.9.9.zip",
                "browser_download_url": (
                    "https://github.com/UmutErayAltay/artemis/releases/download/v9.9.9/artemis.zip"
                ),
                "size": 1000,
                "digest": f"sha256:{'b' * 64}",
            },
            {
                "name": "Artemis-Setup-9.9.9.exe",
                "browser_download_url": installer_url,
                "size": 123456,
                "digest": f"sha256:{digest_upper}",
            },
        ],
    }
    result = check_latest(fetch=lambda _u, _h, _t: (200, payload))
    assert result.status == "newer"
    assert result.installer_url == installer_url
    assert result.installer_name == "Artemis-Setup-9.9.9.exe"
    assert result.installer_size == 123456
    assert result.installer_sha256 == digest_upper.lower()


def test_check_latest_missing_digest_leaves_installer_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("artemis.update_check.current_version", lambda: "0.1.0")
    payload = {
        "tag_name": "v9.9.9",
        "html_url": "https://github.com/UmutErayAltay/artemis/releases/tag/v9.9.9",
        "assets": [
            {
                "name": "Artemis-Setup-9.9.9.exe",
                "browser_download_url": (
                    "https://github.com/UmutErayAltay/artemis/releases/download/"
                    "v9.9.9/Artemis-Setup-9.9.9.exe"
                ),
                "size": 123456,
            }
        ],
    }
    result = check_latest(fetch=lambda _u, _h, _t: (200, payload))
    assert result.status == "newer"
    assert result.installer_url == ""
    assert result.installer_name == ""
    assert result.installer_size == 0
    assert result.installer_sha256 == ""


def test_check_latest_untrusted_url_leaves_installer_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("artemis.update_check.current_version", lambda: "0.1.0")
    payload = {
        "tag_name": "v9.9.9",
        "html_url": "https://github.com/UmutErayAltay/artemis/releases/tag/v9.9.9",
        "assets": [
            {
                "name": "Artemis-Setup-9.9.9.exe",
                "browser_download_url": "https://evil.example/Artemis-Setup-9.9.9.exe",
                "size": 123456,
                "digest": f"sha256:{'a' * 64}",
            }
        ],
    }
    result = check_latest(fetch=lambda _u, _h, _t: (200, payload))
    assert result.status == "newer"
    assert result.installer_url == ""
    assert result.installer_sha256 == ""


@pytest.mark.parametrize("bad_size", [0, -1, 1_500_000_001, True])
def test_check_latest_invalid_size_leaves_installer_empty(
    monkeypatch: pytest.MonkeyPatch, bad_size: int | bool
) -> None:
    monkeypatch.setattr("artemis.update_check.current_version", lambda: "0.1.0")
    payload = {
        "tag_name": "v9.9.9",
        "html_url": "https://github.com/UmutErayAltay/artemis/releases/tag/v9.9.9",
        "assets": [
            {
                "name": "Artemis-Setup-9.9.9.exe",
                "browser_download_url": (
                    "https://github.com/UmutErayAltay/artemis/releases/download/"
                    "v9.9.9/Artemis-Setup-9.9.9.exe"
                ),
                "size": bad_size,
                "digest": f"sha256:{'a' * 64}",
            }
        ],
    }
    result = check_latest(fetch=lambda _u, _h, _t: (200, payload))
    assert result.status == "newer"
    assert result.installer_url == ""
    assert result.installer_size == 0


def _make_installer_result(content: bytes) -> UpdateResult:
    digest = hashlib.sha256(content).hexdigest()
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
        installer_size=len(content),
        installer_sha256=digest,
    )


def test_download_installer_success(tmp_path: Path) -> None:
    content = b"MZfake-installer-binary-payload"
    result = _make_installer_result(content)
    seen_progress: list[tuple[int, int]] = []

    def fake_stream(_url: str, _timeout: float) -> Iterable[bytes]:
        yield content[:10]
        yield content[10:]

    path = download_installer(
        result,
        tmp_path,
        stream=fake_stream,
        progress=lambda done, total: seen_progress.append((done, total)),
    )
    assert path == tmp_path / "Artemis-Setup-9.9.9.exe"
    assert path.read_bytes() == content
    assert not (tmp_path / "Artemis-Setup-9.9.9.exe.part").exists()
    assert seen_progress == [(10, len(content)), (len(content), len(content))]


def test_download_installer_size_overflow_cleans_part(tmp_path: Path) -> None:
    content = b"0123456789"
    result = _make_installer_result(content)

    def overflow_stream(_url: str, _timeout: float) -> Iterable[bytes]:
        yield b"01234"
        yield b"56789EXTRA"

    with pytest.raises(UpdateDownloadError):
        download_installer(result, tmp_path, stream=overflow_stream)

    assert not (tmp_path / "Artemis-Setup-9.9.9.exe.part").exists()
    assert not (tmp_path / "Artemis-Setup-9.9.9.exe").exists()


def test_download_installer_sha_mismatch_cleans_part(tmp_path: Path) -> None:
    expected = b"0123456789"
    wrong = b"9876543210"
    result = _make_installer_result(expected)

    with pytest.raises(UpdateDownloadError, match="SHA-256"):
        download_installer(result, tmp_path, stream=lambda _u, _t: [wrong])

    assert not (tmp_path / "Artemis-Setup-9.9.9.exe.part").exists()
    assert not (tmp_path / "Artemis-Setup-9.9.9.exe").exists()


def test_download_installer_cancelled_cleans_part(tmp_path: Path) -> None:
    content = b"abcdefghij"
    result = _make_installer_result(content)
    cancel_flag = False

    def two_chunk_stream(_url: str, _timeout: float) -> Iterable[bytes]:
        nonlocal cancel_flag
        yield content[:5]
        cancel_flag = True
        yield content[5:]

    with pytest.raises(UpdateDownloadError, match="İndirme iptal edildi."):
        download_installer(
            result,
            tmp_path,
            stream=two_chunk_stream,
            cancelled=lambda: cancel_flag,
        )

    assert not (tmp_path / "Artemis-Setup-9.9.9.exe.part").exists()
    assert not (tmp_path / "Artemis-Setup-9.9.9.exe").exists()


def test_download_installer_stream_http_error_cleans_part(tmp_path: Path) -> None:
    content = b"abcdefghij"
    result = _make_installer_result(content)

    def broken_stream(_url: str, _timeout: float) -> Iterable[bytes]:
        yield content[:3]
        raise httpx.ConnectError("connection reset")

    with pytest.raises(UpdateDownloadError):
        download_installer(result, tmp_path, stream=broken_stream)

    assert not (tmp_path / "Artemis-Setup-9.9.9.exe.part").exists()
    assert not (tmp_path / "Artemis-Setup-9.9.9.exe").exists()
