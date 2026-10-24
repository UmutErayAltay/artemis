"""PySide6 paneli; bütün model ve araç işleri Qt'siz köprüde kalır."""

from __future__ import annotations

import html
import json
import sys
import tempfile
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError
from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QAction,
    QBrush,
    QCloseEvent,
    QColor,
    QFont,
    QFontMetrics,
    QIcon,
    QKeyEvent,
    QPainter,
    QPen,
    QRadialGradient,
)
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMainWindow,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QSystemTrayIcon,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from artemis.brain.providers import provider_problem
from artemis.channels.autostart import Autostart
from artemis.channels.desktop.bridge import (
    Activity,
    ActivityGap,
    Approval,
    BridgeEvent,
    DesktopBridge,
    Failed,
    Finished,
    Proactive,
    State,
    ToolFinished,
    VoiceCommand,
)
from artemis.channels.desktop.hotkey import GlobalHotkey
from artemis.channels.desktop.instance import InstanceServer, claim_instance, instance_name
from artemis.channels.desktop.pet import TEAL, PetWidget, level_from_message
from artemis.channels.desktop.projects_panel import ProjectsPanel
from artemis.channels.desktop.settings_panel import (
    SettingsPanel,
    save_choices,
    save_computer_mode,
)
from artemis.channels.desktop.steps import StepLine, step_line
from artemis.channels.runtime import Runtime, create_runtime
from artemis.config.settings import load_settings
from artemis.safety.audit import AuditLog

_STYLE = """
QWidget {
  background: #0B1220; color: #E6EDF5;
  font-family: 'Segoe UI Variable Text', 'Segoe UI', sans-serif; font-size: 14px;
}
QLabel { background: transparent; }
QFrame#card {
  background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #15243A, stop:1 #101B2D);
  border: 1px solid #243750; border-radius: 22px;
}
QFrame#raised {
  background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #1A2B43, stop:1 #15233A);
  border: 1px solid #2E4260; border-radius: 20px;
}
QFrame#messageUser {
  background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #1E4A50, stop:1 #173A44);
  border: 1px solid #2F6A6B; border-radius: 20px; border-bottom-right-radius: 6px;
}
QFrame#messageAssistant {
  background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #1D2F48, stop:1 #172840);
  border: 1px solid #2E4260; border-radius: 20px; border-bottom-left-radius: 6px;
}
QFrame#chip {
  background: #0E1828; border: 1px solid #2E4260; border-radius: 17px;
}
QLabel#eyebrow { color: #B4C3D4; font-size: 11px; font-weight: 700; letter-spacing: 1px; }
QLabel#title {
  color: #F2F6FA; font-size: 24px; font-weight: 800;
  font-family: 'Segoe UI Variable Display', 'Segoe UI', sans-serif;
}
QLabel#muted { color: #B4C3D4; }
QLabel#section { color: #FFD78C; font-size: 16px; font-weight: 800; padding-top: 10px; }
QLabel#status { background: transparent; border: none; padding: 0 2px; font-weight: 700; }
QLabel#rawToolName {
  color: #B4C3D4; font-family: 'Cascadia Mono', Consolas, monospace; font-size: 12px;
}
QFrame#stepBlock {
  background: #0F1A2B; border: 1px solid #2A3E58; border-radius: 16px;
}
QPushButton#stepDetailsToggle {
  color: #B4C3D4; background: transparent; border: none;
  padding: 2px 0px; min-height: 0px; font-size: 12px;
}
QPushButton#stepDetailsToggle:hover { color: #53D6C7; background: transparent; border: none; }
QPlainTextEdit#mainPrompt { padding: 8px 14px; }
QLabel#demo {
  color: #3B2A07; background: #FFD78C; border-radius: 15px;
  padding: 6px 14px; font-weight: 800;
}
QPlainTextEdit, QListWidget, QLineEdit, QComboBox {
  background: #0E1828; border: 1px solid #2A3E58; border-radius: 16px;
  padding: 12px; selection-background-color: #255B61;
}
QLineEdit, QComboBox { padding: 7px 14px; border-radius: 18px; min-height: 22px; }
QComboBox::drop-down { border: none; width: 34px; }
QComboBox::down-arrow { image: url(@CHEVRON@); width: 12px; height: 8px; }
QComboBox QAbstractItemView {
  background: #12203A; border: 1px solid #2E4260; border-radius: 12px;
  selection-background-color: #255B61; outline: none; padding: 4px;
}
QPlainTextEdit:focus, QListWidget:focus, QLineEdit:focus, QComboBox:focus {
  border: 2px solid #53D6C7;
}
QPushButton {
  background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #1F3350, stop:1 #182A44);
  color: #E6EDF5; border: 1px solid #33496A;
  border-radius: 19px; padding: 8px 18px; min-height: 22px; font-weight: 700;
}
QPushButton:hover { border-color: #53D6C7; background: #223954; }
QPushButton:pressed { background: #16263D; }
QPushButton:focus { border: 2px solid #53D6C7; }
QPushButton:disabled { color: #8493A8; background: #111C2D; border-color: #243247; }
QPushButton#primary {
  color: #0B1220; border-color: #53D6C7; font-weight: 800;
  background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #72E5D8, stop:1 #45C2B3);
}
QPushButton#primary:hover { background: #8AEEE2; }
QPushButton#primary:pressed { background: #3DB3A5; }
QPushButton#primary:focus { border: 2px solid #FFD78C; }
QPushButton#danger { color: #FFD9D5; border-color: #8A504D; background: #3A252B; }
QPushButton#danger:hover { border-color: #F0B2AA; background: #47292F; }
QPushButton#amber { color: #FFD78C; border-color: #795D31; background: #332B1E; }
QPushButton#suggestion { color: #FFD78C; border-color: #795D31; background: #2A2619; }
QPushButton#suggestion:hover { border-color: #FFD78C; background: #3A3120; }
QPushButton#voiceToggle:checked { color: #0B1220; background: #53D6C7; border-color: #53D6C7; }
QSplitter::handle { background: #0B1220; width: 10px; }
QScrollBar:vertical { background: transparent; width: 12px; margin: 4px; }
QScrollBar::handle:vertical { background: #3A5070; min-height: 28px; border-radius: 4px; }
QScrollBar::handle:vertical:hover { background: #53D6C7; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
QListWidget::item { padding: 8px 6px; border-bottom: 1px solid #1C2A3D; }
QDialog { background: #111C2D; }
QTabWidget::pane { border: none; top: 6px; }
QTabBar { background: transparent; }
QTabBar::tab {
  background: #121F33; color: #B4C3D4; border: 1px solid #2A3E58; border-radius: 17px;
  padding: 7px 22px; margin: 0 6px 0 2px; font-weight: 700; min-height: 20px;
}
QTabBar::tab:hover { border-color: #53D6C7; }
QTabBar::tab:selected { background: #53D6C7; color: #0B1220; border-color: #53D6C7; }
QTabBar::tab:focus { border: 2px solid #78E4D7; }
QCheckBox { spacing: 10px; padding: 3px 0; }
QCheckBox::indicator {
  width: 18px; height: 18px; border-radius: 6px;
  border: 2px solid #4A6285; background: #0E1828;
}
QCheckBox::indicator:hover { border-color: #53D6C7; }
QCheckBox::indicator:checked {
  border-color: #53D6C7;
  background: qradialgradient(cx:.5, cy:.5, radius:.6, fx:.5, fy:.5,
    stop:0 #53D6C7, stop:0.45 #53D6C7, stop:0.55 #0E1828, stop:1 #0E1828);
}
QCheckBox::indicator:disabled { border-color: #2F3F57; }
QCheckBox:focus { color: #FFD78C; }
QProgressBar {
  background: #0E1828; border: 1px solid #2A3E58; border-radius: 12px;
  text-align: center; min-height: 22px; font-weight: 600;
}
QProgressBar::chunk { background: #53D6C7; border-radius: 11px; }
QPushButton#primary:disabled { color: #8493A8; background: #23313F; border-color: #334354; }
QPushButton#danger:disabled { color: #8D7C7B; background: #29272C; border-color: #454047; }
""".replace("@CHEVRON@", (Path(__file__).with_name("chevron.svg")).as_posix())


# Köprü ses durumunu makine okunur taşır; Türkçe metne ekran çevirir.
_VOICE_LABELS = {
    "off": "Ses kapalı",
    "preparing": "Ses hazırlanıyor…",
    "listening": "Dinliyor",
    "awaiting_command": "Seni dinliyorum",
    "thinking": "Düşünüyor",
    "speaking": "Konuşuyor",
    "error": "Ses hatası",
}


