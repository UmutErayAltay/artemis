"""Koşullu rutin öneri zinciri ekleme/düzenleme penceresi (ADR 0086).

Yalnız Ayarlar > Tetikleyiciler altından kullanıcı eylemiyle açılır; hiçbir araç ya da
model öneri zinciri ekleyemez. Doğrulamanın tek kaynağı `SuggestionChainConfig`tir.
"""

from __future__ import annotations

from collections.abc import Iterable

from pydantic import ValidationError
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from artemis.channels.desktop.dialog_shell import FormDialog
from artemis.channels.desktop.widgets import with_help
from artemis.config.triggers import MAX_CHAINS, ChainWorkProfile, SuggestionChainConfig
from artemis.config.work_profile import PROFILE_LABELS, WORK_PROFILES
from artemis.i18n import t

DEFAULT_CHAIN_MESSAGE = "Sıradaki adım önerisi: {source}"

_CHAIN_ERRORS = {
    "name": "Ad 1-32 harf, rakam, _ ya da - olmalı.",
    "after_routine": "Bir kaynak rutin veya kaynak tetikleyici seç.",
    "time_range": "Saat aralığı SS:DD-SS:DD biçiminde olmalı (ör. 09:00-18:00).",
    "message": "Bildirim metni 1-300 karakter olmalı.",
    "suggest": "Öneri metni 1-500 karakter olmalı.",
    "routine": "Bir rutin seç.",
}


def validate_chain(
    *,
    name: str,
    enabled: bool = True,
    after_routine: str = "",
    after_trigger: str = "",
    work_profiles: tuple[ChainWorkProfile, ...] = (),
    active_app: str = "",
    time_range: str = "",
    cooldown_minutes: int = 60,
    message: str = DEFAULT_CHAIN_MESSAGE,
    suggest: str | None = None,
    routine: str | None = None,
    taken: Iterable[str] = (),
    count: int = 0,
) -> tuple[SuggestionChainConfig | None, dict[str, str]]:
    """(zincir, {alan: Türkçe hata})."""
    errors: dict[str, str] = {}
    name = name.strip()
    after_routine = after_routine.strip()
    after_trigger = after_trigger.strip()
    active_app = active_app.strip()
    time_range = time_range.strip()
    if count >= MAX_CHAINS:
        errors["name"] = t("En çok {max_chains} öneri zinciri olabilir.").format(
            max_chains=MAX_CHAINS
        )
    elif name.casefold() in {n.casefold() for n in taken}:
        errors["name"] = t("Bu ad zaten kullanılıyor.")
    if not after_routine and not after_trigger:
        errors["after_routine"] = t("Bir kaynak rutin veya kaynak tetikleyici seç.")
    if suggest is not None and routine is not None:
        errors["routine"] = t("Düğme ya öneri ya rutin olur, ikisi birden olmaz.")
    if (
        after_routine
        and routine is not None
        and after_routine.casefold() == routine.strip().casefold()
    ):
        errors["routine"] = t("Zincir aynı rutini doğrudan yeniden öneremez.")
    if errors:
        return None, errors
    try:
        item = SuggestionChainConfig(
            name=name,
            enabled=enabled,
            after_routine=after_routine,
            after_trigger=after_trigger,
            work_profiles=work_profiles,
            active_app=active_app,
            time_range=time_range,
            cooldown_minutes=cooldown_minutes,
            message=message,
            suggest=suggest,
            routine=routine,
        )
    except ValidationError as error:
        for detail in error.errors():
            key = str(detail["loc"][0]) if detail["loc"] else "name"
            key = key if key in _CHAIN_ERRORS else "name"
            errors.setdefault(key, t(_CHAIN_ERRORS[key]))
        return None, errors
    return item, {}


