"""Platform adaptörü tarayıcı URL doğrulama testleri (tur 3).

Gerçek tarayıcı veya alt süreç başlatılmaz: `platform.system` ve `subprocess.Popen`
yalnız kayıt tutan sahte nesnelerle değiştirilir.
"""

from __future__ import annotations

from typing import Any

import pytest

from artemis.tools import platform as platform_module
from artemis.tools.platform import WindowsDesktop


def _patch_windows_popen(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[list[str], dict[str, Any]]]:
    """Gerçek tarayıcı açılmasını önlemek için platform.system ve Popen'ı yamalar."""
    recorded: list[tuple[list[str], dict[str, Any]]] = []

    def fake_popen(command: list[str], **kwargs: Any) -> Any:
        recorded.append((list(command), dict(kwargs)))
        return object()

    monkeypatch.setattr(platform_module.platform, "system", lambda: "Windows")
    monkeypatch.setattr(platform_module.subprocess, "Popen", fake_popen)
    return recorded


def test_open_browser_rejects_file_url_without_calling_popen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded = _patch_windows_popen(monkeypatch)
    desktop = WindowsDesktop()

    with pytest.raises(ValueError, match="Yalnız http/https adresi açılabilir."):
        desktop.open_browser("file:///C:/Windows/System32/calc.exe")

    # Güvenlik sınırı Popen'dan önce reddetmelidir.
    assert recorded == []


@pytest.mark.parametrize(
    "bad_url",
    [
        r"C:\Windows\System32\calc.exe",
        "javascript:alert(1)",
        "https:///missing-host",
        "https://user:secret@example.com/",
        "https://@example.com/",
        "https://example.com/path\nnewline",
        "https://example.com/path\x7fdel",
    ],
)
def test_open_browser_rejects_unsafe_urls(
    monkeypatch: pytest.MonkeyPatch, bad_url: str
) -> None:
    recorded = _patch_windows_popen(monkeypatch)
    desktop = WindowsDesktop()

    with pytest.raises(ValueError):
        desktop.open_browser(bad_url)

    assert recorded == []


def test_open_browser_accepts_https_url_and_calls_popen_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded = _patch_windows_popen(monkeypatch)
    desktop = WindowsDesktop()

    desktop.open_browser("https://example.com/")

    assert len(recorded) == 1
    command, kwargs = recorded[0]
    assert command[-1] == "https://example.com/"
    assert kwargs["shell"] is False
