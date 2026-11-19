"""Sohbet sayfasının widget'ları: yazı kutusu, adım blokları, işlem kartları, döküm ve perde."""

from __future__ import annotations

import html
import json
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QKeyEvent,
    QPainter,
    QPixmap,
)
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from artemis.channels.desktop import themes
from artemis.channels.desktop.bridge import (
    Activity,
)
from artemis.channels.desktop.operations import (
    CHIP_TONES,
    STATE_TONES,
    Card,
)
from artemis.channels.desktop.retranslate import set_i18n_context
from artemis.channels.desktop.shell_assets import greeting_for_hour
from artemis.channels.desktop.steps import StepLine
from artemis.channels.desktop.widgets import PetFace
from artemis.config.settings import Settings
from artemis.i18n import t

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

_STEP_TONES = {"ok": "success", "sent": "warning", "fail": "error", "denied": "error"}

# Sohbet sütunu bu genişliği aşmaz; okunaklı satır uzunluğu için.
_CHAT_MAX_WIDTH = 680
# Balon sütununun iki yandaki eşit iç boşluğu (kaydırıcı ile balon arasındaki pay dahil).
_BUBBLE_GUTTER = 8

# Ayrıntı ipucu büyük denetim kayıtlarında da ekranı ve belleği şişirmez.
_DETAIL_LIMIT = 4000

def _is_user_speaker(speaker: str) -> bool:
    """ "Siz" ve "Siz (ses)" aynı kullanıcı balonudur."""
    return speaker.startswith("Siz")

def _detail_text(activity: Activity) -> str:
    """Ham araç adı ve alanlar; yalnız ayrıntı ipucunda görünür, görünür satıra girmez."""
    raw_tool = activity.fields.get("tool")
    head = (
        t("Ham araç adı: {raw_tool}").format(raw_tool=raw_tool)
        if isinstance(raw_tool, str)
        else t("Olay: {event}").format(event=activity.event)
    )
    payload = json.dumps(activity.fields, ensure_ascii=False, indent=2, sort_keys=True, default=str)
    if len(payload) > _DETAIL_LIMIT:
        payload = payload[:_DETAIL_LIMIT] + "\n…"
    text = f"{head}\n{payload}"
    # İpucu zengin metin sayılmasın diye kaçışlanır; JSON içindeki "<" işaretleri bozmaz.
    return f"<pre>{html.escape(text)}</pre>"


_PROMPT_MIN_HEIGHT = 44
_PROMPT_MAX_LINES = 4


