"""Ayarlar > Rutinler: ekle, düzenle, sil, zaman ve bozuk dosyayı düzelt (ADR 0058 + Ek karar).

Çalıştırma Otomasyon'dadır. Ekle/düzenle/sil panelde kullanıcının tıklamasıyla yapılır ve
`validate_steps` + atomik `RoutineStore.save` ile yazılır: panel kullanıcının kendisidir, bu yüzden
SafetyGate onayı gerekmez (zaman ayarı da böyle). Araçlar (`routine_save`/`routine_delete`) aynen
onaylı kalır. Dış editör açılmaz. Zaman yalnız buradan kurulur; hiçbir araç zaman ayarlayamaz.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError
from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from artemis.channels.desktop.form_kit import button_row, field, group, meta_label
from artemis.channels.desktop.routine_dialog import RoutineDialog, RoutineFileDialog
from artemis.channels.desktop.settings_lists import ListWithEmpty
from artemis.config.settings import load_settings
from artemis.proactive.routines import (
    MAX_ROUTINES,
    Routine,
    RoutineSchedule,
    RoutinesError,
    RoutineStore,
    name_key,
)
from artemis.tools.base import ToolResult
from artemis.tools.registry import ToolRegistry, discover_tools

_DAYS = ("Pzt", "Sal", "Çar", "Per", "Cum", "Cmt", "Paz")


def _describe(routine: Routine) -> str:
    tools = ", ".join(step.tool for step in routine.steps)
    when = ""
    if routine.schedule is not None:
        days = " ".join(_DAYS[day] for day in routine.schedule.days)
        when = f" · {routine.schedule.at} ({days})"
    return f"{routine.name} · {len(routine.steps)} adım: {tools}{when}"


class RoutinesSection(QWidget):
    # Artık yayılmaz (silme panelde doğrudan); pencere bağlantıları bozulmasın diye durur.
    tool_requested = Signal(str, object)

    def __init__(
        self, config_path: Path, *, demo: bool = False, registry: ToolRegistry | None = None
    ) -> None:
        super().__init__()
        self._registry = registry
        self._corrupt = False
        self._config_path = config_path
        self._demo = demo
        self._enabled = False
        self._store: RoutineStore | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(24)
        list_box, listing = group("Kayıtlı rutinler", divided=False)
        layout.addWidget(list_box)
        self.routine_list = QListWidget()
        self.routine_list.setAccessibleName("Kayıtlı rutinler")
        self.routine_area = ListWithEmpty(self.routine_list, "Henüz rutin yok")
        listing.addWidget(self.routine_area)
        self.run_hint = meta_label("Rutinleri Otomasyon sayfasından çalıştırırsın.")
        listing.addWidget(self.run_hint)
        self.delete_button = QPushButton("Sil")
        self.edit_button = QPushButton("Düzenle")
        self.fix_button = QPushButton("Dosyayı düzelt")
        self.add_button = QPushButton("Rutin ekle")
        self.add_button.setObjectName("primary")
        self.action_row = QWidget()
        self.action_row.setLayout(
            button_row(
                self.fix_button, self.delete_button, self.edit_button, self.add_button,
            )
        )
        listing.addWidget(self.action_row)
        listing.addWidget(meta_label(
            "Rutini burada yazarsın; adımların kendi onayları çalışırken yine geçerlidir. "
            "Silme onay ister."
        ))
        when_box, when = group("Zaman", "Zamanı gelince Artemis çalıştırmaz, yalnız sorar.")
        layout.addWidget(when_box)
        self.time_edit = QLineEdit()
        self.time_edit.setMaxLength(5)
        self.time_edit.setPlaceholderText("08:30")
        self.time_edit.setAccessibleName("Rutin saati")
        field(when, "Saat (SS:DD)", self.time_edit)
        days_row = QHBoxLayout()
        days_row.setSpacing(8)
        self.day_boxes = [QCheckBox(name) for name in _DAYS]
        for box in self.day_boxes:
            box.setChecked(True)
            days_row.addWidget(box)
        days_row.addStretch(1)
        field(when, "Günler", days_row)
        self.save_time_button = QPushButton("Zamanı kaydet")
        self.clear_time_button = QPushButton("Zamanı kaldır")
        when.addLayout(button_row(
            self.clear_time_button, self.save_time_button,
        ))
        self.notice = meta_label("")
        layout.addWidget(self.notice)
        self.delete_button.clicked.connect(self._delete)
        self.add_button.clicked.connect(self._add)
        self.edit_button.clicked.connect(self._edit)
        self.fix_button.clicked.connect(self._fix_file)
        self.save_time_button.clicked.connect(self._save_time)
        self.clear_time_button.clicked.connect(self._clear_time)
        self.routine_list.currentRowChanged.connect(self._selected)
        self.refresh()

    def refresh(self) -> None:
        self.routine_list.clear()
        self.notice.setText("")
        self._corrupt = False
        try:
            settings = load_settings(self._config_path)
        except (OSError, ValueError):
            self._enabled = False
            self._store = None
            self.notice.setText("Ayar dosyası okunamadı.")
            self.routine_area.show_empty("Rutinler okunamadı")
            self._set_controls()
            return
        self._enabled = settings.routines.enabled
        self._store = RoutineStore(settings.state_dir / "routines.json")
        if not self._enabled:
            self.routine_area.show_empty("Rutinler kapalı")
        else:
            try:
                for routine in self._store.load():
                    self.routine_list.addItem(_describe(routine))
            except RoutinesError as error:
                # Bozuk dosya sessizce sıfırlanmaz; kullanıcı "Dosyayı düzelt" ile düzeltir.
                self._corrupt = True
                self.notice.setText(str(error))
                self.routine_area.show_empty("Rutinler okunamadı")
            else:
                if not settings.proactive.enabled:
                    self.notice.setText("Zamanlı bildirim için Zamanlı işler açık olmalı.")
                if self.routine_list.count():
                    self.routine_area.show_list()
                else:
                    self.routine_area.show_empty(
                        "Henüz rutin yok",
                        "“Rutin ekle” ile başla ya da sohbette "
                        "“işe başla adında bir rutin kaydet: …” de.",
                    )
        self._set_controls()

    def _routines(self) -> list[Routine]:
        if self._store is None:
            return []
        try:
            return self._store.load()
        except RoutinesError as error:
            self.notice.setText(str(error))
            return []

    def _current(self) -> Routine | None:
        row = self.routine_list.currentRow()
        if row < 0:
            return None
        routines = self._routines()
        return routines[row] if row < len(routines) else None

    def _set_controls(self) -> None:
        usable = self._enabled and not self._demo
        editable = usable and not self._corrupt  # bozukken yazmak dosyayı ezerdi
        has_selection = self.routine_list.currentRow() >= 0
        self.action_row.setVisible(self._enabled)
        self.run_hint.setVisible(self._enabled)
        self.add_button.setEnabled(editable)
        self.edit_button.setEnabled(editable and has_selection)
        self.delete_button.setEnabled(editable and has_selection)
        self.fix_button.setVisible(self._corrupt)
        self.fix_button.setEnabled(usable)
        self.save_time_button.setEnabled(usable and has_selection)
        self.clear_time_button.setEnabled(usable and has_selection)

    def _selected(self, _row: int) -> None:
        routine = self._current()
        schedule = routine.schedule if routine is not None else None
        self.time_edit.setText(schedule.at if schedule is not None else "")
        for index, box in enumerate(self.day_boxes):
            box.setChecked(schedule is None or index in schedule.days)
        self._set_controls()

    def _tools(self) -> ToolRegistry:
        if self._registry is None:
            self._registry = discover_tools()  # çalıştırıcıyla aynı kayıt; ikinci kayıt yok
        return self._registry

    def _load_strict(self) -> list[Routine] | None:
        """Yazma öncesi okuma: hata varsa None (boş listeye düşüp dosyayı ezmesin)."""
        if self._store is None or self._demo:
            return None
        try:
            return self._store.load()
        except RoutinesError as error:
            self.notice.setText(str(error))
            return None

    def _commit(self, routines: list[Routine], note: str, select: int = -1) -> None:
        if self._store is None:
            return
        try:
            self._store.save(routines)
        except RoutinesError as error:
            self.notice.setText(str(error))
            return
        self.refresh()
        if select >= 0:
            self.routine_list.setCurrentRow(select)
        self.notice.setText(note)

    def confirm_box(self, text: str) -> tuple[QMessageBox, QPushButton]:
        """Silme onayı: varsayılan "Vazgeç" (Enter yanlışlıkla silmesin)."""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Rutin")
        box.setText(text)
        yes = box.addButton("Sil", QMessageBox.ButtonRole.DestructiveRole)
        no = box.addButton("Vazgeç", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(no)
        box.setEscapeButton(no)
        return box, yes

    def _confirm(self, text: str) -> bool:
        box, yes = self.confirm_box(text)
        box.exec()
        return box.clickedButton() is yes

    def _add(self) -> None:
        self._edit_routine(None)

    def _edit(self) -> None:
        self._edit_routine(self.routine_list.currentRow())

    def _edit_routine(self, row: int | None) -> None:
        routines = self._load_strict()
        if routines is None:
            return
        current = routines[row] if row is not None and 0 <= row < len(routines) else None
        if row is not None and current is None:
            return
        if current is None and len(routines) >= MAX_ROUTINES:
            self.notice.setText(f"En çok {MAX_ROUTINES} rutin olabilir.")
            return
        others = [item for item in routines if item is not current]
        dialog = RoutineDialog(current, others, self._tools(), self)
        if dialog.exec() != RoutineDialog.DialogCode.Accepted or dialog.result_routine is None:
            return
        result = dialog.result_routine
        updated = [result if item is current else item for item in routines]
        if current is None:
            updated.append(result)
        note = "Rutin kaydedildi."
        if current is not None and name_key(current.name) != name_key(result.name):
            note += " Adı değiştirdiysen tetikleyicilerdeki rutin adını da güncelle."
        self._commit(updated, note, updated.index(result))

    def _delete(self) -> None:
        routine = self._current()
        routines = self._load_strict()
        if routine is None or routines is None:
            return
        if not self._confirm(f"«{routine.name}» rutini silinsin mi?"):
            return
        key = name_key(routine.name)
        self._commit([item for item in routines if name_key(item.name) != key], "Rutin silindi.")

    def _fix_file(self) -> None:
        if self._store is None or self._demo:
            return
        try:
            dialog = RoutineFileDialog(self._store, self._tools(), self)
        except OSError as error:
            self.notice.setText(f"Rutin dosyası okunamadı ({type(error).__name__}).")
            return
        if dialog.exec() == RoutineFileDialog.DialogCode.Accepted and dialog.saved:
            self.refresh()
            self.notice.setText("Rutin dosyası düzeltildi.")

    def accept_result(self, tool_name: str, result: ToolResult) -> None:
        self.notice.setText(result.message)
        if tool_name in {"routine_delete", "routine_save"} and result.success:
            self.refresh()
            self.notice.setText(result.message)

    def _write_schedule(self, schedule: RoutineSchedule | None) -> None:
        routine = self._current()
        if routine is None or self._store is None:
            return
        try:
            routines = self._store.load()
            key = name_key(routine.name)
            updated = [
                item.model_copy(update={"schedule": schedule})
                if name_key(item.name) == key
                else item
                for item in routines
            ]
            self._store.save(updated)
        except RoutinesError as error:
            self.notice.setText(str(error))
            return
        row = self.routine_list.currentRow()
        self.refresh()
        self.routine_list.setCurrentRow(row)
        self.notice.setText("Zaman kaydedildi." if schedule else "Zaman kaldırıldı.")

    def _save_time(self) -> None:
        days = tuple(index for index, box in enumerate(self.day_boxes) if box.isChecked())
        try:
            schedule = RoutineSchedule(at=self.time_edit.text().strip(), days=days)
        except ValidationError:
            self.notice.setText("Saat SS:DD biçiminde (örn. 08:30) ve en az bir gün seçili olmalı.")
            return
        self._write_schedule(schedule)

    def _clear_time(self) -> None:
        self._write_schedule(None)
