"""Yerel geçmiş ve hatırlatma kontrolleri; tüm işlemler worker'ın tipli araç yolunda."""

from __future__ import annotations

from datetime import datetime
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
from artemis.tools.plugins.companion import ChangesArguments, UndoArguments
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

_MEMORY_FILES = {"kimlik": "Kimlik", "isler": "Isler", "son_oturum": "SonOturum"}
_MEMORY_SOURCES = {
    "session_update": "oturum güncellemesi",
    "rule_add": "kural",
    "job_receipt": "makbuz",
    "undo": "geri alma",
}


def _local_time(iso: str) -> str:
    """Günlük UTC yazar; kullanıcıya yerel saat gösterilir. Çözülemezse ham metin kalır."""
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return iso


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
        self._memory_loaded = False
        self._memory_pending = False
        self._memory_keep_notice = False
        self._undo_buttons: list[QPushButton] = []
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

        # Kart 3: Artemis'in kendi hafıza dosyalarına yazdıkları; geri alma araç kapısından geçer.
        memory_card, mem = make_card()
        mem_header = QHBoxLayout()
        mem_header.setSpacing(8)
        mem_header.addWidget(make_label("Hafıza değişiklikleri", "sectionTitle"))
        mem_header.addStretch()
        self.memory_refresh = QPushButton("Yenile")
        self.memory_refresh.setAccessibleName("Hafıza değişikliklerini yenile")
        self.memory_refresh.clicked.connect(self._request_memory)
        mem_header.addWidget(self.memory_refresh)
        mem.addLayout(mem_header)
        mem.addWidget(
            make_label(
                "Artemis'in kendi hafıza dosyalarına yazdığı son değişiklikler. "
                "Dosya sonradan değiştiyse geri alma reddedilir.",
                "fieldLabel",
            )
        )
        self.memory_rows = QVBoxLayout()
        self.memory_rows.setSpacing(8)
        rows_box = QWidget()
        rows_box.setLayout(self.memory_rows)
        self.memory_empty = EmptyState("Henüz yüklenmedi", "Görmek için Yenile'ye bas.")
        self.memory_stack = QStackedWidget()
        self.memory_stack.addWidget(rows_box)
        self.memory_stack.addWidget(self.memory_empty)
        self.memory_stack.setCurrentIndex(1)
        mem.addWidget(self.memory_stack)
        layout.addWidget(memory_card)
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

    def _request_memory(self) -> None:
        self.tool_requested.emit("memory_changes", ChangesArguments(limit=20))

    def _undo_clicked(self) -> None:
        button = self.sender()
        if button is not None:
            self.tool_requested.emit(
                "memory_change_undo", UndoArguments(change_id=button.property("change_id"))
            )

    def _render_memory(self, changes: list[dict[str, Any]]) -> None:
        while self.memory_rows.count():
            child = self.memory_rows.takeAt(0)
            widget = child.widget() if child is not None else None
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self._undo_buttons = []
        for change in changes:
            row = QFrame()
            row.setObjectName("card")
            lay = QVBoxLayout(row)
            lay.setContentsMargins(12, 8, 12, 8)
            lay.setSpacing(4)
            top = QHBoxLayout()
            top.setSpacing(8)
            source = _MEMORY_SOURCES.get(change["source"], change["source"])
            file = _MEMORY_FILES.get(change["file"], change["file"])
            top.addWidget(make_label(f"{_local_time(change['at'])} · {file} · {source}", "meta"), 1)
            if change.get("tainted"):
                top.addWidget(make_label("dış içerik", "warnNote"))
            undo = QPushButton("Geri al")
            undo.setAccessibleName(f"{file} değişikliğini geri al")
            undo.setProperty("change_id", int(change["id"]))
            undo.clicked.connect(self._undo_clicked)
            self._undo_buttons.append(undo)
            top.addWidget(undo)
            lay.addLayout(top)
            lay.addWidget(make_label(f"{change['before']} → {change['after']}", "fieldLabel"))
            self.memory_rows.addWidget(row)
        if changes:
            self.memory_stack.setCurrentIndex(0)
        else:
            self.memory_empty.set_text("Kayıt yok", "Henüz hafıza değişikliği yok.")
            self.memory_stack.setCurrentIndex(1)

    def _flush_memory_request(self) -> None:
        if self._memory_pending and not self._busy and not self._demo:
            self._memory_pending = False
            self._request_memory()

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
        self.memory_refresh.setEnabled(enabled)
        for button in self._undo_buttons:
            button.setEnabled(enabled)
        if enabled:
            self._schedule_autoload()
            if self._memory_pending:
                # Tür/yeniden giriş nedeniyle doğrudan emit yok; olay döngüsüne ertelenir.
                QTimer.singleShot(0, self._flush_memory_request)

    def _retry(self) -> None:
        item = self.items.currentItem()
        if item is not None:
            data = item.data(Qt.ItemDataRole.UserRole)
            self.tool_requested.emit(
                "reminder_retry", ReminderCancelArguments(reminder_id=data["reminder_id"])
            )

    def accept_result(self, tool_name: str, result: ToolResult) -> None:
        if tool_name == "memory_changes" and self._memory_keep_notice:
            self._memory_keep_notice = False  # geri alma mesajı liste mesajıyla ezilmesin
        else:
            self.notice.setText(result.message)
        if tool_name == "memory_change_undo":
            # Çakışma dahil her sonuçta liste tazelenir; iş bitip meşguliyet kalkınca istenir.
            self._memory_pending = True
            self._memory_keep_notice = True
        if not result.success:
            return
        if tool_name == "conversation_history":
            if not self._memory_loaded:
                self._memory_loaded = True
                self._memory_pending = True
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
        elif tool_name == "memory_changes":
            self._render_memory(list(result.data.get("changes", [])))
        elif tool_name == "reminder_retry":
            # Aynı eski kaydı ikinci kez tıklamayı önle; geçmiş yenilenince seçilebilir.
            self.items.setCurrentRow(-1)
        self.set_busy(self._busy)