class PromptEdit(QPlainTextEdit):
    def __init__(
        self, on_send: Any, parent: QWidget | None = None, min_height: int = _PROMPT_MIN_HEIGHT
    ) -> None:
        super().__init__(parent)
        self._on_send = on_send
        self._min_height = min_height
        self.setPlaceholderText(t("Artemis'e yaz…"))
        # Sakin tek satır başlar (44px), en çok ~4 satıra kadar büyür.
        self.setFixedHeight(min_height)
        self.document().documentLayout().documentSizeChanged.connect(self._fit_height)
        self.setTabChangesFocus(True)
        self.setAccessibleName(t("Komut metni"))

    def _line_height(self, lines: int) -> int:
        document = self.document()
        return (
            lines * self.fontMetrics().lineSpacing()
            + int(2 * document.documentMargin())
            + 2 * self.frameWidth()
        )

    def _fit_height(self) -> None:
        """Belge yüksekliği satır sayısıdır; 44px ile 4 satır arasında kalır."""
        lines = max(1, int(self.document().size().height()))
        wanted = self._line_height(min(lines, _PROMPT_MAX_LINES))
        self.setFixedHeight(max(self._min_height, wanted))

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
        self._toggle = QPushButton(t("Ayrıntılar"))
        self._toggle.setObjectName("ghost")
        self._toggle.setCheckable(True)
        self._toggle.setAccessibleName(t("Adım ayrıntılarını göster"))
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

        title = self._label(card.title or t("İstek"), palette["text"], 13)
        title.setObjectName("opTitle")
        layout.addWidget(title)
        head = QHBoxLayout()
        head.setSpacing(8)
        stamp = QLabel(card.started)
        stamp.setObjectName("meta")
        color = palette[CHIP_TONES[card.chip]]
        chip = QLabel(t(card.chip, context="status"))
        set_i18n_context(chip, "status")
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
            # Ham argümanlar ve süre varsayılan olarak "Ayrıntılar" altındadır.
            row.addWidget(self._label(op.state_text, tone, 12))
            if op.message:
                row.addWidget(self._label(op.message, palette["muted"], 12))
            if op.approval:
                row.addWidget(self._label(op.approval, palette["muted"], 12))
            for sub in op.subrows:
                sub_tone = {"ok": "success", "sent": "warning"}.get(sub.kind, "muted")
                row.addWidget(self._label("› " + sub.text, palette[sub_tone], 12, indent=8))
            layout.addLayout(row)

        toggle = QPushButton(t("Ayrıntılar"))
        toggle.setObjectName("ghost")
        toggle.setCheckable(True)
        toggle.setChecked(card.expanded)
        toggle.setAccessibleName(t("Mesaj işlem ayrıntılarını göster"))
        layout.addWidget(toggle, 0, Qt.AlignmentFlag.AlignLeft)
        lines = [
            f"{op.label}: " + " · ".join(part for part in (op.summary, op.duration) if part)
            for op in card.ops
            if op.summary or op.duration
        ]
        self.details = self._label("\n".join(lines + card.details), palette["muted"], 12)
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
        return t("Sen" + speaker.removeprefix("Siz"))
    return t(speaker)


def _hour_now() -> int:
    return datetime.now().hour


# Asistan satırında 28px pet yüzü ve 8px boşluk; adım bloğu aynı girintiyi alır.
_AVATAR = 28
_AVATAR_GAP = 8
_AVATAR_INDENT = _AVATAR + _AVATAR_GAP
# Demo çalışma zamanı yalnız kendi örneklerini bilir.
_DEMO_EXAMPLES = ("Merhaba", "Örnek akış")
_MAX_CHIPS = 4
_WELCOME_BODY = (
    "Uygulama açabilir, hatırlatma kurabilir, dosyalarınla çalışabilirim; "
    "onay gereken işlerde ayrıntıları önce gösteririm."
)


def welcome_suggestions(settings: Settings | None) -> tuple[str, ...]:
    """Açık özelliklere göre karşılama önerileri (en çok 4); "…" ile bitenler yazma alanında
    tamamlanır. İki genel öneri (current_time, reminder_create) satırı hiç boş bırakmaz."""
    chips: list[str] = []
    if settings is not None:
        if settings.calendar.enabled:
            chips.append(t("Bugün ne var?"))
        if settings.routines.enabled:
            chips.append(t("Bir rutin çalıştır"))
        if settings.research.enabled:
            chips.append(t("Şunu araştır: …"))
        if settings.file_search.enabled:
            chips.append(t("Dosyalarımda ara: …"))
    chips += [t("Hatırlatma kur"), t("Saat kaç?")]
    return tuple(chips[:_MAX_CHIPS])


