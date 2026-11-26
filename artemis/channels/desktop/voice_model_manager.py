"""Ayarlar > Ses ve Hakkında: ses modeli sağlık yöneticisi bileşeni (ADR 0088)."""

from __future__ import annotations

import contextlib
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

import httpx
from PySide6.QtCore import QSize, Qt, QThread, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QFrame,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from artemis.channels.desktop.form_kit import button_row, meta_label
from artemis.channels.desktop.page_kit import PathHint
from artemis.channels.desktop.thread_shutdown import DETACHED, shutdown_thread, start_unparented
from artemis.channels.voice.model_health import (
    VoiceModelEntry,
    clean_voice_model,
    download_voice_model,
    format_size_bytes,
    inventory_voice_models,
    summarize_health,
    verify_voice_model,
)
from artemis.i18n import t

_DETACHED = DETACHED

InventoryFn = Callable[[Path], list[VoiceModelEntry]]
EntryOpFn = Callable[[str, Path], VoiceModelEntry]


def _status_badge(status: str) -> str:
    if status == "ok":
        return t("Hazır")
    if status == "corrupt":
        return t("Bozuk")
    return t("Eksik")


def _family_name(family: str) -> str:
    if family == "whisper":
        return "Whisper"
    if family == "piper":
        return "Piper"
    if family == "silero":
        return "Silero VAD"
    if family == "dfn3":
        return "DeepFilterNet3"
    return family


