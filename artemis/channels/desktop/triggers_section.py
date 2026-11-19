"""Ayarlar > Tetikleyiciler: tetikleyici ekle/düzenle/sil ve son bildirim zamanı (ADR 0061).

Tetikleyiciyi yalnız kullanıcı kurar: bu panel formu config'e aynı doğrulamalı yazım yolundan
(`save_triggers`) yazar; hiçbir araç ya da model tetikleyici ekleyemez veya değiştiremez.
Silme yerel bir ayar düzenlemesidir, araç çağrısı değildir.
"""

from __future__ import annotations

from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from artemis.channels.desktop.form_kit import button_row, group, meta_label
from artemis.channels.desktop.settings_io import save_triggers
from artemis.channels.desktop.settings_lists import ListWithEmpty
from artemis.channels.desktop.trigger_dialog import TriggerDialog
from artemis.config.settings import MAX_TRIGGERS, TriggerConfig, load_settings
from artemis.i18n import t
from artemis.proactive.routines import RoutinesError, RoutineStore
from artemis.proactive.schedule import format_local
from artemis.proactive.store import ProactiveStore, ProactiveStoreError

_APPLY_NOTE = "Çalışan uygulama yeni listeyi Ayarlar'dan çıkınca (ya da yeniden açılınca) alır."