class ChatTranscript(QWidget):
    """Seçilebilir, düz metin mesaj kartları ve yoldaş karşılaması."""

    def __init__(
        self,
        demo: bool,
        on_suggestion: Any,
        palette: dict[str, str] | None = None,
        hour_of_day: Callable[[], int] | None = None,
        pet_source: Callable[[int], QPixmap | None] | None = None,
    ) -> None:
        super().__init__()
        self._palette = palette or themes.THEMES[themes.DEFAULT_THEME]
        # Saat enjekte edilebilir: selamlama testi gerçek saate bağlı kalmasın.
        self._hour = hour_of_day or _hour_now
        self._pet_source = pet_source or (lambda _size: None)
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
        welcome_layout.setSpacing(16)
        self.pet_face = PetFace(96, self._palette)
        self.pet_face.set_pixmap(self._pet_source(96))
        welcome_layout.addWidget(self.pet_face, 0, Qt.AlignmentFlag.AlignHCenter)
        self.greeting = QLabel()
        self.greeting.setObjectName("greeting")
        self.greeting.setTextFormat(Qt.TextFormat.PlainText)
        self.greeting.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self._demo = demo
        description = QLabel(t(_WELCOME_BODY))
        self.description = description
        description.setObjectName("emptyBody")
        description.setWordWrap(True)
        description.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        description.setMaximumWidth(480)
        description.setTextFormat(Qt.TextFormat.PlainText)
        welcome_layout.addWidget(self.greeting)
        welcome_layout.addWidget(description, 0, Qt.AlignmentFlag.AlignHCenter)
        self._suggestions = QHBoxLayout()
        self._suggestions.setSpacing(8)
        self.suggestion_buttons: list[QPushButton] = []
        welcome_layout.addLayout(self._suggestions)
        self.set_suggestions(_DEMO_EXAMPLES if demo else welcome_suggestions(None))
        self.refresh_greeting()
        # Karşılama kutusu esner ve ortalanır; mesaj gelince gizlenir, kaydırıcı alanı alır.
        outer.addWidget(self.welcome, 1, Qt.AlignmentFlag.AlignCenter)

        self._scroll_area = QScrollArea()
        self._scroll_area.setWidgetResizable(True)
        self._scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # Okunur sütun 680px'i aşmaz ve ortalanır; fazlası kenar boşluğudur.
        self._scroll_area.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.content = QWidget()
        self.content.setObjectName("flat")
        self.content.setMaximumWidth(_CHAT_MAX_WIDTH)
        self.content_layout = QVBoxLayout(self.content)
        # Kenar boşluğu iki yanda eşit: yalnız sağda olsaydı balonlar yazma alanına göre sola
        # kayık görünürdü (sağ sütun açıkken sohbet alanı dar olduğundan en çok o zaman).
        self.content_layout.setContentsMargins(_BUBBLE_GUTTER, 0, _BUBBLE_GUTTER, 0)
        self.content_layout.setSpacing(16)
        self.content_layout.addStretch(1)
        self._scroll_area.setWidget(self.content)
        self._scroll_area.hide()
        outer.addWidget(self._scroll_area, 1)

    def set_suggestions(self, chips: Sequence[str], dictation_hotkey: str = "") -> None:
        """Öneri satırını ve (dikte açıksa) kısayol cümlesini yeniden kurar. Demo sabit kalır."""
        if self._demo:
            chips, dictation_hotkey = _DEMO_EXAMPLES, ""
        body = t(_WELCOME_BODY)
        if dictation_hotkey:
            body += " " + t("Dikte için {hotkey} tuşlarına bas.").format(
                hotkey=dictation_hotkey.strip().title()
            )
        self.description.setText(body)
        while self._suggestions.count():
            item = self._suggestions.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.deleteLater()
        self.suggestion_buttons = []
        self._suggestions.addStretch(1)
        for text in chips:
            label = t(text)
            button = QPushButton(label)
            button.setObjectName("chip")
            button.setAccessibleName(t("Örnek komut: {text}").format(text=label))
            fills = self._demo or text.endswith("…")
            button.setToolTip(
                t("Komut kutusuna yazar, göndermez") if fills else t("Hemen gönderir")
            )
            button.setProperty("chip", text)
            button.clicked.connect(self._chip_clicked)
            self._suggestions.addWidget(button)
            self.suggestion_buttons.append(button)
        self._suggestions.addStretch(1)

    def _chip_clicked(self) -> None:
        if (button := self.sender()) is not None:
            self._on_suggestion(str(button.property("chip")))

    def refresh_greeting(self) -> None:
        self.greeting.setText(greeting_for_hour(self._hour()))

    def refresh_pet(self) -> None:
        """Seçili pet değişince karşılamadaki ve balonlardaki yüzleri günceller."""
        for face in self.findChildren(PetFace):
            face.set_pixmap(self._pet_source(face.width()))
            face.set_palette_tokens(self._palette)

    def _avatar_for(self, speaker: str) -> QWidget:
        # "Sistem" notları pet konuşması değildir; yalnız hizayı korumak için boşluk alır.
        if not speaker.startswith("Artemis"):
            spacer = QWidget()
            spacer.setFixedSize(_AVATAR, _AVATAR)
            return spacer
        face = PetFace(_AVATAR, self._palette)
        face.set_pixmap(self._pet_source(_AVATAR))
        return face

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
            steps_row = QHBoxLayout()
            steps_row.setContentsMargins(_AVATAR_INDENT, 0, 0, 0)
            steps_row.addWidget(steps, 0, Qt.AlignmentFlag.AlignLeft)
            steps_row.addStretch(1)
            column.addLayout(steps_row)
        bubble_row = QHBoxLayout()
        bubble_row.setContentsMargins(0, 0, 0, 0)
        bubble_row.setSpacing(_AVATAR_GAP)
        bubble = QFrame()
        bubble.setObjectName("userBubble" if user else "assistantBubble")
        bubble_layout = QVBoxLayout(bubble)
        # Kullanıcı balonu 16x8 dolgulu accent_soft; Artemis yanıtı düz metindir.
        if user:
            bubble_layout.setContentsMargins(16, 8, 16, 8)
        else:
            bubble_layout.setContentsMargins(0, 0, 0, 0)
        bubble_layout.setSpacing(4)
        # Kullanıcıda etiket yalnız "Sen (ses)" gibi kaynağı ayıran durumda gösterilir.
        if not user or "(" in speaker:
            caption = QLabel(_speaker_caption(speaker))
            caption.setObjectName("meta")
            caption.setTextFormat(Qt.TextFormat.PlainText)
            bubble_layout.addWidget(caption)
        body = QLabel(text)
        body.setTextFormat(Qt.TextFormat.PlainText)
        body.setWordWrap(True)
        body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        body.setAccessibleName(t("{speaker} mesajı").format(speaker=t(speaker)))
        bubble_layout.addWidget(body)
        self._bubbles.append((bubble, speaker, text))
        self._resize_bubble(bubble, speaker, text)
        if user:
            bubble_row.addStretch(1)
            bubble_row.addWidget(bubble)
        else:
            bubble_row.addWidget(self._avatar_for(speaker), 0, Qt.AlignmentFlag.AlignTop)
            bubble_row.addWidget(bubble)
            bubble_row.addStretch(1)
        column.addLayout(bubble_row)
        self.content_layout.insertWidget(self.content_layout.count() - 1, row)
        QTimer.singleShot(
            0,
            lambda: self._scroll_area.verticalScrollBar().setValue(
                self._scroll_area.verticalScrollBar().maximum()
            ),
        )

    def apply_theme(self, palette: dict[str, str]) -> None:
        self._palette = palette
        self.refresh_pet()

    def clear(self) -> None:
        """Yeni sohbette eski balonları kaldırır ve karşılamayı geri getirir."""
        while self.content_layout.count() > 1:
            item = self.content_layout.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()
        self._messages.clear()
        self._bubbles.clear()
        self.refresh_greeting()
        self._scroll_area.hide()
        self.welcome.show()

    def toPlainText(self) -> str:
        """Test ve kopyalama için görüntülenen düz metin dökümü."""
        return "\n\n".join(f"{speaker}: {text}" for speaker, text in self._messages)

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        for bubble, speaker, text in self._bubbles:
            self._resize_bubble(bubble, speaker, text)

    def _resize_bubble(self, bubble: QFrame, speaker: str, text: str) -> None:
        available = max(200, self.width() - 28)
        max_width = min(_CHAT_MAX_WIDTH - 2 * _BUBBLE_GUTTER, available)
        if not _is_user_speaker(speaker):
            # Artemis yanıtı tema/yazı tipine bağlı olmadan pet yüzünden kalan sütunu kullanır.
            bubble.setFixedWidth(max_width - _AVATAR_INDENT)
            return
        longest_line = max(text.splitlines(), key=len, default="")
        natural = QFontMetrics(bubble.font()).horizontalAdvance(longest_line) + 48
        bubble.setFixedWidth(min(max_width, max(min(120, max_width), natural)))


