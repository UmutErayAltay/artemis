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
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FONTS = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "Fonts"


def narrow_problems(window: object, app: object) -> list[str]:
    """Her Ayarlar bölümü için taşma ve kırpılan düğme bildirir."""
    from PySide6.QtWidgets import QPushButton

    panel = window.settings_panel  # type: ignore[attr-defined]
    scroll = panel.sections.scroll_area
    problems: list[str] = []
    for index, name in enumerate(panel.section_names()):
        panel.show_section(index)
        for _ in range(4):
            app.processEvents()  # type: ignore[attr-defined]
        bar = scroll.horizontalScrollBar().maximum()
        if bar:
            problems.append(f"{name}: yatay taşma {bar}")
        page = panel.stack.currentWidget()
        for button in page.findChildren(QPushButton):
            if button.isVisibleTo(page) and button.width() < button.sizeHint().width():
                problems.append(f"{name}: '{button.text()}' kırpıldı")
    return problems


def _measure(width: int, height: int) -> list[str]:
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from PySide6.QtWidgets import QApplication

    from artemis.channels.desktop.app import build_window

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


@pytest.mark.parametrize("size", [(768, 1024), (720, 560)])
def test_settings_fit_narrow_window(size: tuple[int, int]) -> None:
    pytest.importorskip("PySide6")
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONPATH": str(ROOT)}
    if FONTS.is_dir():
        env["QT_QPA_FONTDIR"] = str(FONTS)
    done = subprocess.run(
        [sys.executable, __file__, str(size[0]), str(size[1])],
        capture_output=True, text=True, env=env, cwd=ROOT, timeout=120, check=False,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    assert json.loads(done.stdout.splitlines()[-1]) == []


if __name__ == "__main__":
    print(json.dumps(_measure(int(sys.argv[1]), int(sys.argv[2])), ensure_ascii=False))