class ChainDialog(FormDialog):
    def __init__(
        self,
        existing: SuggestionChainConfig | None,
        others: list[SuggestionChainConfig],
        routines: list[str],
        triggers: list[str],
        routines_hint: str,
        parent: QWidget | None = None,
    ) -> None:
        title = t("Öneri zincirini düzenle") if existing else t("Öneri zinciri ekle")
        super().__init__(title, 500, parent)
        self._others = others
        self.result_chain: SuggestionChainConfig | None = None
        self.setWindowTitle(title)
        layout = self.form
        self._errors: dict[str, QLabel] = {}

        self.name = QLineEdit(existing.name if existing else "")
        self.name.setMaxLength(32)
        self.name.setAccessibleName(t("Ad"))
        self._add(layout, t("Ad"), self.name, "name", t("Harf, rakam, _ ya da -"))

        self.enabled_box = QCheckBox(t("Zincir etkin"))
        self.enabled_box.setChecked(existing.enabled if existing else True)
        layout.addWidget(self.enabled_box)

        self.after_routine = QComboBox()
        self.after_routine.setAccessibleName(t("Rutin bitince"))
        self.after_routine.addItem(t("(yok)"), "")
        r_names = list(routines)
        if existing and existing.after_routine and existing.after_routine not in r_names:
            r_names.append(existing.after_routine)
        for rname in r_names:
            self.after_routine.addItem(rname, rname)
        if existing and existing.after_routine:
            idx = self.after_routine.findData(existing.after_routine)
            if idx >= 0:
                self.after_routine.setCurrentIndex(idx)
        self._add(layout, t("Rutin bitince"), self.after_routine, "after_routine")

        self.after_trigger = QComboBox()
        self.after_trigger.setAccessibleName(t("Tetikleyici sonrası"))
        self.after_trigger.addItem(t("(yok)"), "")
        t_names = list(triggers)
        if existing and existing.after_trigger and existing.after_trigger not in t_names:
            t_names.append(existing.after_trigger)
        for tname in t_names:
            self.after_trigger.addItem(tname, tname)
        if existing and existing.after_trigger:
            idx = self.after_trigger.findData(existing.after_trigger)
            if idx >= 0:
                self.after_trigger.setCurrentIndex(idx)
        self._add(layout, t("Tetikleyici sonrası"), self.after_trigger, "after_trigger")

        prof_label = QLabel(t("Çalışma profilleri (boşsa tümü)"))
        prof_label.setObjectName("fieldLabel")
        layout.addWidget(prof_label)
        prof_row = QHBoxLayout()
        prof_row.setSpacing(8)
        self.profile_boxes: dict[ChainWorkProfile, QCheckBox] = {}
        selected_profiles = set(existing.work_profiles) if existing else set()
        for prof in WORK_PROFILES:
            box = QCheckBox(t(PROFILE_LABELS[prof]))
            box.setChecked(prof in selected_profiles)
            self.profile_boxes[prof] = box
            prof_row.addWidget(box)
        prof_row.addStretch(1)
        layout.addLayout(prof_row)

        self.active_app = QLineEdit(existing.active_app if existing else "")
        self.active_app.setMaxLength(120)
        self.active_app.setPlaceholderText(t("Örn. code.exe (isteğe bağlı)"))
        self.active_app.setAccessibleName(t("Etkin uygulama"))
        self._add(layout, t("Etkin uygulama koşulu"), self.active_app, "active_app")

        self.time_range = QLineEdit(existing.time_range if existing else "")
        self.time_range.setMaxLength(11)
        self.time_range.setPlaceholderText(t("Örn. 09:00-18:00 (isteğe bağlı)"))
        self.time_range.setAccessibleName(t("Saat aralığı"))
        self._add(layout, t("Saat aralığı"), self.time_range, "time_range")

        self.cooldown_minutes = QSpinBox()
        self.cooldown_minutes.setRange(1, 1440)
        self.cooldown_minutes.setValue(existing.cooldown_minutes if existing else 60)
        self.cooldown_minutes.setSuffix(" " + t("dk"))
        self.cooldown_minutes.setAccessibleName(t("Bekleme süresi"))
        self._add(layout, t("Bekleme süresi"), self.cooldown_minutes, "cooldown_minutes")

        self.message = QLineEdit(existing.message if existing else t(DEFAULT_CHAIN_MESSAGE))
        self.message.setAccessibleName(t("Bildirim metni"))
        self._add(
            layout,
            t("Bildirim metni"),
            self.message,
            "message",
            t("{source} tetikleyici veya rutin adıdır."),
        )

        btn_label = QLabel(t("Düğme"))
        btn_label.setObjectName("fieldLabel")
        layout.addLayout(
            with_help(
                btn_label,
                t(
                    "Koşullar sağlanınca yalnız bu öneri gösterilir; tıklamadan hiçbir rutin "
                    "kendiliğinden çalışmaz."
                ),
            )
        )
        self.none_radio = QRadioButton(t("Yok"))
        self.suggest_radio = QRadioButton(t("Öneri yaz"))
        self.routine_radio = QRadioButton(t("Rutin çalıştır"))
        group = QButtonGroup(self)
        for radio in (self.none_radio, self.suggest_radio, self.routine_radio):
            group.addButton(radio)
            layout.addWidget(radio)
        self.suggest = QLineEdit(
            existing.suggest if existing and existing.suggest else t("Sıradaki adımı hazırla")
        )
        self.suggest.setAccessibleName(t("Öneri metni"))
        self._add(layout, "", self.suggest, "suggest")
        self.routine = QComboBox()
        self.routine.setAccessibleName(t("Rutin"))
        names = list(routines)
        if existing and existing.routine and existing.routine not in names:
            names.append(existing.routine)
        self.routine.addItems(names)
        if existing and existing.routine:
            self.routine.setCurrentText(existing.routine)
        self._add(layout, "", self.routine, "routine", None if names else routines_hint)
        self.routine_radio.setEnabled(bool(names))
        group.buttonToggled.connect(self._button_changed)
        (
            self.suggest_radio
            if existing and existing.suggest
            else self.routine_radio
            if existing and existing.routine
            else self.none_radio
        ).setChecked(True)
        self._button_changed()
        self.add_buttons(t("Kaydet") if existing else t("Ekle"))

    def _add(
        self,
        layout: QVBoxLayout,
        caption: str,
        control: QWidget,
        key: str,
        hint: str | None = None,
    ) -> None:
        if caption:
            label = QLabel(caption)
            label.setObjectName("fieldLabel")
            layout.addWidget(label)
        layout.addWidget(control)
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

    def _button_changed(self, *_args: object) -> None:
        self.suggest.setEnabled(self.suggest_radio.isChecked())
        self.routine.setEnabled(self.routine_radio.isChecked())

    def submit(self) -> None:
        selected_profiles = tuple(
            prof for prof, box in self.profile_boxes.items() if box.isChecked()
        )
        item, errors = validate_chain(
            name=self.name.text(),
            enabled=self.enabled_box.isChecked(),
            after_routine=str(self.after_routine.currentData() or ""),
            after_trigger=str(self.after_trigger.currentData() or ""),
            work_profiles=selected_profiles,
            active_app=self.active_app.text(),
            time_range=self.time_range.text(),
            cooldown_minutes=self.cooldown_minutes.value(),
            message=self.message.text(),
            suggest=self.suggest.text().strip() if self.suggest_radio.isChecked() else None,
            routine=self.routine.currentText() if self.routine_radio.isChecked() else None,
            taken=[item.name for item in self._others],
            count=len(self._others),
        )
        for key, label in self._errors.items():
            label.setText(errors.get(key, ""))
            label.setVisible(key in errors)
        if item is None:
            return
        self.result_chain = item
        self.accept()
