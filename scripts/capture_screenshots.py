"""README ekran görüntülerini gerçek demo penceresinden yeniden üretir.

Kullanım:
    python scripts/capture_screenshots.py [--scale 2] [--lang en] [--out DİZİN] [--extra]

Qt offscreen çalışır; sahte sağlayıcı, geçici bellek, ağ/mikrofon/kayıt defteri yok.
Pet ayarları (QSettings) geçici dizine yönlendirilir; varsayılan çıktı docs/assets/desktop/
altına yazılır.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import shutil
import sys
import tempfile
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PySide6.QtWidgets import QApplication

    from artemis.channels.desktop.app import DesktopWindow
    from artemis.channels.desktop.pet import PetWidget

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "docs" / "assets" / "desktop"
DEMO_CONFIG_DIR = r"C:\Users\demo\AppData\Roaming\Artemis"
DEMO_REPOS_DIR = r"C:\Users\demo\Desktop\Projeler"

_EXTRA_SECTIONS: tuple[tuple[str, str], ...] = (
    ("Model", "settings-model.png"),
    ("Rutinler", "settings-routines.png"),
    ("Takvim ve posta", "settings-calendar.png"),
    ("Görünüm", "settings-look.png"),
    ("Hakkında", "settings-about.png"),
)


def _positive_scale(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"Geçersiz ölçek değeri: {raw!r}") from error
    if value <= 0:
        raise argparse.ArgumentTypeError(f"Ölçek sıfırdan büyük olmalı: {raw!r}")
    return value


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Komut satırı seçeneklerini ayrıştırır."""
    parser = argparse.ArgumentParser(
        description="Artemis masaüstü ekran görüntülerini yakalar.",
    )
    parser.add_argument(
        "--scale",
        type=_positive_scale,
        default=1,
        help="Qt ölçek katsayısı (QT_SCALE_FACTOR, varsayılan: 1).",
    )
    parser.add_argument(
        "--lang",
        choices=("tr", "en"),
        default="tr",
        help="Arayüz dili (varsayılan: tr).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=OUT,
        help="PNG çıktılarının yazılacağı dizin (varsayılan: docs/assets/desktop).",
    )
    parser.add_argument(
        "--extra",
        action="store_true",
        help="Ek ayar sayfalarını ve ConnectWizard sihirbazını da yakalar.",
    )
    return parser.parse_args(argv)


def _format_scale(scale: float) -> str:
    return str(int(scale)) if scale == int(scale) else str(scale)


def _configure_qt_env(scale: float) -> None:
    """PySide6 içe aktarılmadan önce offscreen, yazı tipi ve HiDPI ayarlarını uygular."""
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    # Offscreen eklentisi sistem yazı tiplerini kendiliğinden görmez (Türkçe harfler kutu çıkar).
    if sys.platform == "win32":
        os.environ.setdefault(
            "QT_QPA_FONTDIR",
            os.environ.get("SYSTEMROOT", r"C:\Windows") + r"\Fonts",
        )
    os.environ["QT_SCALE_FACTOR"] = _format_scale(scale)


