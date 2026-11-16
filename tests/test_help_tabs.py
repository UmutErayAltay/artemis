"""Sekmelerdeki "?" baloncukları (Sohbet, Projeler, Geçmiş, Otomasyon, Petler, Rutinler, Beceriler),
rutin formu cilası ve uygulama içi metin görüntüleyici bağlantısı."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QMessageBox, QWidget

from artemis.channels.desktop import automation_panel as automation_module
from artemis.channels.desktop import text_viewer
from artemis.channels.desktop.app import build_window
from artemis.channels.desktop.automation_panel import AutomationPanel
from artemis.channels.desktop.routine_dialog import RoutineDialog
from artemis.channels.desktop.widgets import HelpTip
from artemis.tools.registry import discover_tools


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])  # type: ignore[return-value]


def tips(widget: QWidget) -> list[HelpTip]:
    return widget.findChildren(HelpTip)


def check_texts(found: list[HelpTip]) -> None:
    for tip in found:
        text = tip.toolTip()
        assert text and len(text) <= 260, (len(text), text)
        assert tip.accessibleDescription() == text


def test_every_tab_stays_within_caps_and_keeps_key_bubbles(
    qt_app: QApplication, tmp_path: Path
) -> None:
    window = build_window(tmp_path / "ignored.yaml", demo=True, app=qt_app)
    try:
        settings = window.settings_panel
        chat = tips(window.model_box.parentWidget())  # type: ignore[arg-type]  # üst çubuk
        pages = {
            "chat": (chat, 4, ["Otomatik"]),
            "projects": (tips(window.projects_panel), 4, ["push yapmaz", "kesilir"]),
            "data": (tips(window.data_panel), 4, ["geri gelmez", "geri alınamaz"]),
            "automation": (tips(window.automation_panel), 4, ["Telegram'a gitmez", "web"]),
            "pet": (tips(settings.pet), 4, []),
            "routines": (tips(settings.routines), 5, []),
            "skills": (tips(settings.skills), 5, []),
        }
        for name, (found, cap, words) in pages.items():
            assert 1 <= len(found) <= cap or (not words and len(found) <= cap), (name, len(found))
            check_texts(found)
            for word in words:
                assert any(word in tip.toolTip() for tip in found), (name, word)
    finally:
        window.close()


def test_chat_buttons_are_covered(qt_app: QApplication, tmp_path: Path) -> None:
    window = build_window(tmp_path / "ignored.yaml", demo=True, app=qt_app)
    try:
        topbar_tips = tips(window.model_box.parentWidget())  # type: ignore[arg-type]
        texts = " ".join(t.toolTip() for t in topbar_tips)
        assert "Otomatik" in texts and "Onaylı" in texts and "onay" in texts
    finally:
        window.close()


def test_pet_creator_bubbles(qt_app: QApplication, tmp_path: Path) -> None:
    window = build_window(tmp_path / "ignored.yaml", demo=True, app=qt_app)
    try:
        creator = window.settings_panel.pet.creator
        assert creator is not None
        found = tips(creator)
        assert 1 <= len(found) <= 3
        check_texts(found)
        assert any("gönderir" in tip.toolTip() for tip in found)  # ne, hangi modele gider
    finally:
        window.close()


def test_routine_dialog_shows_turkish_names(qt_app: QApplication) -> None:
    dialog = RoutineDialog(None, [], discover_tools())
    editor = dialog.editors[0]
    items = [editor.tool.itemText(i) for i in range(editor.tool.count())]
    assert "Web'de ara (web_search)" in items
    editor.tool.setCurrentIndex(editor.tool.findData("web_search"))
    assert editor.tool_id() == "web_search"
    assert editor.about.text() == "İnternette arama yapar."
    labels = [f.text() for f in editor.findChildren(QWidget) if f.objectName() == "fieldLabel"]
    assert any(t.startswith("Arama metni") and t.endswith("*") for t in labels)
    assert not any("query" in t for t in labels)
    # Dar/geniş her yerde aynı: gövde şeffaf (pencere rengi), düğme çubuğu sabit.
    assert dialog.body_scroll.widget().objectName() == "flat"
    assert dialog.ok_button.objectName() == "primary"


def test_automation_report_opens_in_app_viewer(
    qt_app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = tmp_path / "rapor.md"
    report.write_text("# Başlık\n[bağ](https://example.com)\n", encoding="utf-8")
    shown: list[str] = []

    def fake_exec(self: text_viewer.TextViewerDialog) -> int:
        shown.append(self.browser.toPlainText())
        return 0

    monkeypatch.setattr(text_viewer.TextViewerDialog, "exec", fake_exec)
    monkeypatch.setattr(
        QDesktopServices, "openUrl", lambda url: pytest.fail("dış uygulama açıldı")
    )
    panel = AutomationPanel(config_path=tmp_path / "x.yaml")
    panel._open(str(report))
    assert shown and "Başlık" in shown[0]
    assert automation_module.open_text_file is text_viewer.open_text_file
    _ = QUrl  # openUrl imzası değişirse bu import kullanımda kalır


def test_unreadable_file_shows_message_not_crash(
    qt_app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    messages: list[str] = []
    monkeypatch.setattr(
        QMessageBox, "warning", lambda parent, title, text: messages.append(text)
    )
    text_viewer.open_text_file(tmp_path / "yok.md", "Rapor")
    assert messages == ["Dosya okunamadı."]


def test_invalid_utf8_is_replaced_and_big_file_bounded(
    qt_app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []
    monkeypatch.setattr(
        text_viewer.TextViewerDialog, "exec",
        lambda self: seen.append(self.copy_text.__self__._text) or 0,
    )
    bad = tmp_path / "bad.md"
    bad.write_bytes(b"abc \xff\xfe son")
    text_viewer.open_text_file(bad, "Özet")
    assert "abc" in seen[0] and "son" in seen[0]
    big = tmp_path / "big.md"
    big.write_bytes(b"a" * (text_viewer._MAX_BYTES + 10))
    text_viewer.open_text_file(big, "Özet")
    assert len(seen[1]) < text_viewer._MAX_BYTES + 200 and "çok uzun" in seen[1]