# Durum ve ses noktaları ton renkleriyle okunur; metin kendisi düz kalır.
_STATUS_TONES = {"ready": "#53D6C7", "running": "#FFD78C", "error": "#F0B2AA"}
_VOICE_TONES = {
    "off": "#718096",
    "preparing": "#FFD78C",
    "listening": "#53D6C7",
    "awaiting_command": "#53D6C7",
    "thinking": "#FFD78C",
    "speaking": "#53D6C7",
    "error": "#F0B2AA",
}
_STEP_COLORS = {
    "ok": "#53D6C7",
    "sent": "#FFD78C",
    "fail": "#F0B2AA",
    "denied": "#F0B2AA",
    "info": "#A9B8C9",
}
# Ayrıntı ipucu büyük denetim kayıtlarında da ekranı ve belleği şişirmez.
_DETAIL_LIMIT = 4000
# Adım blokları yalnız son birkaç isteğin canlı bağlantısını tutar.
_STEP_TURNS_KEPT = 8
_STEP_EVENTS_PER_TURN = 200


class Mascot(QWidget):
    """Başlıktaki küçük, statik pet yüzü; pet.py renkleriyle çizilir, hareketi yoktur."""

    def __init__(self) -> None:
        super().__init__()
        self.setFixedSize(54, 56)
        self.setAccessibleName("Artemis yüzü")

    def paintEvent(self, event: Any) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor("#2A3B52"), 2.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(QPointF(27, 12), QPointF(27, 5))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(TEAL)
        p.drawEllipse(QPointF(27, 5), 3.2, 3.2)
        grad = QRadialGradient(QPointF(19, 21), 36)
        grad.setColorAt(0.0, QColor("#2F4A6C"))
        grad.setColorAt(0.6, QColor("#16253B"))
        grad.setColorAt(1.0, QColor("#0A1220"))
        p.setBrush(QBrush(grad))
        p.setPen(QPen(TEAL, 2))
        p.drawEllipse(QRectF(3, 11, 48, 43))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 215, 140, 60))
        for x in (11, 43):
            p.drawEllipse(QPointF(x, 40), 4.5, 3)
        p.setBrush(TEAL)
        for x in (19, 35):
            p.drawEllipse(QPointF(x, 31), 3.6, 4.4)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(TEAL, 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawArc(QRectF(21, 34, 12, 8), 200 * 16, 140 * 16)
        p.end()


def _is_user_speaker(speaker: str) -> bool:
    """ "Siz" ve "Siz (ses)" aynı kullanıcı balonudur."""
    return speaker.startswith("Siz")


def _dot_style(color: str) -> str:
    """Durum noktasının rengi; metin ayrı etikette düz kalır."""
    return f"QLabel {{ color: {color}; font-size: 20px; background: transparent; }}"


def _detail_text(activity: Activity) -> str:
    """Ham araç adı ve alanlar; yalnız ayrıntı ipucunda görünür, görünür satıra girmez."""
    raw_tool = activity.fields.get("tool")
    head = f"Ham araç adı: {raw_tool}" if isinstance(raw_tool, str) else f"Olay: {activity.event}"
    payload = json.dumps(activity.fields, ensure_ascii=False, indent=2, sort_keys=True, default=str)
    if len(payload) > _DETAIL_LIMIT:
        payload = payload[:_DETAIL_LIMIT] + "\n…"
    text = f"{head}\n{payload}"
    # İpucu zengin metin sayılmasın diye kaçışlanır; JSON içindeki "<" işaretleri bozmaz.
    return f"<pre>{html.escape(text)}</pre>"


class PromptEdit(QPlainTextEdit):
    def __init__(self, on_send: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._on_send = on_send
        self.setPlaceholderText("Artemis'e ne yapmak istediğinizi yazın…")
        self.setMinimumHeight(82)
        self.setMaximumHeight(150)
        self.setTabChangesFocus(True)
        self.setAccessibleName("Komut metni")

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not (
            event.modifiers() & Qt.KeyboardModifier.ShiftModifier
        ):
            self._on_send()
            event.accept()
            return
        super().keyPressEvent(event)


class StepBlock(QFrame):
    """Bir isteğin sade adım listesi (ADR 0014).

    Görünür satır yoksa blok hiç görünmez. "Ayrıntılar" açılınca iç satırlar da
    görünür ve her satırın ipucunda ham ad ile alanlar yer alır.
    """

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("stepBlock")
        self.setMaximumWidth(520)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 9, 12, 8)
        layout.setSpacing(4)
        self._rows_layout = QVBoxLayout()
        self._rows_layout.setSpacing(3)
        layout.addLayout(self._rows_layout)
        self._rows: list[tuple[QLabel, StepLine, Activity]] = []
        self._details = False
        self._toggle = QPushButton("Ayrıntılar")
        self._toggle.setObjectName("stepDetailsToggle")
        self._toggle.setCheckable(True)
        self._toggle.setFlat(True)
        self._toggle.setAccessibleName("Adım ayrıntılarını göster")
        self._toggle.toggled.connect(self._set_details)
        layout.addWidget(self._toggle, 0, Qt.AlignmentFlag.AlignLeft)
        self._refresh()

    def add_line(self, activity: Activity, line: StepLine) -> None:
        label = QLabel(line.text)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        label.setStyleSheet(f"QLabel {{ color: {_STEP_COLORS[line.kind]}; font-size: 12px; }}")
        self._rows.append((label, line, activity))
        self._rows_layout.addWidget(label)
        label.setVisible(self._details or not line.internal)
        if self._details:
            label.setToolTip(_detail_text(activity))
        self._refresh()

    def _set_details(self, on: bool) -> None:
        self._details = on
        for label, line, activity in self._rows:
            label.setVisible(on or not line.internal)
            label.setToolTip(_detail_text(activity) if on else "")
        self._refresh()

    def _refresh(self) -> None:
        visible = any(not line.internal for _label, line, _activity in self._rows)
        self._toggle.setVisible(visible)
        self._fit_width()
        self.setVisible(visible)

    def _fit_width(self) -> None:
        """Sohbette sola hizalı blok, sarılan etiketin küçük ipucuna göre daralmasın.

        Sarmalı QLabel genişlik ipucunu birkaç sözcüğe göre verir; blok o genişliğe
        sıkışınca satırlar üst üste biner. En uzun görünür satır sığacak kadar (en çok
        520 px) genişlik ayrılır; daha uzun satır o genişlikte sarılır.
        """
        layout = self.layout()
        if layout is None:
            return
        margins = layout.contentsMargins()
        widest = self._toggle.sizeHint().width()
        for label, line, _activity in self._rows:
            if self._details or not line.internal:
                widest = max(widest, label.fontMetrics().horizontalAdvance(label.text()))
        self.setMinimumWidth(min(520, widest + margins.left() + margins.right() + 4))


@dataclass
class _TurnSteps:
    """Bir isteğin denetim olayları; sonuç bildirimi olaylardan önce ya da sonra gelebilir.

    Olaylar ayrı bir kuyruktan okunur, bu yüzden bitiş bildirimi işlendiğinde son
    birkaç olay henüz gelmemiş olabilir. Blok bir kez kurulur, sonra gelen satırlar
    aynı bloğa eklenir.
    """

    activities: list[Activity] = field(default_factory=list)
    block: StepBlock | None = None


class ChatTranscript(QWidget):
    """Seçilebilir, düz metin mesaj kartları ve sakin ilk ekran."""

    def __init__(self, demo: bool, on_suggestion: Any) -> None:
        super().__init__()
        self._messages: deque[tuple[str, str]] = deque(maxlen=500)
        self._bubbles: deque[tuple[QFrame, str, str]] = deque(maxlen=500)
        self._on_suggestion = on_suggestion
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.welcome = QFrame()
        self.welcome.setObjectName("raised")
        welcome_layout = QVBoxLayout(self.welcome)
        welcome_layout.setContentsMargins(24, 24, 24, 24)
        welcome_layout.setSpacing(12)
        greeting = QLabel("Merhaba. Bugün neyi halledelim?")
        greeting.setObjectName("title")
        greeting.setTextFormat(Qt.TextFormat.PlainText)
        description = QLabel(
            "Dosyalar, uygulamalar ve günlük işler için bir komut yazın. "
            "Onay gereken işlemlerde ayrıntıları önce gösteririm."
        )
        description.setObjectName("muted")
        description.setWordWrap(True)
        description.setTextFormat(Qt.TextFormat.PlainText)
        welcome_layout.addWidget(greeting)
        welcome_layout.addWidget(description)
        suggestions = QHBoxLayout()
        suggestions.addWidget(QLabel("Örnekler"))
        suggestion_texts = (
            ("Merhaba", "örnek akış")
            if demo
            else (
                "Saat kaç?",
                "Dosyalarımı listele",
            )
        )
        for text in suggestion_texts:
            button = QPushButton(text)
            button.setObjectName("suggestion")
            button.clicked.connect(lambda _checked=False, value=text: on_suggestion(value))
            suggestions.addWidget(button)
        suggestions.addStretch(1)
        welcome_layout.addLayout(suggestions)
        outer.addWidget(self.welcome, 0, Qt.AlignmentFlag.AlignCenter)

        self._scroll_area = QScrollArea()
        self._scroll_area.setWidgetResizable(True)
        self._scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll_area.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        self._scroll_area.viewport().setStyleSheet("background: transparent;")
        self.content = QWidget()
        self.content.setStyleSheet("background: transparent;")
        self.content_layout = QVBoxLayout(self.content)
        self.content_layout.setContentsMargins(2, 2, 8, 10)
        self.content_layout.setSpacing(10)
        self.content_layout.addStretch(1)
        self._scroll_area.setWidget(self.content)
        self._scroll_area.hide()
        outer.addWidget(self._scroll_area, 1)

    def append(self, speaker: str, text: str, steps: QWidget | None = None) -> None:
        if len(self._messages) == self._messages.maxlen:
            item = self.content_layout.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.deleteLater()
            self._messages.popleft()
            if self._bubbles:
                self._bubbles.popleft()
        self._messages.append((speaker, text))
        self.welcome.hide()
        self._scroll_area.show()
        row = QWidget()
        column = QVBoxLayout(row)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(6)
        if steps is not None:
            # Adım bloğu yanıt balonunun hemen üstüne gelir; konuşma kaydına girmez.
            column.addWidget(steps, 0, Qt.AlignmentFlag.AlignLeft)
        bubble_row = QWidget()
        row_layout = QHBoxLayout(bubble_row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        bubble = QFrame()
        bubble.setObjectName("messageUser" if _is_user_speaker(speaker) else "messageAssistant")
        bubble_layout = QVBoxLayout(bubble)
        bubble_layout.setContentsMargins(14, 10, 14, 11)
        bubble_layout.setSpacing(5)
        caption = QLabel(speaker.upper())
        caption.setObjectName("eyebrow")
        body = QLabel(text)
        body.setTextFormat(Qt.TextFormat.PlainText)
        body.setWordWrap(True)
        body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        body.setAccessibleName(f"{speaker} mesajı")
        bubble_layout.addWidget(caption)
        bubble_layout.addWidget(body)
        self._bubbles.append((bubble, speaker, text))
        self._resize_bubble(bubble, speaker, text)
        if _is_user_speaker(speaker):
            row_layout.addStretch(1)
            row_layout.addWidget(bubble)
        else:
            row_layout.addWidget(bubble)
            row_layout.addStretch(1)
        column.addWidget(bubble_row)
        self.content_layout.insertWidget(self.content_layout.count() - 1, row)
        QTimer.singleShot(
            0,
            lambda: self._scroll_area.verticalScrollBar().setValue(
                self._scroll_area.verticalScrollBar().maximum()
            ),
        )

    def clear(self) -> None:
        """Yeni sohbette eski balonları kaldırır; son öğe (esneme boşluğu) kalır."""
        while self.content_layout.count() > 1:
            item = self.content_layout.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.deleteLater()
        self._messages.clear()
        self._bubbles.clear()

    def toPlainText(self) -> str:
        """Test ve kopyalama için görüntülenen düz metin dökümü."""
        return "\n\n".join(f"{speaker}: {text}" for speaker, text in self._messages)

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        for bubble, speaker, text in self._bubbles:
            self._resize_bubble(bubble, speaker, text)

    def _resize_bubble(self, bubble: QFrame, speaker: str, text: str) -> None:
        available = max(200, self.width() - 28)
        max_width = min(520, available)
        longest_line = max(text.splitlines(), key=len, default="")
        natural = QFontMetrics(bubble.font()).horizontalAdvance(longest_line) + 48
        min_width = min(180 if _is_user_speaker(speaker) else 320, max_width)
        bubble.setFixedWidth(min(max_width, max(min_width, natural)))


class ApprovalDialog(QDialog):
    """İşçi beklerken Qt event loop'unu açık tutan açık seçim penceresi."""

    def __init__(self, item: Approval, on_decision: Any, parent: QWidget) -> None:
        super().__init__(parent)
        self.item = item
        self._on_decision = on_decision
        self._decided = False
        self.setWindowTitle("Artemis · İşlem onayı")
        self.setModal(True)
        self.setMinimumSize(540, 420)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(12)

        # Okunur ad büyük başlıktır; ham ad altında küçük yazılır (ADR 0014).
        title = QLabel(item.tool_label or item.tool_name)
        title.setObjectName("title")
        title.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(title)
        raw_name = QLabel(item.tool_name)
        raw_name.setObjectName("rawToolName")
        raw_name.setTextFormat(Qt.TextFormat.PlainText)
        raw_name.setAccessibleName("Ham araç adı")
        layout.addWidget(raw_name)
        notice = QLabel(item.notice or "Bu işlem onay gerektirir.")
        notice.setObjectName("amber")
        notice.setTextFormat(Qt.TextFormat.PlainText)
        notice.setWordWrap(True)
        notice.setStyleSheet(
            "QLabel { color:#FFD78C; background:#332B1E; border:1px solid #795D31; "
            "border-radius:16px; padding:12px 14px; }"
        )
        layout.addWidget(notice)
        layout.addWidget(QLabel("Doğrulanmış araç argümanları"))
        arguments = QPlainTextEdit()
        arguments.setObjectName("approvalArguments")
        arguments.setReadOnly(True)
        arguments.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        arguments.setPlainText(json.dumps(item.arguments, ensure_ascii=False, indent=2))
        arguments.setAccessibleName("Salt okunur doğrulanmış araç argümanları")
        layout.addWidget(arguments, 1)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.no_button = QPushButton("Hayır")
        self.no_button.setObjectName("danger")
        self.no_button.setDefault(True)
        self.no_button.setAutoDefault(True)
        self.yes_button = QPushButton("Evet, onayla")
        self.yes_button.setObjectName("primary")
        self.yes_button.setDefault(False)
        self.yes_button.setAutoDefault(False)
        buttons.addWidget(self.no_button)
        self.site_button: QPushButton | None = None
        if item.site:
            self.site_button = QPushButton(f"Evet, {item.site} için bir daha sorma")
            self.site_button.setAutoDefault(False)
            self.site_button.clicked.connect(lambda: self._choose(True, remember_site=True))
            buttons.addWidget(self.site_button)
        buttons.addWidget(self.yes_button)
        layout.addLayout(buttons)
        self.no_button.clicked.connect(lambda: self._choose(False))
        self.yes_button.clicked.connect(lambda: self._choose(True))
        self.no_button.setFocus(Qt.FocusReason.OtherFocusReason)

    def _choose(self, approved: bool, remember_site: bool = False) -> None:
        if self._decided:
            return
        self._decided = True
        accepted = self._on_decision(self.item, approved, remember_site)
        decision = approved and accepted
        self.done(QDialog.DialogCode.Accepted if decision else QDialog.DialogCode.Rejected)

    def reject(self) -> None:
        self._choose(False)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (
            Qt.Key.Key_Escape,
            Qt.Key.Key_Return,
            Qt.Key.Key_Enter,
        ):
            self._choose(False)
            event.accept()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event: QCloseEvent) -> None:
        if not self._decided:
            self._choose(False)
        event.accept()

    def showEvent(self, event: Any) -> None:
        super().showEvent(event)
        self.no_button.setFocus(Qt.FocusReason.ActiveWindowFocusReason)


class QuickOverlay(QWidget):
    def __init__(
        self,
        on_send: Any,
        on_cancel: Any,
        on_show_panel: Any,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent, Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint)
        self.setWindowTitle("Artemis · Hızlı komut")
        self.setMinimumSize(460, 160)
        self.resize(480, 170)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)
        top = QHBoxLayout()
        title = QLabel("ARTEMIS  /  HIZLI KOMUT")
        title.setObjectName("eyebrow")
        panel_button = QPushButton("Paneli aç")
        panel_button.clicked.connect(on_show_panel)
        self.status = QLabel("Ana panelle aynı oturum")
        self.status.setObjectName("muted")
        self.voice_state = QLabel(_VOICE_LABELS["off"])
        self.voice_state.setObjectName("muted")
        self.voice_state.setAccessibleName("Ses durumu")
        top.addWidget(title)
        top.addStretch(1)
        top.addWidget(panel_button)
        top.addWidget(self.voice_state)
        top.addWidget(self.status)
        layout.addLayout(top)
        self.input = PromptEdit(on_send, self)
        self.input.setMinimumHeight(54)
        self.input.setMaximumHeight(70)
        layout.addWidget(self.input)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.cancel_button = QPushButton("İptal et")
        self.cancel_button.setObjectName("danger")
        self.send_button = QPushButton("Gönder")
        self.send_button.setObjectName("primary")
        self.cancel_button.clicked.connect(on_cancel)
        self.send_button.clicked.connect(on_send)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.send_button)
        layout.addLayout(buttons)

    def set_busy(self, busy: bool, status: str = "") -> None:
        self.input.setEnabled(not busy)
        self.send_button.setEnabled(not busy)
        self.cancel_button.setEnabled(busy)
        self.status.setText(status or ("Çalışıyor…" if busy else "Ana panelle aynı oturum"))