class TriggersSection(QWidget):
    saved = Signal()  # config yazıldı (triggers.enabled de değişmiş olabilir)
    proactive_requested = Signal()  # "Zamanlı işleri aç": panel kutuyu işaretler, yazmaz

    def __init__(self, config_path: Path, *, demo: bool = False) -> None:
        super().__init__()
        self._config_path = config_path
        self._demo = demo
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(24)
        box, listing = group(t("Tanımlı tetikleyiciler"), divided=False)
        layout.addWidget(box)
        self.trigger_list = QListWidget()
        self.trigger_list.setAccessibleName(t("Tanımlı tetikleyiciler"))
        self.trigger_list.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        self.trigger_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.trigger_area = ListWithEmpty(
            self.trigger_list, t("Tanımlı tetikleyici yok"), "", t("Tetikleyici ekle"), height=200,
        )
        empty_add = self.trigger_area.empty.action_button
        if empty_add is not None:
            empty_add.setObjectName("primary")
            empty_add.setEnabled(not demo)
            empty_add.clicked.connect(self._add)
        listing.addWidget(self.trigger_area)
        self.refresh_button = QPushButton(t("Yenile"))
        self.add_button = QPushButton(t("Tetikleyici ekle"))
        self.add_button.setObjectName("primary")
        # Liste boşken tek birincil eylem boş durumdaki düğmedir; alttaki yalnız liste doluyken
        # görünür (UX-09). Ayar dosyasını dış editörde açan düğme yok: form her alanı kapsar.
        self.add_button.setVisible(False)
        listing.addLayout(button_row(self.refresh_button, self.add_button))
        listing.addWidget(meta_label(
            t(
                "Bildirim yalnız sorar: düğmeye basmadan hiçbir şey çalışmaz. "
                "Tetikleyiciyi yalnız sen eklersin; Artemis kendi başına ekleyemez."
            )
        ))
        warn_row = QHBoxLayout()
        self.proactive_warning = QLabel(t("Tetikleyiciler için Zamanlı işler açık olmalı."))
        self.proactive_warning.setObjectName("warnNote")
        self.proactive_warning.setWordWrap(True)
        self.proactive_button = QPushButton(t("Zamanlı işleri aç"))
        warn_row.addWidget(self.proactive_warning, 1)
        warn_row.addWidget(self.proactive_button)
        self.warning_row = QWidget()
        self.warning_row.setLayout(warn_row)
        warn_row.setContentsMargins(0, 0, 0, 0)
        self.warning_row.hide()
        layout.addWidget(self.warning_row)
        self.notice = meta_label("")
        layout.addWidget(self.notice)
        self.refresh_button.clicked.connect(self.refresh)
        self.add_button.clicked.connect(self._add)
        self.proactive_button.clicked.connect(self._tick_proactive)
        for button in (self.refresh_button, self.add_button, self.proactive_button):
            button.setEnabled(not demo)
        self.refresh()

    def refresh(self) -> None:
        self.trigger_list.clear()
        self.notice.setText("")
        self.warning_row.hide()
        self.proactive_warning.setText(t("Tetikleyiciler için Zamanlı işler açık olmalı."))
        try:
            settings = load_settings(self._config_path)
        except (OSError, ValueError):
            self.notice.setText(t("Ayar dosyası okunamadı."))
            self.trigger_area.show_empty(t("Tetikleyiciler okunamadı"))
            self.add_button.setEnabled(False)
            return
        items = settings.triggers.items
        self.add_button.setVisible(bool(items))
        self.add_button.setEnabled(not self._demo and len(items) < MAX_TRIGGERS)
        if items and not settings.triggers.enabled:
            self.notice.setText(
                t("Tetikleyiciler kapalı: yukarıdaki kutuyu açınca kendiliğinden kaydedilir.")
            )
        if (settings.triggers.enabled or items) and not settings.proactive.enabled:
            self.warning_row.show()
            self.proactive_button.show()
        store = None
        if settings.proactive.enabled and not self._demo:
            try:
                store = ProactiveStore(settings.state_dir / "proactive.sqlite3")
            except ProactiveStoreError:
                store = None
        tz = ZoneInfo(settings.proactive.timezone)
        for item in items:
            last = t("henüz yok")
            if store is not None:
                try:
                    moment = store.last_delivered(f"trigger:{item.name}:")
                except ProactiveStoreError:
                    moment = None
                if moment is not None:
                    last = format_local(moment, tz)
            kind = (
                t("rutin {routine}").format(routine=item.routine)
                if item.routine
                else t("öneri")
            )
            button = t(" · düğme: {kind}").format(kind=kind)
            has_button = item.routine or item.suggest
            self._add_row(
                item.name,
                t("{folder} ({pattern}){button} · son bildirim: {last}").format(
                    folder=item.folder,
                    pattern=item.pattern,
                    button=button if has_button else "",
                    last=last,
                ),
            )
        if items:
            self.trigger_area.show_list()
        elif settings.triggers.enabled:
            self.trigger_area.show_empty(t("Tanımlı tetikleyici yok"), action=True)
        else:
            self.trigger_area.show_empty(t("Tetikleyiciler kapalı"), action=True)

    def _add_row(self, name: str, text: str) -> None:
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(8)
        texts = QVBoxLayout()
        texts.setSpacing(2)
        title = QLabel(name)
        title.setTextFormat(Qt.TextFormat.PlainText)
        texts.addWidget(title)
        texts.addWidget(meta_label(text))
        lay.addLayout(texts, 1)
        for caption, slot in ((t("Düzenle"), self._edit_clicked), (t("Sil"), self._delete_clicked)):
            button = QPushButton(caption)
            button.setProperty("trigger", name)
            button.setAccessibleName(
                t("{caption}: {name}").format(caption=caption, name=name)
            )
            button.setEnabled(not self._demo)
            button.clicked.connect(slot)
            lay.addWidget(button)
        item = QListWidgetItem()
        item.setSizeHint(QSize(0, 76))  # genişlik görünüm alanını izler; metin sarılır
        self.trigger_list.addItem(item)
        self.trigger_list.setItemWidget(item, row)

    # -- eylemler ---------------------------------------------------------------------------

    def _routine_choices(self) -> tuple[list[str], str]:
        try:
            settings = load_settings(self._config_path)
            if not settings.routines.enabled:
                return [], t("Rutinler kapalı; önce Rutinler sayfasından aç.")
            names = [r.name for r in RoutineStore(settings.state_dir / "routines.json").load()]
        except (OSError, ValueError, RoutinesError):
            return [], t("Rutinler okunamadı.")
        return names, "" if names else t("Kayıtlı rutin yok.")

    def confirm_box(self, text: str) -> tuple[QMessageBox, QPushButton]:
        """Silme onayı: Türkçe düğmeler, varsayılan "Vazgeç" (Enter yanlışlıkla silmesin)."""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle(t("Tetikleyici"))
        box.setText(text)
        yes = box.addButton(t("Sil"), QMessageBox.ButtonRole.DestructiveRole)
        no = box.addButton(t("Vazgeç"), QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(no)
        box.setEscapeButton(no)
        return box, yes

    def _confirm(self, text: str) -> bool:
        box, yes = self.confirm_box(text)
        box.exec()
        return box.clickedButton() is yes

    def _add(self) -> None:
        self._edit(None)

    def _edit_clicked(self) -> None:
        sender = self.sender()
        if sender is not None:
            self._edit(str(sender.property("trigger")))

    def _delete_clicked(self) -> None:
        sender = self.sender()
        if sender is not None:
            self._delete(str(sender.property("trigger")))

    def _edit(self, name: str | None) -> None:
        if self._demo:
            return
        try:
            settings = load_settings(self._config_path)
        except (OSError, ValueError):
            self.notice.setText(t("Ayar dosyası okunamadı."))
            return
        items = list(settings.triggers.items)
        current = next((i for i in items if i.name == name), None)
        if name is None and len(items) >= MAX_TRIGGERS:
            self.notice.setText(
                t("En çok {max_triggers} tetikleyici olabilir.").format(max_triggers=MAX_TRIGGERS)
            )
            return
        others = [i for i in items if i is not current]
        routines, hint = self._routine_choices()
        dialog = TriggerDialog(current, others, routines, hint, self)
        if dialog.exec() != TriggerDialog.DialogCode.Accepted or dialog.result_trigger is None:
            return
        result = dialog.result_trigger
        updated = [result if i is current else i for i in items]
        if current is None:
            updated.append(result)
        self._write(
            updated, enable=not settings.triggers.enabled,
            note=(
                t("Tetikleyiciler kapalıydı, açıldı.") + " "
                if not settings.triggers.enabled
                else ""
            ),
        )

    def _delete(self, name: str) -> None:
        if self._demo or not self._confirm(
            t("'{name}' tetikleyicisi silinsin mi?").format(name=name)
        ):
            return
        try:
            items = [i for i in load_settings(self._config_path).triggers.items if i.name != name]
        except (OSError, ValueError):
            self.notice.setText(t("Ayar dosyası okunamadı."))
            return
        self._write(items, enable=False, note="")

    def _write(self, items: list[TriggerConfig], *, enable: bool, note: str) -> None:
        try:
            save_triggers(
                self._config_path, [i.model_dump(exclude_none=True) for i in items], enable=enable
            )
        except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError):
            self.notice.setText(t("Kaydedilemedi; mevcut ayarlar korunuyor."))
            return
        self.refresh()
        self.notice.setText(
            t("Kaydedildi. {note}{apply_note}").format(note=note, apply_note=t(_APPLY_NOTE))
        )
        self.saved.emit()

    def _tick_proactive(self) -> None:
        self.proactive_requested.emit()
        self.proactive_button.hide()
        self.proactive_warning.setText(t("Zamanlı işler işaretlendi; kendiliğinden kaydedilir."))
