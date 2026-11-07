"""Onay penceresi ve özet yardımcıları."""

from __future__ import annotations

import html
import json
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QCloseEvent, QKeyEvent
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from artemis.channels.desktop.bridge import Approval


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

        # Okunur ad büyük başlıktır; snake_case ham ad gündelik kullanıcıya gürültüdür, bu yüzden
        # "Teknik ayrıntılar" alanında durur (ADR 0014); etiketsiz araçta başlık ham ada düşer.
        title = QLabel(item.tool_label or item.tool_name)
        title.setObjectName("title")
        title.setTextFormat(Qt.TextFormat.PlainText)
        title.setWordWrap(True)
        body_layout.addWidget(title)

        # Kod ajanı: hangi ajanın nerede başlayacağı ilk düz dil bloğudur (ADR 0049). Metin
        # arka uçtan gelir; proje dışı klasörde uyarı satırı uyarı tonlu kutuda ayrılır.
        self.approval_text_label: QLabel | None = None
        self.workdir_warning_box: QLabel | None = None
        spec = item.arguments.get("spec")
        if item.tool_name == "code_agent_start" and isinstance(spec, dict):
            lines = str(spec.get("approval_text", "")).split("\n")
            # İlk 4 satır sabit (ajan, klasör, repo, kip); sonrakiler uyarıdır (proje dışı
            # klasör, görünür pencere kipi) ve uyarı tonlu kutuda ayrı gösterilir.
            head, warn = lines[:4], lines[4:]
            if head and any(head):
                self.approval_text_label = QLabel("\n".join(head))
                self.approval_text_label.setTextFormat(Qt.TextFormat.PlainText)
                self.approval_text_label.setWordWrap(True)
                self.approval_text_label.setAccessibleName("Ajan ve başlangıç klasörü")
                body_layout.addWidget(self.approval_text_label)
            if warn:
                self.workdir_warning_box = QLabel("\n".join(warn))
                self.workdir_warning_box.setObjectName("approvalNotice")
                self.workdir_warning_box.setTextFormat(Qt.TextFormat.PlainText)
                self.workdir_warning_box.setWordWrap(True)
                body_layout.addWidget(self.workdir_warning_box)

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
            row = QLabel(f"{key}: {shown}")
            row.setTextFormat(Qt.TextFormat.PlainText)
            row.setWordWrap(True)
            row.setToolTip(f"<pre>{html.escape(f'{key}: {full}')}</pre>")
            row.setAccessibleName(f"Argüman {key}")
            rows.addWidget(row)
        if not item.arguments:
            rows.addWidget(QLabel("Argüman yok."))

        # Geçit iki ayrı iletiyi ". " ile birleştirir (dış içerik/onay notu + aracın açıklaması);
        # tek paragrafta okunmaz, bu yüzden uyarı kutusu ile açıklama ayrı satırlara bölünür.
        warning, _sep, tool_note = (item.notice or "Bu işlem onay gerektirir.").partition(". ")
        notice = QLabel(warning + ("." if tool_note else ""))
        notice.setObjectName("approvalNotice")
        notice.setTextFormat(Qt.TextFormat.PlainText)
        notice.setWordWrap(True)
        body_layout.addWidget(notice)
        self.tool_note_label: QLabel | None = None
        if tool_note:
            self.tool_note_label = QLabel(tool_note)
            self.tool_note_label.setTextFormat(Qt.TextFormat.PlainText)
            self.tool_note_label.setWordWrap(True)
            body_layout.addWidget(self.tool_note_label)

        self.details_toggle = QPushButton("Teknik ayrıntılar")
        self.details_toggle.setObjectName("ghost")
        self.details_toggle.setCheckable(True)
        self.details_toggle.setAccessibleName("Teknik ayrıntıları göster veya gizle")
        body_layout.addWidget(self.details_toggle, 0, Qt.AlignmentFlag.AlignLeft)
        self.raw_name = QLabel(f"Araç adı: {item.tool_name}")
        self.raw_name.setObjectName("rawToolName")
        self.raw_name.setTextFormat(Qt.TextFormat.PlainText)
        self.raw_name.setAccessibleName("Ham araç adı")
        body_layout.addWidget(self.raw_name)
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
        self.raw_name.setVisible(on)
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