class DesktopWindow(QMainWindow):
    # Kısayol kendi iş parçacığından yayar; Qt bunu ana iş parçacığına sıraya alır.
    _hotkey_pressed = Signal()

    def __init__(
        self,
        config_path: Path,
        *,
        demo: bool = False,
        voice: bool = False,
        app: QApplication | None = None,
    ) -> None:
        super().__init__()
        app_instance = app if app is not None else QApplication.instance()
        if not isinstance(app_instance, QApplication):
            raise RuntimeError("Önce QApplication oluşturulmalı.")
        self._app = app_instance
        self._config_path = config_path
        self._restart_settings = False
        self._demo = demo
        self._continuous = False
        self._voice_state = "off"
        # Demo hiçbir koşulda mikrofon açmaz; --ses bayrağı da demoda yok sayılır.
        self._voice_on_ready = voice and not demo
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        if demo:
            self._temporary = tempfile.TemporaryDirectory(prefix="artemis-demo-")
            temp_root = Path(self._temporary.name)

            def factory(path: Path, approve: Any, on_record: Any) -> Runtime:
                from artemis.channels.desktop.demo import create_demo_parts

                settings, provider, registry, audit = create_demo_parts(temp_root, on_record)
                runtime = create_runtime(
                    path,
                    approve,
                    settings=settings,
                    provider=provider,
                    registry=registry,
                    audit=audit,
                )
                runtime.context.services["demo_records"] = {"sample": "Demo kitaplığı: örnek metin"}
                return runtime

            self.bridge = DesktopBridge(config_path, factory)
        else:
            self.bridge = DesktopBridge(config_path)
        self.setWindowTitle("Artemis")
        self.resize(1180, 780)
        self.setMinimumSize(720, 560)
        self._busy = False
        self._worker_ready = False
        self._exiting = False
        self._allow_close = False
        self._tray: QSystemTrayIcon | None = None
        self._autostart_action: QAction | None = None
        self._tray_attempts = 0
        self._approval_dialog: ApprovalDialog | None = None
        self._active_request: str | None = None
        self._turn_steps: dict[str, _TurnSteps] = {}
        self._activity_count = 0
        self._activity_empty = True
        self._build_pet()
        self._build_ui()
        self._build_overlay()
        self._build_tray()
        self._hotkey: GlobalHotkey | None = None
        if not demo:
            self._start_hotkey()
        self.pet.start()
        self._timer = QTimer(self)
        self._timer.setInterval(60)
        self._timer.timeout.connect(self._drain_bridge)
        self._timer.start()

    def _build_pet(self) -> None:
        # Pet yalnız durum alır; komut ve onay yolları panelinkiyle aynıdır.
        self.pet = PetWidget(
            on_toggle_voice=self.voice_button_click,
            on_show_panel=self._show_panel,
            on_quick=self._show_overlay,
            on_approval=self._focus_approval,
            on_quit=self._request_exit,
        )

    def _start_hotkey(self) -> None:
        try:
            text = load_settings(self._config_path).voice.listening_hotkey.strip()
        except (OSError, ValueError, ValidationError, yaml.YAMLError):
            return
        if not text:
            return
        try:
            hotkey = GlobalHotkey(text, self._hotkey_pressed.emit)
        except ValueError:
            self._hotkey_problem(f"Ses kısayolu tanınmadı: {text}")
            return
        self._hotkey_pressed.connect(self.voice_button_click)
        if hotkey.start():
            self._hotkey = hotkey
        elif sys.platform == "win32":
            self._hotkey_problem(f"Ses kısayolu ({text}) başka bir programda kayıtlı.")

    def _hotkey_problem(self, message: str) -> None:
        if self._tray is not None:
            self._tray.showMessage("Artemis", message)
        else:
            self._set_status(message, "error")

    def voice_button_click(self) -> None:
        """Pet tıkı: panelin "Ses" düğmesiyle aynı yol (_toggle_voice); düğme kapalıysa işlemez."""
        self.voice_button.click()

    def _focus_approval(self) -> None:
        dialog = self._approval_dialog
        if dialog is None:
            self._show_panel()
            return
        self._show_panel()
        dialog.raise_()
        dialog.activateWindow()

    def _build_ui(self) -> None:
        root = QWidget()
        main = QVBoxLayout(root)
        main.setContentsMargins(24, 20, 24, 22)
        main.setSpacing(16)

        header = QFrame()
        header.setObjectName("card")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(18, 15, 18, 15)
        header_layout.setSpacing(14)
        mark = Mascot()
        header_layout.addWidget(mark)
        title_stack = QVBoxLayout()
        title = QLabel("Artemis")
        title.setObjectName("title")
        subtitle = QLabel("Güvenli masaüstü asistanı")
        subtitle.setObjectName("muted")
        title_stack.addWidget(title)
        title_stack.addWidget(subtitle)
        header_layout.addLayout(title_stack)
        if self._demo:
            demo_banner = QLabel("DEMO · GERÇEK İŞLEM YOK")
            demo_banner.setObjectName("demo")
            demo_banner.setFixedHeight(30)
            demo_banner.setAccessibleName("Demo modu; gerçek işlem yapılmaz")
            header_layout.addWidget(demo_banner, 0, Qt.AlignmentFlag.AlignVCenter)
        header_layout.addStretch(1)
        model_stack = QVBoxLayout()
        model_caption = QLabel("SEÇİLİ MODEL")
        model_caption.setObjectName("eyebrow")
        self.model_value = QLabel("Yükleniyor…")
        self.model_value.setObjectName("muted")
        self.model_value.setTextFormat(Qt.TextFormat.PlainText)
        model_stack.addWidget(model_caption, 0, Qt.AlignmentFlag.AlignRight)
        model_stack.addWidget(self.model_value, 0, Qt.AlignmentFlag.AlignRight)
        header_layout.addLayout(model_stack)
        self.voice_dot = QLabel("●")
        self.voice_dot.setObjectName("voiceDot")
        self.voice_dot.setStyleSheet(_dot_style(_VOICE_TONES["off"]))
        self.voice_state = QLabel(_VOICE_LABELS["off"])
        self.voice_state.setObjectName("muted")
        self.voice_state.setAccessibleName("Ses durumu")
        self.voice_button = QPushButton("Ses: kapalı")
        self.voice_button.setCheckable(True)
        self.voice_button.setObjectName("voiceToggle")
        self.voice_button.setAccessibleName("Sesi aç veya kapat")
        if self._demo:
            self.voice_button.setToolTip("Demo modunda ses yok")
        else:
            self.voice_button.setToolTip("Mikrofonu yalnız siz açarsınız; onaylar ekrandadır")
        self.voice_button.clicked.connect(self._toggle_voice)
        voice_chip = self._chip(self.voice_dot, self.voice_state)
        header_layout.addWidget(voice_chip, 0, Qt.AlignmentFlag.AlignVCenter)
        header_layout.addWidget(self.voice_button)
        self.auto_button = QPushButton("Oto onay: kapalı")
        self.auto_button.setCheckable(True)
        self.auto_button.setObjectName("voiceToggle")
        self.auto_button.setAccessibleName("Bilgisayar kullanımında otomatik onayı aç veya kapat")
        self.auto_button.setToolTip(
            "Açıkken bilgisayar kullanımı adımları sormadan çalışır; Esc yine durdurur. "
            "Değişiklik hemen geçerli, Uygula gerekmez."
        )
        self.auto_button.toggled.connect(self._toggle_auto_mode)
        header_layout.addWidget(self.auto_button)
        self.status_dot = QLabel("●")
        self.status_dot.setObjectName("statusDot")
        self.status_label = QLabel("")
        self.status_label.setObjectName("status")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setMinimumWidth(92)
        self._set_status("Başlatılıyor…", "running")
        status_chip = self._chip(self.status_dot, self.status_label)
        header_layout.addWidget(status_chip, 0, Qt.AlignmentFlag.AlignVCenter)
        main.addWidget(header)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        conversation = QFrame()
        conversation.setObjectName("card")
        conversation_layout = QVBoxLayout(conversation)
        conversation_layout.setContentsMargins(18, 16, 18, 16)
        conversation_layout.setSpacing(12)
        conversation_header = QHBoxLayout()
        chat_title = QLabel("Sohbet")
        chat_title.setObjectName("title")
        self.steps_toggle = QPushButton("İşlem adımları")
        self.steps_toggle.clicked.connect(self._toggle_steps)
        self.quick_action = QPushButton("Hızlı komut")
        self.quick_action.clicked.connect(self._show_overlay)
        self.new_chat_button = QPushButton("Yeni sohbet")
        self.new_chat_button.setToolTip("Önceki konuşmayı unutturur")
        self.new_chat_button.clicked.connect(self._new_chat)
        conversation_header.addWidget(chat_title)
        conversation_header.addStretch(1)
        conversation_header.addWidget(self.new_chat_button)
        conversation_header.addWidget(self.quick_action)
        conversation_header.addWidget(self.steps_toggle)
        conversation_layout.addLayout(conversation_header)
        self.chat = ChatTranscript(self._demo, self._set_suggestion)
        self.chat.setObjectName("chatTranscript")
        self.chat.setAccessibleName("Seçilebilir sohbet geçmişi")
        conversation_layout.addWidget(self.chat, 1)
        input_card = QFrame()
        input_card.setObjectName("raised")
        input_layout = QVBoxLayout(input_card)
        input_layout.setContentsMargins(12, 10, 12, 10)
        input_layout.setSpacing(8)
        self.prompt = PromptEdit(self._submit_main)
        self.prompt.setObjectName("mainPrompt")
        self.prompt.setMinimumHeight(38)
        self.prompt.setMaximumHeight(56)
        self.prompt.setPlaceholderText(
            "Artemis'e ne yapmak istediğinizi yazın…  (Enter gönderir · Shift+Enter yeni satır)"
        )
        input_layout.addWidget(self.prompt)
        input_controls = QHBoxLayout()
        self.cancel_button = QPushButton("İptal et")
        self.cancel_button.setObjectName("danger")
        self.send_button = QPushButton("Gönder")
        self.send_button.setObjectName("primary")
        self.cancel_button.clicked.connect(self._cancel_active)
        self.send_button.clicked.connect(self._submit_main)
        input_controls.addStretch(1)
        input_controls.addWidget(self.cancel_button)
        input_controls.addWidget(self.send_button)
        input_layout.addLayout(input_controls)
        conversation_layout.addWidget(input_card)
        self.splitter.addWidget(conversation)

        activity_card = QFrame()
        activity_card.setObjectName("card")
        self.activity_panel = activity_card
        activity_layout = QVBoxLayout(activity_card)
        activity_layout.setContentsMargins(16, 16, 16, 16)
        activity_layout.setSpacing(10)
        activity_title = QLabel("İşlem adımları")
        activity_title.setObjectName("title")
        activity_hint = QLabel("Gerçek araç ve onay olayları")
        activity_hint.setObjectName("muted")
        self.activity = QListWidget()
        self.activity.setAccessibleName("Gerçek işlem adımları")
        self.activity.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.activity.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.activity.addItem("Henüz bir işlem yok")
        self.activity.item(0).setFlags(Qt.ItemFlag.NoItemFlags)
        self.activity.item(0).setForeground(QColor("#718096"))
        self.activity.item(0).setToolTip("Henüz bir işlem yok")
        activity_layout.addWidget(activity_title)
        activity_layout.addWidget(activity_hint)
        activity_layout.addWidget(self.activity, 1)
        self.splitter.addWidget(activity_card)
        self.splitter.setStretchFactor(0, 65)
        self.splitter.setStretchFactor(1, 35)
        self.splitter.setSizes([740, 390])
        self.tabs = QTabWidget()
        self.tabs.addTab(self.splitter, "Sohbet")
        self.projects_panel = ProjectsPanel(demo=self._demo)
        self.projects_panel.tool_requested.connect(self._submit_project_tool)
        self._projects_timer = QTimer(self)
        self._projects_timer.setInterval(2000)
        self._projects_timer.timeout.connect(self._refresh_projects)
        self._projects_timer.start()
        self.settings_panel = SettingsPanel(self._config_path, demo=self._demo)
        self.settings_panel.apply_requested.connect(self._apply_settings)
        # Üst çubuktaki düğme açılışta config'teki kipi gösterir; sinyal kapalı ki açılış
        # kendiliğinden config yazmasın.
        on = self.settings_panel.computer_auto.isChecked()
        self.auto_button.blockSignals(True)
        self.auto_button.setChecked(on)
        self.auto_button.blockSignals(False)
        self.auto_button.setText(f"Oto onay: {'açık' if on else 'kapalı'}")
        self.settings_panel.continuous_mode.toggled.connect(self._live_continuous)
        self.settings_panel.computer_auto.toggled.connect(self._toggle_auto_mode)
        self.settings_panel.github_synced.connect(self._github_synced)
        self.settings_panel.providers.changed.connect(self._providers_changed)
        self.settings_scroll = QScrollArea()
        self.settings_scroll.setWidgetResizable(True)
        self.settings_scroll.setWidget(self.settings_panel)
        self.tabs.addTab(self.settings_scroll, "Ayarlar")
        self.tabs.addTab(self.projects_panel, "Projeler")
        main.addWidget(self.tabs, 1)

        self.setCentralWidget(root)
        self.setStyleSheet(_STYLE)
        self._set_controls()
        self.activity_panel.hide()
        self.steps_toggle.hide()
        QTimer.singleShot(0, self.prompt.setFocus)
        # Açılışta bir kez, arka plan thread'inde; CLI'da eşitleme yoktur.
        QTimer.singleShot(0, self.settings_panel.start_auto_sync)
        if not self._demo:
            self._check_provider(open_settings=True)

    def _check_provider(self, *, open_settings: bool) -> None:
        """Birincil model çağrılamayacaksa ilk tur patlamadan Ayarlar'ı sağlayıcı bölümünde açar."""
        try:
            reason = provider_problem(load_settings(self._config_path))
        except (OSError, ValueError):
            return
        self.settings_panel.providers.show_problem(reason)
        if reason and open_settings:
            self.tabs.setCurrentWidget(self.settings_scroll)
            # Sağlayıcı eksikse Ayarlar doğrudan Model sekmesinde açılır.
            self.settings_panel.sections.setCurrentIndex(1)
            QTimer.singleShot(
                0, lambda: self.settings_scroll.ensureWidgetVisible(self.settings_panel.providers)
            )

    def _providers_changed(self) -> None:
        self._check_provider(open_settings=False)
        if self._busy or self._exiting or self._demo:
            self.settings_panel.providers.status.setText(
                "Kaydedildi; işlem bitince Uygula ile yeni ayarlar devreye girer."
            )
            return
        self._restart_runtime()

    @staticmethod
    def _chip(dot: QLabel, label: QLabel) -> QFrame:
        """Nokta + metin tek hap rozetinde; widget'lar aynı kalır, yalnız kapsayıcı eklenir."""
        chip = QFrame()
        chip.setObjectName("chip")
        row = QHBoxLayout(chip)
        row.setContentsMargins(12, 3, 14, 3)
        row.setSpacing(6)
        row.addWidget(dot)
        row.addWidget(label)
        return chip

    def _github_synced(self, report: Any, auto: bool) -> None:
        if not (auto and report.cloned):
            return
        text = (
            f"{len(report.cloned)} yeni GitHub reposu çekildi; "
            "Ayarlar'da Uygula ile kullanıma girer."
        )
        self.pet.say(text, "job")
        if self._tray is not None:
            self._tray.showMessage("Artemis", text, QSystemTrayIcon.MessageIcon.Information, 6000)

    def _build_overlay(self) -> None:
        self.overlay = QuickOverlay(self._submit_overlay, self._cancel_active, self._show_panel)
        self.overlay.setStyleSheet(_STYLE)

    def _apply_settings(self, device: int | str | None) -> None:
        if self._demo or self._busy or self._exiting:
            self.settings_panel.notice.setText(
                "Ayarları değiştirmek için işlemin bitmesini bekleyin."
            )
            return
        try:
            save_choices(
                self._config_path,
                device,
                values=self.settings_panel.values(),
                applications=self.settings_panel.applications,
                code_agent=self.settings_panel.github_values(),
                stt_model=self.settings_panel.stt_model_path(),
            )
        except (OSError, ValueError, KeyError, TypeError):
            self.settings_panel.notice.setText("Ayarlar kaydedilemedi; mevcut ayarlar korunuyor.")
            return
        self._restart_runtime()

    def _restart_runtime(self) -> None:
        """Config değişince çalışma zamanını yeniden kurar (Uygula ve sağlayıcı kaydı)."""
        self._voice_on_ready = self.bridge.voice_active
        self._restart_settings = True
        self._worker_ready = False
        self._busy = True
        self.bridge.shutdown()
        self._set_status("Ayarlar uygulanıyor…", "running")
        self._set_controls()

    def _build_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self._tray = QSystemTrayIcon(app_icon(), self)
        self._tray.setToolTip("Artemis")
        menu = QMenu(self)
        open_action = menu.addAction("Paneli aç")
        quick_action = menu.addAction("Hızlı komut")
        pet_action = menu.addAction("Peti göster/gizle")
        if sys.platform == "win32" and not self._demo:
            # ADR 0030: kayıt defteri değişikliği yalnız kullanıcının bu tıklamasıyla olur.
            self._autostart_action = menu.addAction("Windows açılışında başlat")
            self._autostart_action.setCheckable(True)
            self._autostart_action.setChecked(self._autostart_state() == "on")
            self._autostart_action.toggled.connect(self._toggle_autostart)
        menu.addSeparator()
        exit_action = menu.addAction("Çıkış")
        open_action.triggered.connect(self._show_panel)
        quick_action.triggered.connect(self._show_overlay)
        pet_action.triggered.connect(self.pet.toggle_pet)
        exit_action.triggered.connect(self._request_exit)
        self._tray.setContextMenu(menu)
        self._tray.activated.connect(self._tray_activated)
        self._tray.show()
        self._app.setQuitOnLastWindowClosed(False)

    def _autostart(self) -> Autostart | None:
        try:
            settings = load_settings(self._config_path)
        except (OSError, ValueError, ValidationError, yaml.YAMLError):
            return None
        return Autostart(self._config_path, emit=AuditLog(settings.audit_path).emit)

    def _autostart_state(self) -> str:
        autostart = self._autostart()
        return autostart.status().state if autostart is not None else "off"

    def _toggle_autostart(self, enabled: bool) -> None:
        autostart = self._autostart()
        if autostart is None:
            message, success = "Ayarlar okunamadı; açılış kaydı değiştirilmedi.", False
        else:
            result = autostart.enable() if enabled else autostart.disable()
            message, success = result.message, result.success
        if not success and self._autostart_action is not None:
            # Seçim kutusu gerçek durumu göstermeli: başarısızlıkta eski haline döner.
            self._autostart_action.blockSignals(True)
            self._autostart_action.setChecked(not enabled)
            self._autostart_action.blockSignals(False)
        if self._tray is not None:
            self._tray.showMessage(
                "Artemis", message, QSystemTrayIcon.MessageIcon.Information, 6000
            )

    def present(self, *, background: bool) -> None:
        """`--arka-plan` paneli göstermez; tepsi yoksa görünmez süreç bırakılmaz.

        Windows oturum açılışında Explorer tepsisi Artemis'ten sonra hazır olabilir: arka
        planda tepsi bir süre yeniden denenir, ancak hâlâ yoksa panel gösterilir.
        """
        if not background:
            self.show()
            return
        self._tray_attempts = 0
        self._wait_for_tray()

    def _wait_for_tray(self) -> None:
        if self._tray is None and not self._exiting:
            self._build_tray()
        if self._tray is not None or self._exiting:
            return
        self._tray_attempts += 1
        if self._tray_attempts >= TRAY_RETRY_ATTEMPTS:
            self.show()
            return
        QTimer.singleShot(TRAY_RETRY_MS, self, self._wait_for_tray)

    def _tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._show_panel()

    def show_from_instance(self) -> None:
        if not self._exiting:
            self._show_panel()

    def _show_panel(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _show_overlay(self) -> None:
        self.overlay.show()
        self.overlay.raise_()
        self.overlay.activateWindow()
        self.overlay.input.setFocus()

    def _toggle_steps(self) -> None:
        self.activity_panel.setVisible(not self.activity_panel.isVisible())

    def resizeEvent(self, event: Any) -> None:
        narrow = self.width() < 900
        self.steps_toggle.setVisible(narrow)
        # Ham etkinlik paneli kendiliğinden açılmaz; adımlar sohbette gösterilir.
        if not narrow or self.activity_panel.isVisible():
            self.activity_panel.hide()
        super().resizeEvent(event)

    def _submit_main(self) -> None:
        text = self.prompt.toPlainText().strip()
        if text and self._submit_text(text):
            self.prompt.clear()

    def _set_suggestion(self, text: str) -> None:
        self.prompt.setPlainText(text)
        self.prompt.setFocus()

    def _submit_overlay(self) -> None:
        text = self.overlay.input.toPlainText().strip()
        if text and self._submit_text(text):
            self.overlay.input.clear()

    def _submit_text(self, text: str) -> bool:
        request_id = self.bridge.submit(text)
        if request_id is None:
            self._set_status("Hazır değil veya önceki işlem temizleniyor.", "running")
            return False
        self._active_request = request_id
        self._busy = True
        self._append_chat("Siz", text)
        self._set_status("Çalışıyor…", "running")
        self._set_controls()
        self.overlay.set_busy(True)
        return True

    def _submit_project_tool(self, name: str, arguments: BaseModel) -> None:
        request_id = self.bridge.submit_tool(name, arguments)
        if request_id is None:
            self.projects_panel.notice.setText("Başka işlem sürüyor; istek çalıştırılmadı.")
            return
        self._active_request = request_id
        self._busy = True
        self._set_controls()

    def _refresh_projects(self) -> None:
        if (
            self.tabs.currentWidget() is self.projects_panel
            and self._worker_ready
            and not self._busy
        ):
            self.projects_panel.refresh()

    def _new_chat(self) -> None:
        # Ekran, işçi sıfırlamayı doğruladığında (context_reset olayı) temizlenir;
        # işçi reddederse görünen sohbet ile modelin bağlamı ayrışmaz.
        if self.bridge.reset_conversation():
            self.new_chat_button.setEnabled(False)

    def _cancel_active(self) -> None:
        if self._active_request is not None:
            self.bridge.cancel(self._active_request)
        if self._approval_dialog is not None:
            self._approval_dialog.reject()

    def _toggle_voice(self, checked: bool) -> None:
        if checked:
            if not self.bridge.start_voice():
                self.voice_button.setChecked(False)
        elif not self.bridge.stop_voice():
            self.voice_button.setChecked(False)

    def _toggle_auto_mode(self, checked: bool) -> None:
        """Üst çubuktaki düğme: kip anında değişir ve config'e yazılır.

        Kip oturum içi onayı belirler; tarayıcıdan sonra "Uygula" beklemek istenmeyen
        sürpriz olurdu, bu yüzden düğme hem runtime'a hem config'e anında yazar.
        """
        mode = "auto" if checked else "accept"
        saved = True
        if not self._demo:
            # Önce config: yazılamayan kip runtime'a uygulanırsa güvenlik ayarı sessizce
            # diskten ayrışırdı (yeniden açılışta eski kip döner).
            try:
                save_computer_mode(self._config_path, mode)
            except (OSError, ValueError):
                saved = False
                self._set_status("Kip kaydedilemedi", "error")
            else:
                if not self.bridge.set_computer_mode(mode):
                    # Runtime hazır değilse yalnız config yazılmıştır; açılışta geçerli olur.
                    self._set_status("Kip kaydedilecek", "running")
        shown = checked if saved else not checked
        # İki denetim de sinyalsiz eşitlenir: düğme ve ayar kutusu aynı kipi göstermeli.
        for control in (self.settings_panel.computer_auto, self.auto_button):
            control.blockSignals(True)
            control.setChecked(shown)
            control.blockSignals(False)
        self.auto_button.setText(f"Oto onay: {'açık' if shown else 'kapalı'}")
        if saved and not self._demo:
            self._append_chat("Sistem", f"Bilgisayar kullanımı kipi: {mode}.")

    def _live_continuous(self, checked: bool) -> None:
        # Kayıt Uygula ile olur; ses açıksa kip hemen uygulanır.
        self.bridge.set_continuous(checked)

    def _show_continuous(self, on: bool) -> None:
        self._continuous = on
        box = self.settings_panel.continuous_mode
        box.blockSignals(True)
        box.setChecked(on)
        box.blockSignals(False)
        self._show_voice_state(self._voice_state)

    def _set_status(self, text: str, tone: str) -> None:
        # Durum düz metindir; ton yalnız yanındaki noktanın rengini belirler.
        self.status_label.setText(text)
        self.status_dot.setStyleSheet(_dot_style(_STATUS_TONES[tone]))

    def _show_voice_state(self, state: str) -> None:
        self._voice_state = state
        if state == "off":
            self._continuous = False
        text = _VOICE_LABELS.get(state, _VOICE_LABELS["off"])
        if self._continuous:
            text += " · Kesintisiz"
        self.voice_dot.setStyleSheet(_dot_style(_VOICE_TONES.get(state, _VOICE_TONES["off"])))
        self.voice_state.setText(text)
        self.overlay.voice_state.setText(text)
        on = state in {"preparing", "listening", "awaiting_command", "thinking", "speaking"}
        self.voice_button.setChecked(on)
        self.voice_button.setText("Ses: açık" if on else "Ses: kapalı")
        self.pet.set_voice(state)
        if state == "off":
            self.settings_panel.input_level.setValue(0)

    def _set_controls(self) -> None:
        enabled = self._worker_ready and not self._busy and not self._exiting
        self.settings_panel.apply_button.setEnabled(enabled and not self._demo)
        self.voice_button.setEnabled(self._worker_ready and not self._exiting and not self._demo)
        # Kip değişimi çalışan turu kesmez; yalnız bitişte kapanmasın diye korunur.
        self.auto_button.setEnabled(not self._exiting and not self._demo)
        self.projects_panel.set_busy(not enabled)
        self.prompt.setEnabled(enabled)
        self.send_button.setEnabled(enabled)
        self.new_chat_button.setEnabled(enabled)
        self.cancel_button.setEnabled(self._busy and not self._exiting)
        self.pet.set_busy(self._busy and not self._exiting)
        if self._tray is not None:
            status = "çalışıyor" if self._busy else "hazır"
            self._tray.setToolTip(f"Artemis · {status}")

    def _drain_bridge(self) -> None:
        for event in self.bridge.poll_events(64):
            self._handle_event(event)
        if self.bridge.stopped:
            if self._restart_settings and not self._exiting:
                self._restart_settings = False
                self._busy = False
                self._active_request = None
                self._turn_steps.clear()
                self.chat.clear()
                self._append_chat("Sistem", "Ayarlar uygulandı; yeni sohbet başladı.")
                self._show_voice_state("off")
                self.pet.set_busy(False)
                self.bridge = DesktopBridge(self._config_path)
                self.settings_panel.refresh()
                self.settings_panel.notice.setText(
                    "Ayarlar uygulandı; seçilen model ve mikrofon kullanılıyor."
                )
            else:
                self._finish_exit()

    def _handle_event(self, event: BridgeEvent) -> None:
        if isinstance(event, State):
            if event.request_id is not None and event.request_id != self._active_request:
                return
            if event.state == "ready":
                if (
                    event.request_id is None
                    and not self._busy
                    and event.message.startswith("Hazır · ")
                ):
                    self.model_value.setText(event.message.removeprefix("Hazır · "))
                if event.request_id is not None or not self._busy:
                    self._worker_ready = True
                    self._set_status("Hazır", "ready")
                    self._busy = False
                    self._active_request = None
                    self._set_controls()
                    self.overlay.set_busy(False, "Hazır")
                if self._voice_on_ready and self._worker_ready:
                    self._voice_on_ready = False
                    self.bridge.start_voice()
                if event.request_id is not None:
                    self._dismiss_approval_for(event.request_id)
            elif event.state == "running":
                self._busy = True
                self._set_status("Çalışıyor…", "running")
                self._set_controls()
                self.overlay.set_busy(True)
            elif event.state == "computer":
                # Bilgisayar kullanımı oturumu (ADR 0039): ne yaptığı ve nasıl durdurulacağı
                # hep görünür; boş metin oturum bitti demektir, normal duruma dönülür.
                if event.message:
                    line = f"Bilgisayarı kullanıyorum · {event.message} · durdurmak için Esc"
                    self._set_status(line, "running")
                    self.overlay.set_busy(True, line)
                elif self._busy:
                    self._set_status("Çalışıyor…", "running")
                    self.overlay.set_busy(True)
                else:
                    self._set_status("Hazır", "ready")
            elif event.state == "cleanup_pending":
                self._busy = True
                self._set_status("Önceki işlem temizleniyor…", "running")
                self.overlay.set_busy(True, "Temizleniyor…")
                self._set_controls()
            elif event.state == "voice":
                self._show_voice_state(event.message)
            elif event.state == "voice_mode":
                self._show_continuous(event.message == "on")
            elif event.state == "voice_level":
                level = level_from_message(event.message)
                if level is not None:
                    self.settings_panel.input_level.setValue(round(level * 100))
                    self.pet.set_level(level)
            elif event.state == "voice_heard":
                self.settings_panel.last_heard.setText(event.message)
                self.pet.heard(event.message)
            elif event.state == "voice_denoise":
                if event.message.startswith("unavailable:"):
                    kind, _, reason = event.message.removeprefix("unavailable:").partition(":")
                    fix = (
                        'Paketleri `pip install -e ".[voice]"` ile kurun'
                        if kind == "deps"
                        else "Artemis'i yeniden kurun veya `voice.noise_model_dir` ayarına bakın"
                    )
                    self._append_chat(
                        "Artemis",
                        f"Gürültü engelleme açılamadı: {reason}. {fix}; ses işlenmeden sürüyor.",
                    )
                elif event.message == "off":
                    self._append_chat(
                        "Artemis",
                        "Gürültü engelleme hata verdi ve bu oturumda kapatıldı; "
                        "ses işlenmeden devam ediyor.",
                    )
            elif event.state == "voice_echo":
                if event.message == "off":
                    self._append_chat(
                        "Artemis",
                        "Yankı engelleme hata verdi ve bu oturumda kapatıldı; "
                        "ses yankı gidermeden sürüyor.",
                    )
            elif event.state == "voice_error":
                self._append_chat("Artemis", event.message)
                self.pet.flash_error()
                self._show_voice_state("off")
            elif event.state == "context_reset":
                # Adım blokları eski balonlarla birlikte silinir; kayıt da sıfırlanır.
                self._turn_steps.clear()
                self.chat.clear()
                self._append_chat("Sistem", "Yeni sohbet başladı.")
                if not self._busy:
                    self._set_status("Hazır", "ready")
                self._set_controls()
            elif event.state == "cancelling":
                self._set_status("İptal ediliyor…", "running")
            elif event.state == "stopping":
                self._set_status("Temizleniyor…", "running")
            elif event.state == "stopped":
                self._finish_exit()
            elif event.state == "cancelled":
                self._set_status("İptal edildi", "ready")
            return
        if isinstance(event, VoiceCommand):
            self._active_request = event.request_id
            self._busy = True
            self._append_chat("Siz (ses)", event.text)
            self._set_status("Çalışıyor…", "running")
            self._set_controls()
            self.overlay.set_busy(True)
            return
        if isinstance(event, Approval):
            self._show_approval(event)
            return
        if isinstance(event, ToolFinished):
            if event.request_id == self._active_request:
                self._dismiss_approval_for(event.request_id)
                self.projects_panel.accept_result(event.tool_name, event.result)
            return
        if isinstance(event, Finished):
            if event.request_id == self._active_request:
                self._dismiss_approval_for(event.request_id)
                self._append_chat(
                    "Artemis", event.result.text, self._step_block_for(event.request_id)
                )
                self.pet.answer(event.result.text)
                self.overlay.status.setText("Yanıt hazır")
            return
        if isinstance(event, Failed):
            if not event.request_id:
                self._set_status(f"Başlatılamadı · {event.error_type}", "error")
                self._append_chat("Artemis", event.message)
            elif event.request_id == self._active_request:
                self._dismiss_approval_for(event.request_id)
                self._append_chat(
                    "Artemis",
                    f"İstek tamamlanamadı ({event.error_type}).",
                    self._step_block_for(event.request_id),
                )
                self.pet.flash_error()
            return
        if isinstance(event, Proactive):
            # Kendiliğinden mesaj ayrı konuşmacıyla görünür; modelin geçmişine eklenmez.
            self._append_chat("Artemis (kendiliğinden)", f"{event.title}\n{event.body}")
            self.pet.answer(event.body)
            return
        if isinstance(event, ActivityGap):
            self._add_activity(f"{event.dropped_count} eski etkinlik görünümden düştü", "#FFD78C")
            return
        if isinstance(event, Activity):
            self._pet_activity(event)
            self._render_activity(event)
            self._collect_step(event)

    def _pet_activity(self, event: Activity) -> None:
        text = self.pet.code_agent_event(event.event, event.fields)
        if text is not None and self._tray is not None:
            self._tray.showMessage("Artemis", text, QSystemTrayIcon.MessageIcon.Information, 6000)

    def _show_approval(self, item: Approval) -> None:
        if item.request_id != self._active_request:
            self.bridge.decide(item.request_id, item.turn_id, item.approval_id, False)
            return
        if self._approval_dialog is not None:
            self._approval_dialog.reject()
        self.overlay.hide()
        self._show_panel()
        dialog = ApprovalDialog(item, self._decide_approval, self)
        self._approval_dialog = dialog
        dialog.finished.connect(lambda _result, current=dialog: self._clear_approval(current))
        dialog.open()
        self.pet.set_approval(True)

    def _decide_approval(
        self, item: Approval, approved: bool, remember_site: bool = False
    ) -> bool:
        return self.bridge.decide(
            item.request_id, item.turn_id, item.approval_id, approved, remember_site
        )

    def _dismiss_approval_for(self, request_id: str) -> None:
        dialog = self._approval_dialog
        if dialog is not None and dialog.item.request_id == request_id:
            dialog.reject()

    def _clear_approval(self, dialog: ApprovalDialog) -> None:
        if self._approval_dialog is dialog:
            self._approval_dialog = None
            self.pet.set_approval(False)

    def _render_activity(self, event: Activity) -> None:
        fields = event.fields
        tool = str(fields.get("tool", ""))
        if event.event == "turn_start":
            label = "İstek alındı"
        elif event.event == "tool_requested":
            label = f"Araç istendi · {tool}"
        elif event.event == "tool_dispatch":
            label = f"Araç başlatıldı · {tool}"
        elif event.event == "tool_result":
            result = fields.get("result", {})
            status = (
                result.get("status", "bilinmiyor") if isinstance(result, dict) else "bilinmiyor"
            )
            status_labels = {
                "ok": "tamamlandı",
                "error": "başarısız",
                "denied": "reddedildi",
                "sent": "gönderildi; başarı doğrulanmadı",
            }
            label = f"Araç sonucu · {tool} · {status_labels.get(str(status), 'bilinmiyor')}"
        elif event.event == "approval":
            label = f"Onay {'verildi' if fields.get('approved') is True else 'reddedildi'} · {tool}"
        elif event.event == "tool_interrupted":
            label = f"Araç kesildi; sonuç doğrulanamadı · {tool}"
        elif event.event == "turn_end":
            reasons = {
                "completed": "yanıt hazır",
                "cancelled": "iptal edildi",
                "timeout": "süre sınırı",
                "budget": "bütçe sınırı",
                "max_steps": "adım sınırı",
                "cleanup_pending": "önceki işlem temizleniyor",
                "denied_repeat": "reddedilen çağrı tekrarlandı; durduruldu",
                "repeated_call": "yinelenen çağrı durduruldu",
                "local_limit": "yerel araç sınırı",
            }
            reason = str(fields.get("stop_reason", "unknown"))
            label = f"Tur tamamlandı · {reasons.get(reason, 'sonuç bilinmiyor')}"
        elif event.event == "model_response":
            label = "Model yanıtı alındı"
        elif event.event == "privacy_decision":
            label = "Gizlilik kontrolü"
        else:
            label = event.event.replace("_", " ")
        color = "#F0B2AA" if "failed" in event.event or "exception" in event.event else "#A9B8C9"
        self._add_activity(f"{event.seq:04d}  {label}", color)

    def _add_activity(self, text: str, color: str = "#A9B8C9") -> None:
        if self._activity_empty:
            self.activity.clear()
            self._activity_empty = False
        self.activity.addItem(text)
        row = self.activity.count() - 1
        self.activity.item(row).setForeground(QColor(color))
        self.activity.item(row).setToolTip(text)
        self._activity_count += 1
        while self.activity.count() > 500:
            self.activity.takeItem(0)
        self.activity.scrollToBottom()

    def _append_chat(self, speaker: str, text: str, steps: StepBlock | None = None) -> None:
        self.chat.append(speaker, text, steps)

    def _turn_for(self, request_id: str) -> _TurnSteps:
        turn = self._turn_steps.get(request_id)
        if turn is None:
            turn = _TurnSteps()
            self._turn_steps[request_id] = turn
            while len(self._turn_steps) > _STEP_TURNS_KEPT:
                del self._turn_steps[next(iter(self._turn_steps))]
        return turn

    def _collect_step(self, event: Activity) -> None:
        if event.request_id is None:
            return
        turn = self._turn_for(event.request_id)
        if len(turn.activities) >= _STEP_EVENTS_PER_TURN:
            return
        turn.activities.append(event)
        if turn.block is not None:
            self._add_step(turn.block, event)

    def _step_block_for(self, request_id: str) -> StepBlock:
        """Yanıt balonunun üstündeki blok; bloğa henüz satır gelmediyse gizli kalır."""
        turn = self._turn_for(request_id)
        if turn.block is None:
            block = StepBlock()
            for activity in turn.activities:
                self._add_step(block, activity)
            turn.block = block
        return turn.block

    def _add_step(self, block: StepBlock, activity: Activity) -> None:
        # Etiket tablosu işçi açılınca dolar; her satırda güncel kopya okunur.
        line = step_line(activity.event, activity.fields, self.bridge.labels())
        if line is not None:
            block.add_line(activity, line)

    def _request_exit(self) -> None:
        self._exiting = True
        if self._approval_dialog is not None:
            self._approval_dialog.reject()
        self.bridge.shutdown()
        self._set_status("Temizleniyor…", "running")
        self._set_controls()
        self.overlay.set_busy(True, "Temizleniyor…")

    def _finish_exit(self) -> None:
        if not self._exiting:
            return
        self._timer.stop()
        if self._hotkey is not None:
            self._hotkey.stop()
            self._hotkey = None
        self.settings_panel.finish_inventory()
        if self._tray is not None:
            self._tray.hide()
        self.overlay.close()
        self.pet.close()
        if self._temporary is not None:
            self._temporary.cleanup()
            self._temporary = None
        self._allow_close = True
        self.close()
        self._app.quit()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._allow_close:
            event.accept()
            return
        if self._approval_dialog is not None:
            self._approval_dialog.reject()
        if self._tray is not None and not self._exiting:
            self.hide()
            event.ignore()
            return
        event.ignore()
        self._request_exit()


def build_window(
    config_path: Path,
    *,
    demo: bool = False,
    voice: bool = False,
    app: QApplication | None = None,
) -> DesktopWindow:
    """GUI factory; offscreen kabul testleri aynı gerçek pencereyi kullanır."""
    return DesktopWindow(config_path, demo=demo, voice=voice, app=app)


TRAY_RETRY_MS = 2000
TRAY_RETRY_ATTEMPTS = 15


def _instance_key(config_path: Path) -> Path:
    try:
        return load_settings(config_path).state_dir
    except (OSError, ValueError, ValidationError, yaml.YAMLError):
        # Bozuk config'te pencere zaten hatayı gösterir; anahtar yine kurulum başına tekildir.
        return config_path.resolve()


def app_icon() -> QIcon:
    """Paketle gelen çok boyutlu simge; tepsi ve tüm pencereler aynı dosyayı kullanır."""
    return QIcon(str(Path(__file__).with_name("artemis.ico")))


def _set_app_user_model_id() -> None:
    """Görev çubuğu pythonw'ın simgesi yerine pencere simgesini göstersin diye (yalnız Windows)."""
    if sys.platform != "win32":
        return
    import ctypes

    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("UmutErayAltay.Artemis")
    except (AttributeError, OSError):
        return  # simge yalnız kozmetik; kimlik atanamazsa uygulama yine açılır


def run_desktop(
    config_path: Path, *, demo: bool = False, voice: bool = False, background: bool = False
) -> int:
    _set_app_user_model_id()
    current = QApplication.instance()
    app = current if isinstance(current, QApplication) else QApplication([])
    server: InstanceServer | None = None
    if not demo:
        # ADR 0030: aynı durum dizininde ikinci örnek açılmaz; var olanın paneli öne gelir.
        # Pencere kurulmadan önce sahiplenilir: kurulum uzun sürer ve yarış penceresi açar.
        server = claim_instance(instance_name(_instance_key(config_path)), show=not background)
        if server is None:
            if not background:
                print("Artemis zaten açık; panel öne getirildi.")
            return 0
        if not server.is_listening():
            print("Uyarı: tek örnek koruması kurulamadı; ikinci Artemis açılırsa çakışabilir.")
    app.setApplicationName("Artemis")
    app.setWindowIcon(app_icon())
    app.setFont(QFont("Segoe UI", 10))
    app.setStyleSheet(_STYLE)
    window = build_window(config_path, demo=demo, voice=voice, app=app)
    if server is not None:
        server.setParent(window)
        server.show_requested.connect(window.show_from_instance)
    window.present(background=background)
    return app.exec()
