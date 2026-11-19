"""Ayarlar > Güncelleme bölümü (ADR 0075, ADR 0081).

Kullanıcı isteğiyle GitHub release sürümünü denetler; arka plan iş parçacığı
arayüzü kilitlemez. İndirme yalnızca kullanıcının açık tıklamasıyla başlar ve
GitHub API'sinden gelen SHA-256 özetiyle doğrulanır; henüz Authenticode imzası
yoktur (SignPath bekleniyor).
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Callable
from pathlib import Path
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
from artemis.update_check import (
    UpdateDownloadError,
    UpdateResult,
    check_latest,
    current_version,
    download_installer,
)

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


class UpdateDownloadThread(QThread):
    progress = Signal(int, int)
    done = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        result: UpdateResult,
        download: Callable[..., Path],
        cancel: threading.Event,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._result = result
        self._download = download
        self._cancel = cancel

    def run(self) -> None:
        try:
            path = self._download(
                self._result,
                progress=self.progress.emit,
                cancelled=self._cancel.is_set,
            )
        except UpdateDownloadError as exc:
            self.failed.emit(str(exc))
            return
        self.done.emit(path)


class UpdateSection(QWidget):
    install_ready = Signal(object)

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        check: Callable[[], UpdateResult] = check_latest,
        download: Callable[..., Path] = download_installer,
        frozen: bool | None = None,
    ) -> None:
        super().__init__(parent)
        self._check = check
        self._download = download
        self._frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
        self._thread: UpdateCheckThread | None = None
        self._download_thread: UpdateDownloadThread | None = None
        self._cancel_event: threading.Event | None = None
        self._release_url = ""
        self._result: UpdateResult | None = None

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

        self.install_button = QPushButton(t("İndir ve kur"), self)
        self.install_button.setVisible(False)
        self.install_button.setEnabled(False)
        self.install_button.clicked.connect(self._on_install_clicked)
        btn_row.addWidget(self.install_button)

        self.cancel_button = QPushButton(t("İptal"), self)
        self.cancel_button.setVisible(False)
        self.cancel_button.clicked.connect(self._on_cancel_clicked)
        btn_row.addWidget(self.cancel_button)

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
        self.install_button.setVisible(False)
        self.install_button.setEnabled(False)
        self._result = None

        self._thread = UpdateCheckThread(self._check, self)
        self._thread.done.connect(self._on_done)
        self._thread.finished.connect(self._on_thread_finished)
        self._thread.start()

    def _on_done(self, result: UpdateResult) -> None:
        self.check_button.setEnabled(True)
        self.result_label.setText(result.message)
        self._result = result
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

        can_install = (
            result.status == "newer"
            and bool(result.installer_url)
            and bool(result.installer_sha256)
            and result.installer_size > 0
            and self._frozen
        )
        self.install_button.setVisible(can_install)
        self.install_button.setEnabled(can_install)

    def _on_thread_finished(self) -> None:
        self.check_button.setEnabled(True)
        self._thread = None

    def _on_open_clicked(self) -> None:
        if self._release_url:
            QDesktopServices.openUrl(QUrl(self._release_url))

    def _on_install_clicked(self) -> None:
        if self._result is None:
            return
        if self._download_thread is not None and self._download_thread.isRunning():
            return
        self.check_button.setEnabled(False)
        self.install_button.setEnabled(False)
        self.cancel_button.setVisible(True)
        self.cancel_button.setEnabled(True)
        self.result_label.setText(t("İndiriliyor…"))

        self._cancel_event = threading.Event()
        self._download_thread = UpdateDownloadThread(
            self._result, self._download, self._cancel_event, self
        )
        self._download_thread.progress.connect(self._on_download_progress)
        self._download_thread.done.connect(self._on_download_done)
        self._download_thread.failed.connect(self._on_download_failed)
        self._download_thread.finished.connect(self._on_download_finished)
        self._download_thread.start()

    def _on_cancel_clicked(self) -> None:
        if self._cancel_event is not None:
            self._cancel_event.set()
        self.cancel_button.setEnabled(False)

    def _on_download_progress(self, done: int, total: int) -> None:
        self.result_label.setText(
            t("İndiriliyor: {done} MB / {total} MB").format(
                done=f"{done / 1e6:.1f}",
                total=f"{total / 1e6:.1f}",
            )
        )

    def _on_download_done(self, path: Path) -> None:
        self.result_label.setText(
            t("İndirildi ve doğrulandı (SHA-256). Kurulum için Artemis kapanacak.")
        )
        self.cancel_button.setVisible(False)
        self.install_ready.emit(path)

    def _on_download_failed(self, msg: str) -> None:
        self.result_label.setText(t(msg))
        self.cancel_button.setVisible(False)
        self.check_button.setEnabled(True)
        self.install_button.setEnabled(True)

    def _on_download_finished(self) -> None:
        self.check_button.setEnabled(True)
        self.install_button.setEnabled(True)
        self._download_thread = None

    def finish(self) -> None:
        if self._cancel_event is not None:
            self._cancel_event.set()
        if _shutdown_thread(
            self._download_thread,
            ("progress", self._on_download_progress),
            ("done", self._on_download_done),
            ("failed", self._on_download_failed),
            ("finished", self._on_download_finished),
        ):
            self._download_thread = None
        if _shutdown_thread(
            self._thread,
            ("done", self._on_done),
            ("finished", self._on_thread_finished),
        ):
            self._thread = None

    def closeEvent(self, event: QCloseEvent) -> None:
        self.finish()
        super().closeEvent(event)
