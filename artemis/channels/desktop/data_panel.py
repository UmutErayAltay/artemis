"""Yerel geçmiş ve hatırlatma kontrolleri; tüm işlemler worker'ın tipli araç yolunda."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QWidget,
)

from artemis.channels.desktop.projects_panel import (
    EmptyState,
    make_card,
    make_field,
    make_label,
    make_page_header,
    make_scroll_page,
)
from artemis.tools.base import ToolResult
from artemis.tools.plugins.history import HistoryArguments
from artemis.tools.plugins.reminders import ReminderCancelArguments, ReminderListArguments

_STATES = {
    "pending": "Bekliyor",
    "claimed": "İşleniyor",
    "delivered": "Teslim edildi",
    "failed": "Başarısız",
    "missed": "Kaçırıldı",
    "uncertain": "Teslim doğrulanamadı",
    "cancelled": "İptal edildi",
}


def _fold(text: str) -> str:
    """Türkçe i/ı/İ/I ayrımını arama için eşitler; salt casefold 'IŞIK' ile 'ışık'ı eşlemez."""
    return text.translate(str.maketrans("İIı", "iii")).casefold()


class DataPanel(QWidget):
    tool_requested = Signal(str, object)

    def __init__(self, *, demo: bool = False) -> None:
        super().__init__()
        self._demo = demo
        self._busy = True
        self._history_loaded = False
        self._turns: list[dict[str, Any]] = []
        self._turn_total = 0
        layout = make_scroll_page(self)
        layout.addWidget(
            make_page_header(
                "Geçmiş ve hatırlatmalar",
                "Konuşma geçmişini incele, saklama süresini ayarla, "
                "hatırlatma teslimlerini izle.",
            )
        )

        # Kart 1: konuşma geçmişi.
        history_card, hist = make_card()
        header = QHBoxLayout()
        header.setSpacing(8)
        header.addWidget(make_label("Konuşma geçmişi", "sectionTitle"))
        header.addStretch()
        self.history = QPushButton("Konuşma geçmişini göster/yenile")
        self.history.clicked.connect(
            lambda: self.tool_requested.emit(
                "conversation_history", HistoryArguments(action="list")
            )
        )
        self.delete = QPushButton("Tüm konuşma geçmişini sil…")
        self.delete.setObjectName("danger")
        self.delete.clicked.connect(
            lambda: self.tool_requested.emit(
                "conversation_history", HistoryArguments(action="delete")
            )
        )
        header.addWidget(self.history)
        header.addWidget(self.delete)
        hist.addLayout(header)
        hist.addWidget(
            make_label(
                "Yeni sohbet geçmişi silmez. Saklama süresi 0: otomatik silme kapalı. "
                "Pozitif süre seçimi eski konuşmaları hemen siler; kalıcı öz notlar ayrı kalır. "
                "Silme, disk ve yedeklerden güvenli silme garantisi vermez.",
                "meta",
            )
        )
        # Arama yalnız yüklenmiş satırları süzer; yeni veri kaynağı ya da araç çağrısı yok.
        self.search = QLineEdit()
        self.search.setClearButtonEnabled(True)
        self.search.setPlaceholderText("Örn. toplantı")
        self.search.textChanged.connect(lambda _: self._render_history())
        hist.addWidget(make_field("Geçmişte ara", self.search))
        self.days = QSpinBox()
        self.days.setRange(0, 36500)
        self.days.setSuffix(" gün")
        self.days.setSpecialValueText("Otomatik silme kapalı")
        self.retention = QPushButton("Uygula")
        self.retention.clicked.connect(
            lambda: self.tool_requested.emit(
                "conversation_history",
                HistoryArguments(action="retention", retention_days=self.days.value()),
            )
        )
        retention_row = QHBoxLayout()
        retention_row.setSpacing(8)
        retention_row.addWidget(make_field("Saklama süresi", self.days))
        retention_row.addWidget(self.retention, 0, Qt.AlignmentFlag.AlignBottom)
        retention_row.addStretch()
        hist.addLayout(retention_row)
        self.retention_hint = make_label("Önce geçmişi yükle.", "meta")
        hist.addWidget(self.retention_hint)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.history_empty = EmptyState("Henüz yüklenmedi", "Görmek için Göster'e bas.")
        self.history_stack = QStackedWidget()
        self.history_stack.setMinimumHeight(160)
        self.history_stack.addWidget(self.preview)
        self.history_stack.addWidget(self.history_empty)
        self.history_stack.setCurrentIndex(1)
        hist.addWidget(self.history_stack, 1)
        layout.addWidget(history_card, 1)

        # Kart 2: hatırlatma teslimleri.
        reminder_card, rem = make_card()
        rem.addWidget(make_label("Hatırlatma teslimleri", "sectionTitle"))
        self.filter = QComboBox()
        self.filter.addItem("Tüm teslim durumları", "all")
        for state, label in _STATES.items():
            self.filter.addItem(label, state)
        self.refresh = QPushButton("Yenile")
        self.refresh.clicked.connect(
            lambda: self.tool_requested.emit(
                "reminder_list", ReminderListArguments(state=self.filter.currentData())
            )
        )
        self.retry = QPushButton("Yeniden dene… (çift bildirim olabilir)")
        self.retry.setObjectName("amber")
        self.retry.clicked.connect(self._retry)
        filter_row = QHBoxLayout()
        filter_row.setSpacing(8)
        filter_row.addWidget(make_field("Teslim durumu", self.filter))
        filter_row.addWidget(self.refresh, 0, Qt.AlignmentFlag.AlignBottom)
        filter_row.addStretch()
        filter_row.addWidget(self.retry, 0, Qt.AlignmentFlag.AlignBottom)
        rem.addLayout(filter_row)
        self.items = QListWidget()
        self.items.currentRowChanged.connect(lambda _: self.set_busy(self._busy))
        self.items_empty = EmptyState("Henüz yüklenmedi", "Görmek için Yenile'ye bas.")
        self.items_stack = QStackedWidget()
        self.items_stack.setMinimumHeight(160)
        self.items_stack.addWidget(self.items)
        self.items_stack.addWidget(self.items_empty)
        self.items_stack.setCurrentIndex(1)
        rem.addWidget(self.items_stack, 1)
        layout.addWidget(reminder_card, 1)
        self.notice = make_label("Verileri görmek için ilgili yenileme düğmesini kullanın.", "meta")
        layout.addWidget(self.notice)
        self.set_busy(True)

    def _render_history(self) -> None:
        """Yüklü turları arama kutusuna göre süz; casefold Türkçe büyük/küçük harf için yeterli."""
        needle = _fold(self.search.text().strip())
        turns = [
            t
            for t in self._turns
            if not needle or needle in _fold(f"{t['created_at']} {t['user']} {t['answer']}")
        ]
        if not turns:
            if not self._history_loaded:
                self.history_empty.set_text("Henüz yüklenmedi", "Görmek için Göster'e bas.")
            elif needle:
                self.history_empty.set_text("Eşleşme yok", "Aramayı değiştir veya temizle.")
            else:
                self.history_empty.set_text("Kayıt yok", "Kayıtlı konuşma turu yok.")
            self.history_stack.setCurrentIndex(1)
            return
        lines = [f"Toplam {self._turn_total} konuşma turu; en son 50 tur gösterilir."]
        for turn in turns:
            lines.append(
                f"\n{turn['created_at']}\nSiz: {turn['user']}\nArtemis: {turn['answer']}"
            )
        self.preview.setPlainText("\n".join(lines))
        self.history_stack.setCurrentIndex(0)

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        enabled = not busy and not self._demo
        for widget in (
            self.history,
            self.retention,
            self.delete,
            self.days,
            self.filter,
            self.refresh,
        ):
            widget.setEnabled(enabled)
        for control in (self.days, self.retention, self.delete):
            control.setEnabled(enabled and self._history_loaded)
        self.retention_hint.setVisible(not self._history_loaded)
        item = self.items.currentItem()
        data = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        self.retry.setEnabled(
            enabled
            and data is not None
            and data["state"]
            in {
                "failed",
                "uncertain",
                "missed",
            }
        )

    def _retry(self) -> None:
        item = self.items.currentItem()
        if item is not None:
            data = item.data(Qt.ItemDataRole.UserRole)
            self.tool_requested.emit(
                "reminder_retry", ReminderCancelArguments(reminder_id=data["reminder_id"])
            )

    def accept_result(self, tool_name: str, result: ToolResult) -> None:
        self.notice.setText(result.message)
        if not result.success:
            return
        if tool_name == "conversation_history":
            self._history_loaded = True
            self.days.setValue(result.data["retention_days"])
            self._turn_total = result.data["turns"]
            self._turns = list(result.data.get("recent_turns", []))
            self._render_history()
        elif tool_name == "reminder_list":
            self.items.clear()
            for reminder in result.data.get("reminders", []):
                row: dict[str, Any] = reminder
                item = QListWidgetItem(
                    f"{_STATES.get(row['state'], row['state'])} · "
                    f"{row['due_local']} · {row['text']}"
                )
                item.setData(Qt.ItemDataRole.UserRole, row)
                self.items.addItem(item)
            if self.items.count():
                self.items_stack.setCurrentIndex(0)
            else:
                self.items_empty.set_text("Kayıt yok", "Bu filtreye uyan hatırlatma yok.")
                self.items_stack.setCurrentIndex(1)
        elif tool_name == "reminder_retry":
            # Aynı eski kaydı ikinci kez tıklamayı önle; geçmiş yenilenince seçilebilir.
            self.items.setCurrentRow(-1)
        self.set_busy(self._busy)
