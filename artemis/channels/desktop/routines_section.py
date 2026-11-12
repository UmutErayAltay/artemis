"""Ayarlar > Rutinler: liste, sil, zaman ve dosyayı aç (ADR 0058). Çalıştırma Otomasyon'dadır.

Silme araç çağrısıdır: bu bölüm yalnız `tool_requested` yayar, pencere onu aynı
köprü ve SafetyGate üzerinden gönderir (ikinci onay yolu yok). Adım yazma arayüzü bilerek yoktur;
rutin sohbetle (onaylı `routine_save`) ya da JSON'u elle düzenleyerek oluşur. Zaman yalnız buradan
kurulur; hiçbir araç zaman ayarlayamaz.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ValidationError
from PySide6.QtCore import QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLineEdit,
    QListWidget,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from artemis.channels.desktop.provider_section import button_row, field, group, meta_label
from artemis.channels.desktop.settings_lists import ListWithEmpty
from artemis.config.settings import load_settings
from artemis.proactive.routines import (
    Routine,
    RoutineSchedule,
    RoutinesError,
    RoutineStore,
    name_key,
)
from artemis.tools.base import ToolResult
from artemis.tools.plugins.routines import RoutineNameArguments

_DAYS = ("Pzt", "Sal", "Çar", "Per", "Cum", "Cmt", "Paz")


def _describe(routine: Routine) -> str:
    tools = ", ".join(step.tool for step in routine.steps)
    when = ""
    if routine.schedule is not None:
        days = " ".join(_DAYS[day] for day in routine.schedule.days)
        when = f" · {routine.schedule.at} ({days})"
    return f"{routine.name} · {len(routine.steps)} adım: {tools}{when}"


class RoutinesSection(QWidget):
    tool_requested = Signal(str, object)  # (araç adı, pydantic argümanları)

    def __init__(self, config_path: Path, *, demo: bool = False) -> None:
        super().__init__()
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
        self.open_button = QPushButton("Dosyayı aç")
        self.action_row = QWidget()
        self.action_row.setLayout(button_row(self.open_button, self.delete_button))
        listing.addWidget(self.action_row)
        listing.addWidget(meta_label(
            "Silme onay ister; adımların kendi onayları da geçerlidir. "
            "Dosyayı elle de düzenleyebilirsin."
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
        when.addLayout(button_row(self.clear_time_button, self.save_time_button))
        self.notice = meta_label("")
        layout.addWidget(self.notice)
        self.delete_button.clicked.connect(self._delete)
        self.open_button.clicked.connect(self._open_file)
        self.save_time_button.clicked.connect(self._save_time)
        self.clear_time_button.clicked.connect(self._clear_time)
        self.routine_list.currentRowChanged.connect(self._selected)
        self.refresh()

    def refresh(self) -> None:
        self.routine_list.clear()
        self.notice.setText("")
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
                # Bozuk dosya sessizce sıfırlanmaz; kullanıcı dosyayı açıp düzeltir.
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
                        "Sohbette “işe başla adında bir rutin kaydet: …” de.",
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
        has_selection = self.routine_list.currentRow() >= 0
        self.action_row.setVisible(self._enabled)
        self.run_hint.setVisible(self._enabled)
        self.delete_button.setEnabled(usable and has_selection)
        self.save_time_button.setEnabled(usable and has_selection)
        self.clear_time_button.setEnabled(usable and has_selection)
        self.open_button.setEnabled(not self._demo and self._store is not None)

    def _selected(self, _row: int) -> None:
        routine = self._current()
        schedule = routine.schedule if routine is not None else None
        self.time_edit.setText(schedule.at if schedule is not None else "")
        for index, box in enumerate(self.day_boxes):
            box.setChecked(schedule is None or index in schedule.days)
        self._set_controls()

    def _request(self, tool: str) -> None:
        routine = self._current()
        if routine is not None:
            args: BaseModel = RoutineNameArguments(name=routine.name)
            self.tool_requested.emit(tool, args)

    def _delete(self) -> None:
        self._request("routine_delete")

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

    def _open_file(self) -> None:
        store = self._store
        if store is None:
            return
        try:
            if not store.path.exists():
                store.save([])  # boş ama geçerli dosya: elle düzenleyen şemayı görür
        except RoutinesError as error:
            self.notice.setText(str(error))
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(store.path)))
