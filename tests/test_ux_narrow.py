"""Dar pencerede Ayarlar: yatay taşma ve kırpılan düğme yok (UX-01/UX-12).

Ölçüm gerçek Windows yazı tipleriyle yapılır: offscreen Qt'nin yedek yazı tipi harfleri ~2 kat
geniş çizer ve kimsenin görmediği bir taşma raporlar. QT_QPA_FONTDIR Qt başlamadan önce
verilmeli, bu yüzden ölçüm ayrı süreçte koşar (aynı dosya `python tests/test_ux_narrow.py`).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FONTS = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "Fonts"


@pytest.fixture(autouse=True)
def _restore_language() -> Iterator[None]:
    """Her testten sonra dili varsayılan Türkçeye döndürür."""
    from artemis.i18n import set_language

    set_language("tr")
    try:
        yield
    finally:
        set_language("tr")


def _section_problems(panel: object, scroll: object, app: object, label: str) -> list[str]:
    """Tek Ayarlar sayfasında yatay taşma ve kırpılan düğme denetler."""
    from PySide6.QtWidgets import QPushButton

    for _ in range(4):
        app.processEvents()  # type: ignore[attr-defined]
    problems: list[str] = []
    bar = scroll.horizontalScrollBar().maximum()  # type: ignore[attr-defined]
    if bar:
        problems.append(f"{label}: yatay taşma {bar}")
    page = panel.stack.currentWidget()  # type: ignore[attr-defined]
    for button in page.findChildren(QPushButton):
        if button.isVisibleTo(page) and button.width() < button.sizeHint().width():
            problems.append(f"{label}: '{button.text()}' kırpıldı")
    return problems


def narrow_problems(window: object, app: object) -> list[str]:
    """Her Ayarlar bölümü için taşma ve kırpılan düğme bildirir."""
    panel = window.settings_panel  # type: ignore[attr-defined]
    scroll = panel.sections.scroll_area
    problems: list[str] = []
    names = panel.section_names()
    for index, name in enumerate(names):
        panel.show_section(index)
        problems.extend(_section_problems(panel, scroll, app, name))
    # Güncelleme bölümünde tüm düğmeler (denetle/aç/indir/iptal) açıkken de sarma taşmamalı.
    about_idx = next((i for i, n in enumerate(names) if n in ("Hakkında", "About")), None)
    if about_idx is not None:
        panel.show_section(about_idx)
        upd = panel.update_section
        for btn in (upd.open_button, upd.install_button, upd.cancel_button):
            btn.setVisible(True)
        problems.extend(_section_problems(panel, scroll, app, f"{names[about_idx]} (tüm düğmeler)"))
    return problems


def _measure(width: int, height: int, lang: str = "tr") -> list[str]:
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from PySide6.QtWidgets import QApplication

    from artemis.channels.desktop.app import build_window
    from artemis.i18n import set_language

    set_language(lang)
    app = QApplication.instance() or QApplication([])
    window = build_window(Path(tempfile.mkdtemp()) / "ignored.yaml", demo=True, app=app)
    try:
        window.pet.hide()
        window.resize(width, height)
        window.show()
        for _ in range(4):
            app.processEvents()
        window.tabs.setCurrentWidget(window.settings_scroll)
        for _ in range(4):
            app.processEvents()
        return narrow_problems(window, app)
    finally:
        window._request_exit()
        for _ in range(400):
            if window.bridge.stopped:
                break
            app.processEvents()
        set_language("tr")


@pytest.mark.parametrize("lang", ["tr", "en"])
@pytest.mark.parametrize("size", [(768, 1024), (720, 560)])
def test_settings_fit_narrow_window(size: tuple[int, int], lang: str) -> None:
    pytest.importorskip("PySide6")
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONPATH": str(ROOT)}
    if FONTS.is_dir():
        env["QT_QPA_FONTDIR"] = str(FONTS)
    done = subprocess.run(
        [sys.executable, __file__, str(size[0]), str(size[1]), lang],
        capture_output=True, text=True, env=env, cwd=ROOT, timeout=120, check=False,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    assert json.loads(done.stdout.splitlines()[-1]) == []


if __name__ == "__main__":
    chosen_lang = sys.argv[3] if len(sys.argv) > 3 else "tr"
    print(
        json.dumps(
            _measure(int(sys.argv[1]), int(sys.argv[2]), chosen_lang),
            ensure_ascii=False,
        )
    )