def wait(app: QApplication, condition: Callable[[], bool], timeout: float = 5.0) -> None:
    """Koşul sağlanana dek Qt olay döngüsünü ilerletir."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if condition():
            return
        time.sleep(0.005)
    raise TimeoutError("GUI durumu zamanında oluşmadı")


def save(widget_or_pixmap: object, name: str, out_dir: Path | None = None) -> None:
    """Bileşeni veya QPixmap nesnesini hedef dizine PNG olarak kaydeder."""
    from PySide6.QtGui import QPixmap

    target_dir = out_dir if out_dir is not None else OUT
    pixmap = (
        widget_or_pixmap
        if isinstance(widget_or_pixmap, QPixmap)
        else widget_or_pixmap.grab()  # type: ignore[attr-defined]
    )
    if not pixmap.save(str(target_dir / name), "PNG"):
        raise OSError(f"{name} yazılamadı")
    print(f"{name}: {pixmap.width()}x{pixmap.height()}")


def make_pet(app: QApplication, ini: Path, **state: object) -> PetWidget:
    """Tek durumlu pet; animasyon, sabit adımlarla yakınsayana dek ilerletilir."""
    del app
    from PySide6.QtCore import QPoint, QSettings
    from PySide6.QtGui import QGuiApplication

    from artemis.channels.desktop.pet import BODY, PetWidget

    pet = PetWidget(
        on_toggle_voice=lambda: None,
        on_show_panel=lambda: None,
        on_quick=lambda: None,
        on_approval=lambda: None,
        on_quit=lambda: None,
        on_submit=lambda text: True,
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


def capture_pet(
    app: QApplication,
    tmp: Path,
    *,
    lang: str = "tr",
    out_dir: Path | None = None,
) -> None:
    """Pet durum şeridini (Hazır, Dinliyor, Düşünüyor, Onay, Yanıt balonu) yakalar."""
    from PySide6.QtCore import QRect, Qt
    from PySide6.QtGui import QColor, QFont, QPainter, QPixmap

    from artemis.i18n import t

    reply_label = "Reply bubble" if lang == "en" else "Yanıt balonu"
    say_text = (
        "Created notes.txt on the desktop."
        if lang == "en"
        else "Masaüstünde notlar.txt oluşturdum."
    )
    cells: list[tuple[str, dict[str, object]]] = [
        (t("Hazır"), {"voice": "idle"}),
        (t("Dinliyor"), {"voice": "listening"}),
        (t("Düşünüyor"), {"busy": True}),
        (t("Onay bekliyor"), {"approval": True}),
        (reply_label, {"voice": "idle", "say": say_text}),
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
    save(canvas, "pet.png", out_dir=out_dir)


def capture_projects(
    app: QApplication,
    *,
    lang: str = "tr",
    out_dir: Path | None = None,
) -> None:
    """Projeler sekmesini sahte taslak ve proje durumuyla yakalar."""
    from PySide6.QtWidgets import QTabWidget

    from artemis.channels.desktop.app import _STYLE
    from artemis.channels.desktop.projects_panel import ProjectsPanel
    from artemis.i18n import t
    from artemis.tools.base import ToolResult

    panel = ProjectsPanel(demo=True)
    if lang == "en":
        project_name = "sample-notes"
        spec = {
            "spec_id": str(uuid.UUID(int=1)),
            "project": project_name,
            "task": "Add a short usage section to the README",
            "scope": ["Explain installation steps", "Keep existing heading structure"],
            "acceptance": ["Clear English wording", "Valid Markdown links"],
        }
        status_message = "Mock sample status; no real job was queried."
    else:
        project_name = "ornek-notlar"
        spec = {
            "spec_id": str(uuid.UUID(int=1)),
            "project": project_name,
            "task": "README için kısa bir kullanım bölümü ekle",
            "scope": ["Kurulum adımlarını açıkla", "Mevcut başlık düzenini koru"],
            "acceptance": ["Türkçe anlatım", "Markdown bağlantıları geçerli"],
        }
        status_message = "Sahte örnek durum; gerçek iş sorgulanmadı."
    panel.set_busy(False)
    panel.accept_result(
        "code_agent_status",
        ToolResult(
            success=True,
            status="ok",
            message=status_message,
            data={"projects": [project_name], "drafts": [spec], "jobs": []},
        ),
    )
    panel.set_busy(False)
    tabs = QTabWidget()
    tabs.addTab(panel, t("Projeler"))
    tabs.setStyleSheet(_STYLE)
    tabs.resize(1080, 920)
    tabs.show()
    app.processEvents()
    save(tabs, "projects.png", out_dir=out_dir)
    tabs.close()


def _select_settings_section(panel: object, title: str) -> int:
    select = getattr(panel, "select_section", None)
    if callable(select):
        if not select(title):
            raise RuntimeError(f"Ayar bölümü bulunamadı: {title}")
        return int(panel.sections.currentRow())  # type: ignore[attr-defined]
    from artemis.i18n import t

    sections = panel.sections  # type: ignore[attr-defined]
    rows = [i for i in range(sections.count()) if sections.item(i).text() == t(title)]
    if not rows:
        raise RuntimeError(f"Ayar bölümü bulunamadı: {title}")
    sections.setCurrentRow(rows[0])
    return rows[0]


def neutralize_captured_paths(panel: object) -> None:
    """Görüntülerde gerçek kullanıcı/geçici yol görünmesin diye kurmaca yol yazar."""
    from artemis.i18n import t

    about = getattr(panel, "about_section", None)
    if about is not None and hasattr(about, "config_label"):
        about.config_label.setText(
            t("Ayar klasörü: {path}").format(path=DEMO_CONFIG_DIR)
        )
    repos_dir = getattr(panel, "repos_dir", None)
    if repos_dir is not None and hasattr(repos_dir, "setPlaceholderText"):
        repos_dir.setPlaceholderText(DEMO_REPOS_DIR)


def capture_settings(
    app: QApplication,
    window: DesktopWindow,
    *,
    extra: bool = False,
    tmp: Path | None = None,
    out_dir: Path | None = None,
) -> None:
    """Ayarlar sayfalarını, Oto onay risk uyarısını ve istenirse ConnectWizard'ı yakalar."""
    from PySide6.QtWidgets import QScrollArea

    from artemis.channels.desktop.connect_wizard import ConnectWizard

    panel = window.settings_panel
    neutralize_captured_paths(panel)
    window.tabs.setCurrentWidget(window.settings_scroll)
    for title, name in (("Ses", "settings-ses.png"), ("Telegram", "settings-telegram.png")):
        _select_settings_section(panel, title)
        if title == "Ses":
            if hasattr(panel, "voice_tabs") and panel.voice_tabs.count() > 1:
                panel.voice_tabs.setCurrentIndex(1)
            # Karma tanıma alanları kutu işaretlenince açılır; sayfa o bölüme kaydırılır.
            panel.hybrid_stt.setChecked(True)
            app.processEvents()
            area = panel.cloud_box.parentWidget()
            while area is not None and not isinstance(area, QScrollArea):
                area = area.parentWidget()
            if area is not None:
                area.ensureWidgetVisible(panel.cloud_box, 0, 120)
        app.processEvents()
        save(window, name, out_dir=out_dir)

    if extra:
        for title, name in _EXTRA_SECTIONS:
            _select_settings_section(panel, title)
            app.processEvents()
            save(window, name, out_dir=out_dir)

    window.tabs.setCurrentIndex(0)
    box, _yes = window._auto_mode_box()
    box.show()
    app.processEvents()
    save(box, "auto-warning.png", out_dir=out_dir)
    box.close()

    if extra:
        with tempfile.TemporaryDirectory(prefix="artemis-wiz-", dir=tmp) as wiz_dir:
            cfg_dir = Path(wiz_dir) / "cfg"
            cfg_dir.mkdir(parents=True, exist_ok=True)
            cfg = cfg_dir / "config.yaml"
            shutil.copy(REPO / "config" / "config.example.yaml", cfg)
            wiz = ConnectWizard(cfg, local_probe=lambda: [], open_url=lambda _u: None)
            try:
                wiz.resize(760, 640)
                wiz.show()
                app.processEvents()
                save(wiz, "connect-wizard.png", out_dir=out_dir)
            finally:
                wiz.close()