class QuickOverlay(QWidget):
    def __init__(
        self,
        on_send: Any,
        on_cancel: Any,
        on_show_panel: Any,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent, Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint)
        self.setWindowTitle(t("Artemis · Hızlı komut"))
        self.setMinimumSize(480, 240)
        self.resize(520, 260)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)
        top = QHBoxLayout()
        title = QLabel(t("Artemis · Hızlı komut"))
        title.setObjectName("sectionTitle")
        panel_button = QPushButton(t("Paneli aç"))
        panel_button.setObjectName("ghost")
        panel_button.clicked.connect(on_show_panel)
        top.addWidget(title)
        top.addStretch(1)
        top.addWidget(panel_button)
        layout.addLayout(top)
        self.input = PromptEdit(on_send, self, min_height=72)
        layout.addWidget(self.input, 1)
        self.status = QLabel(t("Ana panelle aynı oturum"))
        self.status.setObjectName("meta")
        self.voice_state = QLabel(t(_VOICE_LABELS["off"]))
        self.voice_state.setObjectName("meta")
        self.voice_state.setAccessibleName(t("Ses durumu"))
        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        # İki durum etiketi ayraçsız yan yana "Ses kapalı  Hazır" diye tek cümle gibi okunuyordu.
        divider = QLabel("·")
        divider.setObjectName("meta")
        divider.setAccessibleName("")
        buttons.addWidget(self.voice_state)
        buttons.addWidget(divider)
        buttons.addWidget(self.status)
        buttons.addStretch(1)
        self.cancel_button = QPushButton(t("İptal"))
        self.cancel_button.setMinimumWidth(88)
        self.send_button = QPushButton(t("Gönder"))
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
        self.status.setText(status or (t("Çalışıyor…") if busy else t("Ana panelle aynı oturum")))


