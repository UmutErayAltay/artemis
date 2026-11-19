"""Tetikleyici ekleme/düzenleme penceresi (ADR 0061 Ek).

Yalnız Ayarlar > Tetikleyiciler formundan, kullanıcı eylemiyle açılır; tetikleyiciyi hiçbir araç
ya da model kurmaz. Doğrulamanın tek kaynağı `TriggerConfig`tir; burada yalnız hatalar düz
Türkçeye çevrilir ve formun kendine özgü kuralları (klasör var mı, tek desen, benzersiz ad) eklenir.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

from pydantic import ValidationError
from PySide6.QtCore import QStandardPaths
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from artemis.channels.desktop.dialog_shell import FormDialog
from artemis.channels.desktop.widgets import with_help
from artemis.config.settings import MAX_TRIGGERS, TriggerConfig
from artemis.i18n import t

DEFAULT_MESSAGE = "Yeni dosya geldi: {file}"
DEFAULT_SUGGEST = "Şu dosyayı özetle: {path}"
# Tarayıcı tek bir fnmatch deseni kullanır (`;` ile çoklu desen yok); yalnız o sunulur.
PATTERN_PRESETS = (
    ("Tümü (*)", "*"),
    ("PDF (*.pdf)", "*.pdf"),
    ("Word (*.docx)", "*.docx"),
    ("Excel (*.xlsx)", "*.xlsx"),
    ("Arşiv (*.zip)", "*.zip"),
)
_TR = str.maketrans("çÇğĞıİöÖşŞüÜ", "cCgGiIoOsSuU")
_FIELD_ERRORS = {
    "name": "Ad 1-32 harf, rakam, _ ya da - olmalı.",
    "folder": "Geçerli bir klasör seç.",
    "pattern": "Dosya türü boş olamaz (en çok 100 karakter).",
    "message": "Bildirim metni 1-300 karakter olmalı.",
    "suggest": "Öneri metni 1-500 karakter olmalı.",
    "routine": "Bir rutin seç.",
}


def slug_name(folder_name: str, taken: Iterable[str] = ()) -> str:
    """Klasör adından izinli bir tetikleyici adı; alınmışsa -2, -3 eklenir. Üretilemezse boş."""
    base = re.sub(r"[^A-Za-z0-9_-]+", "-", folder_name.translate(_TR)).strip("-")[:28]
    if not base:
        return ""
    used = {name.casefold() for name in taken}
    candidate, number = base, 2
    while candidate.casefold() in used:
        candidate, number = f"{base}-{number}", number + 1
    return candidate


def validate_trigger(
    *,
    name: str,
    folder: str,
    pattern: str,
    message: str,
    suggest: str | None,
    routine: str | None,
    taken: Iterable[str],
    count: int,
) -> tuple[TriggerConfig | None, dict[str, str]]:
    """(tetikleyici, {alan: Türkçe hata}). `taken` düzenlenen tetikleyici HARİÇ mevcut adlar,
    `count` düzenlenen HARİÇ mevcut sayıdır."""
    errors: dict[str, str] = {}
    name, folder, pattern = name.strip(), folder.strip(), pattern.strip()
    if count >= MAX_TRIGGERS:
        errors["name"] = t("En çok {max_triggers} tetikleyici olabilir.").format(
            max_triggers=MAX_TRIGGERS
        )
    elif name.casefold() in {n.casefold() for n in taken}:
        errors["name"] = t("Bu ad zaten kullanılıyor.")
    if folder and not Path(folder).is_absolute():
        errors["folder"] = t("Klasör tam yol olmalı.")
    elif folder and not Path(folder).is_dir():
        errors["folder"] = t("Klasör bulunamadı.")
    if ";" in pattern or "/" in pattern or "\\" in pattern:
        errors["pattern"] = t(
            "Tek bir dosya adı deseni yaz; ; / \\ kullanılamaz. "
            "Başka tür için ayrı tetikleyici ekle."
        )
    if suggest is not None and routine is not None:
        errors["routine"] = t("Düğme ya öneri ya rutin olur, ikisi birden olmaz.")
    if errors:
        return None, errors
    try:
        item = TriggerConfig(
            name=name, folder=folder, pattern=pattern, message=message,
            suggest=suggest, routine=routine,
        )
    except ValidationError as error:
        for detail in error.errors():
            key = str(detail["loc"][0]) if detail["loc"] else "name"
            key = key if key in _FIELD_ERRORS else "name"
            errors.setdefault(key, t(_FIELD_ERRORS[key]))
        return None, errors
    return item, {}


class TriggerDialog(FormDialog):
    def __init__(
        self,
        existing: TriggerConfig | None,
        others: list[TriggerConfig],
        routines: list[str],
        routines_hint: str,
        parent: QWidget | None = None,
    ) -> None:
        title = t("Tetikleyiciyi düzenle") if existing else t("Tetikleyici ekle")
        super().__init__(title, 500, parent)
        self._others = others
        self.result_trigger: TriggerConfig | None = None
        self._name_touched = existing is not None
        self.setWindowTitle(title)
        layout = self.form
        self._errors: dict[str, QLabel] = {}

        self.name = QLineEdit(existing.name if existing else "")
        self.name.setMaxLength(32)
        self.name.setAccessibleName(t("Ad"))
        self.name.textEdited.connect(self._name_edited)
        self._add(layout, t("Ad"), self.name, "name", t("Harf, rakam, _ ya da -"))

        self.folder = QLineEdit(existing.folder if existing else "")
        self.folder.setReadOnly(True)
        self.folder.setPlaceholderText(t("Bir klasör seç"))
        self.folder.setAccessibleName(t("Klasör"))
        self.browse = QPushButton(t("Seç…"))
        self.browse.clicked.connect(self._browse)
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(self.folder, 1)
        row.addWidget(self.browse)
        self._add(layout, t("Klasör"), row, "folder", help=t(
            "İzlenecek klasör. Klasör yoklanır; ilk bakışta var olan dosyalar bildirilmez, yalnız "
            "sonradan gelenler bildirilir. Hızlı düğmeler sık klasörleri seçer."
        ))
        quick = QHBoxLayout()
        quick.setSpacing(8)
        self.quick_buttons: list[QPushButton] = []
        for caption, location in (
            (t("İndirilenler"), QStandardPaths.StandardLocation.DownloadLocation),
            (t("Masaüstü"), QStandardPaths.StandardLocation.DesktopLocation),
            (t("Belgeler"), QStandardPaths.StandardLocation.DocumentsLocation),
        ):
            path = QStandardPaths.writableLocation(location)
            button = QPushButton(caption)
            button.setProperty("folder", path)
            button.setEnabled(bool(path) and Path(path).is_dir())
            button.clicked.connect(self._quick_folder)
            quick.addWidget(button)
            self.quick_buttons.append(button)
        quick.addStretch(1)
        layout.addLayout(quick)

        self.kind = QComboBox()
        self.kind.setAccessibleName(t("Dosya türü"))
        for caption, glob in PATTERN_PRESETS:
            self.kind.addItem(t(caption), glob)
        self.kind.addItem(t("Özel"), None)
        self.pattern = QLineEdit()
        self.pattern.setPlaceholderText(t("Örn. rapor*.pdf"))
        self.pattern.setAccessibleName(t("Özel dosya türü"))
        current = existing.pattern if existing else "*"
        preset = self.kind.findData(current)
        self.kind.setCurrentIndex(preset if preset >= 0 else self.kind.count() - 1)
        if preset < 0:
            self.pattern.setText(current)
        self.kind.currentIndexChanged.connect(self._kind_changed)
        box = QVBoxLayout()
        box.setSpacing(8)
        box.addWidget(self.kind)
        box.addWidget(self.pattern)
        self._add(
            layout, t("Dosya türü"), box, "pattern",
            t("Özelde tek desen yazılır: * her şeyi, ? tek harfi tutar."),
        )
        self._kind_changed()

        self.message = QLineEdit(existing.message if existing else t(DEFAULT_MESSAGE))
        self.message.setAccessibleName(t("Bildirim metni"))
        self._add(
            layout, t("Bildirim metni"), self.message, "message", t("{file} dosyanın adıdır."),
        )

        label = QLabel(t("Düğme"))
        label.setObjectName("fieldLabel")
        layout.addLayout(with_help(label, t(
            "Bildirime eklenecek düğme. Yok: yalnız haber verir. Öneri yaz: metni sohbet kutusuna "
            "yazar, göndermez. Rutin çalıştır: düğmeye basınca seçilen rutini çalıştırır."
        )))
        self.none_radio = QRadioButton(t("Yok"))
        self.suggest_radio = QRadioButton(t("Öneri yaz"))
        self.routine_radio = QRadioButton(t("Rutin çalıştır"))
        group = QButtonGroup(self)
        for radio in (self.none_radio, self.suggest_radio, self.routine_radio):
            group.addButton(radio)
            layout.addWidget(radio)
        self.suggest = QLineEdit(
            existing.suggest if existing and existing.suggest else t(DEFAULT_SUGGEST)
        )
        self.suggest.setAccessibleName(t("Öneri metni"))
        self._add(
            layout, "", self.suggest, "suggest",
            t("{path} dosyanın tam yoludur. Sohbet kutusuna yazılır, gönderilmez."),
        )
        self.routine = QComboBox()
        self.routine.setAccessibleName(t("Rutin"))
        names = list(routines)
        if existing and existing.routine and existing.routine not in names:
            names.append(existing.routine)  # kayıttan silinmiş olsa da sessizce düşmesin
        self.routine.addItems(names)
        if existing and existing.routine:
            self.routine.setCurrentText(existing.routine)
        self._add(layout, "", self.routine, "routine", None if names else routines_hint)
        self.routine_radio.setEnabled(bool(names))
        group.buttonToggled.connect(self._button_changed)
        (self.suggest_radio if existing and existing.suggest else
         self.routine_radio if existing and existing.routine else self.none_radio).setChecked(True)
        self._button_changed()

        self.add_buttons(t("Kaydet") if existing else t("Ekle"))

    def _add(self, layout: QVBoxLayout, caption: str, control: QWidget | QHBoxLayout | QVBoxLayout,
             key: str, hint: str | None = None, help: str = "") -> None:  # noqa: A002
        if caption:
            label = QLabel(caption)
            label.setObjectName("fieldLabel")
            layout.addLayout(with_help(label, help)) if help else layout.addWidget(label)
        if isinstance(control, QWidget):
            layout.addWidget(control)
        else:
            layout.addLayout(control)
        if hint:
            note = QLabel(hint)
            note.setObjectName("meta")
            note.setWordWrap(True)
            layout.addWidget(note)
        error = QLabel("")
        error.setObjectName("warnNote")
        error.setWordWrap(True)
        error.hide()
        layout.addWidget(error)
        self._errors[key] = error

    def _taken(self) -> list[str]:
        return [item.name for item in self._others]

    def _name_edited(self, _text: str) -> None:
        self._name_touched = True

    def _set_folder(self, folder: str) -> None:
        self.folder.setText(folder)
        if not self._name_touched:
            self.name.setText(slug_name(Path(folder).name, self._taken()))

    def _browse(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, t("Klasör seç"), self.folder.text())
        if chosen:
            self._set_folder(chosen)

    def _quick_folder(self) -> None:
        sender = self.sender()
        if sender is not None:
            self._set_folder(str(sender.property("folder")))

    def _kind_changed(self, *_args: object) -> None:
        self.pattern.setVisible(self.kind.currentData() is None)

    def _button_changed(self, *_args: object) -> None:
        self.suggest.setEnabled(self.suggest_radio.isChecked())
        self.routine.setEnabled(self.routine_radio.isChecked())

    def submit(self) -> None:
        glob = self.kind.currentData()
        item, errors = validate_trigger(
            name=self.name.text(),
            folder=self.folder.text(),
            pattern=self.pattern.text() if glob is None else str(glob),
            message=self.message.text(),
            suggest=self.suggest.text().strip() if self.suggest_radio.isChecked() else None,
            routine=self.routine.currentText() if self.routine_radio.isChecked() else None,
            taken=self._taken(),
            count=len(self._others),
        )
        for key, label in self._errors.items():
            label.setText(errors.get(key, ""))
            label.setVisible(key in errors)
        if item is None:
            return
        self.result_trigger = item
        self.accept()
