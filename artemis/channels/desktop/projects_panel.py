"""Projeler düz metin sunumudur; eylem sahipliği CommandRunner'da kalır."""

from __future__ import annotations

import html
import json
from typing import Any

from pydantic import ValidationError
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QResizeEvent, QShowEvent
from PySide6.QtWidgets import (
    QBoxLayout,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from artemis.tools.base import ToolResult
from artemis.tools.plugins.code_agent import (
    AnswerArguments,
    CancelArguments,
    PrepareArguments,
    StartArguments,
    StatusArguments,
)
from artemis.tools.project_spec import AUTHORITY_BOUNDARY, ProjectSpec

STATUS_LABELS = {
    "queued": "Sırada",
    "running": "Çalışıyor",
    "waiting": "Cevap bekliyor",
    "cancelling": "Durduruluyor",
    "cancelled": "İptal edildi",
    "timeout": "Süre doldu",
    "done": "Doğrulandı",
    "error": "Başarısız",
    "interrupted": "Kesintiye uğradı",
    "cleanup_pending": "Süreç temizliği belirsiz",
}


def make_label(text: str, name: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName(name)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


class AutoHideLabel(QLabel):
    """Metin boşken gizlenen `meta` etiketi; boş durum satırı kartta boşluk bırakmaz."""

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("meta")
        self.setWordWrap(True)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.hide()

    def setText(self, text: str) -> None:  # noqa: N802 - Qt imzası
        super().setText(text)
        self.setVisible(bool(text))


def make_field(caption: str, widget: QWidget) -> QWidget:
    """Etiket alanın üstünde kalıcıdır; placeholder yalnız örnek verir."""
    box = QWidget()
    lay = QVBoxLayout(box)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(4)
    lay.addWidget(make_label(caption, "fieldLabel"))
    lay.addWidget(widget)
    return box


def make_page_header(
    title: str, subtitle: str | None = None, actions: tuple[QWidget, ...] = ()
) -> QWidget:
    """Her sayfanın aynı başlık satırı: 48px sabit, sağda eylemler, 4px altında alt başlık.

    Sayfalar arası başlık konumu bu tek yerden gelir; sayfa düzenindeki 16px aralık
    ilk karta kadar olan boşluğu verir.
    """
    host = QWidget()
    column = QVBoxLayout(host)
    column.setContentsMargins(0, 0, 0, 0)
    column.setSpacing(4)
    row = QHBoxLayout()
    row.setSpacing(8)
    heading = QLabel(title)
    heading.setObjectName("pageTitle")
    heading.setTextFormat(Qt.TextFormat.PlainText)
    heading.setFixedHeight(48)
    row.addWidget(heading)
    row.addStretch(1)
    for action in actions:
        row.addWidget(action)
    column.addLayout(row)
    if subtitle:
        column.addWidget(make_label(subtitle, "meta"))
    return host


def make_scroll_page(panel: QWidget) -> QVBoxLayout:
    """Sayfada tek kaydırma bölgesi: dar pencerede kartlar sıkışmak yerine kayar."""
    outer = QVBoxLayout(panel)
    outer.setContentsMargins(0, 0, 0, 0)
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    content = QWidget()
    scroll.setWidget(content)
    outer.addWidget(scroll)
    layout = QVBoxLayout(content)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(16)
    return layout


def make_card() -> tuple[QFrame, QVBoxLayout]:
    card = QFrame()
    card.setObjectName("card")
    lay = QVBoxLayout(card)
    lay.setContentsMargins(16, 16, 16, 16)
    lay.setSpacing(12)
    return card, lay


class EmptyState(QFrame):
    """Boş durum kutusu; başlık ve gövde etiketleri açık alan olarak tutulur."""

    def __init__(self, title: str, body: str, action_text: str | None = None) -> None:
        super().__init__()
        self.setObjectName("emptyState")
        lay = QVBoxLayout(self)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.setSpacing(4)
        self.title_label = make_label(title, "emptyTitle")
        self.body_label = make_label(body, "emptyBody")
        for label in (self.title_label, self.body_label):
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lay.addWidget(label)
        # Yalnız "yüklenmedi" durumunda eylem sunulur (DESIGN: yüklendi-boş durumunda eylem yok).
        self.action_button: QPushButton | None = None
        if action_text:
            self.action_button = QPushButton(action_text)
            lay.addWidget(self.action_button, 0, Qt.AlignmentFlag.AlignCenter)
        self.set_text(title, body)

    def set_text(self, title: str, body: str) -> None:
        self.title_label.setText(title)
        self.body_label.setText(body)
        self._fit_width()

    def _fit_width(self) -> None:
        """Sarmalı etiket dar ipucu verir ("Göster'e / bas."); gövde ~360px'e kadar tek satır kalır.

        Ölçü stil uygulandıktan sonraki yazı tipiyle alınmalı; bu yüzden gösterimde de çağrılır.
        """
        wide = self.body_label.fontMetrics().horizontalAdvance(self.body_label.text()) + 8
        self.body_label.setFixedWidth(min(360, wide))
        title = self.title_label.fontMetrics().horizontalAdvance(self.title_label.text())
        self.title_label.setMinimumWidth(title + 8)

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self._fit_width()


# Durum çipi: (etiket, ton). Ton QSS'te `tone` özelliğiyle renklenir; renk kodu burada yok.
STATUS_CHIPS = {
    "queued": ("Hazırlanıyor", "running"),
    "running": ("Çalışıyor", "running"),
    "waiting": ("Cevap bekliyor", "waiting"),
    "cancelling": ("Durduruluyor", "waiting"),
    "cancelled": ("Durduruldu", "neutral"),
    "timeout": ("Hata", "error"),
    "done": ("Bitti", "done"),
    "error": ("Hata", "error"),
    "interrupted": ("Hata", "error"),
    "cleanup_pending": ("Hata", "error"),
}
FLOW_STEPS = ("Hazırla", "İncele", "Başlat")
LIST_MIN_ROWS = 2
LIST_MAX_ROWS = 6


def repolish(widget: QWidget) -> None:
    """Dinamik özellik seçicileri (tone/state) yalnız unpolish+polish ile güncellenir."""
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)


class CollapsibleSection(QWidget):
    """Başlık satırı (ok + başlık) ve katlı gövde; varsayılan kapalı.

    Güvenlik metni silinmez, yalnız katlı durur; bu yüzden gövde etiketi ayrı tutulur.
    """

    def __init__(self, title: str, body: str) -> None:
        super().__init__()
        self._title = title
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        self.toggle = QPushButton()
        self.toggle.setObjectName("disclosure")
        self.toggle.setCheckable(True)
        self.toggle.setAccessibleName(title)
        self.body = make_label(body, "fieldLabel")
        self.body.setVisible(False)
        self.toggle.toggled.connect(self._sync)
        self._sync(False)
        row = QHBoxLayout()
        row.addWidget(self.toggle)
        row.addStretch()
        lay.addLayout(row)
        lay.addWidget(self.body)

    def _sync(self, opened: bool) -> None:
        self.body.setVisible(opened)
        self.toggle.setText(("▾  " if opened else "▸  ") + self._title)


def spec_to_html(kind: str, record: dict[str, Any]) -> str:
    """Taslak/iş ayrıntısı: kalın başlıklar + madde listeleri.

    Kullanıcı ve model metni html.escape'ten geçer; ham HTML asla yorumlanmaz.
    """
    spec = record if kind == "draft" else record.get("spec", {})
    esc = html.escape

    def section(title: str, text: str) -> str:
        return f"<p><b>{title}</b><br>{esc(text)}</p>"

    def bullets(title: str, items: list[Any]) -> str:
        rows = "".join(f"<li>{esc(str(item))}</li>" for item in items) or "<li>—</li>"
        return f"<p><b>{title}</b></p><ul>{rows}</ul>"

    parts = [
        section("Proje", str(spec.get("project", ""))),
        section("Görev", str(spec.get("task", ""))),
        bullets("Kapsam", list(spec.get("scope", []))),
        bullets("Kabul ölçütleri", list(spec.get("acceptance", []))),
    ]
    if kind == "job":
        commits = json.dumps(record.get("commits", []), ensure_ascii=False)
        parts += [
            section("Durum", STATUS_LABELS.get(record.get("status", ""), "Bilinmiyor")),
            section("Özet", str(record.get("summary", ""))),
            section("Soru", str(record.get("question") or "—")),
            section("Commit'ler", commits),
            section("Kalan değişiklikler", str(record.get("leftover_changes"))),
            section("İş kimliği", str(record.get("job_id", ""))),
        ]
    return "".join(parts)


class ProjectsPanel(QWidget):
    tool_requested = Signal(str, object)

    # Bu içerik genişliğinin altında iki sütun alt alta dizilir.
    WIDE_BREAKPOINT = 1000

    def __init__(self, *, demo: bool = False) -> None:
        super().__init__()
        self._demo = demo
        self._items: list[tuple[str, dict[str, Any]]] = []
        self._busy = True
        layout = make_scroll_page(self)
        layout.addWidget(
            make_page_header(
                "Projeler",
                "Yerel bir depoda iş taslağı hazırla, kapsamını incele, onaylayıp başlat.",
            )
        )
        # Üç adımlı akış yalnız seçili taslak/iş durumundan türer (_update_flow); durum tutmaz.
        self.flow_bar = QFrame()
        self.flow_bar.setObjectName("stageBar")
        flow_row = QHBoxLayout(self.flow_bar)
        flow_row.setContentsMargins(0, 0, 0, 0)
        flow_row.setSpacing(8)
        self._flow_labels: list[QLabel] = []
        for i, name in enumerate(FLOW_STEPS):
            label = make_label(name, "stage")
            label.setWordWrap(False)
            self._flow_labels.append(label)
            flow_row.addWidget(label)
            if i < len(FLOW_STEPS) - 1:
                flow_row.addWidget(make_label("→", "meta"))
        flow_row.addStretch(1)
        layout.addWidget(self.flow_bar)

        # Güvenlik metni silinmedi/yumuşatılmadı; yalnız varsayılan olarak katlı.
        self.rules = CollapsibleSection(
            "Bu işler nasıl çalışır?",
            "Taslağı hazırlayın, tam kapsamı inceleyin ve onayla başlatın. " + AUTHORITY_BOUNDARY,
        )
        self.rules_toggle = self.rules.toggle
        self.hint = self.rules.body
        layout.addWidget(self.rules)

        # Sol kart: yeni iş formu.
        left, left_lay = make_card()
        left_lay.addWidget(make_label("Yeni iş", "sectionTitle"))
        self.project = QComboBox()
        self.task = QPlainTextEdit()
        self.task.setPlaceholderText("Örn. Giriş formundaki doğrulamayı düzelt")
        self.scope = QPlainTextEdit()
        self.scope.setPlaceholderText("Örn. Her satır bir madde: yalnız login modülü")
        self.acceptance = QPlainTextEdit()
        self.acceptance.setPlaceholderText("Örn. Her satır bir madde: testler geçiyor")
        for editor in (self.task, self.scope, self.acceptance):
            editor.setMinimumHeight(72)
            editor.setMaximumHeight(96)  # varsayılan sizeHint sütunu şişirmesin
            editor.textChanged.connect(lambda: self.set_busy(self._busy))
        for caption, widget in (
            ("Proje", self.project),
            ("Görev", self.task),
            ("Kapsam", self.scope),
            ("Kabul ölçütleri", self.acceptance),
        ):
            widget.setAccessibleName(caption)
            left_lay.addWidget(make_field(caption, widget))
        left_lay.addStretch()
        self.prepare_button = QPushButton("Taslak hazırla")
        prepare_row = QHBoxLayout()
        prepare_row.addWidget(self.prepare_button)
        prepare_row.addStretch()
        left_lay.addLayout(prepare_row)
        # Pasif düğmenin nedeni düğmenin hemen altında tek satır muted metindir.
        self.prepare_reason = AutoHideLabel()
        left_lay.addWidget(self.prepare_reason)

        # Sağ kart: liste, ayrıntı, eylemler.
        right, right_lay = make_card()
        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        title_row.addWidget(make_label("Taslaklar ve işler", "sectionTitle"))
        self.status_chip = QLabel()
        self.status_chip.setObjectName("statusChip")
        self.status_chip.setTextFormat(Qt.TextFormat.PlainText)
        self.status_chip.setVisible(False)
        title_row.addWidget(self.status_chip)
        title_row.addStretch()
        right_lay.addLayout(title_row)
        self.items = QListWidget()
        self.items.setAccessibleName("Taslaklar ve işler")
        self.items.currentRowChanged.connect(self._select)
        self.list_stack = QStackedWidget()
        self.list_stack.addWidget(self.items)
        self.list_stack.addWidget(
            EmptyState("Henüz taslak yok", "Soldan bir görev yazıp taslak hazırla.")
        )
        right_lay.addWidget(self.list_stack)
        self.preview = QTextBrowser()
        self.preview.setOpenLinks(False)
        self.preview.setOpenExternalLinks(False)
        self.preview.setAccessibleName("Taslak ayrıntısı")
        self.preview.setPlaceholderText("Bir taslak veya iş seç; ayrıntısı burada görünür.")
        self.preview.setMinimumHeight(120)
        right_lay.addWidget(self.preview, 1)
        # Bekleyen soru alanı yalnız seçili iş "waiting" iken görünür (set_busy).
        self.question_box = QWidget()
        q_lay = QVBoxLayout(self.question_box)
        q_lay.setContentsMargins(0, 0, 0, 0)
        q_lay.setSpacing(4)
        self.answer = QPlainTextEdit()
        self.answer.setPlaceholderText("Örn. Mavi olsun")
        self.answer.setAccessibleName("Bekleyen soru")
        self.answer.setMinimumHeight(72)
        self.answer.setMaximumHeight(96)
        q_lay.addWidget(make_label("Bekleyen soru", "fieldLabel"))
        q_lay.addWidget(self.answer)
        self.answer_button = QPushButton("Cevabı onayla ve devam et")
        answer_row = QHBoxLayout()
        answer_row.addStretch()
        answer_row.addWidget(self.answer_button)
        q_lay.addLayout(answer_row)
        right_lay.addWidget(self.question_box)
        self.refresh_button = QPushButton("Durumu yenile")
        self.start_button = QPushButton("Taslağı onayla ve başlat")
        self.start_button.setObjectName("primary")
        actions = QHBoxLayout()
        actions.setSpacing(8)
        actions.addStretch()
        actions.addWidget(self.refresh_button)
        actions.addWidget(self.start_button)
        right_lay.addLayout(actions)
        self.start_reason = AutoHideLabel()
        self.start_reason.setAlignment(Qt.AlignmentFlag.AlignRight)
        right_lay.addWidget(self.start_reason)
        # Yıkıcı eylem birincil eylemden ayrı satırda ve yalnız iş seçiliyken görünür.
        self.cancel_button = QPushButton("İşi durdur")
        self.cancel_button.setObjectName("danger")
        cancel_row = QHBoxLayout()
        cancel_row.addWidget(self.cancel_button)
        cancel_row.addStretch()
        right_lay.addSpacing(8)
        right_lay.addLayout(cancel_row)
        self.notice = make_label(
            "Demo modunda proje süreçleri kapalı." if demo else "Durum bekleniyor.", "meta"
        )
        right_lay.addWidget(self.notice)

        self._columns = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        self._columns.setSpacing(16)
        self._columns.addWidget(left, 5)
        self._columns.addWidget(right, 6)
        layout.addLayout(self._columns, 1)
        self.prepare_button.clicked.connect(self._prepare)
        self.start_button.clicked.connect(self._start)
        self.refresh_button.clicked.connect(self.refresh)
        self.answer_button.clicked.connect(self._answer)
        self.cancel_button.clicked.connect(self._cancel)
        self._sync_list()
        self.set_busy(True)

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802 (Qt adı)
        super().resizeEvent(event)
        wide = event.size().width() >= self.WIDE_BREAKPOINT
        self._columns.setDirection(
            QBoxLayout.Direction.LeftToRight if wide else QBoxLayout.Direction.TopToBottom
        )

    def _sync_list(self) -> None:
        """Boş liste yerine açıklayıcı durum göster; liste yüksekliği satır sayısına uyar."""
        count = self.items.count()
        self.list_stack.setCurrentIndex(0 if count else 1)
        if count:
            # Tek satır için koca kutu yerine en az 2, en çok 6 satır; fazlası kaydırılır.
            row = max(self.items.sizeHintForRow(0), 36)
            rows = min(max(count, LIST_MIN_ROWS), LIST_MAX_ROWS)
            self.list_stack.setFixedHeight(rows * row + 12)
        else:
            self.list_stack.setFixedHeight(120)

    def _update_flow(self, kind: str, record: dict[str, Any]) -> None:
        """Hazırla → İncele → Başlat: taslak yoksa 0, taslak seçiliyse 1, iş seçiliyse 2."""
        current = 0 if not kind else 1 if kind == "draft" else 2
        finished = kind == "job" and record.get("status") == "done"
        for i, label in enumerate(self._flow_labels):
            state = "done" if finished or i < current else "current" if i == current else "todo"
            label.setText(("✓ " if state == "done" else "") + FLOW_STEPS[i])
            if label.property("state") != state:
                label.setProperty("state", state)
                repolish(label)

    def _update_chip(self, kind: str, record: dict[str, Any]) -> None:
        if not kind:
            self.status_chip.setVisible(False)
            return
        text, tone = (
            ("Taslak", "neutral")
            if kind == "draft"
            else STATUS_CHIPS.get(record.get("status", ""), ("Bilinmiyor", "neutral"))
        )
        self.status_chip.setText(text)
        if self.status_chip.property("tone") != tone:
            self.status_chip.setProperty("tone", tone)
            repolish(self.status_chip)
        self.status_chip.setVisible(True)

    def _set_prepare_primary(self, primary: bool) -> None:
        name = "primary" if primary else ""
        if self.prepare_button.objectName() != name:
            self.prepare_button.setObjectName(name)
            repolish(self.prepare_button)

    def refresh(self) -> None:
        if not self._busy and not self._demo:
            self.tool_requested.emit("code_agent_status", StatusArguments())

    def _prepare_block(self) -> str:
        """Taslak düğmesi pasifse kullanıcıya gösterilecek tek satır neden ('' = açık)."""
        if self._demo:
            return "Demo modunda taslak hazırlanamaz."
        if self._busy:
            return "Artemis meşgul; hazır olunca açılır."
        if not self.project.currentText():
            return "Önce bir proje tanımla."
        if not self.task.toPlainText().strip():
            return "Önce görev yaz."
        if not self.scope.toPlainText().strip():
            return "Kapsamı yaz (her satır bir madde)."
        if not self.acceptance.toPlainText().strip():
            return "Kabul ölçütünü yaz (her satır bir madde)."
        return ""

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        enabled = not busy and not self._demo
        prepare_reason = self._prepare_block()
        self.prepare_button.setEnabled(not prepare_reason)
        self.prepare_reason.setText(prepare_reason)
        self.refresh_button.setEnabled(enabled)
        selected = self._selected()
        kind, record = selected if selected else ("", {})
        self.start_button.setEnabled(enabled and kind == "draft")
        if self._demo:
            start_reason = "Demo modunda proje süreçleri kapalı."
        elif busy:
            start_reason = "Artemis meşgul; hazır olunca açılır."
        elif kind == "job":
            start_reason = "Bu bir iş; yeni iş için bir taslak seç."
        elif kind != "draft":
            start_reason = "Önce bir taslak hazırla veya listeden seç."
        else:
            start_reason = ""
        self.start_reason.setText(start_reason)
        self._set_prepare_primary(kind != "draft")
        self.question_box.setVisible(record.get("status") == "waiting")
        self.answer_button.setEnabled(enabled and record.get("status") == "waiting")
        self.cancel_button.setVisible(kind == "job")
        self.cancel_button.setEnabled(
            enabled
            and record.get("status")
            in {
                "queued",
                "running",
                "waiting",
                "cancelling",
            }
        )
        self._update_flow(kind, record)
        self._update_chip(kind, record)

    def _selected(self) -> tuple[str, dict[str, Any]] | None:
        row = self.items.currentRow()
        return self._items[row] if 0 <= row < len(self._items) else None

    def _select(self, row: int) -> None:
        selected = self._selected()
        if selected:
            self.preview.setHtml(spec_to_html(*selected))
        else:
            self.preview.clear()
        self.set_busy(self._busy)

    def accept_result(self, tool_name: str, result: ToolResult) -> None:
        self.notice.setText(result.message)
        if tool_name == "code_agent_status" and result.success:
            selected = self._selected()
            identity = (
                None if selected is None else selected[1].get("job_id", selected[1].get("spec_id"))
            )
            self.project.blockSignals(True)
            current_project = self.project.currentText()
            projects = result.data.get("projects", [])
            if projects != [self.project.itemText(i) for i in range(self.project.count())]:
                self.project.clear()
                self.project.addItems(projects)
                self.project.setCurrentText(current_project)
            self.project.blockSignals(False)
            self.items.blockSignals(True)
            self.items.clear()
            self._items = [("draft", item) for item in result.data.get("drafts", [])]
            self._items += [("job", item) for item in result.data.get("jobs", [])]
            row = 0
            for i, (kind, item) in enumerate(self._items):
                self.items.addItem(
                    f"{item['project']} · "
                    + (
                        "Taslak"
                        if kind == "draft"
                        else STATUS_LABELS.get(item.get("status", ""), "Bilinmiyor")
                    )
                )
                if item.get("job_id", item.get("spec_id")) == identity:
                    row = i
            self.items.setCurrentRow(row if self._items else -1)
            self.items.blockSignals(False)
            self._sync_list()
            self._select(row)
        elif tool_name == "code_agent_prepare" and result.success:
            spec = result.data["spec"]
            self._items.append(("draft", spec))
            self.items.addItem(spec["project"] + " · Taslak")
            self.items.setCurrentRow(len(self._items) - 1)
            self._sync_list()

    def _prepare(self) -> None:
        try:
            arguments = PrepareArguments(
                project=self.project.currentText(),
                task=self.task.toPlainText(),
                scope=tuple(self.scope.toPlainText().splitlines()),
                acceptance=tuple(self.acceptance.toPlainText().splitlines()),
            )
        except ValidationError:
            self.notice.setText("Proje, görev, kapsam ve kabul ölçütleri gerekli.")
            return
        self.tool_requested.emit("code_agent_prepare", arguments)

    def _start(self) -> None:
        selected = self._selected()
        if selected and selected[0] == "draft":
            self.tool_requested.emit(
                "code_agent_start", StartArguments(spec=ProjectSpec.model_validate(selected[1]))
            )

    def _answer(self) -> None:
        selected = self._selected()
        if selected and selected[1].get("status") == "waiting":
            record = selected[1]
            try:
                arguments = AnswerArguments(
                    job_id=record["job_id"],
                    question_id=record["question_id"],
                    answer=self.answer.toPlainText(),
                )
            except ValidationError:
                self.notice.setText("Cevap 1-4000 karakter olmalı.")
                return
            self.tool_requested.emit("code_agent_answer", arguments)

    def _cancel(self) -> None:
        selected = self._selected()
        if selected and selected[0] == "job":
            self.tool_requested.emit(
                "code_agent_cancel", CancelArguments(job_id=selected[1]["job_id"])
            )
