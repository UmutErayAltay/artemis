"""Tetikleyici ekleme/düzenleme penceresi (ADR 0061, ADR 0086).

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
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from artemis.channels.desktop.dialog_shell import FormDialog
from artemis.channels.desktop.widgets import with_help
from artemis.config.triggers import (
    DEFAULT_TRIGGER_MESSAGES,
    MAX_TRIGGERS,
    TriggerConfig,
    TriggerKind,
)
from artemis.i18n import t

DEFAULT_MESSAGE = DEFAULT_TRIGGER_MESSAGES["folder"]
DEFAULT_SUGGEST = "Şu dosyayı özetle: {path}"
DEFAULT_SUGGEST_BY_KIND: dict[TriggerKind, str] = {
    "folder": "Şu dosyayı özetle: {path}",
    "app_opened": "{app} açıldı; çalışma bağlamını hazırla",
    "calendar_before": "{title} toplantısı için hazırlık notlarını getir",
    "system_resume": "Bekleyen özet ve hatırlatmaları göster",
}
_MESSAGE_HINTS: dict[TriggerKind, str] = {
    "folder": "{file} dosyanın adıdır.",
    "app_opened": "{app} açılan uygulamanın adıdır.",
    "calendar_before": "{title} etkinlik adı, {minutes} kalan dakika, {location} konumdur.",
    "system_resume": "{name} tetikleyici adıdır.",
}
_SUGGEST_HINTS: dict[TriggerKind, str] = {
    "folder": "{path} dosyanın tam yoludur. Sohbet kutusuna yazılır, gönderilmez.",
    "app_opened": "Sohbet kutusuna yazılır, gönderilmez ({app} kullanılabilir).",
    "calendar_before": "Sohbet kutusuna yazılır, gönderilmez ({title}, {minutes} kullanılabilir).",
    "system_resume": "Sohbet kutusuna yazılır, gönderilmez.",
}
# Tarayıcı tek bir fnmatch deseni kullanır (`;` ile çoklu desen yok); yalnız o sunulur.
PATTERN_PRESETS = (
    ("Tümü (*)", "*"),
    ("PDF (*.pdf)", "*.pdf"),
    ("Word (*.docx)", "*.docx"),
    ("Excel (*.xlsx)", "*.xlsx"),
    ("Arşiv (*.zip)", "*.zip"),
)
TRIGGER_KIND_CHOICES: tuple[tuple[str, TriggerKind], ...] = (
    ("Klasöre yeni dosya", "folder"),
    ("Uygulama açılınca", "app_opened"),
    ("Takvim toplantısı öncesi", "calendar_before"),
    ("Sistem uykudan uyanınca", "system_resume"),
)
_TR = str.maketrans("çÇğĞıİöÖşŞüÜ", "cCgGiIoOsSuU")
_FIELD_ERRORS = {
    "name": "Ad 1-32 harf, rakam, _ ya da - olmalı.",
    "folder": "Geçerli bir klasör seç.",
    "pattern": "Dosya türü boş olamaz (en çok 100 karakter).",
    "app": "Uygulama veya süreç adı boş olamaz (en çok 120 karakter).",
    "minutes_before": "Dakika değeri 1-120 arasında olmalı.",
    "title_filter": "Başlık süzgeci en çok 100 karakter olmalı.",
    "cooldown_minutes": "Bekleme süresi 1-1440 dakika arasında olmalı.",
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
    kind: TriggerKind = "folder",
    folder: str = "",
    pattern: str = "*",
    app: str = "",
    minutes_before: int = 10,
    title_filter: str = "",
    cooldown_minutes: int = 30,
    message: str = DEFAULT_MESSAGE,
    suggest: str | None = None,
    routine: str | None = None,
    taken: Iterable[str] = (),
    count: int = 0,
) -> tuple[TriggerConfig | None, dict[str, str]]:
    """(tetikleyici, {alan: Türkçe hata}). `taken` düzenlenen tetikleyici HARİÇ mevcut adlar,
    `count` düzenlenen HARİÇ mevcut sayıdır."""
    errors: dict[str, str] = {}
    name = name.strip()
    folder = folder.strip()
    pattern = pattern.strip()
    app = app.strip()
    title_filter = title_filter.strip()
    if count >= MAX_TRIGGERS:
        errors["name"] = t("En çok {max_triggers} tetikleyici olabilir.").format(
            max_triggers=MAX_TRIGGERS
        )
    elif name.casefold() in {n.casefold() for n in taken}:
        errors["name"] = t("Bu ad zaten kullanılıyor.")
    if kind == "folder":
        if not folder:
            errors["folder"] = t(_FIELD_ERRORS["folder"])
        elif not Path(folder).is_absolute():
            errors["folder"] = t("Klasör tam yol olmalı.")
        elif not Path(folder).is_dir():
            errors["folder"] = t("Klasör bulunamadı.")
        if ";" in pattern or "/" in pattern or "\\" in pattern:
            errors["pattern"] = t(
                "Tek bir dosya adı deseni yaz; ; / \\ kullanılamaz. "
                "Başka tür için ayrı tetikleyici ekle."
            )
    else:
        folder = ""
        pattern = "*"
    if kind == "app_opened" and not app:
        errors["app"] = t("Uygulama veya süreç adı gir (ör. code.exe).")
    if suggest is not None and routine is not None:
        errors["routine"] = t("Düğme ya öneri ya rutin olur, ikisi birden olmaz.")
    if errors:
        return None, errors
    try:
        item = TriggerConfig(
            name=name,
            kind=kind,
            folder=folder,
            pattern=pattern,
            app=app,
            minutes_before=minutes_before,
            title_filter=title_filter,
            cooldown_minutes=cooldown_minutes,
            message=message,
            suggest=suggest,
            routine=routine,
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

        self.trigger_kind = QComboBox()
        self.trigger_kind.setAccessibleName(t("Tetikleyici türü"))
        for caption, kind_id in TRIGGER_KIND_CHOICES:
            self.trigger_kind.addItem(t(caption), kind_id)
        initial_kind: TriggerKind = existing.kind if existing else "folder"
        kind_idx = self.trigger_kind.findData(initial_kind)
        self.trigger_kind.setCurrentIndex(max(0, kind_idx))
        self.trigger_kind.currentIndexChanged.connect(self._trigger_kind_changed)
        self._add(layout, t("Tetikleyici türü"), self.trigger_kind, "trigger_kind")

        self._folder_group = QWidget()
        f_lay = QVBoxLayout(self._folder_group)
        f_lay.setContentsMargins(0, 0, 0, 0)
        f_lay.setSpacing(12)
        self._build_folder_fields(f_lay, existing)
        layout.addWidget(self._folder_group)

        self._app_group = QWidget()
        a_lay = QVBoxLayout(self._app_group)
        a_lay.setContentsMargins(0, 0, 0, 0)
        a_lay.setSpacing(12)
        self.app_input = QLineEdit(existing.app if existing else "")
        self.app_input.setMaxLength(120)
        self.app_input.setPlaceholderText(t("Örn. code.exe, notepad.exe"))
        self.app_input.setAccessibleName(t("Uygulama adı"))
        self._add(
            a_lay,
            t("Uygulama veya süreç adı"),
            self.app_input,
            "app",
            t("Ön planda açıldığında bir kez bildirilir; açılışta zaten açıksa bildirilmez."),
        )
        layout.addWidget(self._app_group)

        self._calendar_group = QWidget()
        c_lay = QVBoxLayout(self._calendar_group)
        c_lay.setContentsMargins(0, 0, 0, 0)
        c_lay.setSpacing(12)
        self.minutes_before = QSpinBox()
        self.minutes_before.setRange(1, 120)
        self.minutes_before.setValue(existing.minutes_before if existing else 10)
        self.minutes_before.setSuffix(" " + t("dk"))
        self.minutes_before.setAccessibleName(t("Kaç dakika önce"))
        self._add(
            c_lay, t("Toplantıdan kaç dakika önce"), self.minutes_before, "minutes_before"
        )
        self.title_filter = QLineEdit(existing.title_filter if existing else "")
        self.title_filter.setMaxLength(100)
        self.title_filter.setPlaceholderText(t("Örn. Standup (boşsa tüm toplantılar)"))
        self.title_filter.setAccessibleName(t("Başlık süzgeci"))
        self._add(c_lay, t("Başlık süzgeci (isteğe bağlı)"), self.title_filter, "title_filter")
        layout.addWidget(self._calendar_group)

        self._cooldown_group = QWidget()
        cd_lay = QVBoxLayout(self._cooldown_group)
        cd_lay.setContentsMargins(0, 0, 0, 0)
        cd_lay.setSpacing(12)
        self.cooldown_minutes = QSpinBox()
        self.cooldown_minutes.setRange(1, 1440)
        self.cooldown_minutes.setValue(existing.cooldown_minutes if existing else 30)
        self.cooldown_minutes.setSuffix(" " + t("dk"))
        self.cooldown_minutes.setAccessibleName(t("Bekleme süresi"))
        self._add(
            cd_lay,
            t("Yeniden bildirme bekleme süresi"),
            self.cooldown_minutes,
            "cooldown_minutes",
            t("Aynı olay bu süre dolmadan tekrar bildirilmez."),
        )
        layout.addWidget(self._cooldown_group)

        self.message = QLineEdit(existing.message if existing else t(DEFAULT_MESSAGE))
        self.message.setAccessibleName(t("Bildirim metni"))
        self._message_hint = self._add(
            layout, t("Bildirim metni"), self.message, "message", t(_MESSAGE_HINTS[initial_kind]),
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
        self._suggest_hint = self._add(
            layout, "", self.suggest, "suggest", t(_SUGGEST_HINTS[initial_kind]),
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
        self._sync_kind_visibility(initial_kind, update_defaults=False)

        self.add_buttons(t("Kaydet") if existing else t("Ekle"))

    def _build_folder_fields(self, layout: QVBoxLayout, existing: TriggerConfig | None) -> None:
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

    def _add(self, layout: QVBoxLayout, caption: str, control: QWidget | QHBoxLayout | QVBoxLayout,
             key: str, hint: str | None = None, help: str = "") -> QLabel | None:  # noqa: A002
        if caption:
            label = QLabel(caption)
            label.setObjectName("fieldLabel")
            layout.addLayout(with_help(label, help)) if help else layout.addWidget(label)
        if isinstance(control, QWidget):
            layout.addWidget(control)
        else:
            layout.addLayout(control)
        note: QLabel | None = None
        if hint is not None:
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
        return note

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

    def _current_trigger_kind(self) -> TriggerKind:
        data = self.trigger_kind.currentData()
        if data in {"folder", "app_opened", "calendar_before", "system_resume"}:
            return data  # type: ignore[return-value]
        return "folder"

    def _trigger_kind_changed(self, *_args: object) -> None:
        self._sync_kind_visibility(self._current_trigger_kind(), update_defaults=True)

    def _sync_kind_visibility(self, kind: TriggerKind, *, update_defaults: bool) -> None:
        self._folder_group.setVisible(kind == "folder")
        self._app_group.setVisible(kind == "app_opened")
        self._calendar_group.setVisible(kind == "calendar_before")
        self._cooldown_group.setVisible(kind in {"app_opened", "system_resume"})
        if self._message_hint is not None:
            self._message_hint.setText(t(_MESSAGE_HINTS[kind]))
        if self._suggest_hint is not None:
            self._suggest_hint.setText(t(_SUGGEST_HINTS[kind]))
        if update_defaults:
            known_msgs = {DEFAULT_TRIGGER_MESSAGES[k] for k in DEFAULT_TRIGGER_MESSAGES} | {
                t(DEFAULT_TRIGGER_MESSAGES[k]) for k in DEFAULT_TRIGGER_MESSAGES
            }
            if not self.message.text().strip() or self.message.text().strip() in known_msgs:
                self.message.setText(t(DEFAULT_TRIGGER_MESSAGES[kind]))
            known_sug = {DEFAULT_SUGGEST_BY_KIND[k] for k in DEFAULT_SUGGEST_BY_KIND} | {
                t(DEFAULT_SUGGEST_BY_KIND[k]) for k in DEFAULT_SUGGEST_BY_KIND
            }
            if not self.suggest.text().strip() or self.suggest.text().strip() in known_sug:
                self.suggest.setText(t(DEFAULT_SUGGEST_BY_KIND[kind]))

    def _button_changed(self, *_args: object) -> None:
        self.suggest.setEnabled(self.suggest_radio.isChecked())
        self.routine.setEnabled(self.routine_radio.isChecked())

    def submit(self) -> None:
        glob = self.kind.currentData()
        kind = self._current_trigger_kind()
        item, errors = validate_trigger(
            name=self.name.text(),
            kind=kind,
            folder=self.folder.text(),
            pattern=self.pattern.text() if glob is None else str(glob),
            app=self.app_input.text(),
            minutes_before=self.minutes_before.value(),
            title_filter=self.title_filter.text(),
            cooldown_minutes=self.cooldown_minutes.value(),
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
