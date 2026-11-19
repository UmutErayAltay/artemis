"""Ayarlar > Güncelleme bölümü (ADR 0075).

Kullanıcı isteğiyle GitHub release sürümünü denetler; arka plan iş parçacığı
arayüzü kilitlemez. Otomatik indirme yapmaz.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import Qt, QThread, QUrl, Signal
from PySide6.QtGui import QCloseEvent, QDesktopServices
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from artemis.channels.desktop.thread_shutdown import DETACHED, shutdown_thread
from artemis.i18n import t
from artemis.update_check import UpdateResult, check_latest, current_version

_SHUTDOWN_WAIT_MS = 6000
# Süresi dolunca ayrılan iş parçacığı bitene dek GC tarafından toplanmasın diye tutulur.
_DETACHED = DETACHED


def _shutdown_thread(thread: QThread | None, *connections: tuple[str, Any]) -> bool:
    # Bekleme süresi modülde kalır (testler kısaltır); gövde ortak modülde.
    return shutdown_thread(thread, *connections, wait_ms=_SHUTDOWN_WAIT_MS)


class UpdateCheckThread(QThread):
    done = Signal(object)

    def __init__(
        self,
        check: Callable[[], UpdateResult] = check_latest,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._check = check

    def run(self) -> None:
        result = self._check()
        self.done.emit(result)


class UpdateSection(QWidget):
    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        check: Callable[[], UpdateResult] = check_latest,
    ) -> None:
        super().__init__(parent)
        self._check = check
        self._thread: UpdateCheckThread | None = None
        self._release_url = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self.version_label = QLabel(
            t("Kurulu sürüm: {version}").format(version=current_version()), self
        )
        layout.addWidget(self.version_label)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        self.check_button = QPushButton(t("Güncellemeyi denetle"), self)
        self.check_button.setToolTip(t("En son sürümü GitHub üzerinden denetler."))
        self.check_button.clicked.connect(self._on_check_clicked)
        btn_row.addWidget(self.check_button)

        self.open_button = QPushButton(t("Sürüm sayfasını aç"), self)
        self.open_button.setToolTip(t("En son sürüm sayfasını tarayıcıda açar."))
        self.open_button.setVisible(False)
        self.open_button.setEnabled(False)
        self.open_button.clicked.connect(self._on_open_clicked)
        btn_row.addWidget(self.open_button)

        btn_row.addStretch(1)
        layout.addLayout(btn_row)

        self.result_label = QLabel("", self)
        self.result_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.result_label.setWordWrap(True)
        layout.addWidget(self.result_label)

        self.url_label = QLabel("", self)
        self.url_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.url_label.setWordWrap(True)
        self.url_label.setVisible(False)
        layout.addWidget(self.url_label)

    def _on_check_clicked(self) -> None:
        if self._thread is not None and self._thread.isRunning():
            return
        self.check_button.setEnabled(False)
        self.result_label.setText(t("Güncelleme denetleniyor…"))
        self.url_label.setVisible(False)
        self.open_button.setVisible(False)
        self.open_button.setEnabled(False)

        self._thread = UpdateCheckThread(self._check, self)
        self._thread.done.connect(self._on_done)
        self._thread.finished.connect(self._on_thread_finished)
        self._thread.start()

    def _on_done(self, result: UpdateResult) -> None:
        self.check_button.setEnabled(True)
        self.result_label.setText(result.message)
        if result.status == "newer" and result.url:
            self._release_url = result.url
            self.url_label.setText(result.url)
            self.url_label.setVisible(True)
            self.open_button.setVisible(True)
            self.open_button.setEnabled(True)
        else:
            self._release_url = ""
            self.url_label.setText("")
            self.url_label.setVisible(False)
            self.open_button.setVisible(False)
            self.open_button.setEnabled(False)

    def _on_thread_finished(self) -> None:
        self.check_button.setEnabled(True)
        self._thread = None

    def _on_open_clicked(self) -> None:
        if self._release_url:
            QDesktopServices.openUrl(QUrl(self._release_url))

    def finish(self) -> None:
        if _shutdown_thread(
            self._thread,
            ("done", self._on_done),
            ("finished", self._on_thread_finished),
        ):
            self._thread = None

    def closeEvent(self, event: QCloseEvent) -> None:
        self.finish()
        super().closeEvent(event)
