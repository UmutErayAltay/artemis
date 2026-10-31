"""Masaüstü pet (ADR 0023): köşede duran, Artemis'in ne yaptığını gösteren küçük karakter.

Pet'in komut ya da onay mantığı yoktur: durumu `DesktopWindow` besler, tık ve menü
seçimleri yine pencerenin mevcut yöntemlerine döner. Varsayılan görünüm QPainter küredir
(depoda görsel dosya yok); ADR 0048 ile kullanıcı Codex uyumlu sprite pet seçebilir
(`pet_sprite.py`). Durum, balon, konum ve zamanlayıcı ortaktır; yalnız çizim ve kare
ilerletme `self._sprite` varlığına göre ayrılır. Zamanlayıcı yalnız pencere görünürken çalışır.
"""

from __future__ import annotations

import logging
import math
import os
import random
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, QSettings, Qt, QTimer
from PySide6.QtGui import (
    QBrush,
    QColor,
    QContextMenuEvent,
    QCursor,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
    QPolygonF,
    QRadialGradient,
)
from PySide6.QtWidgets import QApplication, QMenu, QWidget

from artemis.channels.desktop import pet_sprite, themes
from artemis.channels.desktop.pet_quick import QuickBox
from artemis.channels.desktop.pet_sprite import SpritePet

log = logging.getLogger(__name__)

BODY = 120  # karakter alanı (px); yuvarlak gövde bunun içinde ~80 px
BUBBLE_W = 210
GAP = 6
MARGIN = 24
BUBBLE_SECONDS = 6.0
BUBBLE_LIMIT = 140

_RADIUS = 40.0
_CENTER_Y = 60.0

DESCRIPTIONS = {
    "approval": "Onayını bekliyor",
    "speaking": "Konuşuyor",
    "listening": "Seni dinliyor",
    "thinking": "Düşünüyor",
    "error": "Bir hata oldu",
    "off": "Uyuyor, ses kapalı",
    "idle": "Hazır",
}


def os_reduced_motion() -> bool:
    """Windows "istemci alanı animasyonları" kapalıysa hareket azaltılır (ADR 0048 madde 7)."""
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes

    enabled = wintypes.BOOL(True)
    ok = ctypes.windll.user32.SystemParametersInfoW(  # SPI_GETCLIENTAREAANIMATION
        0x1042, 0, ctypes.byref(enabled), 0
    )
    return bool(ok) and not enabled.value


def level_from_message(message: str) -> float | None:
    """Köşe `voice_level` iletisi (0..100 tam sayı metni) -> 0..1; geçersizse None."""
    if not message.isdigit():
        return None
    return min(100, int(message)) / 100


def truncate(text: str, limit: int = BUBBLE_LIMIT) -> str:
    """Boşlukları sadeleştirir; uzun metni '…' ile keser (balon düz metindir)."""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def commit_count(commits: Any) -> int:
    if isinstance(commits, (list, tuple)):
        return len(commits)
    if isinstance(commits, int) and not isinstance(commits, bool):
        return max(0, commits)
    return 0


def job_summary(project: str, status: str, commits: Any) -> str:
    name = truncate(project or "kod ajanı", 40)
    if status != "done":
        return f"{name}: hata"
    count = commit_count(commits)
    return f"{name}: bitti, {count} commit" if count else f"{name}: bitti, commit yok"


def clamp_anchor(pos: QPoint, area: QRect) -> QPoint:
    """Gövde alanının tamamı verilen ekran alanında kalır."""
    x = max(area.left(), min(pos.x(), area.right() + 1 - BODY))
    y = max(area.top(), min(pos.y(), area.bottom() + 1 - BODY))
    return QPoint(x, y)


def _ease(current: float, target: float, dt: float, rate: float) -> float:
    return current + (target - current) * (1 - math.exp(-rate * dt))


def _mix(a: QColor, b: QColor, k: float) -> QColor:
    return QColor(
        round(a.red() + (b.red() - a.red()) * k),
        round(a.green() + (b.green() - a.green()) * k),
        round(a.blue() + (b.blue() - a.blue()) * k),
    )


def _alpha(color: QColor, alpha: float) -> QColor:
    out = QColor(color)
    out.setAlphaF(max(0.0, min(1.0, alpha)))
    return out


@dataclass
class _Particle:
    """Gövde merkezine göre konumlanan kısa ömürlü süs: uyku "z"si ya da kıvılcım."""

    kind: str
    x: float
    y: float
    vx: float
    vy: float
    life: float
    age: float = 0.0


