"""PySide6 paneli; bütün model ve araç işleri Qt'siz köprüde kalır."""

from __future__ import annotations

import html
import json
import tempfile
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import (
    QCloseEvent,
    QColor,
    QFont,
    QFontMetrics,
    QIcon,
    QKeyEvent,
    QPainter,
    QPen,
    QPixmap,
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

from jarvis.channels.desktop.bridge import (
    Activity,
    ActivityGap,
    Approval,
    BridgeEvent,
    DesktopBridge,
    Failed,
    Finished,
    State,
    VoiceCommand,
)
from jarvis.channels.desktop.pet import PetWidget, level_from_message
from jarvis.channels.desktop.settings_panel import SettingsPanel, save_choices
from jarvis.channels.desktop.steps import StepLine, step_line
from jarvis.channels.runtime import Runtime, create_runtime

_STYLE = """
QWidget {
  background: #0B1220; color: #E6EDF5;
  font-family: 'Segoe UI', sans-serif; font-size: 14px;
}
QLabel { background: transparent; }
QFrame#card {
  background: #111C2D; border: 1px solid #223148; border-radius: 14px;
}
QFrame#raised {
  background: #17263A; border: 1px solid #2A3B52; border-radius: 12px;
}
QFrame#messageUser {
  background: #193840; border: 1px solid #2A5358; border-radius: 13px;
}
QFrame#messageAssistant {
  background: #17263A; border: 1px solid #2A3B52; border-radius: 13px;
}
QLabel#eyebrow { color: #A9B8C9; font-size: 12px; font-weight: 600; }
QLabel#title { color: #E6EDF5; font-size: 24px; font-weight: 700; }
QLabel#muted { color: #A9B8C9; }
QLabel#status { background: transparent; border: none; padding: 0 2px; }
QLabel#rawToolName {
  color: #A9B8C9; font-family: 'Cascadia Mono', Consolas, monospace; font-size: 12px;
}
QFrame#stepBlock {
  background: #0E1828; border: 1px solid #26374D; border-radius: 11px;
}
QPushButton#stepDetailsToggle {
  color: #A9B8C9; background: transparent; border: none;
  padding: 2px 0px; min-height: 0px; font-size: 12px;
}
QPushButton#stepDetailsToggle:hover { color: #53D6C7; background: transparent; border: none; }
QPlainTextEdit#mainPrompt { padding: 8px 12px; }
QLabel#demo {
  color: #0B1220; background: #53D6C7; border-radius: 9px;
  padding: 6px 10px; font-weight: 800;
}
QPlainTextEdit, QListWidget {
  background: #0E1828; border: 1px solid #26374D; border-radius: 11px;
  padding: 12px; selection-background-color: #255B61;
}
QPlainTextEdit:focus, QListWidget:focus { border: 1px solid #53D6C7; }
QPushButton {
  background: #17263A; color: #E6EDF5; border: 1px solid #30445E;
  border-radius: 10px; padding: 9px 14px; min-height: 20px; font-weight: 600;
}
QPushButton:hover { border-color: #53D6C7; background: #1D3046; }
QPushButton:focus { border: 2px solid #53D6C7; }
QPushButton:disabled { color: #718096; background: #111C2D; border-color: #243247; }
QPushButton#primary {
  color: #0B1220; background: #53D6C7;
  border-color: #53D6C7; font-weight: 800;
}
QPushButton#primary:hover { background: #78E4D7; }
QPushButton#danger { color: #FFD9D5; border-color: #79423F; background: #3A252B; }
QPushButton#amber { color: #FFD78C; border-color: #795D31; background: #332B1E; }
QSplitter::handle { background: #0B1220; width: 8px; }
QScrollBar:vertical { background: #0E1828; width: 11px; margin: 3px; }
QScrollBar::handle:vertical { background: #34465E; min-height: 24px; border-radius: 5px; }
QListWidget::item { padding: 7px 5px; border-bottom: 1px solid #1C2A3D; }
QDialog { background: #111C2D; }
QPushButton#primary:disabled { color: #718096; background: #23313F; border-color: #334354; }
QPushButton#danger:disabled { color: #887574; background: #29272C; border-color: #454047; }
"""


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


def _is_user_speaker(speaker: str) -> bool:
    """"Siz" ve "Siz (ses)" aynı kullanıcı balonudur."""
    return speaker.startswith("Siz")


def _dot_style(color: str) -> str:
    """Durum noktasının rengi; metin ayrı etikette düz kalır."""
    return f"QLabel {{ color: {color}; font-size: 15px; background: transparent; }}"


def _detail_text(activity: Activity) -> str:
    """Ham araç adı ve alanlar; yalnız ayrıntı ipucunda görünür, görünür satıra girmez."""
    raw_tool = activity.fields.get("tool")
    head = f"Ham araç adı: {raw_tool}" if isinstance(raw_tool, str) else f"Olay: {activity.event}"
    payload = json.dumps(
        activity.fields, ensure_ascii=False, indent=2, sort_keys=True, default=str
    )
    if len(payload) > _DETAIL_LIMIT:
        payload = payload[:_DETAIL_LIMIT] + "\n…"
    text = f"{head}\n{payload}"
    # İpucu zengin metin sayılmasın diye kaçışlanır; JSON içindeki "<" işaretleri bozmaz.
    return f"<pre>{html.escape(text)}</pre>"


class PromptEdit(QPlainTextEdit):
    def __init__(self, on_send: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._on_send = on_send
        self.setPlaceholderText("Jarvis'e ne yapmak istediğinizi yazın…")
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
        self.setVisible(visible)


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
        suggestion_texts = ("Merhaba", "örnek akış") if demo else (
            "Saat kaç?",
            "Dosyalarımı listele",
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
        bubble.setObjectName(
            "messageUser" if _is_user_speaker(speaker) else "messageAssistant"
        )
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
            0, lambda: self._scroll_area.verticalScrollBar().setValue(
                self._scroll_area.verticalScrollBar().maximum()
            )
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
        self.setWindowTitle("Jarvis · İşlem onayı")
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
            "border-radius:10px; padding:11px; }"
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
        buttons.addWidget(self.yes_button)
        layout.addLayout(buttons)
        self.no_button.clicked.connect(lambda: self._choose(False))
        self.yes_button.clicked.connect(lambda: self._choose(True))
        self.no_button.setFocus(Qt.FocusReason.OtherFocusReason)

    def _choose(self, approved: bool) -> None:
        if self._decided:
            return
        self._decided = True
        accepted = self._on_decision(self.item, approved)
        decision = approved and accepted
        self.done(
            QDialog.DialogCode.Accepted if decision else QDialog.DialogCode.Rejected
        )

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
        self.setWindowTitle("Jarvis · Hızlı komut")
        self.setMinimumSize(460, 160)
        self.resize(480, 170)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)
        top = QHBoxLayout()
        title = QLabel("JARVIS  /  HIZLI KOMUT")
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
        # Demo hiçbir koşulda mikrofon açmaz; --ses bayrağı da demoda yok sayılır.
        self._voice_on_ready = voice and not demo
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        if demo:
            self._temporary = tempfile.TemporaryDirectory(prefix="jarvis-demo-")
            temp_root = Path(self._temporary.name)

            def factory(path: Path, approve: Any, on_record: Any) -> Runtime:
                from jarvis.channels.desktop.demo import create_demo_parts

                settings, provider, registry, audit = create_demo_parts(temp_root, on_record)
                runtime = create_runtime(
                    path,
                    approve,
                    settings=settings,
                    provider=provider,
                    registry=registry,
                    audit=audit,
                )
                runtime.context.services["demo_records"] = {
                    "sample": "Demo kitaplığı: örnek metin"
                }
                return runtime

            self.bridge = DesktopBridge(config_path, factory)
        else:
            self.bridge = DesktopBridge(config_path)
        self.setWindowTitle("Jarvis")
        self.resize(1180, 780)
        self.setMinimumSize(720, 560)
        self._busy = False
        self._worker_ready = False
        self._exiting = False
        self._allow_close = False
        self._tray: QSystemTrayIcon | None = None
        self._approval_dialog: ApprovalDialog | None = None
        self._active_request: str | None = None
        self._turn_steps: dict[str, _TurnSteps] = {}
        self._activity_count = 0
        self._activity_empty = True
        self._build_pet()
        self._build_ui()
        self._build_overlay()
        self._build_tray()
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
        )

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
        mark = QLabel("J")
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mark.setFixedSize(44, 44)
        mark.setStyleSheet(
            "QLabel { color:#0B1220; background:#53D6C7; border-radius:14px; "
            "font-size:26px; font-weight:900; }"
        )
        header_layout.addWidget(mark)
        title_stack = QVBoxLayout()
        title = QLabel("Jarvis")
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
        self.voice_button.setAccessibleName("Sesi aç veya kapat")
        if self._demo:
            self.voice_button.setToolTip("Demo modunda ses yok")
        else:
            self.voice_button.setToolTip("Mikrofonu yalnız siz açarsınız; onaylar ekrandadır")
        self.voice_button.clicked.connect(self._toggle_voice)
        header_layout.addWidget(self.voice_dot, 0, Qt.AlignmentFlag.AlignVCenter)
        header_layout.addWidget(self.voice_state, 0, Qt.AlignmentFlag.AlignVCenter)
        header_layout.addWidget(self.voice_button)
        self.status_dot = QLabel("●")
        self.status_dot.setObjectName("statusDot")
        self.status_label = QLabel("")
        self.status_label.setObjectName("status")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setMinimumWidth(92)
        self._set_status("Başlatılıyor…", "running")
        header_layout.addWidget(self.status_dot, 0, Qt.AlignmentFlag.AlignVCenter)
        header_layout.addWidget(self.status_label, 0, Qt.AlignmentFlag.AlignVCenter)
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
            "Jarvis'e ne yapmak istediğinizi yazın…  (Enter gönderir · Shift+Enter yeni satır)"
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
        self.settings_panel = SettingsPanel(self._config_path, demo=self._demo)
        self.settings_panel.apply_requested.connect(self._apply_settings)
        self.settings_panel.github_synced.connect(self._github_synced)
        self.settings_scroll = QScrollArea()
        self.settings_scroll.setWidgetResizable(True)
        self.settings_scroll.setWidget(self.settings_panel)
        self.tabs.addTab(self.settings_scroll, "Ayarlar")
        main.addWidget(self.tabs, 1)

        self.setCentralWidget(root)
        self.setStyleSheet(_STYLE)
        self._set_controls()
        self.activity_panel.hide()
        self.steps_toggle.hide()
        QTimer.singleShot(0, self.prompt.setFocus)
        # Açılışta bir kez, arka plan thread'inde; CLI'da eşitleme yoktur.
        QTimer.singleShot(0, self.settings_panel.start_auto_sync)

    def _github_synced(self, report: Any, auto: bool) -> None:
        if not (auto and report.cloned):
            return
        text = (
            f"{len(report.cloned)} yeni GitHub reposu çekildi; "
            "Ayarlar'da Uygula ile kullanıma girer."
        )
        self.pet.say(text, "job")
        if self._tray is not None:
            self._tray.showMessage("Jarvis", text, QSystemTrayIcon.MessageIcon.Information, 6000)

    def _build_overlay(self) -> None:
        self.overlay = QuickOverlay(
            self._submit_overlay, self._cancel_active, self._show_panel
        )
        self.overlay.setStyleSheet(_STYLE)

    def _apply_settings(self, model_id: str, device: int | str | None) -> None:
        if self._demo or self._busy or self._exiting:
            self.settings_panel.notice.setText(
                "Ayarları değiştirmek için işlemin bitmesini bekleyin."
            )
            return
        try:
            save_choices(
                self._config_path, model_id, device,
                applications=self.settings_panel.applications,
                allow_all_applications=self.settings_panel.all_applications.isChecked(),
                code_agent=self.settings_panel.github_values(),
            )
        except (OSError, ValueError, KeyError, TypeError):
            self.settings_panel.notice.setText("Ayarlar kaydedilemedi; mevcut ayarlar korunuyor.")
            return
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
        self._tray = QSystemTrayIcon(QIcon(self._tray_pixmap()), self)
        self._tray.setToolTip("Jarvis")
        menu = QMenu(self)
        open_action = menu.addAction("Paneli aç")
        quick_action = menu.addAction("Hızlı komut")
        pet_action = menu.addAction("Peti göster/gizle")
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

    @staticmethod
    def _tray_pixmap() -> QPixmap:
        pixmap = QPixmap(64, 64)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QColor("#111C2D"))
        painter.setPen(QPen(QColor("#53D6C7"), 3))
        painter.drawRoundedRect(3, 3, 58, 58, 18, 18)
        painter.setPen(QPen(QColor("#53D6C7"), 6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawLine(39, 16, 39, 42)
        painter.drawArc(21, 31, 20, 18, 180 * 16, 180 * 16)
        painter.end()
        return pixmap

    def _tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
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

    def _set_status(self, text: str, tone: str) -> None:
        # Durum düz metindir; ton yalnız yanındaki noktanın rengini belirler.
        self.status_label.setText(text)
        self.status_dot.setStyleSheet(_dot_style(_STATUS_TONES[tone]))

    def _show_voice_state(self, state: str) -> None:
        text = _VOICE_LABELS.get(state, _VOICE_LABELS["off"])
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
        self.voice_button.setEnabled(
            self._worker_ready and not self._exiting and not self._demo
        )
        self.prompt.setEnabled(enabled)
        self.send_button.setEnabled(enabled)
        self.new_chat_button.setEnabled(enabled)
        self.cancel_button.setEnabled(self._busy and not self._exiting)
        self.pet.set_busy(self._busy and not self._exiting)
        if self._tray is not None:
            status = "çalışıyor" if self._busy else "hazır"
            self._tray.setToolTip(f"Jarvis · {status}")

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
            elif event.state == "cleanup_pending":
                self._busy = True
                self._set_status("Önceki işlem temizleniyor…", "running")
                self.overlay.set_busy(True, "Temizleniyor…")
                self._set_controls()
            elif event.state == "voice":
                self._show_voice_state(event.message)
            elif event.state == "voice_level":
                level = level_from_message(event.message)
                if level is not None:
                    self.settings_panel.input_level.setValue(round(level * 100))
                    self.pet.set_level(level)
            elif event.state == "voice_heard":
                self.settings_panel.last_heard.setText(event.message)
                self.pet.heard(event.message)
            elif event.state == "voice_error":
                self._append_chat("Jarvis", event.message)
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
        if isinstance(event, Finished):
            if event.request_id == self._active_request:
                self._dismiss_approval_for(event.request_id)
                self._append_chat(
                    "Jarvis", event.result.text, self._step_block_for(event.request_id)
                )
                self.pet.answer(event.result.text)
                self.overlay.status.setText("Yanıt hazır")
            return
        if isinstance(event, Failed):
            if not event.request_id:
                self._set_status(f"Başlatılamadı · {event.error_type}", "error")
                self._append_chat("Jarvis", event.message)
            elif event.request_id == self._active_request:
                self._dismiss_approval_for(event.request_id)
                self._append_chat(
                    "Jarvis",
                    f"İstek tamamlanamadı ({event.error_type}).",
                    self._step_block_for(event.request_id),
                )
                self.pet.flash_error()
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
            self._tray.showMessage("Jarvis", text, QSystemTrayIcon.MessageIcon.Information, 6000)

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

    def _decide_approval(self, item: Approval, approved: bool) -> bool:
        return self.bridge.decide(
            item.request_id, item.turn_id, item.approval_id, approved
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
                result.get("status", "bilinmiyor")
                if isinstance(result, dict)
                else "bilinmiyor"
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


def run_desktop(config_path: Path, *, demo: bool = False, voice: bool = False) -> int:
    current = QApplication.instance()
    app = current if isinstance(current, QApplication) else QApplication([])
    app.setApplicationName("Jarvis")
    app.setFont(QFont("Segoe UI", 10))
    app.setStyleSheet(_STYLE)
    window = build_window(config_path, demo=demo, voice=voice, app=app)
    window.show()
    return app.exec()
