"""Öğretilebilir makro rutin kaydedici penceresi (ADR 0087).

Kullanıcı gözlemi başlatıp uygulama odak/tıklama olaylarını toplayabilir ya da doğal dil
adımları yazabilir; parola ve engelli pencereler otomatik gizlenir/atlanır. Kaydet düğmesi
yalnız `RoutineSaveArguments` üretir ve `RoutinesSection.tool_requested` üzerinden
`SafetyGate.dispatch` + `ApprovalDialog` onayına gider; dosyaya doğrudan yazmaz.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError
from PySide6.QtCore import QThread, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPlainTextEdit,
    QPushButton,
    QWidget,
)

from artemis.channels.desktop.dialog_shell import FormDialog
from artemis.channels.desktop.form_kit import meta_label
from artemis.channels.desktop.thread_shutdown import shutdown_thread
from artemis.channels.desktop.tool_titles import tool_display
from artemis.channels.desktop.widgets import with_help
from artemis.config.settings import Settings
from artemis.i18n import t
from artemis.proactive.macro_recorder import (
    MacroDraft,
    MacroObserverBackend,
    MacroRecorder,
    ObservedEvent,
    sanitize_macro_text,
)
from artemis.proactive.routines import MAX_ROUTINES, MAX_STEPS, Routine, RoutineStep, name_key
from artemis.tools.plugins.routines import RoutineSaveArguments
from artemis.tools.registry import ToolRegistry

_POLL_INTERVAL_MS = 250
_NAME_PROBLEM = "Ad 1-40 karakter olmalı: harf, rakam, boşluk, _ ya da -."


def _format_step_preview(number: int, step: RoutineStep) -> str:
    args_text = json.dumps(step.arguments, ensure_ascii=False)
    safe_args, _ = sanitize_macro_text(args_text)
    if step.arguments:
        return t("{number}. {tool} — {args}").format(
            number=number, tool=tool_display(step.tool), args=safe_args
        )
    return t("{number}. {tool}").format(number=number, tool=tool_display(step.tool))


class _MacroObserverThread(QThread):
    """UIA/fare makro gözlemini Qt ana iş parçacığı dışında yürütür."""

    polled = Signal(object)

    def __init__(
        self,
        recorder: MacroRecorder,
        interval_ms: int,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._recorder = recorder
        self._interval_s = max(0.01, interval_ms / 1000.0)
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()
        self.requestInterruption()

    def run(self) -> None:
        while not self._stop_event.wait(self._interval_s):
            if self.isInterruptionRequested() or not self._recorder.recording:
                break
            event = self._recorder.poll_once()
            if not self.isInterruptionRequested():
                self.polled.emit(event)


class MacroDialog(FormDialog):
    """Doğal dil + UIA gözlemiyle makro taslağı oluşturma ve onaylı kayıt penceresi."""

    def __init__(
        self,
        others: list[Routine],
        registry: ToolRegistry,
        settings: Settings | None = None,
        *,
        backend: MacroObserverBackend | None = None,
        services: Mapping[str, Any] | None = None,
        poll_interval_ms: int = _POLL_INTERVAL_MS,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(t("Makro kaydet"), 540, parent)
        self.setWindowTitle(t("Makro kaydet"))
        self._others = others
        self._registry = registry
        self._settings = settings
        self._recorder = MacroRecorder(backend=backend, services=services)
        self._poll_interval_ms = poll_interval_ms
        self._observer_thread: _MacroObserverThread | None = None
        self._latest_draft: MacroDraft | None = None
        self.result_args: RoutineSaveArguments | None = None
        self.result_draft: MacroDraft | None = None

        name_label = QLabel(t("Rutin adı"))
        name_label.setObjectName("fieldLabel")
        self.name = QLineEdit()
        self.name.setMaxLength(40)
        self.name.setMinimumWidth(280)
        self.name.setPlaceholderText(t("örn. sabah-rutini"))
        self.name.setAccessibleName(t("Rutin adı"))
        self.form.addWidget(name_label)
        self.form.addWidget(self.name)

        obs_label = QLabel(t("Etkileşim gözlemi"))
        obs_label.setObjectName("fieldLabel")
        self.form.addLayout(
            with_help(
                obs_label,
                t(
                    "Açıkken uygulama geçişleri ve tıklamalar okunur; parola alanları, terminaller "
                    "ve parola yöneticileri otomatik atlanır. En çok {max_steps} adım, kayıt ve "
                    "çalıştırma güvenlik kapısı onayından geçer."
                ).format(max_steps=MAX_STEPS),
            )
        )
        ctrl_row = QHBoxLayout()
        ctrl_row.setSpacing(8)
        self.record_button = QPushButton(t("Gözlemi başlat"))
        self.clear_button = QPushButton(t("Temizle"))
        ctrl_row.addWidget(self.record_button)
        ctrl_row.addWidget(self.clear_button)
        ctrl_row.addStretch(1)
        self.form.addLayout(ctrl_row)

        self.record_status = meta_label(
            t("Hazır — gözlemi başlatabilir ya da doğal dil adımları yazabilirsiniz.")
        )
        self.form.addWidget(self.record_status)

        self.privacy_badge = meta_label("")
        self.privacy_badge.setObjectName("approvalNotice")
        self.privacy_badge.hide()
        self.form.addWidget(self.privacy_badge)

        nl_label = QLabel(t("Doğal dil adımları (satır başına bir adım)"))
        nl_label.setObjectName("fieldLabel")
        self.form.addWidget(nl_label)
        self.nl_input = QPlainTextEdit()
        self.nl_input.setAccessibleName(t("Doğal dil adımları"))
        self.nl_input.setPlaceholderText(
            t("Örn.\nNot Defteri aç\nhttps://example.com aç\nBelgeye merhaba yaz")
        )
        self.nl_input.setMinimumHeight(72)
        self.nl_input.setMaximumHeight(110)
        self.form.addWidget(self.nl_input)

        steps_head = QHBoxLayout()
        steps_label = QLabel(t("Derlenen adımlar"))
        steps_label.setObjectName("fieldLabel")
        self.steps_summary = meta_label(t("0/{max_steps} adım").format(max_steps=MAX_STEPS))
        steps_head.addWidget(steps_label)
        steps_head.addStretch(1)
        steps_head.addWidget(self.steps_summary)
        self.form.addLayout(steps_head)

        self.steps_list = QListWidget()
        self.steps_list.setAccessibleName(t("Derlenen adımlar"))
        self.steps_list.setMinimumHeight(90)
        self.steps_list.setMaximumHeight(150)
        self.form.addWidget(self.steps_list)

        self.warning_label = meta_label("")
        self.warning_label.hide()
        self.form.addWidget(self.warning_label)
        self.form.addStretch(1)

        self.error_label = QLabel("")
        self.error_label.setObjectName("warnNote")
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        self._outer.addWidget(self.error_label)

        self.add_buttons(t("Kaydet"))
        self.save_button = self.ok_button

        self.record_button.clicked.connect(self._toggle_recording)
        self.clear_button.clicked.connect(self._clear_all)
        self.name.textChanged.connect(self._refresh_preview)
        self.nl_input.textChanged.connect(self._refresh_preview)
        self._refresh_preview()

    @property
    def recorder(self) -> MacroRecorder:
        return self._recorder

    def _toggle_recording(self) -> None:
        if self._recorder.recording:
            self.stop_recording()
        else:
            self.start_recording()

    def _stop_observer_thread(self) -> None:
        thread = self._observer_thread
        self._observer_thread = None
        if thread is not None:
            thread.stop()
            shutdown_thread(thread, ("polled", self._on_polled), wait_ms=3000)

    def _on_polled(self, _event: object) -> None:
        if self._recorder.recording:
            self.record_status.setText(
                t("Gözleniyor… {count} olay yakalandı.").format(
                    count=len(self._recorder.events)
                )
            )
        self._refresh_preview()

    def start_recording(self) -> None:
        self._stop_observer_thread()
        self._recorder.start()
        thread = _MacroObserverThread(self._recorder, self._poll_interval_ms, self)
        thread.polled.connect(self._on_polled)
        self._observer_thread = thread
        thread.start()
        self.record_button.setText(t("Gözlemi durdur"))
        self.record_status.setText(
            t("Gözleniyor… Başka bir uygulamaya geçip tıklayın, bitince durdurun.")
        )

    def stop_recording(self) -> None:
        self._stop_observer_thread()
        self._recorder.stop()
        self.record_button.setText(t("Gözlemi başlat"))
        self.record_status.setText(
            t("Durduruldu · {count} olay yakalandı.").format(count=len(self._recorder.events))
        )
        self._refresh_preview()

    def poll_once(self) -> ObservedEvent | None:
        event = self._recorder.poll_once()
        self._on_polled(event)
        return event

    def _clear_all(self) -> None:
        self._recorder.clear()
        self.nl_input.clear()
        self.record_status.setText(
            t("Temizlendi — gözlemi başlatabilir ya da doğal dil adımları yazabilirsiniz.")
        )
        self._refresh_preview()

    def _validate_name(self, raw_name: str) -> str:
        cleaned = " ".join(raw_name.split())
        try:
            Routine(name=cleaned, steps=[RoutineStep(tool="x")])
        except ValidationError:
            return t(_NAME_PROBLEM)
        if name_key(cleaned) in {name_key(item.name) for item in self._others}:
            return t("Bu ad zaten kullanılıyor.")
        if len(self._others) >= MAX_ROUTINES:
            return t("En çok {max_routines} rutin olabilir.").format(max_routines=MAX_ROUTINES)
        return ""

    def _refresh_preview(self, *_args: Any) -> None:
        raw_name = self.name.text().strip()
        draft = self._recorder.compile_draft(
            raw_name or "makro",
            self._registry,
            self._settings,
            nl_text=self.nl_input.toPlainText(),
        )
        self._latest_draft = draft

        self.steps_list.clear()
        for idx, step in enumerate(draft.steps, start=1):
            self.steps_list.addItem(_format_step_preview(idx, step))
        self.steps_summary.setText(
            t("{count}/{max_steps} adım").format(count=len(draft.steps), max_steps=MAX_STEPS)
        )

        if draft.masked_count > 0 or draft.skipped_blocked_count > 0:
            self.privacy_badge.setText(
                t(
                    "Gizlilik koruması: {masked} hassas alan gizlendi, "
                    "{skipped} engelli pencere atlandı."
                ).format(masked=draft.masked_count, skipped=draft.skipped_blocked_count)
            )
            self.privacy_badge.show()
        else:
            self.privacy_badge.hide()

        if draft.warnings:
            self.warning_label.setText(" · ".join(draft.warnings))
            self.warning_label.show()
        else:
            self.warning_label.hide()

        name_err = self._validate_name(raw_name)
        problem = name_err or draft.error
        self.error_label.setText(problem)
        self.error_label.setVisible(bool(problem))
        self.save_button.setEnabled(not problem and bool(draft.steps))

    def submit(self) -> None:
        if self._recorder.recording:
            self.stop_recording()
        self._refresh_preview()
        raw_name = self.name.text().strip()
        name_err = self._validate_name(raw_name)
        draft = self._latest_draft
        if name_err or draft is None or draft.error or not draft.steps:
            fallback = t("En az bir geçerli makro adımı gerekli.")
            problem = name_err or (draft.error if draft else fallback)
            self.error_label.setText(problem)
            self.error_label.setVisible(True)
            return
        clean_name = " ".join(raw_name.split())
        self.result_args = RoutineSaveArguments(name=clean_name, steps=list(draft.steps))
        self.result_draft = draft.model_copy(update={"name": clean_name})
        self.accept()

    def reject(self) -> None:
        self._stop_observer_thread()
        self._recorder.stop()
        super().reject()

    def closeEvent(self, event: QCloseEvent) -> None:
        self._stop_observer_thread()
        self._recorder.stop()
        super().closeEvent(event)
