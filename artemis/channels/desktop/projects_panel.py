"""Projeler düz metin sunumudur; eylem sahipliği CommandRunner'da kalır."""

from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPlainTextEdit,
    QPushButton,
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


class ProjectsPanel(QWidget):
    tool_requested = Signal(str, object)

    def __init__(self, *, demo: bool = False) -> None:
        super().__init__()
        self._demo = demo
        self._items: list[tuple[str, dict[str, Any]]] = []
        self._busy = True
        layout = QVBoxLayout(self)
        hint = QLabel(
            "Taslağı hazırlayın, tam kapsamı inceleyin ve onayla başlatın. " + AUTHORITY_BOUNDARY
        )
        hint.setTextFormat(Qt.TextFormat.PlainText)
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.project = QComboBox()
        self.task = QPlainTextEdit()
        self.task.setPlaceholderText("Görev: ne yapılacak?")
        self.task.setMaximumHeight(75)
        self.scope = QPlainTextEdit()
        self.scope.setPlaceholderText("Kapsam: her satır bir madde")
        self.scope.setMaximumHeight(65)
        self.acceptance = QPlainTextEdit()
        self.acceptance.setPlaceholderText("Kabul ölçütleri: her satır bir madde")
        self.acceptance.setMaximumHeight(65)
        for widget in (self.project, self.task, self.scope, self.acceptance):
            layout.addWidget(widget)
        controls = QHBoxLayout()
        self.prepare_button = QPushButton("Taslak hazırla")
        self.start_button = QPushButton("Taslağı onayla ve başlat")
        self.refresh_button = QPushButton("Durumu yenile")
        for button in (self.prepare_button, self.start_button, self.refresh_button):
            controls.addWidget(button)
        layout.addLayout(controls)
        self.items = QListWidget()
        self.items.setMaximumHeight(140)
        self.items.currentRowChanged.connect(self._select)
        layout.addWidget(self.items)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        layout.addWidget(self.preview, 1)
        self.answer = QPlainTextEdit()
        self.answer.setPlaceholderText("Bekleyen soruya cevap")
        self.answer.setMaximumHeight(60)
        layout.addWidget(self.answer)
        answer_controls = QHBoxLayout()
        self.answer_button = QPushButton("Cevabı onayla ve devam et")
        self.cancel_button = QPushButton("İşi durdur")
        answer_controls.addWidget(self.answer_button)
        answer_controls.addWidget(self.cancel_button)
        layout.addLayout(answer_controls)
        self.notice = QLabel(
            "Demo modunda proje süreçleri kapalı." if demo else "Durum bekleniyor."
        )
        self.notice.setTextFormat(Qt.TextFormat.PlainText)
        self.notice.setWordWrap(True)
        layout.addWidget(self.notice)
        self.prepare_button.clicked.connect(self._prepare)
        self.start_button.clicked.connect(self._start)
        self.refresh_button.clicked.connect(self.refresh)
        self.answer_button.clicked.connect(self._answer)
        self.cancel_button.clicked.connect(self._cancel)
        self.set_busy(True)

    def refresh(self) -> None:
        if not self._busy and not self._demo:
            self.tool_requested.emit("code_agent_status", StatusArguments())

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        enabled = not busy and not self._demo
        self.prepare_button.setEnabled(enabled and bool(self.project.currentText()))
        self.refresh_button.setEnabled(enabled)
        selected = self._selected()
        kind, record = selected if selected else ("", {})
        self.start_button.setEnabled(enabled and kind == "draft")
        self.answer_button.setEnabled(enabled and record.get("status") == "waiting")
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

    def _selected(self) -> tuple[str, dict[str, Any]] | None:
        row = self.items.currentRow()
        return self._items[row] if 0 <= row < len(self._items) else None

    def _select(self, row: int) -> None:
        selected = self._selected()
        if selected:
            kind, record = selected
            spec = record if kind == "draft" else record.get("spec", {})
            lines = [
                "Proje: " + str(spec.get("project", "")),
                "Görev: " + str(spec.get("task", "")),
                "Kapsam:",
                *["• " + item for item in spec.get("scope", [])],
                "Kabul ölçütleri:",
                *["• " + item for item in spec.get("acceptance", [])],
            ]
            if kind == "job":
                lines += [
                    "Durum: " + STATUS_LABELS.get(record.get("status", ""), "Bilinmiyor"),
                    "Özet: " + str(record.get("summary", "")),
                    "Soru: " + str(record.get("question") or "—"),
                    "Commit'ler: " + json.dumps(record.get("commits", []), ensure_ascii=False),
                    "Kalan değişiklikler: " + str(record.get("leftover_changes")),
                    "İş kimliği: " + str(record.get("job_id", "")),
                ]
            self.preview.setPlainText("\n".join(lines))
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
            self._select(row)
        elif tool_name == "code_agent_prepare" and result.success:
            spec = result.data["spec"]
            self._items.append(("draft", spec))
            self.items.addItem(spec["project"] + " · Taslak")
            self.items.setCurrentRow(len(self._items) - 1)

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
