"""README ekran görüntülerini gerçek demo penceresinden yeniden üretir.

Kullanım: python scripts/capture_screenshots.py

Qt offscreen çalışır; sahte sağlayıcı, geçici bellek, ağ/mikrofon/kayıt defteri yok.
Pet ayarları (QSettings) geçici dizine yönlendirilir; çıktı docs/assets/desktop/ altına yazılır.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import uuid
from collections.abc import Callable
from pathlib import Path

# Qt içe aktarılmadan önce: ekran/pencere sistemi gerekmez.
os.environ["QT_QPA_PLATFORM"] = "offscreen"
# Offscreen eklentisi sistem yazı tiplerini kendiliğinden görmez (Türkçe harfler kutu çıkar).
if sys.platform == "win32":
    os.environ.setdefault("QT_QPA_FONTDIR", os.environ.get("SYSTEMROOT", r"C:\Windows") + r"\Fonts")

from PySide6.QtCore import QPoint, QRect, QSettings, Qt  # noqa: E402
from PySide6.QtGui import QColor, QFont, QGuiApplication, QPainter, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication, QTabWidget  # noqa: E402

from artemis.channels.desktop.app import _STYLE, ApprovalDialog, build_window  # noqa: E402
from artemis.channels.desktop.pet import BODY, PetWidget  # noqa: E402
from artemis.channels.desktop.projects_panel import ProjectsPanel  # noqa: E402
from artemis.tools.base import ToolResult  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "docs" / "assets" / "desktop"


def wait(app: QApplication, condition: Callable[[], bool], timeout: float = 5.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if condition():
            return
        time.sleep(0.005)
    raise TimeoutError("GUI durumu zamanında oluşmadı")


def save(widget_or_pixmap: object, name: str) -> None:
    pixmap = widget_or_pixmap if isinstance(widget_or_pixmap, QPixmap) else widget_or_pixmap.grab()  # type: ignore[attr-defined]
    if not pixmap.save(str(OUT / name), "PNG"):
        raise OSError(f"{name} yazılamadı")
    print(f"{name}: {pixmap.width()}x{pixmap.height()}")


def make_pet(app: QApplication, ini: Path, **state: object) -> PetWidget:
    """Tek durumlu pet; animasyon, sabit adımlarla yakınsayana dek ilerletilir."""
    pet = PetWidget(
        on_toggle_voice=lambda: None,
        on_show_panel=lambda: None,
        on_quick=lambda: None,
        on_approval=lambda: None,
        on_quit=lambda: None,
        settings=QSettings(str(ini), QSettings.Format.IniFormat),
        reduced_motion=True,  # rastgele göz kırpma/bakış yok: çıktı tekrarlanabilir
    )
    if state.get("voice"):
        pet.set_voice(str(state["voice"]))
    pet.set_busy(bool(state.get("busy")))
    pet.set_approval(bool(state.get("approval")))
    if state.get("say"):
        # Balon ekranın ortasına bakan yana açılır; sağ kenarda sola açılır (eski görüntü gibi).
        area = QGuiApplication.primaryScreen().availableGeometry()
        pet.move_anchor(QPoint(area.right() - BODY, area.top() + 50))
        pet.say(str(state["say"]), seconds=999)
    for _ in range(180):
        pet.tick(1 / 60)
    return pet


def capture_pet(app: QApplication, tmp: Path) -> None:
    cells = [
        ("Hazır", {"voice": "idle"}),
        ("Dinliyor", {"voice": "listening"}),
        ("Düşünüyor", {"busy": True}),
        ("Onay bekliyor", {"approval": True}),
        ("Yanıt balonu", {"voice": "idle", "say": "Masaüstünde notlar.txt oluşturdum."}),
    ]
    canvas = QPixmap(960, 198)
    canvas.fill(QColor("#0B1220"))
    painter = QPainter(canvas)
    painter.setFont(QFont("Segoe UI", 10))
    painter.setPen(QColor("#A9B8C9"))
    x = 24
    for i, (label, state) in enumerate(cells):
        pet = make_pet(app, tmp / f"pet{i}.ini", **state)
        shot = pet.grab()
        painter.drawPixmap(x, 24, shot)
        painter.drawText(QRect(x, 148, shot.width(), 20), Qt.AlignmentFlag.AlignHCenter, label)
        x += 144
        pet.close()
    painter.end()
    save(canvas, "pet.png")


def capture_projects(app: QApplication) -> None:
    panel = ProjectsPanel(demo=True)
    spec = {
        "spec_id": str(uuid.UUID(int=1)),
        "project": "ornek-notlar",
        "task": "README için kısa bir kullanım bölümü ekle",
        "scope": ["Kurulum adımlarını açıkla", "Mevcut başlık düzenini koru"],
        "acceptance": ["Türkçe anlatım", "Markdown bağlantıları geçerli"],
    }
    panel.set_busy(False)
    panel.accept_result(
        "code_agent_status",
        ToolResult(
            success=True,
            status="ok",
            message="Sahte örnek durum; gerçek iş sorgulanmadı.",
            data={"projects": ["ornek-notlar"], "drafts": [spec], "jobs": []},
        ),
    )
    panel.set_busy(False)
    tabs = QTabWidget()
    tabs.addTab(panel, "Projeler")
    tabs.setStyleSheet(_STYLE)
    tabs.resize(1080, 920)
    tabs.show()
    app.processEvents()
    save(tabs, "projects.png")
    tabs.close()


def main() -> int:
    app = QApplication.instance() or QApplication([])
    assert isinstance(app, QApplication)
    app.setFont(QFont("Segoe UI", 10))
    app.setStyleSheet(_STYLE)
    with tempfile.TemporaryDirectory(prefix="artemis-shots-") as name:
        tmp = Path(name)
        # Pet konumu/görünürlüğü gerçek kullanıcı ayarlarına bakmasın ya da yazmasın.
        QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, name)
        OUT.mkdir(parents=True, exist_ok=True)

        window = build_window(tmp / "ignored.yaml", demo=True, app=app)
        # 1180 px'te başlık çubuğu (Oto onay düğmesi eklendi) "DEMO" rozetini kırpıyor.
        window.resize(1280, 780)
        window.show()
        wait(app, lambda: window.status_label.text() == "Hazır")
        window.pet.hide()
        app.processEvents()
        save(window, "welcome.png")

        assert window._submit_text("örnek akış")
        wait(app, lambda: isinstance(window._approval_dialog, ApprovalDialog))
        dialog = window._approval_dialog
        assert dialog is not None
        app.processEvents()
        save(dialog, "approval.png")
        dialog.yes_button.click()
        wait(app, lambda: not window._busy)
        window.pet.hide()
        app.processEvents()
        save(window, "panel.png")

        window.overlay.set_busy(False, "Hazır")
        window.overlay.show()
        app.processEvents()
        save(window.overlay, "overlay.png")
        window.overlay.hide()

        capture_pet(app, tmp)
        capture_projects(app)

        window._request_exit()
        wait(app, lambda: window.bridge.stopped)
        wait(app, lambda: window._temporary is None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