def hide_real_credentials() -> None:
    """Görüntüde gerçek kullanıcının kayıtlı token/anahtar durumu görünmesin (kasa okunmaz)."""
    import artemis.brain.providers as providers
    import artemis.channels.telegram.api as telegram_api

    providers.read_secret = lambda *_args, **_kw: None  # type: ignore[assignment]
    telegram_api.read_secret = lambda *_args, **_kw: None  # type: ignore[assignment]


def main(argv: list[str] | None = None) -> int:
    """Ekran görüntülerini belirtilen ölçek, dil ve çıktı dizinine göre üretir."""
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    args = parse_args(argv)
    _configure_qt_env(args.scale)

    from PySide6.QtCore import QSettings
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication

    from artemis.channels.desktop.app import _STYLE, ApprovalDialog, build_window
    from artemis.i18n import set_language, t

    set_language(args.lang)
    out_dir = args.out.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    hide_real_credentials()
    app = QApplication.instance() or QApplication([])
    assert isinstance(app, QApplication)
    app.setFont(QFont("Segoe UI", 10))
    app.setStyleSheet(_STYLE)
    with tempfile.TemporaryDirectory(prefix="artemis-shots-") as name:
        tmp = Path(name)
        # Pet konumu/görünürlüğü gerçek kullanıcı ayarlarına bakmasın ya da yazmasın.
        QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, name)

        # Telegram sayfası config'i okur; örnek config yoksa "Ayarlar okunamadı" görünür.
        # Alt klasöre konur ki örnek config'teki ../desktop ve ../.artemis tmp içinde kalsın.
        cfg_dir = tmp / "cfg"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        demo_config = cfg_dir / "demo.yaml"
        demo_config.write_bytes((REPO / "config" / "config.example.yaml").read_bytes())
        window = build_window(demo_config, demo=True, app=app)
        try:
            # 1180 px'te başlık çubuğu (Oto onay düğmesi eklendi) "DEMO" rozetini kırpıyor.
            window.resize(1280, 780)
            window.show()
            wait(app, lambda: window.status_label.text() == t("Hazır"))
            window.pet.hide()
            app.processEvents()
            save(window, "welcome.png", out_dir=out_dir)

            demo_prompt = "Sample flow" if args.lang == "en" else "örnek akış"
            assert window._submit_text(demo_prompt)
            wait(app, lambda: isinstance(window._approval_dialog, ApprovalDialog))
            dialog = window._approval_dialog
            assert dialog is not None
            app.processEvents()
            save(dialog, "approval.png", out_dir=out_dir)
            dialog.yes_button.click()
            wait(app, lambda: not window._busy)
            window.pet.hide()
            app.processEvents()
            save(window, "panel.png", out_dir=out_dir)

            window.overlay.set_busy(False, t("Hazır"))
            window.overlay.show()
            app.processEvents()
            save(window.overlay, "overlay.png", out_dir=out_dir)
            window.overlay.hide()

            capture_settings(app, window, extra=args.extra, tmp=tmp, out_dir=out_dir)
            capture_pet(app, tmp, lang=args.lang, out_dir=out_dir)
            capture_projects(app, lang=args.lang, out_dir=out_dir)
        finally:
            window._request_exit()
            with contextlib.suppress(TimeoutError):
                wait(app, lambda: window.bridge.stopped)
                wait(app, lambda: window._temporary is None)
            if window._temporary is not None:
                window._temporary.cleanup()
                window._temporary = None
    return 0


if __name__ == "__main__":
    sys.exit(main())
