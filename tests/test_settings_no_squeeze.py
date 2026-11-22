"""Ayarlar > Ses sayfasında (tr ve en) bulut TTS satırlarının
sıkışmadığını ve örtüşmediğini doğrular.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
import yaml

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QFont  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel, QWidget  # noqa: E402

from artemis.brain import providers  # noqa: E402
from artemis.channels.desktop import tts_cloud_box  # noqa: E402
from artemis.channels.desktop.app import _STYLE, build_window  # noqa: E402
from artemis.i18n import set_language  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _stub_cloud_and_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Gerçek kasa okumasını ve bulut TTS model sorgusunu devre dışı bırakır.

    Neden: `TtsCloudBox` gösterildiğinde veya sunucu seçildiğinde kasa ve HTTP çağrısı
    tetiklenebilir; düzen testi tamamen deterministik ve çevrimdışı kalmalıdır.
    """
    monkeypatch.setattr(providers, "read_secret", lambda _target: None)
    monkeypatch.setattr(providers, "stored_endpoint_key", lambda _name: None)
    monkeypatch.setattr(tts_cloud_box, "stored_endpoint_key", lambda _name: None)
    monkeypatch.setattr(
        tts_cloud_box,
        "fetch_tts_models",
        lambda _base_url, _protocol, _key: ("", []),
    )


def _write_demo_config(tmp_path: Path) -> Path:
    """Örnek yapılandırmaya ücretli ve ücretsiz iki kayıtlı ses modeli ekleyerek yazar.

    Neden: `tts_cloud_saved` seçimi değiştiğinde `_manual_box` ve `tts_cloud_paid`
    görünürlüğü dinamik olarak değişir; bu geçişlerde de sıkışma olmadığını sınamak için
    hem ücretli hem ücretsiz kayıtlı konuşma modeline ihtiyaç vardır.
    """
    raw = (ROOT / "config" / "config.example.yaml").read_text(encoding="utf-8")
    data: dict[str, Any] = yaml.safe_load(raw) or {}
    endpoints = dict(data.get("endpoints") or {})
    endpoints.setdefault("bulut", {"base_url": "https://api.example.com/v1"})
    data["endpoints"] = endpoints

    models = dict(data.get("models") or {})
    models["tts_paid"] = {
        "provider": "openai_compatible",
        "kind": "speech",
        "speech_protocol": "openai",
        "model": "tts-test",
        "endpoint": "bulut",
        "input_per_million": "1",
        "output_per_million": "0",
    }
    models["tts_free"] = {
        "provider": "openai_compatible",
        "kind": "speech",
        "speech_protocol": "openai",
        "model": "tts-free-test",
        "endpoint": "bulut",
        "input_per_million": "0",
        "output_per_million": "0",
    }
    data["models"] = models

    cfg = tmp_path / "demo.yaml"
    cfg.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return cfg


def _pump(app: QApplication, count: int = 50) -> None:
    """Qt yerleşim ve boyama olaylarının tamamlanması için olay döngüsünü döndürür."""
    for _ in range(count):
        app.processEvents()


def _widget_desc(widget: QWidget) -> tuple[str, str, str]:
    """Hata mesajında hangi bileşenin sıkıştığını anlamak için sınıf, ad ve metin döndürür."""
    text_getter = getattr(widget, "text", None)
    text = text_getter() if callable(text_getter) else ""
    return (type(widget).__name__, widget.objectName(), str(text))


