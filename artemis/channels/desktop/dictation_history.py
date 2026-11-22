"""Yalnız bellekte (RAM) tutulan son 5 dikte geçmişi ve tek tıkla yeniden kullanım."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from PySide6.QtGui import QAction, QGuiApplication
from PySide6.QtWidgets import QMenu, QPushButton

from artemis.i18n import t

if TYPE_CHECKING:
    from artemis.channels.desktop.app import DesktopWindow

MAX_HISTORY_ENTRIES = 5
_PREVIEW_LIMIT = 44


@dataclass(frozen=True)
class DictationEntry:
    """Bellekteki tek dikte geçmişi kaydı; asla diske ya da denetime yazılmaz."""

    text: str
    timestamp: str


def _default_clock() -> str:
    return datetime.now().strftime("%H:%M")


def _truncate_preview(text: str, limit: int = _PREVIEW_LIMIT) -> str:
    single = " ".join(text.split())
    if len(single) <= limit:
        return single
    return single[: limit - 1] + "…"


class DictationHistory:
    """En çok son 5 dikte sonucunu yalnızca RAM'de (`deque(maxlen=5)`) saklar."""

    def __init__(self, clock: Callable[[], str] | None = None) -> None:
        self._clock = clock or _default_clock
        self._items: deque[DictationEntry] = deque(maxlen=MAX_HISTORY_ENTRIES)

    def add(self, text: str) -> None:
        cleaned = text.strip()
        if not cleaned:
            return
        self._items.appendleft(DictationEntry(text=cleaned, timestamp=self._clock()))

    def items(self) -> tuple[DictationEntry, ...]:
        """En yeni kayıttan en eskiye doğru anlık kopya döndürür."""
        return tuple(self._items)

    def clear(self) -> None:
        self._items.clear()

    def __len__(self) -> int:
        return len(self._items)


def build_dictation_history_button(window: DesktopWindow) -> QPushButton:
    """Sohbet başlığı için son 5 dikte açılır menü düğmesini kurar."""
    window._dictation_history = DictationHistory()
    button = QPushButton(t("Dikte geçmişi"))
    button.setAccessibleName(t("Son dikte geçmişi"))
    button.setToolTip(t("Bu oturumda dikte edilen son 5 metin (yalnız bellekte tutulur)"))
    menu = QMenu(button)
    menu.triggered.connect(window._on_dictation_history_action)
    button.setMenu(menu)
    window.dictation_history_button = button
    window.dictation_history_menu = menu
    refresh_dictation_history_menu(window)
    return button


def refresh_dictation_history_menu(window: DesktopWindow) -> None:
    """Dikte geçmişi menüsünü bellekteki son 5 kayıtla günceller."""
    menu = getattr(window, "dictation_history_menu", None)
    history = getattr(window, "_dictation_history", None)
    if menu is None or history is None:
        return
    menu.clear()
    entries = history.items()
    if not entries:
        empty = menu.addAction(t("Henüz dikte geçmişi yok"))
        empty.setEnabled(False)
        return
    for entry in entries:
        action = menu.addAction(f"{entry.timestamp} · {_truncate_preview(entry.text)}")
        action.setToolTip(entry.text)
        action.setData(("copy_insert", entry.text))
    menu.addSeparator()
    clear_action = menu.addAction(t("Geçmişi temizle"))
    clear_action.setData(("clear", ""))


def on_dictation_history_action(window: DesktopWindow, action: QAction) -> None:
    """Seçilen dikte kaydını panoya kopyalar ve sohbet kutusuna ekler ya da geçmişi siler."""
    data = action.data()
    if not isinstance(data, tuple) or len(data) != 2:
        return
    kind, text = data
    if kind == "clear":
        window._dictation_history.clear()
        refresh_dictation_history_menu(window)
        window._set_status(t("Dikte geçmişi temizlendi."), "ready")
        return
    if kind == "copy_insert" and isinstance(text, str) and text:
        clipboard = QGuiApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(text)
        window.prompt.insertPlainText(text)
        window.prompt.setFocus()
        window._set_status(
            t("Dikte metni panoya kopyalandı ve yazma alanına eklendi."), "ready"
        )


def handle_dictation_ui(window: DesktopWindow, kind: str, message: str) -> None:
    """Dikte olaylarını üst durum, katman, pet ve RAM içi son-5 geçmişe işler."""
    if kind in {"listening", "working"}:
        line = t("Dikte dinliyor…") if kind == "listening" else t("Dikte yazıya çevriliyor…")
        window._set_status(line, "running")
        window.overlay.set_busy(True, line)
        window.pet.set_voice("listening" if kind == "listening" else "thinking")
    elif kind == "idle":
        window._set_status(
            t("Çalışıyor…") if window._busy else t("Hazır"),
            "running" if window._busy else "ready",
        )
        window.overlay.set_busy(window._busy)
        window.pet.set_voice(window._voice_state)
    elif kind == "history" and message:
        window._dictation_history.add(message)
        refresh_dictation_history_menu(window)
    elif kind == "message" and message:
        window._set_status(message, "ready")
    elif kind == "text":
        # Yazılamayan metin kaybolmasın; pano değil panel (konuşma geçmişine girmez).
        window._append_chat(
            "Artemis",
            t("Dikte metni (yazılmadı ya da yarım kaldı): {message}").format(message=message),
        )