class _ModelOpThread(QThread):
    """Envanter, doğrulama, indirme ve temizleme işlerini GUI dışında yürütür."""

    completed = Signal(int, str, object, str)
    failed = Signal(int, str, str)

    def __init__(
        self,
        generation: int,
        op: str,
        work: Callable[[], tuple[list[VoiceModelEntry], str]],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.generation = generation
        self.op = op
        self._work = work

    def run(self) -> None:
        # BaseException dahil her yolda GUI bir sonuç almalı (durum etiketi takılı kalmasın).
        reported = False
        try:
            entries, message = self._work()
            if not self.isInterruptionRequested():
                self.completed.emit(self.generation, self.op, entries, message)
            reported = True
        except (OSError, ValueError, RuntimeError, ImportError, httpx.HTTPError) as error:
            reported = True
            self.failed.emit(
                self.generation, self.op, str(error) or type(error).__name__
            )
        except Exception as error:  # noqa: BLE001 - İşçi iş parçacığı sınırı: beklenmeyen tür de failed sinyaliyle GUI'ye bildirilir, durum etiketi takılı kalmaz.
            reported = True
            self.failed.emit(
                self.generation, self.op, str(error) or type(error).__name__
            )
        finally:
            if not reported:
                exc_type = sys.exc_info()[0]
                self.failed.emit(
                    self.generation, self.op, exc_type.__name__ if exc_type else "Interrupted"
                )


class VoiceModelManagerWidget(QFrame):
    """Whisper, Piper, Silero VAD ve DFN3 ses modellerini yöneten ortak kart."""

    models_changed = Signal(object)

    def __init__(
        self,
        config_path: Path,
        parent: QWidget | None = None,
        *,
        demo: bool = False,
        defer_load: bool = False,
        inventory_fn: InventoryFn | None = None,
        verify_fn: EntryOpFn | None = None,
        download_fn: EntryOpFn | None = None,
        clean_fn: EntryOpFn | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("card")
        self._config_path = Path(config_path)
        self._demo = demo
        self._inventory_fn: InventoryFn = inventory_fn or inventory_voice_models
        self._verify_fn: EntryOpFn = verify_fn or verify_voice_model
        self._download_fn: EntryOpFn = download_fn or download_voice_model
        self._clean_fn: EntryOpFn = clean_fn or clean_voice_model
        self._entries: list[VoiceModelEntry] = []
        self._thread: _ModelOpThread | None = None
        self._generation = 0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(8)

        self.title_label = QLabel(t("Ses modelleri"))
        self.title_label.setObjectName("sectionTitle")
        layout.addWidget(self.title_label)

        self.summary_label = meta_label("")
        self.summary_label.setAccessibleName(t("Ses modelleri özeti"))
        layout.addWidget(self.summary_label)

        self.refresh_button = QPushButton(t("Yenile"))
        self.refresh_button.setAccessibleName(t("Ses modellerini yenile"))
        self.refresh_button.setToolTip(
            t("Yerel diskteki Whisper, Piper, Silero VAD ve DFN3 dosyalarını yeniden tarar.")
        )
        self.refresh_button.clicked.connect(self.trigger_refresh)

        self.verify_all_button = QPushButton(t("Tümünü doğrula"))
        self.verify_all_button.setAccessibleName(t("Tüm ses modellerini doğrula"))
        self.verify_all_button.setToolTip(
            t("Kurulu modellerin SHA-256 ve bütünlük denetimini arka planda çalıştırır.")
        )
        self.verify_all_button.clicked.connect(self.trigger_verify_all)
        layout.addLayout(button_row(self.refresh_button, self.verify_all_button))

        self.model_list = QListWidget()
        self.model_list.setAccessibleName(t("Ses modelleri listesi"))
        self.model_list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.model_list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.model_list.setMinimumHeight(140)
        self.model_list.setMaximumHeight(190)
        self.model_list.currentRowChanged.connect(self._on_selection_changed)
        layout.addWidget(self.model_list)

        self.detail_label = meta_label("")
        self.detail_label.setAccessibleName(t("Seçili ses modeli ayrıntısı"))
        layout.addWidget(self.detail_label)

        self.path_label = PathHint()
        self.path_label.setAccessibleName(t("Seçili ses modeli yolu"))
        layout.addWidget(self.path_label)

        self.download_button = QPushButton(t("İndir"))
        self.download_button.setObjectName("primary")
        self.download_button.setAccessibleName(t("Seçili ses modelini indir"))
        self.download_button.clicked.connect(lambda: self.trigger_download())

        self.verify_button = QPushButton(t("Doğrula"))
        self.verify_button.setAccessibleName(t("Seçili ses modelini doğrula"))
        self.verify_button.clicked.connect(lambda: self.trigger_verify())

        self.clean_button = QPushButton(t("Temizle"))
        self.clean_button.setAccessibleName(t("Seçili ses modelini temizle"))
        self.clean_button.clicked.connect(lambda: self.trigger_clean())

        layout.addLayout(
            button_row(self.download_button, self.verify_button, self.clean_button)
        )

        self.status_label = meta_label("")
        self.status_label.setAccessibleName(t("Ses modeli işlem durumu"))
        layout.addWidget(self.status_label)

        if defer_load:
            # Envanter Piper özetleri/Whisper önbelleği için dosya okur: GUI'yi bekletmesin.
            self.trigger_refresh(quiet=True)
        else:
            self.refresh()

    def heightForWidth(self, width: int) -> int:
        hfw = super().heightForWidth(width)
        min_h = super().minimumSizeHint().height()
        return max(hfw, min_h)

    def minimumSizeHint(self) -> QSize:
        hint = super().minimumSizeHint()
        if self.hasHeightForWidth() and self.width() > 0:
            hfw = super().heightForWidth(self.width())
            if hfw > hint.height():
                hint.setHeight(hfw)
        return hint

    def sizeHint(self) -> QSize:
        return super().sizeHint().expandedTo(self.minimumSizeHint())

    def entries(self) -> list[VoiceModelEntry]:
        return list(self._entries)

    def selected_entry(self) -> VoiceModelEntry | None:
        row = self.model_list.currentRow()
        if 0 <= row < len(self._entries):
            return self._entries[row]
        return None

    def select_entry(self, entry_id: str) -> bool:
        for row, entry in enumerate(self._entries):
            if entry.id == entry_id:
                self.model_list.setCurrentRow(row)
                return True
        return False

    def set_demo(self, demo: bool) -> None:
        self._demo = demo
        self._sync_buttons()

    def refresh(self) -> list[VoiceModelEntry]:
        """Envanteri hemen okur ve arayüzü günceller (açılış/yenileme için hafif)."""
        entries = self._inventory_fn(self._config_path)
        self.apply_entries(entries)
        return entries

    def apply_entries(
        self,
        entries: Sequence[VoiceModelEntry],
        *,
        status_text: str | None = None,
    ) -> None:
        """Hazır envanter listesini sinyal yaymadan arayüze uygular."""
        current = self.selected_entry()
        selected_id = current.id if current is not None else None
        self._entries = list(entries)
        summary = summarize_health(self._entries)
        self.summary_label.setText(
            t(
                "{ok}/{total} hazır · Eksik: {missing} · Bozuk: {corrupt} · Disk: {size}"
            ).format(
                ok=summary.ok_count,
                total=summary.total,
                missing=summary.missing_count,
                corrupt=summary.corrupt_count,
                size=format_size_bytes(summary.total_size_bytes),
            )
        )

        was_blocked = self.model_list.blockSignals(True)
        try:
            self.model_list.clear()
            restore_row = 0
            for idx, entry in enumerate(self._entries):
                size_text = (
                    format_size_bytes(entry.size_bytes) if entry.size_bytes > 0 else "—"
                )
                flags: list[str] = [_status_badge(entry.status), size_text]
                if entry.bundled:
                    flags.append(t("paket içi"))
                if entry.active:
                    flags.append(t("etkin"))
                text = f"{entry.label} · {' · '.join(flags)}"
                item = QListWidgetItem(text)
                item.setData(Qt.ItemDataRole.UserRole, entry.id)
                item.setToolTip(f"{_family_name(entry.family)} · {entry.detail}")
                self.model_list.addItem(item)
                if selected_id is not None and entry.id == selected_id:
                    restore_row = idx
            if self._entries:
                self.model_list.setCurrentRow(restore_row)
        finally:
            self.model_list.blockSignals(was_blocked)

        self._update_detail()
        self._sync_buttons()
        if status_text is not None:
            self.status_label.setText(status_text)

    def _on_selection_changed(self, _row: int) -> None:
        self._update_detail()
        self._sync_buttons()

    def _update_detail(self) -> None:
        entry = self.selected_entry()
        if entry is None:
            self.detail_label.setText(t("Listeden bir ses modeli seç."))
            self.path_label.set_full_text("", "")
            return
        self.detail_label.setText(
            f"{_family_name(entry.family)} · {_status_badge(entry.status)} · {entry.detail}"
        )
        if entry.path is not None:
            path_str = str(entry.path)
            self.path_label.set_full_text(path_str, path_str)
        else:
            self.path_label.set_full_text(t("Konum: yerel diskte yok"), "")

    def _sync_buttons(self) -> None:
        busy = self._thread is not None and self._thread.isRunning()
        entry = self.selected_entry()
        self.refresh_button.setEnabled(not busy)
        self.verify_all_button.setEnabled(not busy and bool(self._entries))
        if entry is None:
            self.download_button.setEnabled(False)
            self.verify_button.setEnabled(False)
            self.clean_button.setEnabled(False)
            return
        self.download_button.setEnabled(
            not busy and not self._demo and entry.can_download
        )
        self.verify_button.setEnabled(
            not busy and entry.status != "missing"
        )
        self.clean_button.setEnabled(
            not busy and not self._demo and not entry.bundled and entry.can_clean
        )
        if entry.bundled:
            self.clean_button.setToolTip(
                t("Paket içi modeller (Silero VAD, DFN3) korunur ve silinemez.")
            )
        elif not entry.can_clean:
            self.clean_button.setToolTip(
                t("Yalnız indirilen önbellek modelleri temizlenebilir; özel klasörler korunur.")
            )
        else:
            self.clean_button.setToolTip(
                t("İndirilen model dosyalarını diskten temizler.")
            )

    def _start_task(
        self,
        op: str,
        busy_message: str,
        work: Callable[[], tuple[list[VoiceModelEntry], str]],
    ) -> bool:
        if self._thread is not None and self._thread.isRunning():
            return False
        self._generation += 1
        gen = self._generation
        thread = _ModelOpThread(gen, op, work)
        thread.completed.connect(self._on_task_done)
        thread.failed.connect(self._on_task_failed)
        thread.finished.connect(self._on_task_finished)
        self._thread = thread
        self.status_label.setText(busy_message)
        self._sync_buttons()
        start_unparented(thread)
        return True

    def trigger_refresh(self, *, quiet: bool = False) -> bool:
        """`quiet`: açılış/yenileme yüklemesi; modeller değişmediği için sinyal yaymaz."""

        def _work() -> tuple[list[VoiceModelEntry], str]:
            entries = self._inventory_fn(self._config_path)
            return entries, "" if quiet else t("Ses modeli listesi güncellendi.")

        op = "load" if quiet else "refresh"
        return self._start_task(op, t("Ses modelleri taranıyor…"), _work)

    def trigger_verify_all(self) -> bool:
        snapshot = list(self._entries)

        def _work() -> tuple[list[VoiceModelEntry], str]:
            for entry in snapshot:
                if entry.status != "missing":
                    self._verify_fn(entry.id, self._config_path)
            entries = self._inventory_fn(self._config_path)
            summary = summarize_health(entries)
            if summary.corrupt_count:
                msg = t(
                    "Doğrulama tamamlandı: {corrupt} bozuk model bulundu."
                ).format(corrupt=summary.corrupt_count)
            else:
                msg = t("Tüm kurulu modeller doğrulandı.")
            return entries, msg

        return self._start_task("verify_all", t("Tüm modeller doğrulanıyor…"), _work)

    def trigger_verify(self, entry_id: str | None = None) -> bool:
        if entry_id is not None:
            self.select_entry(entry_id)
        entry = self.selected_entry()
        if entry is None or entry.status == "missing":
            return False
        target_id = entry.id

        def _work() -> tuple[list[VoiceModelEntry], str]:
            verified = self._verify_fn(target_id, self._config_path)
            entries = self._inventory_fn(self._config_path)
            msg = (
                t("{label} doğrulandı ({detail}).").format(
                    label=verified.label, detail=verified.detail
                )
                if verified.status == "ok"
                else t("{label} doğrulanamadı: {detail}").format(
                    label=verified.label, detail=verified.detail
                )
            )
            return entries, msg

        return self._start_task("verify", t("Model doğrulanıyor…"), _work)

    def trigger_download(self, entry_id: str | None = None) -> bool:
        if self._demo:
            return False
        if entry_id is not None:
            self.select_entry(entry_id)
        entry = self.selected_entry()
        if entry is None or not entry.can_download:
            return False
        target_id = entry.id

        def _work() -> tuple[list[VoiceModelEntry], str]:
            downloaded = self._download_fn(target_id, self._config_path)
            entries = self._inventory_fn(self._config_path)
            msg = t("{label} indirildi ve hazır.").format(label=downloaded.label)
            return entries, msg

        return self._start_task("download", t("Model indiriliyor…"), _work)

    def trigger_clean(self, entry_id: str | None = None) -> bool:
        if self._demo:
            return False
        if entry_id is not None:
            self.select_entry(entry_id)
        entry = self.selected_entry()
        if entry is None or entry.bundled or not entry.can_clean:
            return False
        target_id = entry.id

        def _work() -> tuple[list[VoiceModelEntry], str]:
            cleaned = self._clean_fn(target_id, self._config_path)
            entries = self._inventory_fn(self._config_path)
            msg = t("{label} diskten temizlendi.").format(label=cleaned.label)
            return entries, msg

        return self._start_task("clean", t("Model temizleniyor…"), _work)

    def _on_task_done(
        self, generation: int, op: str, entries: object, message: str
    ) -> None:
        if generation != self._generation or not isinstance(entries, list):
            return
        self.apply_entries(entries, status_text=message)
        if op != "load":
            self.models_changed.emit(entries)

    def _on_task_failed(self, generation: int, _op: str, error_msg: str) -> None:
        if generation != self._generation:
            return
        self.status_label.setText(
            t("İşlem başarısız: {error}").format(error=error_msg)
        )

    def _on_task_finished(self) -> None:
        # Eski iş parçacığının geç gelen finished'ı yeni çalışan işi silmesin.
        sender = self.sender()
        if sender is not None and sender is not self._thread:
            return
        self._thread = None
        self._sync_buttons()

    def finish(self) -> None:
        self._generation += 1
        thread = self._thread
        self._thread = None
        if thread is not None:
            with contextlib.suppress(RuntimeError):
                if shutdown_thread(
                    thread,
                    ("completed", self._on_task_done),
                    ("failed", self._on_task_failed),
                    ("finished", self._on_task_finished),
                ):
                    return
        self._sync_buttons()

    def closeEvent(self, event: QCloseEvent) -> None:
        self.finish()
        super().closeEvent(event)