class PetWidget(QWidget):
    def __init__(
        self,
        *,
        on_toggle_voice: Callable[[], None],
        on_show_panel: Callable[[], None],
        on_quick: Callable[[], None],
        on_approval: Callable[[], None],
        on_quit: Callable[[], None],
        on_submit: Callable[[str], bool],
        settings: QSettings | None = None,
        reduced_motion: bool | None = None,
        pets_dir: Path | None = None,
    ) -> None:
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self._on_toggle_voice = on_toggle_voice
        self._on_show_panel = on_show_panel
        self._on_quick = on_quick
        self._on_approval = on_approval
        self._on_quit = on_quit
        self._settings = settings if settings is not None else QSettings(
            # Ini biçimi: testler setPath ile tmp'ye yönlendirebilir; kayıt defteri kirlenmez.
            QSettings.Format.IniFormat, QSettings.Scope.UserScope, "Artemis", "pet"
        )
        if reduced_motion is None:
            # Hareket azaltma: açık ayar (ortam değişkeni / QSettings "reduced_motion")
            # kazanır; ayar yoksa işletim sistemi tercihi okunur.
            explicit = os.environ.get("ARTEMIS_REDUCE_MOTION") == "1" or bool(
                self._settings.value("reduced_motion", False, type=bool)
            )
            reduced_motion = explicit or (
                not self._settings.contains("reduced_motion") and os_reduced_motion()
            )
        self._reduced = reduced_motion
        # Sprite görünümü (None = küre); ADR 0048. `skin` seçili olanın kimliğidir.
        self._pets_dir = pets_dir
        self._sprite: SpritePet | None = None
        self._skin = pet_sprite.ORB
        self._frame_t = 0.0
        self._sprite_row = pet_sprite.IDLE
        self._run_right = True

        # Dış durum (DesktopWindow besler)
        self._voice = "off"
        self._busy = False
        self._approval = False
        self._level_raw = 0.0
        # job_id -> (proje, "running" | "waiting"); biten iş balonda bir kez gösterilip düşer.
        self._jobs: dict[str, tuple[str, str]] = {}
        self._gear_hover = False
        self._reaction: str | None = None
        self._react_age = 0.0
        self._error_left = 0.0
        self._awake = 0.0  # kapalı sesle bile bir olay olunca kısa süre uyanık görünür

        # Animasyon durumu (hepsi hedefe yumuşakça yaklaşır)
        self._t = 0.0
        self._last = 0.0
        self._level = 0.0
        self._eye_open = 0.4
        self._smile = 0.3
        self._mouth_open = 0.0
        self._mouth_w = 10.0
        self._wave = 0.0
        self._gaze = QPointF(0, 0)
        self._gaze_goal = QPointF(0, 0)
        # Pet ayrı bir üst pencere: ana pencerenin stil sayfasını devralmaz, bu yüzden aktif
        # temanın jetonlarını kendisi tutar. Başlangıçta kayıtlı temayı okur (app.py'den bağımsız).
        self._theme = themes.DEFAULT_THEME
        self._pal = themes.THEMES[self._theme]
        self._tint = self._col("accent")
        self._glow = 0.1
        self._dim = 0.8
        self._happy = 0.0
        self._droop = 0.0
        self._ring = 0.0
        self._shake_age = 9.0
        self._antenna = 0.0
        self._drag_dx = 0.0
        self._blink_p: float | None = None
        self._blink_at = random.uniform(3, 7)
        self._glance_at = random.uniform(2, 5)
        self._shown_state = "off"
        self._hovered = False
        self._hover = 0.0
        self._particles: list[_Particle] = []
        self._z_at = 0.0

        # Balon
        self._bubble_text = ""
        self._bubble_kind = "answer"
        self._bubble_left = 0.0
        self._bubble_alpha = 0.0
        self._bubble_side_left = False

        # Konum ve etkileşim
        self._anchor = QPoint(0, 0)
        self._press_global: QPoint | None = None
        self._press_anchor = QPoint(0, 0)
        self._dragging = False
        self._click_delay_ms = min(QApplication.doubleClickInterval(), 280)
        self._click_timer = QTimer(self)
        self._click_timer.setSingleShot(True)
        self._click_timer.timeout.connect(self._dispatch_clicks)
        self._clicks = 0
        self._quick = QuickBox(self, on_submit)
        self.setMouseTracking(True)  # dişli rozetin üstüne gelme iş listesini açar
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._on_timer)

        saved = QSettings(
            QSettings.Format.IniFormat, QSettings.Scope.UserScope, "Artemis", "appearance"
        ).value("theme", themes.DEFAULT_THEME)
        self.apply_theme(str(saved))
        self._tint = self._col("accent")

        self.setAccessibleName("Artemis pet")
        self.setFixedSize(BODY, BODY)
        self._load_position()
        self._relayout()
        self._refresh_access()

    # ------------------------------------------------------------------ tema

    def _col(self, key: str) -> QColor:
        return QColor(self._pal[key])

    def apply_theme(self, name: str) -> None:
        """Balon, rozet ve çizili avatar renkleri aktif temanın jetonlarından gelir."""
        self._theme = name if name in themes.THEMES else themes.DEFAULT_THEME
        self._pal = themes.THEMES[self._theme]
        self._quick.apply_theme(self._pal)
        self.update()

    # ------------------------------------------------------------------ durum

    @property
    def state(self) -> str:
        """Öncelik: onay > konuşma > dinleme > düşünme > hata > kapalı > hazır."""
        if self._approval:
            return "approval"
        if self._voice == "speaking":
            return "speaking"
        if self._voice in {"listening", "awaiting_command"}:
            return "listening"
        if self._busy or self._voice in {"thinking", "preparing"}:
            return "thinking"
        if self._voice == "error" or self._error_left > 0:
            return "error"
        if self._voice == "off" and self._awake <= 0 and not self._jobs:
            return "off"
        return "idle"

    @property
    def jobs(self) -> int:
        return len(self._jobs)

    def set_voice(self, state: str) -> None:
        self._voice = state
        self._refresh_access()

    def set_level(self, level: float) -> None:
        self._level_raw = max(0.0, min(1.0, level))

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._refresh_access()

    def set_approval(self, pending: bool) -> None:
        self._approval = pending
        self._refresh_access()

    def flash_error(self) -> None:
        self._error_left = 1.4
        self._refresh_access()

    def heard(self, text: str) -> None:
        if text.strip():
            self.say(text, "heard")

    def live(self, text: str) -> None:
        """Konuşurken akan canlı yazı (ADR 0047): uzun sözde başı değil en son sözcükler görünür."""
        flat = " ".join(text.split())
        if not flat:
            return
        if len(flat) > BUBBLE_LIMIT:
            flat = "…" + flat[-(BUBBLE_LIMIT - 1):].lstrip()
        self.say(flat, "heard")

    def answer(self, text: str) -> None:
        if text.strip():
            self.say(text, "answer")

    def say(
        self, text: str, kind: str = "answer", seconds: float = BUBBLE_SECONDS, flat: bool = True
    ) -> None:
        self._bubble_text = truncate(text) if flat else text
        self._bubble_kind = kind
        self._bubble_left = seconds
        self._awake = max(self._awake, 10.0)
        self._relayout()

    def code_agent_event(self, event: str, fields: dict[str, Any]) -> str | None:
        """Başlangıç/bitiş denetim olayları; bitişte gösterilen metni döndürür."""
        job = str(fields.get("job_id") or "?")
        if event in {"code_agent_started", "code_agent_waiting"}:
            project = str(fields.get("project") or self._jobs.get(job, ("", ""))[0])
            status = "running" if event == "code_agent_started" else "waiting"
            self._jobs[job] = (project, status)
            self._awake = max(self._awake, 10.0)
            self._refresh_access()
            self._say_jobs()
            return None
        if event != "code_agent_done":
            return None
        self._jobs.pop(job, None)
        status = str(fields.get("status", ""))
        text = job_summary(str(fields.get("project", "")), status, fields.get("commits"))
        self._reaction = "happy" if status == "done" else "sad"
        self._react_age = 0.0
        if status == "done":
            self._burst()
        self.say(text, "job")
        self._refresh_access()
        return text

    def job_lines(self) -> list[str]:
        """Etkin işlerin kısa listesi: 'web · çalışıyor', 'api · girdi bekliyor'."""
        label = {"running": "çalışıyor", "waiting": "girdi bekliyor"}
        lines = [
            f"{truncate(project or 'kod ajanı', 24)} · {label[status]}"
            for project, status in self._jobs.values()
        ]
        return lines[:4] + ([f"+{len(lines) - 4} iş daha"] if len(lines) > 4 else [])

    def _say_jobs(self) -> None:
        if self._jobs:
            self.say("\n".join(self.job_lines()), "job", flat=False)

    def _refresh_access(self) -> None:
        text = DESCRIPTIONS[self.state]
        if self._jobs:
            text += f"; {len(self._jobs)} kod işi çalışıyor"
        self.setAccessibleDescription(text)
        self.setToolTip(text)

    # ------------------------------------------------------------------ konum

    def _area(self, point: QPoint) -> QRect:
        screen = QGuiApplication.screenAt(point) or QGuiApplication.primaryScreen()
        return screen.availableGeometry() if screen is not None else QRect(0, 0, 1280, 720)

    def _load_position(self) -> None:
        if self._settings.contains("x") and self._settings.contains("y"):
            pos = QPoint(
                cast(int, self._settings.value("x", 0, type=int)),
                cast(int, self._settings.value("y", 0, type=int)),
            )
        else:
            area = self._area(QPoint(0, 0))
            pos = QPoint(area.right() + 1 - BODY - MARGIN, area.bottom() + 1 - BODY - MARGIN)
        self._anchor = clamp_anchor(pos, self._area(pos))

    def move_anchor(self, pos: QPoint) -> None:
        self._anchor = clamp_anchor(pos, self._area(pos))
        self._relayout()

    def _save_position(self) -> None:
        self._settings.setValue("x", self._anchor.x())
        self._settings.setValue("y", self._anchor.y())

    def _relayout(self) -> None:
        """Balon varken pencere genişler; balon ekranın ortasına bakan yana açılır."""
        area = self._area(self._anchor)
        self._bubble_side_left = self._anchor.x() + BODY // 2 > area.center().x()
        extra = GAP + BUBBLE_W if self._bubble_text else 0
        self.setFixedSize(BODY + extra, BODY)
        shift = extra if self._bubble_side_left else 0
        self.move(self._anchor.x() - shift, self._anchor.y())
        if self._quick.isVisible():
            self._place_quick()

    def _place_quick(self) -> None:
        """Kutu gövdenin hemen altında; ekranın altına sığmazsa üstüne çıkar."""
        area = self._area(self._anchor)
        box = self._quick
        x = max(area.left(), min(self._anchor.x() + (BODY - box.width()) // 2,
                                 area.right() + 1 - box.width()))
        y = self._anchor.y() + BODY + GAP
        if y + box.height() > area.bottom() + 1:
            y = self._anchor.y() - GAP - box.height()
        box.move(x, y)

    def _body_rect(self) -> QRect:
        left = self.width() - BODY if self._bubble_side_left else 0
        return QRect(left, 0, BODY, BODY)

    # ------------------------------------------------------------ görünüm

    @property
    def skin(self) -> str:
        return self._skin

    def set_skin(self, pet_id: str | None, *, persist: bool = True) -> str | None:
        """Küre (`None`/"orb") ya da sprite pet seçer; sorun varsa küreye döner ve mesaj verir."""
        if pet_id in (None, pet_sprite.ORB):
            self._sprite, self._skin = None, pet_sprite.ORB
            if persist:
                self._settings.setValue("skin", pet_sprite.ORB)
            self.update()
            return None
        try:
            info = (
                pet_sprite.find_pet(str(pet_id), self._pets_dir)
                if self._pets_dir is not None
                else None
            )
            if info is None:
                raise pet_sprite.PetError(f"'{pet_id}' kimlikli pet bulunamadı.")
            sprite = SpritePet(info, BODY)
        except pet_sprite.PetError as error:
            log.warning("Pet yüklenemedi, küre kullanılıyor: %s", error)
            self._sprite, self._skin = None, pet_sprite.ORB
            return f"Pet yüklenemedi, küre kullanılıyor: {error}"
        self._sprite, self._skin = sprite, info.id
        self._frame_t = 0.0
        if persist:
            self._settings.setValue("skin", info.id)
        self.update()
        return None

    # ------------------------------------------------------------ görünürlük

    def start(self) -> None:
        """Kayıtlı görünürlüğe göre açılır (varsayılan görünür); kayıtlı pet yüklenir."""
        # Bozuk kayıtlı pet kaydı silmez: Codex klasörü geçici yoksa sonra yine çalışır.
        self.set_skin(str(self._settings.value("skin", pet_sprite.ORB)), persist=False)
        if self._settings.value("visible", True, type=bool):
            self.show()

    def set_pet_visible(self, visible: bool) -> None:
        self._settings.setValue("visible", visible)
        self.setVisible(visible)

    def toggle_pet(self) -> None:
        self.set_pet_visible(not self.isVisible())

    def showEvent(self, event: Any) -> None:
        super().showEvent(event)
        self._last = time.monotonic()
        self._timer.start()

    def hideEvent(self, event: Any) -> None:
        self._timer.stop()
        self._quick.hide()
        super().hideEvent(event)

    # --------------------------------------------------------------- etkileşim

    def build_menu(self) -> QMenu:
        menu = QMenu(self)
        menu.addAction("Paneli aç").triggered.connect(self._on_show_panel)
        menu.addAction("Hızlı komut").triggered.connect(self._on_quick)
        voice_on = self._voice != "off"
        menu.addAction("Sesi kapat" if voice_on else "Sesi aç").triggered.connect(
            self._on_toggle_voice
        )
        menu.addSeparator()
        menu.addAction("Peti gizle").triggered.connect(lambda: self.set_pet_visible(False))
        menu.addSeparator()
        # Tepsideki "Çıkış" ile aynı yol: işçi temiz kapanır, uygulama sonlanır.
        menu.addAction("Kapat").triggered.connect(self._on_quit)
        return menu

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:
        self._open_menu(event.globalPos())

    def enterEvent(self, event: Any) -> None:
        super().enterEvent(event)
        self._hovered = True
        self._drag_dx += 5.0  # anten selam verir gibi sallanır

    def leaveEvent(self, event: Any) -> None:
        super().leaveEvent(event)
        self._hovered = False

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            event.ignore()
            return
        if not self._body_rect().contains(event.position().toPoint()):
            self._bubble_left = 0.0  # balona tık onu kapatır
            return
        self._press_global = event.globalPosition().toPoint()
        self._press_anchor = QPoint(self._anchor)
        self._dragging = False

    def _over_gear(self, pos: QPoint) -> bool:
        body = self._body_rect()
        gear = QPointF(body.left() + BODY / 2 + _RADIUS * 0.8, _CENTER_Y - _RADIUS * 0.74)
        return bool(self._jobs) and math.hypot(pos.x() - gear.x(), pos.y() - gear.y()) <= 14

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._press_global is None:
            over = self._over_gear(event.position().toPoint())
            if over and not self._gear_hover:
                self._say_jobs()
            self._gear_hover = over
            return
        delta = event.globalPosition().toPoint() - self._press_global
        if not self._dragging and delta.manhattanLength() >= QApplication.startDragDistance():
            self._dragging = True
        if self._dragging:
            before = self._anchor.x()
            self.move_anchor(self._press_anchor + delta)
            self._drag_dx = float(self._anchor.x() - before)
            if self._drag_dx:
                self._run_right = self._drag_dx > 0

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton or self._press_global is None:
            return
        dragged = self._dragging
        self._press_global = None
        self._dragging = False
        if dragged:
            # Sürükleme tık dizisini böler: tık-sürükle-tık çift tık (ses) sayılmaz.
            self._clicks = 0
            self._click_timer.stop()
            self._save_position()
        else:
            self._clicked()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        # Qt ikinci basışı çift-tık olayı olarak yollar; sıradan basış gibi sayılır ki
        # bırakma ikinci tık olsun.
        self.mousePressEvent(event)

    def _clicked(self) -> None:
        """Tık sayacı (ADR 0048): 1 hızlı kutu, 2 ses, 3 menü; onayda tık hemen onaya gider."""
        if self._approval:
            self._clicks = 0
            self._click_timer.stop()
            self._on_approval()
            return
        self._clicks += 1
        if self._clicks >= 3:
            self._dispatch_clicks()
        else:
            self._click_timer.start(self._click_delay_ms)

    def _dispatch_clicks(self) -> None:
        self._click_timer.stop()
        count, self._clicks = self._clicks, 0
        if count == 1:
            if self._quick.isVisible():
                self._quick.hide()
            else:
                self._quick.open()
                self._place_quick()
        elif count == 2:
            self._on_toggle_voice()
        elif count >= 3:
            self._open_menu(QCursor.pos())

    def _open_menu(self, pos: QPoint) -> None:
        self.build_menu().exec(pos)

    # --------------------------------------------------------------- animasyon

    def _on_timer(self) -> None:
        now = time.monotonic()
        self.tick(now - self._last)
        self._last = now
        self.update()

    def tick(self, dt: float) -> None:
        dt = max(0.0, min(dt, 0.1))
        self._t += dt
        t = self._t
        motion = 0.0 if self._reduced else 1.0
        self._awake = max(0.0, self._awake - dt)
        self._error_left = max(0.0, self._error_left - dt)
        self._react_age += dt
        if self._reaction and self._react_age > (1.7 if self._reaction == "happy" else 2.5):
            self._reaction = None
        state = self.state
        if state != self._shown_state:
            if state == "error":
                self._shake_age = 0.0
            self._shown_state = state
            self._refresh_access()
        self._shake_age += dt

        # Balon süresi ve solması
        if self._bubble_text:
            self._bubble_left = max(0.0, self._bubble_left - dt)
            self._bubble_alpha = _ease(
                self._bubble_alpha, 1.0 if self._bubble_left > 0 else 0.0, dt, 12
            )
            if self._bubble_left <= 0 and self._bubble_alpha < 0.02:
                self._bubble_text = ""
                self._bubble_alpha = 0.0
                self._relayout()

        if self._sprite is not None:
            self._tick_sprite(dt, state)
        else:
            self._tick_orb(dt, state, t, motion)
        self._hover = _ease(self._hover, 1.0 if self._hovered else 0.0, dt, 10)
        self._drag_dx = _ease(self._drag_dx, 0.0, dt, 12)

    def _tick_sprite(self, dt: float, state: str) -> None:
        row = pet_sprite.row_for(
            state, self._reaction, self._react_age, self._dragging, self._run_right
        )
        if row != self._sprite_row:
            self._sprite_row, self._frame_t = row, 0.0
        self._frame_t += dt * (0.5 if state == "off" else 1.0)  # uyurken yavaş
        self._dim = _ease(self._dim, 0.78 if state == "off" else 1.0, dt, 4)

    def _tick_orb(self, dt: float, state: str, t: float, motion: float) -> None:
        # Hedefler
        eye, smile, mopen, mw, wave = 1.0, 0.35, 0.0, 10.0, 0.0
        gaze = self._gaze_goal
        color, glow, dim, happy, droop = self._col("accent"), 0.14, 1.0, 0.0, 0.0
        if state == "off":
            eye, smile, mw, glow, dim = 0.42, 0.1, 8.0, 0.04, 0.78
            gaze = QPointF(0, 0.5)
        elif state == "listening":
            eye, smile, mopen, mw, glow = 1.3, 0.0, 0.3, 6.5, 0.2
            gaze = QPointF(0, 0)
        elif state == "thinking":
            eye, smile, mw = 0.95, 0.0, 7.0
            gaze = QPointF(0.4 * math.sin(t * 0.9) * motion, -0.9)
        elif state == "speaking":
            eye, smile, mw, wave, glow = 1.0, 0.45, 12.0, 1.0, 0.2
            gaze = QPointF(0, 0)
        elif state == "approval":
            eye, smile, mw, glow, color = 1.15, -0.25, 8.0, 0.32, self._col("warning")
            gaze = QPointF(0, 0.2)
        elif state == "error":
            eye, smile, mw, glow, color = 0.85, -0.7, 9.0, 0.3, self._col("error")
            gaze = QPointF(0, 0.5)
        elif self._reaction is None:
            smile += 0.35 * self._hover
            follow = self._cursor_gaze() if motion else None
            if follow is not None:
                gaze = follow
        elif self._reaction == "happy":
            eye, smile, mw, happy, color = 1.0, 1.0, 13.0, 1.0, self._col("success")
        elif self._reaction == "sad":
            eye, smile, mw, droop, gaze = 0.7, -0.8, 9.0, 1.0, QPointF(0, 0.7)
        # idle: hedefler yukarıdaki varsayılanlardır.

        self._eye_open = _ease(self._eye_open, eye, dt, 10)
        self._smile = _ease(self._smile, smile, dt, 8)
        self._mouth_open = _ease(self._mouth_open, mopen, dt, 10)
        self._mouth_w = _ease(self._mouth_w, mw, dt, 8)
        self._wave = _ease(self._wave, wave, dt, 9)
        self._tint = _mix(self._tint, color, 1 - math.exp(-9 * dt))
        self._glow = _ease(self._glow, glow, dt, 6)
        self._dim = _ease(self._dim, dim, dt, 4)
        self._happy = _ease(self._happy, happy, dt, 12)
        self._droop = _ease(self._droop, droop, dt, 5)
        self._ring = _ease(self._ring, 1.0 if state == "listening" else 0.0, dt, 8)
        self._gaze = QPointF(
            _ease(self._gaze.x(), gaze.x(), dt, 9), _ease(self._gaze.y(), gaze.y(), dt, 9)
        )

        # Mikrofon seviyesi: hızlı çıkış, yavaş iniş
        rate = 25 if self._level_raw > self._level else 6
        self._level = _ease(self._level, self._level_raw if state == "listening" else 0.0, dt, rate)

        # Göz kırpma ve bakış gezdirme (azaltılmış harekette yok)
        if motion and self._blink_p is None and t >= self._blink_at:
            self._blink_p = 0.0
        if self._blink_p is not None:
            self._blink_p += dt / 0.16
            if self._blink_p >= 1:
                self._blink_p = None
                self._blink_at = t + random.uniform(3, 7)
        if motion and t >= self._glance_at:
            self._glance_at = t + random.uniform(1.5, 4.5)
            far = random.random() < 0.65
            self._gaze_goal = (
                QPointF(random.choice((-0.8, 0.8)), random.uniform(-0.3, 0.3))
                if far
                else QPointF(0, 0)
            )

        self._tick_particles(dt, state, motion)

        # Anten sürüklemede geride kalır
        self._antenna = _ease(self._antenna, -self._drag_dx * 0.6, dt, 10)

    def _cursor_gaze(self) -> QPointF | None:
        """İmleç yakınsa gözler ona döner; uzaktaysa rastgele bakış sürer."""
        center = self.mapToGlobal(self._body_rect().center())
        d = QCursor.pos() - center
        dist = math.hypot(d.x(), d.y())
        if dist < 1 or dist > 700:
            return None
        k = min(1.0, dist / 160)
        return QPointF(d.x() / dist * k, d.y() / dist * k * 0.8)

    def _burst(self) -> None:
        if self._reduced or self._sprite is not None:
            return
        for i in range(10):
            a = i * 2 * math.pi / 10 + random.uniform(-0.2, 0.2)
            speed = random.uniform(10, 16)  # pencere 120 px: kıvılcım kenarda söner
            self._particles.append(
                _Particle(
                    "spark",
                    math.cos(a) * 42,
                    math.sin(a) * 42,
                    math.cos(a) * speed,
                    math.sin(a) * speed - 6,
                    0.9,
                )
            )

    def _tick_particles(self, dt: float, state: str, motion: float) -> None:
        if motion and state == "off" and self._t >= self._z_at:
            self._z_at = self._t + 1.6
            self._particles.append(_Particle("z", 22, -24, 5, -12, 2.4))
        for pt in self._particles:
            pt.age += dt
            pt.x += pt.vx * dt
            pt.y += pt.vy * dt
            if pt.kind == "spark":
                pt.vy += 30 * dt
        self._particles = [pt for pt in self._particles if pt.age < pt.life]

    # ------------------------------------------------------------------ çizim

    def paintEvent(self, event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHints(
            QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing
        )
        body = self._body_rect()
        self._paint_body(painter, body.left() + BODY / 2)
        if self._bubble_text and self._bubble_alpha > 0.01:
            self._paint_bubble(painter, body)
        painter.end()

    def _offsets(self, state: str) -> tuple[float, float, float, float]:
        """(dx, dy, scale_x, scale_y) -> nefes, zıplama, sarsılma, çökme."""
        motion = 0.0 if self._reduced else 1.0
        t = self._t
        breath = 1 + 0.025 * math.sin(t * 1.6) * motion
        dx = dy = 0.0
        if state == "speaking":
            dy += math.sin(t * 6) * 2.5 * motion
        if state == "approval":
            dy += math.sin(t * 3) * 1.5 * motion
        if self._shake_age < 0.8 and state == "error":
            dx += math.sin(self._shake_age * 55) * 6 * math.exp(-self._shake_age * 3.5) * motion
        if self._reaction == "happy" and self._shown_state in {"idle", "off"}:
            age = self._react_age
            dy -= abs(math.sin(age * 9)) * 16 * math.exp(-age * 2.0) * motion
        grow = 1 + 0.04 * self._hover
        sx, sy = breath * grow, (2 - breath) * grow
        if self._droop > 0.01:
            dy += 5 * self._droop
            sy *= 1 - 0.07 * self._droop
            sx *= 1 + 0.04 * self._droop
        return dx, dy, sx, sy

    def _paint_body(self, p: QPainter, cx: float) -> None:
        state = self.state
        if self._sprite is not None:
            self._paint_sprite(p, cx)
            bx, by = cx, _CENTER_Y
        else:
            bx, by = self._paint_orb(p, cx, state)
        # Rozetler görünümden bağımsız ve ölçeksiz çizilir
        if self._jobs:
            self._paint_gear(p, bx + _RADIUS * 0.8, by - _RADIUS * 0.74)
        if state == "approval":
            self._paint_bang(p, bx - _RADIUS * 0.8, by - _RADIUS * 0.72)

    def _paint_sprite(self, p: QPainter, cx: float) -> None:
        sprite = self._sprite
        assert sprite is not None
        col = sprite.frame_index(self._sprite_row, self._frame_t, self._reduced)
        pixmap = sprite.pixmap(self._sprite_row, col)
        p.save()
        p.setOpacity(self._dim)
        p.drawPixmap(round(cx - pixmap.width() / 2), (BODY - pixmap.height()) // 2, pixmap)
        p.restore()

    def _paint_orb(self, p: QPainter, cx: float, state: str) -> tuple[float, float]:
        """Küreyi çizer; rozetlerin dayanacağı merkezi (x, y) döndürür."""
        t = self._t
        tint = self._tint
        dx, dy, sx, sy = self._offsets(state)
        cy = _CENTER_Y
        radius = _RADIUS

        # Zemin gölgesi: gövde yükseldikçe küçülür
        lift = max(0.0, -dy)
        shadow_w = 27 * (1 - lift * 0.02)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, round(80 * self._dim)))
        p.drawEllipse(QPointF(cx + dx * 0.5, 111), shadow_w, 4.5)

        # Yumuşak ışıma
        glow = self._glow + 0.08 * self._hover
        glow += 0.12 * math.sin(t * 2.2) if state == "approval" else 0.0
        g = QRadialGradient(QPointF(cx + dx, cy + dy), radius + 22)
        g.setColorAt(0.55, _alpha(tint, glow))
        g.setColorAt(1.0, _alpha(tint, 0.0))
        p.setBrush(QBrush(g))
        p.drawEllipse(QPointF(cx + dx, cy + dy), radius + 22, radius + 22)

        # Dinleme halkası: mikrofon seviyesiyle nabız atar
        if self._ring > 0.02:
            pulse = 0.5 + 0.5 * math.sin(t * 3.2)
            ring_r = radius + 6 + self._level * 9 + pulse * 2
            ring_a = self._ring * (0.3 + 0.6 * self._level + 0.1 * pulse)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(_alpha(self._col("accent"), ring_a), 2.5 + self._level * 2.5))
            p.drawEllipse(QPointF(cx + dx, cy + dy), ring_r, ring_r)
            p.setPen(QPen(_alpha(self._col("accent"), ring_a * 0.4), 1.5))
            p.drawEllipse(QPointF(cx + dx, cy + dy), ring_r + 5, ring_r + 5)

        p.save()
        p.setOpacity(self._dim)
        p.translate(cx + dx, cy + dy)
        p.scale(sx, sy)
        self._paint_character(p, tint, state)
        p.restore()

        if state == "speaking":
            self._paint_waves(p, cx + dx, cy + dy, tint)
        self._paint_particles(p, cx + dx, cy + dy)

        if state == "thinking":
            self._paint_dots(p, cx + dx, cy + dy)
        return cx + dx, cy + dy

    def _paint_character(self, p: QPainter, tint: QColor, state: str) -> None:
        t = self._t
        r = _RADIUS
        p.setPen(Qt.PenStyle.NoPen)

        # Anten: ucu durum renginde yanar
        tip = QPointF(self._antenna * 0.4 + math.sin(t * 1.3) * 1.5, -r - 15)
        p.setPen(QPen(self._col("border"), 3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(QPointF(0, -r + 3), tip)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_alpha(tint, 0.25))
        p.drawEllipse(tip, 7, 7)
        p.setBrush(tint)
        p.drawEllipse(tip, 3.6, 3.6)

        # Kulaklar
        p.setBrush(self._col("soft"))
        p.setPen(QPen(_alpha(tint, 0.55), 1.5))
        for side in (-1, 1):
            p.drawRoundedRect(QRectF(side * (r - 2) - 5, -2, 10, 20), 5, 5)

        # Gövde: hafif basık küre; DESIGN.md gereği düz ton (gradyan ve parlama yok)
        body = QRectF(-r * 1.04, -r, r * 2.08, r * 2)
        p.setBrush(self._col("surface"))
        p.setPen(QPen(_alpha(tint, 0.7), 2))
        p.drawEllipse(body)

        # Yanaklar (mutluyken belirir)
        if self._happy > 0.02:
            p.setBrush(_alpha(self._col("warning"), 0.22 * self._happy))
            for side in (-1, 1):
                p.drawEllipse(QPointF(side * 27, 10), 6.5, 4)

        self._paint_eyes(p, tint)
        self._paint_mouth(p, tint)

    def _paint_eyes(self, p: QPainter, tint: QColor) -> None:
        blink = 0.0 if self._blink_p is None else math.sin(math.pi * self._blink_p)
        openness = max(0.08, self._eye_open * (1 - blink))
        width = 10 * (1 + 0.15 * (self._eye_open - 1))
        height = 15 * openness
        gx, gy = self._gaze.x() * 3.6, self._gaze.y() * 3.0
        for side in (-1, 1):
            c = QPointF(side * 15 + gx, -5 + gy + 3 * self._droop)
            if self._happy > 0.02:
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.setPen(QPen(_alpha(tint, self._happy), 3.2, Qt.PenStyle.SolidLine,
                              Qt.PenCapStyle.RoundCap))
                arc = QPainterPath(QPointF(c.x() - 6, c.y() + 3))
                arc.quadTo(QPointF(c.x(), c.y() - 8), QPointF(c.x() + 6, c.y() + 3))
                p.drawPath(arc)
            alpha = 1 - self._happy
            if alpha <= 0.02:
                continue
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(_alpha(tint, 0.2 * alpha))
            p.drawEllipse(c, width + 3, height / 2 + 3)
            p.setBrush(_alpha(tint, alpha))
            p.drawEllipse(c, width / 2, height / 2)
            if height > 6:
                p.setBrush(_alpha(QColor(255, 255, 255), 0.85 * alpha))
                p.drawEllipse(QPointF(c.x() - 1.8, c.y() - height * 0.2), 1.9, 1.9)

    def _paint_mouth(self, p: QPainter, tint: QColor) -> None:
        t = self._t
        w = self._mouth_w
        top: list[QPointF] = []
        bottom: list[QPointF] = []
        steps = 18
        wave = self._wave * (0.0 if self._reduced else 1.0)
        for i in range(steps + 1):
            u = -1 + 2 * i / steps
            env = 1 - u * u
            y = 17 + 5.5 * self._smile * env
            y += wave * 3.2 * math.sin(u * 4.2 - t * 13) * (0.55 + 0.45 * math.sin(t * 3.1)) * (
                0.4 + 0.6 * env
            )
            top.append(QPointF(u * w, y))
            bottom.append(QPointF(u * w, y + self._mouth_open * 12 * env))
        p.setBrush(Qt.BrushStyle.NoBrush)
        if self._mouth_open > 0.06:
            p.setBrush(_mix(self._col("surface"), self._col("text"), 0.85))
            p.setPen(QPen(_alpha(tint, 0.9), 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawPolygon(QPolygonF(top + bottom[::-1]))
            return
        p.setPen(QPen(_alpha(tint, 0.9), 2.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                      Qt.PenJoinStyle.RoundJoin))
        p.drawPolyline(QPolygonF(top))

    def _paint_waves(self, p: QPainter, cx: float, cy: float, tint: QColor) -> None:
        """Konuşurken iki yandan dışa yayılan ses yayları."""
        motion = 0.0 if self._reduced else 1.0
        p.setBrush(Qt.BrushStyle.NoBrush)
        for i in range(2):
            phase = (self._t * 1.6 * motion + i * 0.5) % 1.0
            r = _RADIUS + 10 + phase * 16
            p.setPen(
                QPen(
                    _alpha(tint, 0.55 * (1 - phase)),
                    2.2,
                    Qt.PenStyle.SolidLine,
                    Qt.PenCapStyle.RoundCap,
                )
            )
            box = QRectF(cx - r, cy - r, 2 * r, 2 * r)
            p.drawArc(box, -28 * 16, 56 * 16)
            p.drawArc(box, 152 * 16, 56 * 16)

    def _paint_particles(self, p: QPainter, cx: float, cy: float) -> None:
        for pt in self._particles:
            k = pt.age / pt.life
            x, y = cx + pt.x, cy + pt.y
            if pt.kind == "z":
                alpha = min(1.0, pt.age / 0.3) * (1 - k)
                font = QFont("Segoe UI")
                font.setPixelSize(round(9 + 8 * k))
                font.setBold(True)
                p.setFont(font)
                p.setPen(_alpha(self._col("text"), 0.75 * alpha))
                p.drawText(QPointF(x + 3 * math.sin(pt.age * 2.5), y), "z")
                continue
            size = 1.5 + 4 * (1 - k)
            color = self._col("warning" if int(pt.vx * 10) % 2 else "accent")
            p.setPen(
                QPen(_alpha(color, 1 - k), 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
            )
            p.drawLine(QPointF(x - size, y), QPointF(x + size, y))
            p.drawLine(QPointF(x, y - size), QPointF(x, y + size))

    def _paint_dots(self, p: QPainter, cx: float, cy: float) -> None:
        motion = 0.0 if self._reduced else 1.0
        p.setPen(Qt.PenStyle.NoPen)
        for i in range(3):
            a = self._t * 2.6 * motion + i * 2 * math.pi / 3 + (0.0 if motion else i)
            fade = 0.55 + 0.45 * math.cos(a - self._t * 2.6 * motion)
            p.setBrush(_alpha(self._col("warning"), 0.45 + 0.55 * fade))
            p.drawEllipse(
                QPointF(cx + (_RADIUS + 13) * math.cos(a), cy + (_RADIUS + 11) * math.sin(a)),
                3.6, 3.6,
            )

    def _paint_gear(self, p: QPainter, x: float, y: float) -> None:
        motion = 0.0 if self._reduced else 1.0
        p.save()
        p.translate(x, y)
        p.setPen(QPen(self._col("accent"), 1.6))
        p.setBrush(self._col("surface"))
        p.drawEllipse(QPointF(0, 0), 13, 13)
        p.rotate(math.degrees(self._t * 2.2) * motion)
        p.setPen(QPen(self._col("accent"), 2.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        for i in range(6):
            p.save()
            p.rotate(i * 60)
            p.drawLine(QPointF(0, -5.2), QPointF(0, -8.2))
            p.restore()
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QPointF(0, 0), 4.2, 4.2)
        p.restore()
        count = str(len(self._jobs)) if len(self._jobs) < 10 else "9+"
        pill = QRectF(x + 5, y + 4, 15, 13)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self._col("warning"))
        p.drawRoundedRect(pill, 6.5, 6.5)
        font = QFont("Segoe UI")
        font.setPixelSize(10)
        font.setBold(True)
        p.setFont(font)
        p.setPen(self._col("surface"))
        p.drawText(pill, Qt.AlignmentFlag.AlignCenter, count)

    def _paint_bang(self, p: QPainter, x: float, y: float) -> None:
        bob = 0.0 if self._reduced else math.sin(self._t * 5) * 3
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self._col("warning"))
        p.drawEllipse(QPointF(x, y + bob), 11, 11)
        font = QFont("Segoe UI")
        font.setPixelSize(15)
        font.setBold(True)
        p.setFont(font)
        p.setPen(self._col("surface"))
        p.drawText(QRectF(x - 11, y + bob - 11, 22, 22), Qt.AlignmentFlag.AlignCenter, "!")

    def _paint_bubble(self, p: QPainter, body: QRect) -> None:
        font = QFont("Segoe UI")
        font.setPixelSize(12)
        text_w = BUBBLE_W - 24
        flags = Qt.TextFlag.TextWordWrap | Qt.AlignmentFlag.AlignLeft
        needed = QFontMetrics(font).boundingRect(QRect(0, 0, text_w, 400), int(flags),
                                                 self._bubble_text)
        height = min(BODY - 8, needed.height() + 20)
        slide = (1 - self._bubble_alpha) * 8
        left = body.left() - GAP - BUBBLE_W + slide if self._bubble_side_left else (
            body.right() + 1 + GAP - slide
        )
        rect = QRectF(left, (BODY - height) / 2, BUBBLE_W, height)
        edge = self._col("border")
        accent = {"heard": self._col("accent"), "job": self._col("warning")}.get(
            self._bubble_kind, edge
        )
        p.save()
        p.setOpacity(self._bubble_alpha)
        p.setPen(QPen(_mix(edge, accent, 0.7), 1.0))
        p.setBrush(_alpha(self._col("surface"), 0.96))
        p.drawRoundedRect(rect, 12, 12)
        # Kuyruk gövdeye bakar
        ty = BODY / 2
        if self._bubble_side_left:
            tail = QPolygonF([QPointF(rect.right() - 0.5, ty - 6), QPointF(rect.right() + 6, ty),
                              QPointF(rect.right() - 0.5, ty + 6)])
        else:
            tail = QPolygonF([QPointF(rect.left() + 0.5, ty - 6), QPointF(rect.left() - 6, ty),
                              QPointF(rect.left() + 0.5, ty + 6)])
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_alpha(self._col("surface"), 0.96))
        p.drawPolygon(tail)
        p.setFont(font)
        ink = self._col("text")
        p.setPen(ink if self._bubble_kind != "heard" else _mix(ink, self._col("accent"), 0.35))
        p.drawText(rect.adjusted(12, 8, -12, -8), int(flags), self._bubble_text)
        p.restore()