def _assert_no_squeeze(page: QWidget, state_name: str) -> None:
    """Sayfadaki görünür bileşenlerde sıkışma, satır sarma taşması ve kardeş örtüşmesi arar."""
    for w in page.findChildren(QWidget):
        if not w.isVisibleTo(page):
            continue
        # Açıkça sabit yükseklik verilmiş bileşenler (ör. 6px seviye çubuğu)
        # tasarım tercihidir, sıkışma değildir.
        if w.minimumHeight() == w.maximumHeight():
            continue
        min_h = w.minimumSizeHint().height()
        if min_h > 0:
            assert w.height() >= min_h, (
                state_name,
                _widget_desc(w),
                w.height(),
                min_h,
            )

    for label in page.findChildren(QLabel):
        if not label.isVisibleTo(page) or not label.wordWrap():
            continue
        needed = label.heightForWidth(label.width())
        if needed != -1:
            assert label.height() >= needed, (
                state_name,
                _widget_desc(label),
                label.height(),
                needed,
            )

    parents: list[QWidget] = [page] + [
        w for w in page.findChildren(QWidget) if w.isVisibleTo(page)
    ]
    for parent in parents:
        direct_only = Qt.FindChildOption.FindDirectChildrenOnly
        children = [
            c
            for c in parent.findChildren(QWidget, options=direct_only)
            if c.isVisibleTo(parent)
        ]
        for i, first in enumerate(children):
            rect_a = first.geometry().adjusted(1, 1, -1, -1)
            if not rect_a.isValid():
                continue
            for second in children[i + 1 :]:
                rect_b = second.geometry().adjusted(1, 1, -1, -1)
                if not rect_b.isValid():
                    continue
                assert not rect_a.intersects(rect_b), (
                    state_name,
                    _widget_desc(parent),
                    _widget_desc(first),
                    first.geometry(),
                    _widget_desc(second),
                    second.geometry(),
                )


@pytest.mark.parametrize("lang", ["en", "tr"])
def test_voice_cloud_tts_page_not_squeezed(lang: str, tmp_path: Path) -> None:
    """Ses > Konuşma bulut kutusu tüm görünürlük durumlarında
    minimum boyut ve sarma yüksekliğini korur.
    """
    set_language(lang)
    try:
        app = QApplication.instance() or QApplication([])
        assert isinstance(app, QApplication)
        app.setFont(QFont("Segoe UI", 10))
        app.setStyleSheet(_STYLE)

        cfg = _write_demo_config(tmp_path)
        window = build_window(cfg, demo=True, app=app)
        window.resize(1280, 780)
        window.show()
        _pump(app, 50)
        window.pet.hide()

        panel = window.settings_panel
        # Demo modunda reload_servers erken döndüğü için kayıtlı ses modellerini
        # açıkça yüklüyoruz; böylece `tts_cloud_saved` hem manuel hem kayıtlı
        # (ücretli/ücretsiz) durumlarına geçebilir.
        panel._demo = False
        panel.tts_cloud_box.reload_servers()
        panel._demo = True

        window.tabs.setCurrentWidget(window.settings_scroll)
        _pump(app, 20)

        panel.select_section("Yanıt (Çıkış)")
        _pump(app, 50)

        panel.tts_combo.setCurrentIndex(panel.tts_combo.findData("cloud"))
        _pump(app, 50)

        page = panel.stack.currentWidget()
        assert page is not None

        def select_saved(data: str) -> None:
            idx = panel.tts_cloud_saved.findData(data)
            assert idx >= 0, f"Kayıtlı model bulunamadı: {data!r}"
            panel.tts_cloud_saved.setCurrentIndex(idx)
            _pump(app, 50)

        # 1) Varsayılan: Elle ayarla ("").
        select_saved("")
        _assert_no_squeeze(page, f"{lang}:1-manual")

        # 2) Kayıtlı ücretli model + ücretli onay kutusunu aç/kapat.
        select_saved("tts_paid")
        _assert_no_squeeze(page, f"{lang}:2-saved-paid")
        panel.tts_cloud_paid.setChecked(True)
        _pump(app, 50)
        _assert_no_squeeze(page, f"{lang}:2-saved-paid-checked")
        panel.tts_cloud_paid.setChecked(False)
        _pump(app, 50)
        _assert_no_squeeze(page, f"{lang}:2-saved-paid-unchecked")

        # 3) Kayıtlı ücretsiz model.
        select_saved("tts_free")
        _assert_no_squeeze(page, f"{lang}:3-saved-free")

        # 4) Tekrar Elle ayarla.
        select_saved("")
        _assert_no_squeeze(page, f"{lang}:4-back-to-manual")

        # 5) Tekrar kayıtlı ücretli model.
        select_saved("tts_paid")
        _assert_no_squeeze(page, f"{lang}:5-saved-paid-again")
    finally:
        set_language("tr")
