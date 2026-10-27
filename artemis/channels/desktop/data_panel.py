"""Yerel geçmiş ve hatırlatma kontrolleri; tüm işlemler worker'ın tipli araç yolunda."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
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


class DataPanel(QWidget):
    tool_requested = Signal(str, object)

    def __init__(self, *, demo: bool = False) -> None:
        super().__init__()
        self._demo = demo
        self._busy = True
        self._history_loaded = False
        layout = QVBoxLayout(self)
        hint = QLabel(
            "Yeni sohbet geçmişi silmez. Saklama süresi 0: otomatik silme kapalı. "
            "Pozitif süre seçimi eski konuşmaları hemen siler; kalıcı öz notlar ayrı kalır. "
            "Silme, disk ve yedeklerden güvenli silme garantisi vermez."
        )
        heading = QLabel("Konuşma geçmişi")
        heading.setObjectName("section")
        layout.addWidget(heading)
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        hint.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(hint)
        controls = QHBoxLayout()
        self.history = QPushButton("Konuşma geçmişini göster")
        self.history.clicked.connect(
            lambda: self.tool_requested.emit(
                "conversation_history", HistoryArguments(action="list")
            )
        )
        self.days = QSpinBox()
        self.days.setRange(0, 36500)
        self.days.setSuffix(" gün")
        self.days.setSpecialValueText("Önce geçmişi yükleyin")
        self.retention = QPushButton("Saklama süresini uygula")
        self.retention.clicked.connect(
            lambda: self.tool_requested.emit(
                "conversation_history",
                HistoryArguments(action="retention", retention_days=self.days.value()),
            )
        )
        self.delete = QPushButton("Tüm konuşma geçmişini sil…")
        self.delete.setObjectName("danger")
        self.delete.clicked.connect(
            lambda: self.tool_requested.emit(
                "conversation_history", HistoryArguments(action="delete")
            )
        )
        controls.addWidget(self.history)
        controls.addStretch()
        controls.addWidget(self.delete)
        layout.addLayout(controls)
        retention_controls = QHBoxLayout()
        retention_controls.addWidget(QLabel("Saklama süresi"))
        retention_controls.addWidget(self.days)
        retention_controls.addWidget(self.retention)
        retention_controls.addStretch()
        layout.addLayout(retention_controls)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        layout.addWidget(self.preview, 1)
        reminder_heading = QLabel("Hatırlatma teslimleri")
        reminder_heading.setObjectName("section")
        layout.addWidget(reminder_heading)
        reminders = QHBoxLayout()
        self.filter = QComboBox()
        self.filter.addItem("Tüm teslim durumları", "all")
        for state, label in _STATES.items():
            self.filter.addItem(label, state)
        self.refresh = QPushButton("Hatırlatmaları yenile")
        self.refresh.clicked.connect(
            lambda: self.tool_requested.emit(
                "reminder_list", ReminderListArguments(state=self.filter.currentData())
            )
        )
        self.retry = QPushButton("Yeniden dene… (çift bildirim olabilir)")
        self.retry.setObjectName("amber")
        self.retry.clicked.connect(self._retry)
        for control in (self.filter, self.refresh, self.retry):
            reminders.addWidget(control)
        layout.addLayout(reminders)
        self.items = QListWidget()
        self.items.currentRowChanged.connect(lambda _: self.set_busy(self._busy))
        layout.addWidget(self.items, 1)
        self.notice = QLabel("Verileri görmek için ilgili yenileme düğmesini kullanın.")
        self.notice.setTextFormat(Qt.TextFormat.PlainText)
        self.notice.setWordWrap(True)
        layout.addWidget(self.notice)
        self.set_busy(True)

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
            self.days.setSpecialValueText("Otomatik silme kapalı")
            self.days.setValue(result.data["retention_days"])
            lines = [f"Toplam {result.data['turns']} konuşma turu; en son 50 tur gösterilir."]
            for turn in result.data.get("recent_turns", []):
                lines.append(
                    f"\n{turn['created_at']}\nSiz: {turn['user']}\nArtemis: {turn['answer']}"
                )
            self.preview.setPlainText("\n".join(lines))
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
        elif tool_name == "reminder_retry":
            # Aynı eski kaydı ikinci kez tıklamayı önle; geçmiş yenilenince seçilebilir.
            self.items.setCurrentRow(-1)
        self.set_busy(self._busy)
