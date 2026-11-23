"""Ayarlar > Tetikleyiciler: tetikleyici ve öneri zinciri yönetimi (ADR 0061, ADR 0086).

Tetikleyici ve öneri zincirini yalnız kullanıcı kurar: bu panel formu config'e aynı
doğrulamalı yazım yolundan (`save_triggers`) yazar; hiçbir araç ya da model ekleyemez
veya değiştiremez. Silme yerel bir ayar düzenlemesidir, araç çağrısı değildir.
"""

from __future__ import annotations

from collections.abc import Callable
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

from artemis.channels.desktop.chain_dialog import ChainDialog
from artemis.channels.desktop.form_kit import button_row, group, meta_label
from artemis.channels.desktop.settings_io import save_triggers
from artemis.channels.desktop.settings_lists import ListWithEmpty
from artemis.channels.desktop.trigger_dialog import TriggerDialog
from artemis.config.settings import load_settings
from artemis.config.triggers import MAX_CHAINS, MAX_TRIGGERS, SuggestionChainConfig, TriggerConfig
from artemis.i18n import t
from artemis.proactive.routines import RoutinesError, RoutineStore
from artemis.proactive.schedule import format_local
from artemis.proactive.store import ProactiveStore, ProactiveStoreError

_APPLY_NOTE = "Çalışan uygulama yeni listeyi Ayarlar'dan çıkınca (ya da yeniden açılınca) alır."


def _format_trigger_subtitle(item: TriggerConfig, last: str) -> str:
    kind = t("rutin {routine}").format(routine=item.routine) if item.routine else t("öneri")
    button = t(" · düğme: {kind}").format(kind=kind) if (item.routine or item.suggest) else ""
    if item.kind == "app_opened":
        return t("Uygulama: {app}{button} · son bildirim: {last}").format(
            app=item.app, button=button, last=last
        )
    if item.kind == "calendar_before":
        filt = f" ({item.title_filter})" if item.title_filter else ""
        return t("Takvim: {minutes} dk önce{filter}{button} · son bildirim: {last}").format(
            minutes=item.minutes_before, filter=filt, button=button, last=last
        )
    if item.kind == "system_resume":
        return t("Sistem uyanınca ({cooldown} dk bekleme){button} · son bildirim: {last}").format(
            cooldown=item.cooldown_minutes, button=button, last=last
        )
    return t("{folder} ({pattern}){button} · son bildirim: {last}").format(
        folder=item.folder, pattern=item.pattern, button=button, last=last
    )


