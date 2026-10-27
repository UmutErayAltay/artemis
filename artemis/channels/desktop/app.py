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
from PySide6.QtCore import QPointF, QRectF, QSettings, QSize, Qt, QTimer, Signal
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
    QPalette,
    QPen,
    QPixmap,
    QRadialGradient,
)
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSystemTrayIcon,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from artemis.brain.providers import provider_problem
from artemis.channels.autostart import Autostart
from artemis.channels.desktop import pet_sprite, themes
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
from artemis.channels.desktop.data_panel import DataPanel
from artemis.channels.desktop.hotkey import GlobalHotkey
from artemis.channels.desktop.instance import InstanceServer, claim_instance, instance_name
from artemis.channels.desktop.operations import (
    CHIP_TONES,
    STATE_TONES,
    Card,
    OperationLog,
)
from artemis.channels.desktop.pet import TEAL, PetWidget, level_from_message
from artemis.channels.desktop.pet_sprite import pets_dir_for
from artemis.channels.desktop.projects_panel import ProjectsPanel, make_page_header
from artemis.channels.desktop.settings_panel import (
    SettingsPanel,
    save_choices,
    save_computer_mode,
)
from artemis.channels.desktop.steps import StepLine, step_line
from artemis.channels.runtime import Runtime, create_runtime
from artemis.config.settings import load_settings
from artemis.safety.audit import AuditLog

_STYLE = themes.stylesheet(themes.DEFAULT_THEME)


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


# Durum ve ses noktaları tema renk anahtarıyla okunur; hex değer koda gömülmez.
_STATUS_TONES = {"ready": "success", "running": "warning", "error": "error"}
_VOICE_TONES = {
    "off": "muted",
    "preparing": "warning",
    "listening": "success",
    "awaiting_command": "success",
    "thinking": "warning",
    "speaking": "success",
    "error": "error",
}
_STEP_TONES = {"ok": "success", "sent": "warning", "fail": "error", "denied": "error"}
# Sohbet sütunu bu genişliği aşmaz; okunaklı satır uzunluğu için.
_CHAT_MAX_WIDTH = 680
# Bu pencere genişliğinin altında adım paneli gizlenir, üst çubuk daralır.
_STEP_PANEL_MIN_WINDOW = 1100
_COMPACT_HEADER_WINDOW = 1000
# Ayrıntı ipucu büyük denetim kayıtlarında da ekranı ve belleği şişirmez.
_DETAIL_LIMIT = 4000
# Adım blokları yalnız son birkaç isteğin canlı bağlantısını tutar.
_STEP_TURNS_KEPT = 8
_STEP_EVENTS_PER_TURN = 200


class Mascot(QWidget):
    """Başlıktaki küçük, statik pet yüzü; pet.py renkleriyle çizilir, hareketi yoktur."""

    def __init__(self) -> None:
        super().__init__()
        self.setFixedSize(32, 32)
        self.setAccessibleName("Artemis yüzü")

    def paintEvent(self, event: Any) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        # Çizim 54x56 koordinatlarında kalır; 32px kutuya orantılı küçültülür.
        scale = 32 / 56
        p.translate((32 - 54 * scale) / 2, 0)
        p.scale(scale, scale)
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
    return f"QLabel {{ color: {color}; font-size: 12px; background: transparent; }}"


def _dot_icon(color: str) -> QIcon:
    """Ses düğmesinin solundaki durum noktası; metin düz kalır, renk yalnız noktada."""
    pixmap = QPixmap(14, 10)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(color))
    painter.drawEllipse(1, 1, 8, 8)
    painter.end()
    return QIcon(pixmap)


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


_PROMPT_MIN_HEIGHT = 72
_PROMPT_MAX_HEIGHT = 120