class _Drawer(QWidget):
    """Dar pencerede işlem kartlarını sohbetin üstünde yandan açan sayfa (Esc/Kapat/dış tıklama)."""

    def __init__(self, parent: QWidget, on_close: Callable[[], None]) -> None:
        super().__init__(parent)
        self._on_close = on_close
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._row = QHBoxLayout(self)
        self._row.setContentsMargins(0, 16, 16, 16)
        self._row.addStretch(1)
        self._panel: QWidget | None = None
        self.hide()

    def dock(self, panel: QWidget) -> None:
        self._panel = panel
        self._row.addWidget(panel)

    def undock(self, panel: QWidget) -> None:
        self._row.removeWidget(panel)
        self._panel = None

    def paintEvent(self, event: Any) -> None:
        # Perde: altındaki sohbet görünür ama soluk kalır; düz yarı saydam ton (gradyan yok).
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 90))
        painter.end()

    def mousePressEvent(self, event: Any) -> None:
        panel = self._panel
        if panel is None or event.position().x() < panel.x():
            self._on_close()
        event.accept()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self._on_close()
            event.accept()
            return
        super().keyPressEvent(event)


class _ChatPage(QWidget):
    """Sohbet sayfası; perde sayfa boyutunu izler."""

    def __init__(self) -> None:
        super().__init__()
        self.drawer: _Drawer | None = None

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        if self.drawer is not None:
            self.drawer.setGeometry(self.rect())