def _format_chain_subtitle(chain: SuggestionChainConfig) -> str:
    source = (
        t("rutin bitince: {name}").format(name=chain.after_routine)
        if chain.after_routine
        else t("tetikleyici sonrası: {name}").format(name=chain.after_trigger)
    )
    parts = [source]
    if chain.work_profiles:
        parts.append(", ".join(chain.work_profiles))
    if chain.active_app:
        parts.append(chain.active_app)
    if chain.time_range:
        parts.append(chain.time_range)
    if chain.routine or chain.suggest:
        btn = t("rutin {routine}").format(routine=chain.routine) if chain.routine else t("öneri")
        parts.append(t("düğme: {kind}").format(kind=btn))
    if not chain.enabled:
        parts.append(t("kapalı"))
    return " · ".join(parts)


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
        self.add_button.setVisible(False)
        listing.addLayout(button_row(self.refresh_button, self.add_button))
        listing.addWidget(meta_label(
            t(
                "Bildirim yalnız sorar: düğmeye basmadan hiçbir şey çalışmaz. "
                "Tetikleyiciyi yalnız sen eklersin; Artemis kendi başına ekleyemez."
            )
        ))

        c_box, c_listing = group(t("Koşullu öneri zincirleri"), divided=False)
        layout.addWidget(c_box)
        self.chain_list = QListWidget()
        self.chain_list.setAccessibleName(t("Koşullu öneri zincirleri"))
        self.chain_list.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        self.chain_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.chain_area = ListWithEmpty(
            self.chain_list, t("Tanımlı öneri zinciri yok"), "", t("Zincir ekle"), height=150,
        )
        empty_chain_add = self.chain_area.empty.action_button
        if empty_chain_add is not None:
            empty_chain_add.setObjectName("primary")
            empty_chain_add.setEnabled(not demo)
            empty_chain_add.clicked.connect(self._add_chain)
        c_listing.addWidget(self.chain_area)
        self.add_chain_button = QPushButton(t("Zincir ekle"))
        self.add_chain_button.setObjectName("primary")
        self.add_chain_button.setVisible(False)
        c_listing.addLayout(button_row(self.add_chain_button))

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
        self.add_chain_button.clicked.connect(self._add_chain)
        self.proactive_button.clicked.connect(self._tick_proactive)
        for button in (
            self.refresh_button, self.add_button, self.add_chain_button, self.proactive_button
        ):
            button.setEnabled(not demo)
        self.refresh()

    def refresh(self) -> None:
        self.trigger_list.clear()
        self.chain_list.clear()
        self.notice.setText("")
        self.warning_row.hide()
        self.proactive_warning.setText(t("Tetikleyiciler için Zamanlı işler açık olmalı."))
        try:
            settings = load_settings(self._config_path)
        except (OSError, ValueError):
            self.notice.setText(t("Ayar dosyası okunamadı."))
            self.trigger_area.show_empty(t("Tetikleyiciler okunamadı"))
            self.chain_area.show_empty(t("Tetikleyiciler okunamadı"))
            self.add_button.setEnabled(False)
            self.add_chain_button.setEnabled(False)
            return
        items = settings.triggers.items
        chains = settings.triggers.chains
        self.add_button.setVisible(bool(items))
        self.add_button.setEnabled(not self._demo and len(items) < MAX_TRIGGERS)
        self.add_chain_button.setVisible(bool(chains))
        self.add_chain_button.setEnabled(not self._demo and len(chains) < MAX_CHAINS)
        if (items or chains) and not settings.triggers.enabled:
            self.notice.setText(
                t("Tetikleyiciler kapalı: yukarıdaki kutuyu açınca kendiliğinden kaydedilir.")
            )
        if (settings.triggers.enabled or items or chains) and not settings.proactive.enabled:
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
            self._add_row(
                self.trigger_list, "trigger", item.name, _format_trigger_subtitle(item, last),
                self._edit_clicked, self._delete_clicked,
            )
        for chain in chains:
            self._add_row(
                self.chain_list, "chain", chain.name, _format_chain_subtitle(chain),
                self._edit_chain_clicked, self._delete_chain_clicked,
            )
        if items:
            self.trigger_area.show_list()
        elif settings.triggers.enabled:
            self.trigger_area.show_empty(t("Tanımlı tetikleyici yok"), action=True)
        else:
            self.trigger_area.show_empty(t("Tetikleyiciler kapalı"), action=True)
        if chains:
            self.chain_area.show_list()
        else:
            self.chain_area.show_empty(t("Tanımlı öneri zinciri yok"), action=True)

    def _add_row(
        self,
        target: QListWidget,
        prop_name: str,
        name: str,
        text: str,
        on_edit: Callable[[], None],
        on_delete: Callable[[], None],
    ) -> None:
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
        for caption, slot in ((t("Düzenle"), on_edit), (t("Sil"), on_delete)):
            button = QPushButton(caption)
            button.setProperty(prop_name, name)
            button.setAccessibleName(
                t("{caption}: {name}").format(caption=caption, name=name)
            )
            button.setEnabled(not self._demo)
            button.clicked.connect(slot)
            lay.addWidget(button)
        item = QListWidgetItem()
        item.setSizeHint(QSize(0, 76))
        target.addItem(item)
        target.setItemWidget(item, row)

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

    def _add_chain(self) -> None:
        self._edit_chain(None)

    def _edit_chain_clicked(self) -> None:
        sender = self.sender()
        if sender is not None:
            self._edit_chain(str(sender.property("chain")))

    def _delete_chain_clicked(self) -> None:
        sender = self.sender()
        if sender is not None:
            self._delete_chain(str(sender.property("chain")))

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

    def _edit_chain(self, name: str | None) -> None:
        if self._demo:
            return
        try:
            settings = load_settings(self._config_path)
        except (OSError, ValueError):
            self.notice.setText(t("Ayar dosyası okunamadı."))
            return
        chains = list(settings.triggers.chains)
        current = next((c for c in chains if c.name == name), None)
        if name is None and len(chains) >= MAX_CHAINS:
            self.notice.setText(
                t("En çok {max_chains} öneri zinciri olabilir.").format(max_chains=MAX_CHAINS)
            )
            return
        others = [c for c in chains if c is not current]
        routines, hint = self._routine_choices()
        trigger_names = [i.name for i in settings.triggers.items]
        dialog = ChainDialog(current, others, routines, trigger_names, hint, self)
        if dialog.exec() != ChainDialog.DialogCode.Accepted or dialog.result_chain is None:
            return
        result = dialog.result_chain
        updated = [result if c is current else c for c in chains]
        if current is None:
            updated.append(result)
        self._write_chains(
            list(settings.triggers.items),
            updated,
            enable=not settings.triggers.enabled,
            note=(
                t("Tetikleyiciler kapalıydı, açıldı.") + " "
                if not settings.triggers.enabled
                else ""
            ),
        )

    def _delete_chain(self, name: str) -> None:
        if self._demo or not self._confirm(
            t("'{name}' öneri zinciri silinsin mi?").format(name=name)
        ):
            return
        try:
            settings = load_settings(self._config_path)
        except (OSError, ValueError):
            self.notice.setText(t("Ayar dosyası okunamadı."))
            return
        chains = [c for c in settings.triggers.chains if c.name != name]
        self._write_chains(list(settings.triggers.items), chains, enable=False, note="")

    def _write(self, items: list[TriggerConfig], *, enable: bool, note: str) -> None:
        self._write_chains(items, None, enable=enable, note=note)

    def _write_chains(
        self,
        items: list[TriggerConfig],
        chains: list[SuggestionChainConfig] | None,
        *,
        enable: bool,
        note: str,
    ) -> None:
        try:
            save_triggers(
                self._config_path,
                [i.model_dump(mode="json", exclude_none=True) for i in items],
                enable=enable,
                chains=(
                    [c.model_dump(mode="json", exclude_none=True) for c in chains]
                    if chains is not None
                    else None
                ),
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