class PromptEdit(QPlainTextEdit):
    def __init__(self, on_send: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._on_send = on_send
        self.setPlaceholderText("Artemis'e ne yapmak istediğinizi yazın…")
        # Çok satırlı düzenleyici 72px'ten başlar, ~120px'e kadar büyür (DESIGN.md).
        self.setFixedHeight(_PROMPT_MIN_HEIGHT)
        self.document().documentLayout().documentSizeChanged.connect(self._fit_height)
        self.setTabChangesFocus(True)
        self.setAccessibleName("Komut metni")

    def _fit_height(self) -> None:
        """Boşken 72px; içerik arttıkça en çok 120px olur (belge yüksekliği satırdır)."""
        document = self.document()
        lines = max(1, int(document.size().height()))
        wanted = (
            lines * self.fontMetrics().lineSpacing()
            + int(2 * document.documentMargin())
            + 2 * self.frameWidth()
        )
        self.setFixedHeight(max(_PROMPT_MIN_HEIGHT, min(_PROMPT_MAX_HEIGHT, wanted)))

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

    def __init__(self, palette: dict[str, str] | None = None) -> None:
        super().__init__()
        # Renkler pencerenin temasından gelir; blok sohbete eklenmeden önce de doğru boyanır.
        self._palette = palette or themes.THEMES[themes.DEFAULT_THEME]
        self.setObjectName("stepBlock")
        self.setMaximumWidth(520)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 4)
        layout.setSpacing(4)
        self._rows_layout = QVBoxLayout()
        self._rows_layout.setSpacing(4)
        layout.addLayout(self._rows_layout)
        self._rows: list[tuple[QLabel, StepLine, Activity]] = []
        self._details = False
        self._toggle = QPushButton("Ayrıntılar")
        self._toggle.setObjectName("ghost")
        self._toggle.setCheckable(True)
        self._toggle.setAccessibleName("Adım ayrıntılarını göster")
        self._toggle.toggled.connect(self._set_details)
        layout.addWidget(self._toggle, 0, Qt.AlignmentFlag.AlignLeft)
        self._refresh()

    def _style_row(self, label: QLabel, line: StepLine) -> None:
        color = self._palette[_STEP_TONES.get(line.kind, "muted")]
        label.setStyleSheet(f"QLabel {{ color: {color}; font-size: 13px; }}")

    def add_line(self, activity: Activity, line: StepLine) -> None:
        label = QLabel(line.text)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        self._style_row(label, line)
        self._rows.append((label, line, activity))
        self._rows_layout.addWidget(label)
        label.setVisible(self._details or not line.internal)
        if self._details:
            label.setToolTip(_detail_text(activity))
        self._refresh()

    def apply_theme(self, palette: dict[str, str]) -> None:
        self._palette = palette
        for label, line, _activity in self._rows:
            self._style_row(label, line)

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
        520 px) genişlik ayrılır; daha uzun satır o genişlikte sarılır. Satırlar 13px
        çizildiği için ölçü de 13px'lik yazı tipiyle alınır.
        """
        layout = self.layout()
        if layout is None:
            return
        margins = layout.contentsMargins()
        widest = self._toggle.sizeHint().width()
        for label, line, _activity in self._rows:
            if self._details or not line.internal:
                font = QFont(label.font())
                font.setPixelSize(13)
                widest = max(widest, QFontMetrics(font).horizontalAdvance(label.text()))
        self.setMinimumWidth(min(520, widest + margins.left() + margins.right() + 4))


class OperationCard(QFrame):
    """Bir mesajın işlem kartı: başlık + rozet, araç satırları, kapalı "Ayrıntılar".

    Model (operations.Card) her olayda değişir; kart küçük olduğundan değişen kart baştan
    kurulur. Açık/kapalı tercihi modelde durur, bu yüzden yeniden kurulumda kaybolmaz.
    """

    def __init__(self, card: Card, palette: dict[str, str]) -> None:
        super().__init__()
        self.setObjectName("opCard")
        self.model = card
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(4)

        title = self._label(card.title or "İstek", palette["text"], 13)
        title.setObjectName("opTitle")
        layout.addWidget(title)
        head = QHBoxLayout()
        head.setSpacing(8)
        stamp = QLabel(card.started)
        stamp.setObjectName("meta")
        color = palette[CHIP_TONES[card.chip]]
        chip = QLabel(card.chip)
        chip.setObjectName("opChip")
        # Rozet rengi durum jetonundan gelir; hex değer gömülmez.
        chip.setStyleSheet(
            f"QLabel {{ color: {color}; border: 1px solid {color}; border-radius: 7px;"
            " font-size: 12px; padding: 2px 8px; background: transparent; }"
        )
        head.addWidget(stamp)
        head.addWidget(chip)
        head.addStretch(1)
        layout.addLayout(head)

        for op in card.ops:
            tone = palette[STATE_TONES.get(op.state, "muted")]
            row = QVBoxLayout()
            row.setSpacing(0)
            row.addWidget(self._label(op.label, palette["text"], 13))
            if op.summary:
                row.addWidget(self._label(op.summary, palette["muted"], 12))
            state = op.state_text + (f" · {op.duration}" if op.duration else "")
            row.addWidget(self._label(state, tone, 12))
            if op.message:
                row.addWidget(self._label(op.message, palette["muted"], 12))
            if op.approval:
                row.addWidget(self._label(op.approval, palette["muted"], 12))
            for sub in op.subrows:
                sub_tone = {"ok": "success", "sent": "warning"}.get(sub.kind, "muted")
                row.addWidget(self._label("› " + sub.text, palette[sub_tone], 12, indent=8))
            layout.addLayout(row)

        toggle = QPushButton("Ayrıntılar")
        toggle.setObjectName("ghost")
        toggle.setCheckable(True)
        toggle.setChecked(card.expanded)
        toggle.setAccessibleName("Mesaj işlem ayrıntılarını göster")
        layout.addWidget(toggle, 0, Qt.AlignmentFlag.AlignLeft)
        self.details = self._label("\n".join(card.details), palette["muted"], 12)
        self.details.setObjectName("opDetails")
        self.details.setVisible(card.expanded)
        layout.addWidget(self.details)
        toggle.toggled.connect(self._toggle)
        self.toggle = toggle

    @staticmethod
    def _label(text: str, color: str, size: int, indent: int = 0) -> QLabel:
        label = QLabel(text)
        # Araç ve model çıktısı biçimli metin sayılmaz.
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        label.setStyleSheet(
            f"QLabel {{ color: {color}; font-size: {size}px; margin-left: {indent}px; }}"
        )
        return label

    def _toggle(self, on: bool) -> None:
        self.model.expanded = on
        self.details.setVisible(on)


@dataclass
class _TurnSteps:
    """Bir isteğin denetim olayları; sonuç bildirimi olaylardan önce ya da sonra gelebilir.

    Olaylar ayrı bir kuyruktan okunur, bu yüzden bitiş bildirimi işlendiğinde son
    birkaç olay henüz gelmemiş olabilir. Blok bir kez kurulur, sonra gelen satırlar
    aynı bloğa eklenir.
    """

    activities: list[Activity] = field(default_factory=list)
    block: StepBlock | None = None


def _speaker_caption(speaker: str) -> str:
    """Kullanıcı balonundaki 12px etiket; "Siz" yerine kısa "Sen"."""
    if speaker.startswith("Siz"):
        return "Sen" + speaker.removeprefix("Siz")
    return speaker


class ChatTranscript(QWidget):
    """Seçilebilir, düz metin mesaj kartları ve sakin ilk ekran."""

    def __init__(
        self, demo: bool, on_suggestion: Any, palette: dict[str, str] | None = None
    ) -> None:
        super().__init__()
        self._palette = palette or themes.THEMES[themes.DEFAULT_THEME]
        self._messages: deque[tuple[str, str]] = deque(maxlen=500)
        self._bubbles: deque[tuple[QFrame, str, str]] = deque(maxlen=500)
        self._on_suggestion = on_suggestion
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.welcome = QFrame()
        self.welcome.setObjectName("emptyState")
        welcome_layout = QVBoxLayout(self.welcome)
        welcome_layout.setContentsMargins(24, 24, 24, 24)
        welcome_layout.setSpacing(12)
        greeting = QLabel("Merhaba. Bugün neyi halledelim?")
        greeting.setObjectName("emptyTitle")
        greeting.setTextFormat(Qt.TextFormat.PlainText)
        greeting.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        description = QLabel(
            "Dosyalar, uygulamalar ve günlük işler için bir komut yazın. "
            "Onay gereken işlemlerde ayrıntıları önce gösteririm."
        )
        description.setObjectName("emptyBody")
        description.setWordWrap(True)
        description.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        description.setMaximumWidth(440)
        description.setTextFormat(Qt.TextFormat.PlainText)
        welcome_layout.addWidget(greeting)
        welcome_layout.addWidget(description)
        suggestions = QHBoxLayout()
        suggestions.setSpacing(8)
        suggestions.addStretch(1)
        examples = QLabel("Örnekler")
        examples.setObjectName("fieldLabel")
        suggestions.addWidget(examples)
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
        # Karşılama kutusu esner ve ortalanır; mesaj gelince gizlenir, kaydırıcı alanı alır.
        outer.addWidget(self.welcome, 1, Qt.AlignmentFlag.AlignCenter)

        self._scroll_area = QScrollArea()
        self._scroll_area.setWidgetResizable(True)
        self._scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.content = QWidget()
        self.content.setMaximumWidth(_CHAT_MAX_WIDTH)
        self.content_layout = QVBoxLayout(self.content)
        self.content_layout.setContentsMargins(0, 0, 8, 0)
        self.content_layout.setSpacing(16)
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
        user = _is_user_speaker(speaker)
        row = QWidget()
        column = QVBoxLayout(row)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(8)
        if steps is not None:
            # Adım bloğu yanıt balonunun hemen üstüne gelir; konuşma kaydına girmez.
            column.addWidget(steps, 0, Qt.AlignmentFlag.AlignLeft)
        bubble_row = QWidget()
        row_layout = QHBoxLayout(bubble_row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        bubble = QFrame()
        bubble.setObjectName("messageUser" if user else "messageAssistant")
        bubble_layout = QVBoxLayout(bubble)
        # Kullanıcı balonu 12px dolgulu; Artemis yanıtı yüzeyde düz metindir.
        pad = 12 if user else 0
        bubble_layout.setContentsMargins(pad, pad, pad, pad)
        bubble_layout.setSpacing(4)
        caption = QLabel(_speaker_caption(speaker))
        caption.setObjectName("meta")
        caption.setTextFormat(Qt.TextFormat.PlainText)
        body = QLabel(text)
        body.setTextFormat(Qt.TextFormat.PlainText)
        body.setWordWrap(True)
        body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        body.setAccessibleName(f"{speaker} mesajı")
        bubble_layout.addWidget(caption)
        bubble_layout.addWidget(body)
        self._bubbles.append((bubble, speaker, text))
        self._resize_bubble(bubble, speaker, text)
        if user:
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

    def apply_theme(self, palette: dict[str, str]) -> None:
        self._palette = palette

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
        max_width = min(_CHAT_MAX_WIDTH - 8, available)
        if not _is_user_speaker(speaker):
            # Artemis yanıtı tema/yazı tipine bağlı olmadan tam mesaj sütununu kullanır.
            bubble.setFixedWidth(max_width)
            return
        longest_line = max(text.splitlines(), key=len, default="")
        natural = QFontMetrics(bubble.font()).horizontalAdvance(longest_line) + 48
        bubble.setFixedWidth(min(max_width, max(min(120, max_width), natural)))


def _argument_text(value: Any) -> str:
    """Onay özetinde tek satırlık değer; sözlük ve listeler JSON olarak gösterilir."""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


# Özet satırı bu uzunluğu aşan değeri keser; tam metin ipucunda ve ayrıntıda durur.
_SUMMARY_LIMIT = 80
# Bundan uzun ya da yol/adres taşıyan değer varsa ayrıntı kutusu açık gelir (kural 2).
_DETAIL_OPEN_LENGTH = 60


def _needs_open_details(arguments: dict[str, Any]) -> bool:
    for value in arguments.values():
        text = _argument_text(value)
        if len(text) > _DETAIL_OPEN_LENGTH or any(mark in text for mark in ("/", "\\", "://")):
            return True
    return False


class ApprovalDialog(QDialog):
    """İşçi beklerken Qt event loop'unu açık tutan açık seçim penceresi."""

    def __init__(self, item: Approval, on_decision: Any, parent: QWidget) -> None:
        super().__init__(parent)
        self.item = item
        self._on_decision = on_decision
        self._decided = False
        self.setWindowTitle("Artemis · İşlem onayı")
        self.setModal(True)
        self.setMinimumSize(560, 380)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)

        # Uzun argümanlı onayda düğmeler hep görünür kalsın diye gövde kayar.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        body.setObjectName("flat")
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 8, 0)
        body_layout.setSpacing(12)
        scroll.setWidget(body)
        layout.addWidget(scroll, 1)

        # Okunur ad büyük başlıktır; ham ad altında küçük yazılır (ADR 0014).
        title = QLabel(item.tool_label or item.tool_name)
        title.setObjectName("title")
        title.setTextFormat(Qt.TextFormat.PlainText)
        title.setWordWrap(True)
        body_layout.addWidget(title)
        raw_name = QLabel(item.tool_name)
        raw_name.setObjectName("rawToolName")
        raw_name.setTextFormat(Qt.TextFormat.PlainText)
        raw_name.setAccessibleName("Ham araç adı")
        body_layout.addWidget(raw_name)

        # Özet: her argüman anahtarı görünür; uzun değer kısalır, tamamı ipucunda ve ayrıntıda.
        intro = QLabel("Artemis bu aracı şu değerlerle çalıştırmak için izin istiyor:")
        intro.setWordWrap(True)
        body_layout.addWidget(intro)
        rows = QVBoxLayout()
        rows.setSpacing(4)
        body_layout.addLayout(rows)
        for key, value in item.arguments.items():
            full = _argument_text(value)
            shown = " ".join(full.split())
            if len(shown) > _SUMMARY_LIMIT:
                shown = shown[: _SUMMARY_LIMIT - 1] + "…"
            row = QLabel(f"{key} = {shown}")
            row.setTextFormat(Qt.TextFormat.PlainText)
            row.setWordWrap(True)
            row.setToolTip(f"<pre>{html.escape(f'{key} = {full}')}</pre>")
            row.setAccessibleName(f"Argüman {key}")
            rows.addWidget(row)
        if not item.arguments:
            rows.addWidget(QLabel("Argüman yok."))

        notice = QLabel(item.notice or "Bu işlem onay gerektirir.")
        notice.setObjectName("chipWarning")
        notice.setTextFormat(Qt.TextFormat.PlainText)
        notice.setWordWrap(True)
        # Renk temadan gelir (chipWarning); yalnız okunur boyut ve dolgu burada.
        notice.setStyleSheet("QLabel { font-size: 13px; padding: 8px 12px; }")
        body_layout.addWidget(notice)

        self.details_toggle = QPushButton("Teknik ayrıntılar")
        self.details_toggle.setObjectName("ghost")
        self.details_toggle.setCheckable(True)
        self.details_toggle.setAccessibleName("Teknik ayrıntıları göster veya gizle")
        body_layout.addWidget(self.details_toggle, 0, Qt.AlignmentFlag.AlignLeft)
        label = QLabel("Doğrulanmış araç argümanları")
        label.setObjectName("fieldLabel")
        arguments = QPlainTextEdit()
        arguments.setObjectName("approvalArguments")
        arguments.setReadOnly(True)
        arguments.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        arguments.setPlainText(json.dumps(item.arguments, ensure_ascii=False, indent=2))
        arguments.setAccessibleName("Salt okunur doğrulanmış araç argümanları")
        arguments.setMinimumHeight(120)
        self.details_label = label
        self.arguments_view = arguments
        body_layout.addWidget(label)
        body_layout.addWidget(arguments, 1)
        self.details_toggle.toggled.connect(self._show_details)
        # Uzun değer, yol ya da adres varsa ayrıntı tıklama beklemeden açıktır.
        self.details_toggle.setChecked(_needs_open_details(item.arguments))
        self._show_details(self.details_toggle.isChecked())

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addStretch(1)
        self.no_button = QPushButton("Hayır")
        self.no_button.setMinimumWidth(88)
        self.no_button.setDefault(True)
        self.no_button.setAutoDefault(True)
        self.yes_button = QPushButton("Evet, onayla")
        self.yes_button.setObjectName("primary")
        self.yes_button.setMinimumWidth(88)
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

    def _show_details(self, on: bool) -> None:
        self.details_label.setVisible(on)
        self.arguments_view.setVisible(on)

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
        self.setMinimumSize(480, 220)
        self.resize(520, 240)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        top = QHBoxLayout()
        title = QLabel("Artemis · Hızlı komut")
        title.setObjectName("meta")
        panel_button = QPushButton("Paneli aç")
        panel_button.setObjectName("ghost")
        panel_button.clicked.connect(on_show_panel)
        top.addWidget(title)
        top.addStretch(1)
        top.addWidget(panel_button)
        layout.addLayout(top)
        self.input = PromptEdit(on_send, self)
        layout.addWidget(self.input, 1)
        self.status = QLabel("Ana panelle aynı oturum")
        self.status.setObjectName("meta")
        self.voice_state = QLabel(_VOICE_LABELS["off"])
        self.voice_state.setObjectName("meta")
        self.voice_state.setAccessibleName("Ses durumu")
        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addWidget(self.voice_state)
        buttons.addWidget(self.status)
        buttons.addStretch(1)
        self.cancel_button = QPushButton("İptal")
        self.cancel_button.setMinimumWidth(88)
        self.send_button = QPushButton("Gönder")
        self.send_button.setObjectName("primary")
        self.send_button.setMinimumWidth(88)
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
        self._ops = OperationLog()
        self._op_widgets: dict[str, OperationCard] = {}
        self._theme = themes.DEFAULT_THEME
        self._status_tone = "running"
        self._model_full = ""
        self._build_pet()
        self._build_ui()
        self._build_overlay()
        self._build_tray()
        self._hotkey: GlobalHotkey | None = None
        if not demo:
            self._start_hotkey()
        self.pet.start()
        self._refresh_brand_pet()
        self.settings_panel.pet.set_current(self.pet.skin)
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
            on_submit=lambda text: self._submit_text(text),
            pets_dir=pets_dir_for(self._config_path),
        )

    def _select_skin(self, pet_id: str) -> None:
        problem = self.pet.set_skin(pet_id)
        self._refresh_brand_pet()
        if problem:
            self.settings_panel.pet.show_problem(problem)

    def _refresh_brand_pet(self) -> None:
        """Selected pet beside the immutable Artemis product name."""
        info = pet_sprite.find_pet(self.pet.skin, pets_dir_for(self._config_path))
        try:
            pixmap = pet_sprite.preview(info, 32) if info is not None else None
        except pet_sprite.PetError:
            pixmap = None
        self.brand_pet.setVisible(pixmap is not None)
        self.brand_mark.setVisible(pixmap is None)
        if pixmap is not None:
            self.brand_pet.setPixmap(pixmap)

    def _apply_theme(self, name: str) -> None:
        self._theme = name if name in themes.THEMES else themes.DEFAULT_THEME
        style = themes.stylesheet(self._theme)
        self.setStyleSheet(style)
        # Her yoldan gelen tema değişimi Görünüm kartlarını da eşitler; set_theme sinyal yaymaz.
        self.settings_panel.set_theme(self._theme)
        self.settings_panel.providers.set_theme(themes.THEMES[self._theme])
        palette = themes.THEMES[self._theme]
        for block in self.findChildren(StepBlock):
            block.apply_theme(palette)
        self.chat.apply_theme(palette)
        for request_id in list(self._op_widgets):
            self._render_card(request_id)
        if hasattr(self, "overlay"):
            self.overlay.setStyleSheet(style)
            self._theme_placeholders(self.overlay)
        self._theme_placeholders(self)
        self._refresh_tones()
        dark = self._theme.startswith("dark")
        self.theme_toggle.setText("\u2600  Açık tema" if dark else "\u263E  Koyu tema")
        self.theme_toggle.setAccessibleName("Açık temaya geç" if dark else "Koyu temaya geç")
        if not self._demo:
            self._appearance.setValue("theme", self._theme)

    def _token(self, key: str) -> str:
        return themes.THEMES[self._theme][key]

    def _refresh_tones(self) -> None:
        """Durum ve ses noktaları tema değişince yeni renk anahtarıyla yeniden boyanır."""
        self.status_dot.setStyleSheet(_dot_style(self._token(_STATUS_TONES[self._status_tone])))
        tone = _VOICE_TONES.get(self._voice_state, _VOICE_TONES["off"])
        color = self._token(tone)
        self.voice_dot.setStyleSheet(_dot_style(color))
        self.voice_button.setIcon(_dot_icon(color))

    def _theme_placeholders(self, root: QWidget) -> None:
        color = QColor(themes.THEMES[self._theme]["muted"])
        for widget in [*root.findChildren(QLineEdit), *root.findChildren(QPlainTextEdit)]:
            palette = widget.palette()
            palette.setColor(QPalette.ColorRole.PlaceholderText, color)
            widget.setPalette(palette)

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

    def _build_topbar(self) -> QFrame:
        """56px üst çubuk: marka, demo rozeti, model, tek ses denetimi, oto onay, durum."""
        header = QFrame()
        header.setObjectName("topbar")
        header.setFixedHeight(56)
        row = QHBoxLayout(header)
        row.setContentsMargins(16, 8, 16, 8)
        row.setSpacing(12)
        self.brand_mark = Mascot()
        row.addWidget(self.brand_mark)
        self.brand_pet = QLabel()
        self.brand_pet.setFixedSize(32, 32)
        self.brand_pet.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.brand_pet.setAccessibleName("Seçili pet")
        self.brand_pet.hide()
        row.addWidget(self.brand_pet)
        title = QLabel("Artemis")
        title.setObjectName("sectionTitle")
        title.setToolTip("Güvenli masaüstü asistanı")
        row.addWidget(title)
        self.demo_chip: QLabel | None = None
        if self._demo:
            self.demo_chip = QLabel("Demo · gerçek işlem yok")
            self.demo_chip.setObjectName("chipWarning")
            self.demo_chip.setAccessibleName("Demo modu; gerçek işlem yapılmaz")
            row.addWidget(self.demo_chip, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addStretch(1)

        # Model: sola hizalı düz metin; dar pencerede kısalır (bkz. _fit_shell).
        self.model_box = QWidget()
        model_stack = QVBoxLayout(self.model_box)
        model_stack.setContentsMargins(0, 0, 0, 0)
        model_stack.setSpacing(0)
        model_caption = QLabel("Model")
        model_caption.setObjectName("meta")
        self.model_value = QLabel("Yükleniyor…")
        self.model_value.setTextFormat(Qt.TextFormat.PlainText)
        value_font = self.model_value.font()
        value_font.setPixelSize(13)
        self.model_value.setFont(value_font)
        model_stack.addWidget(model_caption)
        model_stack.addWidget(self.model_value)
        row.addWidget(self.model_box)

        # Tek ses denetimi: düğme nokta + durum metnini taşır. voice_dot/voice_state
        # testler ve köprü için canlı kalan, görünmeyen aynalardır.
        self.voice_dot = QLabel("●", self)
        self.voice_dot.setObjectName("voiceDot")
        self.voice_dot.hide()
        self.voice_state = QLabel(_VOICE_LABELS["off"], self)
        self.voice_state.setAccessibleName("Ses durumu")
        self.voice_state.hide()
        self.voice_button = QPushButton(_VOICE_LABELS["off"])
        self.voice_button.setCheckable(True)
        self.voice_button.setObjectName("voiceToggle")
        self.voice_button.setAccessibleName("Sesi aç veya kapat")
        self.voice_button.setMinimumWidth(144)
        self.voice_button.setIconSize(QSize(14, 10))
        if self._demo:
            self.voice_button.setToolTip("Demo modunda ses yok")
        else:
            self.voice_button.setToolTip("Mikrofonu yalnız siz açarsınız; onaylar ekrandadır")
        self.voice_button.clicked.connect(self._toggle_voice)
        row.addWidget(self.voice_button)
        self.auto_button = QPushButton("Oto onay kapalı")
        self.auto_button.setCheckable(True)
        self.auto_button.setObjectName("voiceToggle")
        self.auto_button.setAccessibleName("Bilgisayar kullanımında otomatik onayı aç veya kapat")
        self.auto_button.setToolTip(
            "Açıkken bilgisayar kullanımı adımları sormadan çalışır; Esc yine durdurur. "
            "Değişiklik hemen geçerli, Uygula gerekmez."
        )
        self.auto_button.toggled.connect(self._toggle_auto_mode)
        row.addWidget(self.auto_button)
        status_pill = QFrame()
        status_pill.setObjectName("statusPill")
        status_row = QHBoxLayout(status_pill)
        status_row.setContentsMargins(0, 0, 0, 0)
        status_row.setSpacing(8)
        self.status_dot = QLabel("●")
        self.status_dot.setObjectName("statusDot")
        self.status_label = QLabel("")
        self.status_label.setObjectName("status")
        self.status_label.setMinimumWidth(64)
        self._set_status("Başlatılıyor…", "running")
        status_row.addWidget(self.status_dot)
        status_row.addWidget(self.status_label)
        row.addWidget(status_pill, 0, Qt.AlignmentFlag.AlignVCenter)
        return header

    def _build_chat_page(self) -> QWidget:
        page = QWidget()
        page_row = QHBoxLayout(page)
        page_row.setContentsMargins(0, 0, 0, 0)
        page_row.setSpacing(0)
        main_column = QWidget()
        main_column.setObjectName("flat")
        page_row.addWidget(main_column, 1)

        column = QVBoxLayout(main_column)
        column.setContentsMargins(24, 0, 24, 24)
        column.setSpacing(16)
        self.quick_action = QPushButton("Hızlı komut")
        self.quick_action.clicked.connect(self._show_overlay)
        self.new_chat_button = QPushButton("Yeni sohbet")
        self.new_chat_button.setToolTip("Yeni bağlam açar; kalıcı konuşma arşivini silmez")
        self.new_chat_button.clicked.connect(self._new_chat)
        column.addWidget(
            make_page_header("Sohbet", actions=(self.new_chat_button, self.quick_action))
        )
        # Başlıktan sonra iki sütun: sohbet+yazma alanı ve (geniş pencerede) son işlem kartı;
        # kartın üstü transkript kartıyla, altı yazma alanıyla aynı hizada biter.
        content = QHBoxLayout()
        content.setSpacing(16)
        column.addLayout(content, 1)
        left = QVBoxLayout()
        left.setSpacing(16)
        content.addLayout(left, 1)

        transcript_card = QFrame()
        transcript_card.setObjectName("card")
        transcript_layout = QVBoxLayout(transcript_card)
        transcript_layout.setContentsMargins(16, 16, 16, 16)
        self.chat = ChatTranscript(self._demo, self._set_suggestion, themes.THEMES[self._theme])
        self.chat.setObjectName("chatTranscript")
        self.chat.setAccessibleName("Seçilebilir sohbet geçmişi")
        transcript_layout.addWidget(self.chat, 1)
        left.addWidget(transcript_card, 1)

        input_card = QFrame()
        input_card.setObjectName("raised")
        input_layout = QVBoxLayout(input_card)
        input_layout.setContentsMargins(12, 12, 12, 12)
        input_layout.setSpacing(8)
        self.prompt = PromptEdit(self._submit_main)
        self.prompt.setObjectName("mainPrompt")
        self.prompt.setPlaceholderText(
            "Artemis'e ne yapmak istediğinizi yazın…  (Enter gönderir · Shift+Enter yeni satır)"
        )
        input_layout.addWidget(self.prompt)
        input_controls = QHBoxLayout()
        input_controls.setSpacing(8)
        self.cancel_button = QPushButton("İptal")
        self.cancel_button.setMinimumWidth(88)
        self.send_button = QPushButton("Gönder")
        self.send_button.setObjectName("primary")
        self.send_button.setMinimumWidth(88)
        self.send_button.setFixedHeight(36)
        self.cancel_button.clicked.connect(self._cancel_active)
        self.send_button.clicked.connect(self._submit_main)
        input_controls.addStretch(1)
        input_controls.addWidget(self.cancel_button)
        input_controls.addWidget(self.send_button)
        input_layout.addLayout(input_controls)
        left.addWidget(input_card)

        # Son işlem paneli: yalnız geniş pencerede görünür (bkz. _fit_shell).
        self.activity_panel = QFrame()
        self.activity_panel.setObjectName("stepPanel")
        self.activity_panel.setFixedWidth(280)
        panel_layout = QVBoxLayout(self.activity_panel)
        panel_layout.setContentsMargins(16, 16, 16, 16)
        panel_layout.setSpacing(12)
        activity_title = QLabel("Mesaj işlemleri")
        activity_title.setObjectName("sectionTitle")
        # Her mesaj bir kart; en yeni üstte. Kartlar kaydırılır, yatay kaydırma yoktur.
        self.activity = QScrollArea()
        self.activity.setAccessibleName("Mesaj başına işlemler")
        self.activity.setWidgetResizable(True)
        self.activity.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.activity.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.activity.setFrameShape(QFrame.Shape.NoFrame)
        self.activity.setStyleSheet(
            "QScrollArea { border: none; background: transparent; }"
            "QScrollArea > QWidget > QWidget { background: transparent; }"
        )
        cards_host = QWidget()
        cards_host.setObjectName("flat")
        self.activity_cards = QVBoxLayout(cards_host)
        self.activity_cards.setContentsMargins(0, 0, 8, 0)
        self.activity_cards.setSpacing(8)
        self.activity_cards.addStretch(1)
        self.activity.setWidget(cards_host)
        self.activity.hide()
        self.activity_notice = QLabel("")
        self.activity_notice.setObjectName("meta")
        self.activity_notice.setWordWrap(True)
        self.activity_notice.hide()
        self.activity_empty = QLabel("Henüz bir işlem yok")
        self.activity_empty.setObjectName("meta")
        panel_layout.addWidget(activity_title)
        # Boş durumda başlığın hemen altında kalır; satırlar üstten dizilir.
        panel_layout.addWidget(self.activity_notice)
        panel_layout.addWidget(self.activity_empty, 1, Qt.AlignmentFlag.AlignTop)
        panel_layout.addWidget(self.activity, 1)
        content.addWidget(self.activity_panel)
        return page

    def _build_sidebar(self, pages: list[tuple[str, QWidget]]) -> QFrame:
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(176)
        navigation = QVBoxLayout(sidebar)
        navigation.setContentsMargins(12, 12, 12, 12)
        navigation.setSpacing(4)
        self.nav_buttons: dict[QWidget, QPushButton] = {}
        for label, page in pages:
            if label == "Ayarlar":
                navigation.addStretch(1)
            button = QPushButton(label)
            button.setObjectName("navItem")
            button.setCheckable(True)
            button.setAutoExclusive(True)
            button.setAccessibleName(label)
            button.clicked.connect(
                lambda _checked=False, target=page: self.tabs.setCurrentWidget(target)
            )
            self.nav_buttons[page] = button
            navigation.addWidget(button)
        self.theme_toggle = QPushButton()
        self.theme_toggle.setObjectName("themeToggle")
        self.theme_toggle.setFixedHeight(32)
        self.theme_toggle.clicked.connect(self._toggle_theme)
        navigation.addWidget(self.theme_toggle)
        return sidebar

    @staticmethod
    def _page(widget: QWidget) -> QWidget:
        """Kendi 24px iç boşluğu olmayan sayfa 24px kenar payıyla barındırılır.

        Ayarlar ve sohbet bu payı kendisi taşır; diğerleri aynı hizada başlasın diye
        burada eklenir. Sayfaların kendi kaydırıcısı korunur.
        """
        host = QWidget()
        host_layout = QVBoxLayout(host)
        host_layout.setContentsMargins(24, 0, 24, 24)
        host_layout.addWidget(widget)
        return host

    def _build_ui(self) -> None:
        root = QWidget()
        main = QVBoxLayout(root)
        main.setContentsMargins(16, 16, 16, 16)
        main.setSpacing(16)
        main.addWidget(self._build_topbar())

        self.chat_page = self._build_chat_page()
        self.tabs = QTabWidget()
        self.tabs.addTab(self.chat_page, "Sohbet")
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
        self.auto_button.setText(f"Oto onay {'açık' if on else 'kapalı'}")
        self.settings_panel.continuous_mode.toggled.connect(self._live_continuous)
        self.settings_panel.computer_auto.toggled.connect(self._toggle_auto_mode)
        self.settings_panel.github_synced.connect(self._github_synced)
        self.settings_panel.providers.changed.connect(self._providers_changed)
        self.settings_panel.pet.skin_selected.connect(self._select_skin)
        self.settings_scroll = self.settings_panel
        self.tabs.addTab(self.settings_scroll, "Ayarlar")
        self.projects_page = self._page(self.projects_panel)
        self.tabs.addTab(self.projects_page, "Projeler")
        self.data_panel = DataPanel(demo=self._demo)
        self.data_panel.tool_requested.connect(self._submit_project_tool)
        self.data_page = self._page(self.data_panel)
        self.tabs.addTab(self.data_page, "Geçmiş ve hatırlatmalar")
        # Pet atölyesi günlük ayarlardan ayrı sayfadır; PetSection kendi başlığını taşır
        # ama kaydırıcısı yoktur.
        self.pet_scroll = QScrollArea()
        self.pet_scroll.setWidgetResizable(True)
        self.pet_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.pet_scroll.setWidget(self.settings_panel.pet)
        self.pet_page = self._page(self.pet_scroll)
        self.tabs.addTab(self.pet_page, "Petler")
        self.tabs.tabBar().hide()
        sidebar = self._build_sidebar([
            ("Sohbet", self.chat_page), ("Projeler", self.projects_page),
            ("Petler", self.pet_page), ("Geçmiş", self.data_page),
            ("Ayarlar", self.settings_scroll),
        ])
        self.tabs.currentChanged.connect(self._sync_navigation)
        self._sync_navigation()
        body = QHBoxLayout()
        body.setSpacing(16)
        body.addWidget(sidebar)
        body.addWidget(self.tabs, 1)
        main.addLayout(body, 1)

        self.setCentralWidget(root)
        self._appearance = QSettings(
            QSettings.Format.IniFormat, QSettings.Scope.UserScope, "Artemis", "appearance"
        )
        selected_theme = str(self._appearance.value("theme", themes.DEFAULT_THEME))
        if selected_theme not in themes.THEMES:
            selected_theme = themes.DEFAULT_THEME
        self.settings_panel.set_theme(selected_theme)
        self.settings_panel.theme_changed.connect(self._apply_theme)
        self._apply_theme(selected_theme)
        self._set_controls()
        self._fit_shell()
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
            # Sağlayıcı eksikse Ayarlar doğrudan Model bölümünde açılır.
            names = self.settings_panel.section_names()
            self.settings_panel.show_section(names.index("Model") if "Model" in names else 1)
            QTimer.singleShot(
                0,
                lambda: self.settings_panel.sections.scroll_area.ensureWidgetVisible(
                    self.settings_panel.providers
                ),
            )

    def _sync_navigation(self, *_args: object) -> None:
        for page, button in self.nav_buttons.items():
            button.setChecked(page is self.tabs.currentWidget())

    def _toggle_theme(self) -> None:
        """Açık/koyu arasında geçer; karma (mixed) türev korunur."""
        name = self._theme
        flipped = name.replace("dark", "light") if name.startswith("dark") else (
            name.replace("light", "dark")
        )
        # Ayarlar'daki seçim sinyal yaymadan eşitlenir; tema tek kez uygulanır.
        self.settings_panel.set_theme(flipped)
        self._apply_theme(flipped)

    def _providers_changed(self) -> None:
        if self.settings_panel.pet.creator is not None:
            self.settings_panel.pet.creator.reload_models()
        self._check_provider(open_settings=False)
        if self._busy or self._exiting or self._demo:
            self.settings_panel.providers.status.setText(
                "Kaydedildi; işlem bitince Uygula ile yeni ayarlar devreye girer."
            )
            return
        self._restart_runtime()

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
        self.overlay.setStyleSheet(themes.stylesheet(self._theme))
        self._theme_placeholders(self.overlay)

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
        if self.settings_panel.pet.creator is not None:
            self.settings_panel.pet.creator.reload_models()
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

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        self._fit_shell()

    def _fit_shell(self) -> None:
        """Pencere genişliğine göre üst çubuğu daraltır, son işlem panelini açar/kapatır.

        900px'te hiçbir şey kırpılmaz: demo rozeti kısalır, model adı "…" ile kesilir,
        çok dar pencerede model kutusu tamamen çekilir. Ham etkinlik paneli yalnız
        geniş pencerede görünür; dar pencerede adımlar zaten sohbette durur.
        """
        width = self.width()
        compact = width < _COMPACT_HEADER_WINDOW
        if self.demo_chip is not None:
            self.demo_chip.setText("Demo" if compact else "Demo · gerçek işlem yok")
        self.model_box.setVisible(width >= 800)
        self._show_model(self._model_full)
        self.activity_panel.setVisible(width >= _STEP_PANEL_MIN_WINDOW)

    def _show_model(self, text: str) -> None:
        self._model_full = text
        limit = 120 if self.width() < _COMPACT_HEADER_WINDOW else 240
        metrics = QFontMetrics(self.model_value.font())
        shown = metrics.elidedText(text, Qt.TextElideMode.ElideRight, limit)
        self.model_value.setText(shown)
        self.model_value.setToolTip(text if shown != text else "")
        self.model_value.setFixedWidth(max(60, metrics.horizontalAdvance(shown) + 4))

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
        self._begin_card(request_id, text)
        self._append_chat("Siz", text)
        self._set_status("Çalışıyor…", "running")
        self._set_controls()
        self.overlay.set_busy(True)
        return True

    def _submit_project_tool(self, name: str, arguments: BaseModel) -> None:
        request_id = self.bridge.submit_tool(name, arguments)
        if request_id is None:
            panel = self.projects_panel if name.startswith("code_agent_") else self.data_panel
            panel.notice.setText("Başka işlem sürüyor; istek çalıştırılmadı.")
            return
        self._active_request = request_id
        self._busy = True
        self._begin_card(request_id, f"Proje işlemi: {name}")
        self._set_controls()

    def _refresh_projects(self) -> None:
        if (
            self.tabs.currentWidget() is self.projects_page
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
        self.auto_button.setText(f"Oto onay {'açık' if shown else 'kapalı'}")
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
        self._status_tone = tone
        self.status_dot.setStyleSheet(_dot_style(self._token(_STATUS_TONES[tone])))

    def _show_voice_state(self, state: str) -> None:
        self._voice_state = state
        if state == "off":
            self._continuous = False
        text = _VOICE_LABELS.get(state, _VOICE_LABELS["off"])
        if self._continuous:
            text += " · Kesintisiz"
        self.voice_state.setText(text)
        self.overlay.voice_state.setText(text)
        on = state in {"preparing", "listening", "awaiting_command", "thinking", "speaking"}
        self.voice_button.setChecked(on)
        self.voice_button.setText(text)
        self.voice_button.setToolTip(text)
        self._refresh_tones()
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
        self.data_panel.set_busy(not enabled)
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
                    self._show_model(event.message.removeprefix("Hazır · "))
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
            elif event.state == "voice_live":
                # Kullanıcı konuşurken pete bakar; ayarlar paneli yalnız nihai metni gösterir.
                # Her güncelleme balon süresini yeniler, nihai `voice_heard` üzerine yazar.
                self.pet.live(event.message)
            elif event.state == "voice_live_unavailable":
                kind, _, reason = event.message.partition(":")
                if kind == "runtime":
                    # Çalışırken düşen canlı yazı bir yapılandırma sorunu değildir.
                    self._append_chat(
                        "Artemis",
                        "Canlı yazı çalışırken hata verdi ve bu oturumda kapatıldı; "
                        "ses canlı yazısız sürüyor.",
                    )
                else:
                    fix = (
                        'Paketleri `pip install -e ".[stream]"` ile kurun'
                        if kind == "deps"
                        else "config'te `voice.live_transcript_model` ve "
                        "`voice.live_transcript_revision` ayarlarına bakın"
                    )
                    self._append_chat(
                        "Artemis",
                        f"Canlı yazı açılamadı: {reason}. {fix}; ses canlı yazısız sürüyor.",
                    )
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
            self._begin_card(event.request_id, event.text)
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
                panel = (self.projects_panel if event.tool_name.startswith("code_agent_")
                         else self.data_panel)
                panel.accept_result(event.tool_name, event.result)
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
                self._ops.fail(event.request_id)
                self._render_card(event.request_id)
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
            self._ops.dropped += event.dropped_count
            self.activity_notice.setText(
                f"{self._ops.dropped} eski etkinlik görünümden düştü; kartlar eksik olabilir."
            )
            self.activity_notice.show()
            return
        if isinstance(event, Activity):
            self._pet_activity(event)
            self._feed_card(event)
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

    def _begin_card(self, request_id: str, text: str) -> None:
        self._ops.begin(request_id, text)
        self._render_card(request_id)

    def _feed_card(self, event: Activity) -> None:
        # Etiket tablosu işçi açılınca dolar; her olayda güncel kopya okunur.
        card = self._ops.feed(event.request_id, event.event, event.fields, self.bridge.labels())
        if card is not None:
            self._render_card(card.request_id)

    def _render_card(self, request_id: str) -> None:
        """Kartı (yeniden) kurar; yeni kart listenin en üstüne, 20'yi aşan eskiler düşer."""
        card = self._ops.cards.get(request_id)
        if card is None:
            return
        if not self.activity_empty.isHidden():
            self.activity_empty.hide()
            self.activity.show()
        widget = OperationCard(card, themes.THEMES[self._theme])
        old = self._op_widgets.get(request_id)
        index = 0
        if old is not None:
            index = self.activity_cards.indexOf(old)
            self.activity_cards.removeWidget(old)
            old.deleteLater()
        self._op_widgets[request_id] = widget
        self.activity_cards.insertWidget(index, widget)
        for stale in [key for key in self._op_widgets if key not in self._ops.cards]:
            gone = self._op_widgets.pop(stale)
            self.activity_cards.removeWidget(gone)
            gone.deleteLater()

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
            block = StepBlock(themes.THEMES[self._theme])
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
