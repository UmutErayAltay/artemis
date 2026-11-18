"""Ayarlar > Dosya araması: kök listesi, indeks durumu, yeniden dizinle, dizini sil (ADR 0065).

Klasör listesi PANEL_BINDINGS ile otomatik kayıtla yazılır; kökleri hiçbir araç değiştirmez.
Düğmeler kayıtlı ayarı kullanır. `FileIndex.shared` runtime'la aynı örneği verir: tek kilit,
tek iptal bayrağı, tek iş parçacığı. "Dizini sil" bekleyip dosyayı sildiği için ayrı bir
QThread'de çalışır (arayüz donmaz).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QFileDialog, QListWidget, QPushButton, QVBoxLayout, QWidget

from artemis.channels.desktop.form_kit import button_row, group, meta_label
from artemis.channels.desktop.settings_lists import ListWithEmpty
from artemis.channels.desktop.thread_shutdown import DETACHED, shutdown_thread
from artemis.config.settings import Settings, load_settings
from artemis.i18n import t
from artemis.safety.audit import AuditLog
from artemis.tools.file_index import FileIndex, usable_roots

_SHUTDOWN_WAIT_MS = 6000
# Süresi dolunca ayrılan iş parçacığı bitene dek GC tarafından toplanmasın diye tutulur.
_DETACHED = DETACHED


def _shutdown_thread(thread: QThread | None, *connections: tuple[str, Any]) -> bool:
    # Bekleme süresi modülde kalır (testler kısaltır); gövde ortak modülde.
    return shutdown_thread(thread, *connections, wait_ms=_SHUTDOWN_WAIT_MS)


class _ClearJob(QThread):
    done = Signal(str)

    def __init__(self, index: FileIndex, parent: QWidget) -> None:
        super().__init__(parent)
        self._index = index

    def run(self) -> None:
        self.done.emit(self._index.clear())


class FileSearchSection(QWidget):
    def __init__(self, config_path: Path, *, demo: bool = False) -> None:
        super().__init__()
        self._config_path = config_path
        self._demo = demo
        self._index: FileIndex | None = None
        self._clearing: _ClearJob | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(24)
        roots_box, roots = group(t("Aranacak klasörler"), divided=False)
        layout.addWidget(roots_box)
        self.roots_list = QListWidget()
        self.roots_list.setAccessibleName(t("Aranacak klasörler"))
        self.roots_area = ListWithEmpty(
            self.roots_list, t("Henüz klasör eklenmedi"), t("Klasör ekle ile başla."),
            height=144,
        )
        roots.addWidget(self.roots_area)
        # Liste Uygula yenilemesinde, ekle/çıkar düğmelerinde ve testlerde değişir; boş durum
        # modelin kendi sinyallerini izler.
        model = self.roots_list.model()
        for signal in (model.rowsInserted, model.rowsRemoved, model.modelReset):
            signal.connect(self._sync_roots)
        self._sync_roots()
        self.add_button = QPushButton(t("Klasör ekle"))
        self.add_button.setObjectName("primary")
        self.remove_button = QPushButton(t("Listeden çıkar"))
        roots.addLayout(button_row(self.remove_button, self.add_button))
        index_box, index = group(t("İndeks"))
        layout.addWidget(index_box)
        self.status_label = meta_label("")
        index.addWidget(self.status_label)
        self.reindex_button = QPushButton(t("Yeniden dizinle"))
        self.clear_button = QPushButton(t("Dizini sil"))
        index.addLayout(button_row(self.clear_button, self.reindex_button))
        index.addWidget(meta_label(
            t("İndeks dosya içeriğinden alınan kişisel metni yalnız Artemis'in "
              "veri klasöründe tutar. Gizli görünen dosyalar (.env, anahtar, "
              "parola adları) ve bağlantılar dizinlenmez.")
        ))
        self.notice = meta_label("")
        layout.addWidget(self.notice)
        self.add_button.clicked.connect(self._add)
        self.remove_button.clicked.connect(self._remove)
        self.reindex_button.clicked.connect(self._reindex)
        self.clear_button.clicked.connect(self._clear)
        self._timer = QTimer(self)  # dizinleme sürerken durumu tazeler
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._show_status)
        for button in (self.add_button, self.remove_button, self.reindex_button, self.clear_button):
            button.setEnabled(not demo)
        if not demo:
            self.refresh()

    def _sync_roots(self, *_args: object) -> None:
        if self.roots_list.count():
            self.roots_area.show_list()
        else:
            self.roots_area.show_empty(
                t("Henüz klasör eklenmedi"), t("Klasör ekle ile başla."),
            )

    def _settings(self) -> Settings | None:
        try:
            return load_settings(self._config_path)
        except (OSError, ValueError):
            self.notice.setText(t("Ayar dosyası okunamadı."))
            return None

    def refresh(self) -> None:
        self.notice.setText("")
        settings = self._settings()
        self._index = None
        if settings is not None:
            self._index = FileIndex.shared(settings, AuditLog(settings.audit_path))
            _, problems = usable_roots(settings)
            if problems:
                self.notice.setText(problems[0])
        self._show_status()

    def _show_status(self) -> None:
        index = self._index
        if index is None:
            self.status_label.setText("")
            return
        if not index.available:
            self.status_label.setText(
                t("Bu SQLite sürümünde FTS5 yok; dosya araması kullanılamaz.")
            )
            return
        info = index.status()
        last_run = info["last_run"] or t("henüz yok")
        text = t("{files} dosya · son dizinleme: {last_run}").format(
            files=info["files"], last_run=last_run,
        )
        if info["running"]:
            text += " · " + t("dizinleniyor")
            self._timer.start()
        else:
            self._timer.stop()
        if info["note"]:
            text += f" · {info['note']}"
        self.status_label.setText(text)

    def _add(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, t("Aranacak klasörü seç"))
        if folder:
            path = str(Path(folder))
            if not self.roots_list.findItems(path, Qt.MatchFlag.MatchExactly):
                self.roots_list.addItem(path)

    def _remove(self) -> None:
        for item in self.roots_list.selectedItems():
            self.roots_list.takeItem(self.roots_list.row(item))

    def _reindex(self) -> None:
        settings = self._settings()
        if settings is None or self._index is None:
            return
        if not settings.file_search.enabled:
            self.notice.setText(
                t("Dosya araması kapalı. Önce Dosya aramasını kullan kutusunu aç.")
            )
        elif self._index.start():
            self.notice.setText(t("Dizinleme başladı."))
        else:
            self.notice.setText(t("Dizinleme zaten sürüyor."))
        self._show_status()

    def _clear(self) -> None:
        if self._index is None or self._clearing is not None:
            return
        self.notice.setText(t("İndeks siliniyor..."))
        self.clear_button.setEnabled(False)
        job = _ClearJob(self._index, self)
        job.done.connect(self._cleared)
        self._clearing = job
        job.start()

    def _cleared(self, message: str) -> None:
        self.notice.setText(message)
        if self._clearing is not None:
            self._clearing.wait()
            self._clearing = None
        self.clear_button.setEnabled(not self._demo)
        self._show_status()

    def finish(self) -> None:
        self._timer.stop()
        if _shutdown_thread(self._clearing, ("done", self._cleared)):
            self._clearing = None

    def closeEvent(self, event: QCloseEvent) -> None:
        self.finish()
        super().closeEvent(event)
