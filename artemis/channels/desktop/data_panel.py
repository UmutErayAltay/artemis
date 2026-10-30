"""Yerel geçmiş ve hatırlatma kontrolleri; tüm işlemler worker'ın tipli araç yolunda."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from artemis.channels.desktop.projects_panel import (
    AutoHideLabel,
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

    # Sayfa içeriği okunabilir genişlikte kalır; fazlası kenar boşluğu olur.
    MAX_CONTENT_WIDTH = 960

    def __init__(self, *, demo: bool = False) -> None:
        super().__init__()
        self._demo = demo
        self._busy = True
        self._history_loaded = False
        self._auto_tried = False
        self._loaded_days = 0
        self._turns: list[dict[str, Any]] = []
        self._turn_total = 0
        outer = make_scroll_page(self)
        body = QWidget()
        body.setMaximumWidth(self.MAX_CONTENT_WIDTH)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)
        holder = QHBoxLayout()
        holder.setContentsMargins(0, 0, 0, 0)
        holder.addWidget(body, 1)
        outer.addLayout(holder)
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
        self.history = QPushButton("Yenile")
        self.history.setAccessibleName("Konuşma geçmişini göster veya yenile")
        self.history.clicked.connect(self._request_history)
        header.addWidget(self.history)
        hist.addLayout(header)
        hist.addWidget(make_label("Yeni sohbet açmak geçmişi silmez.", "fieldLabel"))
        # Arama yalnız yüklenmiş satırları süzer; yeni veri kaynağı ya da araç çağrısı yok.
        self.search = QLineEdit()
        self.search.setClearButtonEnabled(True)
        self.search.setPlaceholderText("Konuşmalarda ara: bir kelime ya da tarih yaz")
        self.search.setAccessibleName("Geçmişte ara")
        self.search.textChanged.connect(lambda _: self._render_history())
        hist.addWidget(make_field("Geçmişte ara", self.search))
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setAccessibleName("Konuşma geçmişi")
        # "Yüklenmedi" eylem sunar (Yükle); "yüklendi, boş" düz cümledir (_render_history).
        self.history_empty = EmptyState(
            "Henüz yüklenmedi", "Konuşma geçmişini görmek için yükle.", "Geçmişi yükle"
        )
        if self.history_empty.action_button is not None:
            self.history_empty.action_button.clicked.connect(self._request_history)
        self.history_stack = QStackedWidget()
        self.history_stack.setMinimumHeight(160)
        self.history_stack.addWidget(self.preview)
        self.history_stack.addWidget(self.history_empty)
        self.history_stack.setCurrentIndex(1)
        hist.addWidget(self.history_stack, 1)
        # Saklama: etiket üstte, iki kısa satır yardım; Uygula değer değişene dek pasif.
        self.days = QSpinBox()
        self.days.setRange(0, 36500)
        self.days.setSuffix(" gün")
        self.days.setSpecialValueText("Otomatik silme kapalı")
        self.days.setAccessibleName("Saklama süresi")
        self.days.valueChanged.connect(lambda _: self.set_busy(self._busy))
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
        self.retention_hint = make_label(
            "0 yazarsan otomatik silme kapalı kalır.\n"
            "Pozitif süre, daha eski konuşmaları hemen siler; öz notlar kalır.",
            "fieldLabel",
        )
        hist.addWidget(self.retention_hint)
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
        # Kısa etiket + ayrı uyarı satırı: kullanıcı ne olacağını okur, düğme uzun cümle olmaz.
        self.retry = QPushButton("Yeniden dene")
        self.retry.setObjectName("amber")
        # Tek başına neyi yeniden denediği belli olmuyordu: ipucu ve ekran okuyucu adı söyler.
        retry_tip = (
            "Listede seçili hatırlatma teslimini yeniden dener. "
            "Yalnız başarısız, belirsiz veya kaçırılmış teslimlerde açılır."
        )
        self.retry.setToolTip(retry_tip)
        self.retry.setAccessibleName("Seçili hatırlatma teslimini yeniden dene")
        self.retry.clicked.connect(self._retry)
        self.retry_warning = make_label("Çift bildirim olabilir", "warnNote")
        self.retry_warning.setVisible(False)
        retry_col = QVBoxLayout()
        retry_col.setSpacing(4)
        retry_col.addWidget(self.retry, 0, Qt.AlignmentFlag.AlignRight)
        retry_col.addWidget(self.retry_warning, 0, Qt.AlignmentFlag.AlignRight)
        filter_row = QHBoxLayout()
        filter_row.setSpacing(8)
        filter_row.addWidget(make_field("Teslim durumu", self.filter))
        filter_row.addWidget(self.refresh, 0, Qt.AlignmentFlag.AlignBottom)
        filter_row.addStretch()
        filter_row.addLayout(retry_col)
        rem.addLayout(filter_row)
        self.items = QListWidget()
        self.items.setAccessibleName("Hatırlatma teslimleri")
        self.items.currentRowChanged.connect(lambda _: self.set_busy(self._busy))
        self.items_empty = EmptyState("Henüz yüklenmedi", "Görmek için Yenile'ye bas.")
        self.items_stack = QStackedWidget()
        self.items_stack.setMinimumHeight(160)
        self.items_stack.addWidget(self.items)
        self.items_stack.addWidget(self.items_empty)
        self.items_stack.setCurrentIndex(1)
        rem.addWidget(self.items_stack, 1)
        layout.addWidget(reminder_card)
        self.notice = make_label("Verileri görmek için ilgili yenileme düğmesini kullanın.", "meta")
        layout.addWidget(self.notice)

        # Yıkıcı eylem sayfanın en altında kendi bloğunda; onay akışı araç kapısında kalır.
        self.danger_zone = QFrame()
        self.danger_zone.setObjectName("dangerZone")
        danger = QVBoxLayout(self.danger_zone)
        danger.setContentsMargins(16, 16, 16, 16)
        danger.setSpacing(8)
        danger.addWidget(make_label("Tehlikeli işlemler", "sectionTitle"))
        danger.addWidget(
            make_label(
                "Tüm konuşma geçmişini siler; öz notlar kalır. "
                "Disk ve yedeklerden güvenli silme garantisi vermez.",
                "fieldLabel",
            )
        )
        self.delete = QPushButton("Tüm konuşma geçmişini sil…")
        self.delete.setObjectName("danger")
        self.delete.clicked.connect(
            lambda: self.tool_requested.emit(
                "conversation_history", HistoryArguments(action="delete")
            )
        )
        self.delete_reason = AutoHideLabel()
        delete_row = QHBoxLayout()
        delete_row.setSpacing(12)
        delete_row.addWidget(self.delete, 0, Qt.AlignmentFlag.AlignLeft)
        delete_row.addWidget(self.delete_reason, 1)
        danger.addLayout(delete_row)
        layout.addWidget(self.danger_zone)
        self.set_busy(True)

    def _request_history(self) -> None:
        self.tool_requested.emit("conversation_history", HistoryArguments(action="list"))

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 (Qt adı)
        super().showEvent(event)
        self._schedule_autoload()

    def _schedule_autoload(self) -> None:
        # Doğrudan emit, çağıran _set_controls'un ortasında yeniden girişe yol açar; olay
        # döngüsüne ertelenir ve koşullar o anda yeniden denetlenir.
        if not self._auto_tried and not self._history_loaded:
            QTimer.singleShot(0, self._autoload)

    def _autoload(self) -> None:
        """Sayfa ilk görününce salt-okunur "list" bir kez istenir; demo/meşgulken istenmez.

        Hata olursa tekrar denenmez (döngü yok); elle Yenile her zaman çalışır.
        """
        if (
            self._auto_tried
            or self._history_loaded
            or self._busy
            or self._demo
            or not self.isVisible()
        ):
            return
        self._auto_tried = True
        self._request_history()

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
                self.history_empty.set_text(
                    "Henüz yüklenmedi", "Konuşma geçmişini görmek için yükle."
                )
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
        for widget in (self.history, self.filter, self.refresh):
            widget.setEnabled(enabled)
        action = self.history_empty.action_button
        if action is not None:
            action.setEnabled(enabled)
            # Yalnız "yüklenmedi" eylem sunar; yüklendi-boş durumunda düğme yoktur.
            action.setVisible(not self._history_loaded)
        loaded = enabled and self._history_loaded
        self.days.setEnabled(loaded)
        self.delete.setEnabled(loaded)
        # Uygula yalnız değer yüklü değerden farklıyken anlamlıdır.
        self.retention.setEnabled(loaded and self.days.value() != self._loaded_days)
        if self._demo:
            self.delete_reason.setText("Demo modunda kapalı.")
        elif not self._history_loaded:
            self.delete_reason.setText("Önce geçmişi yükle.")
        else:
            self.delete_reason.setText("")
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
        self.retry_warning.setVisible(self.retry.isEnabled())
        if enabled:
            self._schedule_autoload()

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
            self._loaded_days = result.data["retention_days"]
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
