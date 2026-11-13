"""Rutin ekleme/düzenleme ve ham dosya düzeltme pencereleri (ADR 0058 Ek karar).

Yalnız Ayarlar > Rutinler'den, kullanıcı tıklamasıyla açılır; rutini hiçbir araç ya da model bu
yoldan yazamaz. Doğrulamanın tek kaynağı `validate_steps`tir (`routine_save` ve çalıştırıcıyla
aynı fonksiyon): form yalnız hataları gösterir ve geçersizken Kaydet'i kapatır. Argüman formu
aracın kendi `args_model` JSON şemasından üretilir; basit alanlar tür-doğru değer (bool gerçek
bool, sayı gerçek sayı) verir, karmaşık alanlar JSON kutusunda kalır.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError
from PySide6.QtCore import Signal
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from artemis.channels.desktop.dialog_shell import FormDialog
from artemis.channels.desktop.tool_titles import arg_label, tool_display, tool_summary
from artemis.channels.desktop.widgets import with_help
from artemis.proactive.routines import (
    FORBIDDEN_PREFIX,
    MAX_ROUTINES,
    MAX_STEPS,
    Routine,
    RoutineFile,
    RoutinesError,
    RoutineStep,
    RoutineStore,
    name_key,
    validate_steps,
)
from artemis.tools.registry import ToolRegistry

_MISSING = object()  # "alan hiç gönderilmesin" (isteğe bağlı ve boş)
_INT_LIMIT = 2_147_483_647  # QSpinBox int32 sınırı
_EMPTY_CAPTION = "(boş)"
_NAME_PROBLEM = "Ad 1-40 karakter olmalı: harf, rakam, boşluk, _ ya da -."


def _resolve(schema: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
    """`$ref` ve `Optional` (anyOf + null) sarmalını açar; tek somut şema döner."""
    if "$ref" in schema:
        target = defs.get(str(schema["$ref"]).rsplit("/", 1)[-1], {})
        schema = {**target, **{k: v for k, v in schema.items() if k != "$ref"}}
    options = schema.get("anyOf")
    if isinstance(options, list):
        concrete = [_resolve(o, defs) for o in options if o.get("type") != "null"]
        if len(concrete) == 1:
            extra = {k: v for k, v in schema.items() if k != "anyOf"}
            return {**concrete[0], **extra}
    return schema


class _ArgField(QWidget):
    """Tek argüman: etiket + "?" + türüne uygun denetim. `value()` gönderilecek değeri verir."""

    touched = Signal()

    def __init__(
        self, name: str, schema: dict[str, Any], required: bool, defs: dict[str, Any],
        initial: Any = _MISSING,
    ) -> None:
        super().__init__()
        self.name = name
        self._required = required
        schema = _resolve(schema, defs)
        default = schema.get("default", _MISSING)
        start = initial if initial is not _MISSING else default
        self._default = default
        self._choices: list[Any] = []
        self._widget: QWidget
        if "enum" in schema or "const" in schema:
            self._kind = "enum"
            self._choices = list(schema.get("enum", [schema.get("const")]))
            combo = QComboBox()
            if not required:
                combo.addItem(_EMPTY_CAPTION)
            combo.addItems([str(choice) for choice in self._choices])
            if start is not _MISSING and start in self._choices:
                combo.setCurrentIndex(self._choices.index(start) + (0 if required else 1))
            combo.currentIndexChanged.connect(self._changed)
            self._widget = combo
        elif schema.get("type") == "boolean":
            self._kind = "bool"
            box = QCheckBox()
            box.setChecked(start is True)
            box.toggled.connect(self._changed)
            self._widget = box
        elif schema.get("type") in {"integer", "number"}:
            self._kind = "int" if schema["type"] == "integer" else "number"
            self._widget = self._spin(schema, start)
        elif schema.get("type") == "string":
            self._kind = "string"
            line = QLineEdit("" if start is _MISSING or start is None else str(start))
            line.setMaxLength(min(int(schema.get("maxLength", 32767)), 32767))
            line.setMinimumWidth(320)
            line.textChanged.connect(self._changed)
            self._widget = line
        else:
            self._kind = "json"
            edit = QPlainTextEdit()
            edit.setMinimumHeight(72)
            edit.setMaximumHeight(140)
            edit.setPlaceholderText("JSON (örn. [\"a\", \"b\"] ya da {\"anahtar\": 1})")
            if start is not _MISSING:
                edit.setPlainText(json.dumps(start, ensure_ascii=False, indent=2))
            edit.textChanged.connect(self._changed)
            self._widget = edit
        label = arg_label(name)
        self._widget.setAccessibleName(label)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        caption = QLabel(f"{label} *" if required else label)
        caption.setObjectName("fieldLabel")
        layout.addWidget(caption)
        layout.addWidget(self._widget)

    def _spin(self, schema: dict[str, Any], start: Any) -> QWidget:
        integer = self._kind == "int"
        low = schema.get("minimum", schema.get("exclusiveMinimum", -_INT_LIMIT))
        high = schema.get("maximum", schema.get("exclusiveMaximum", _INT_LIMIT))
        if "exclusiveMinimum" in schema and "minimum" not in schema and integer:
            low += 1
        if "exclusiveMaximum" in schema and "maximum" not in schema and integer:
            high -= 1
        low, high = max(low, -_INT_LIMIT), min(high, _INT_LIMIT)
        spin: QSpinBox | QDoubleSpinBox
        if integer:
            spin = QSpinBox()
            spin.setRange(int(low) - (0 if self._required else 1), int(high))
        else:
            spin = QDoubleSpinBox()
            spin.setDecimals(4)
            spin.setRange(float(low) - (0 if self._required else 1), float(high))
        if not self._required:
            spin.setSpecialValueText(_EMPTY_CAPTION)  # en küçük değer = "gönderme"
        given = isinstance(start, (int, float)) and not isinstance(start, bool)
        if isinstance(spin, QSpinBox):
            spin.setValue(int(start) if given else spin.minimum())
        else:
            spin.setValue(float(start) if given else spin.minimum())
        spin.valueChanged.connect(self._changed)
        return spin

    def _changed(self, *_args: object) -> None:
        self.touched.emit()

    def value(self) -> Any:
        """Gönderilecek değer; gönderilmeyecekse `_MISSING`. Geçersiz JSON → ValueError."""
        widget = self._widget
        if isinstance(widget, QComboBox):
            index = widget.currentIndex() - (0 if self._required else 1)
            return self._choices[index] if index >= 0 else _MISSING
        if isinstance(widget, QCheckBox):
            checked = widget.isChecked()
            return checked if self._required or checked != (self._default is True) else _MISSING
        if isinstance(widget, (QSpinBox, QDoubleSpinBox)):
            if not self._required and widget.value() == widget.minimum():
                return _MISSING
            return widget.value()
        if isinstance(widget, QLineEdit):
            text = widget.text()
            return text if text or self._required else _MISSING
        text = widget.toPlainText().strip() if isinstance(widget, QPlainTextEdit) else ""
        if not text:
            return _MISSING
        try:
            return json.loads(text)
        except ValueError as error:
            raise ValueError(f"{arg_label(self.name)} alanı geçerli JSON değil.") from error


class _StepEditor(QFrame):
    changed = Signal()
    move_requested = Signal(object, int)
    remove_requested = Signal(object)

    def __init__(self, registry: ToolRegistry, tools: list[str], step: RoutineStep | None) -> None:
        super().__init__()
        self._registry = registry
        self._fields: list[_ArgField] = []
        self._raw: QPlainTextEdit | None = None
        self.setObjectName("card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        head = QHBoxLayout()
        head.setSpacing(6)
        self.title = QLabel("")
        self.title.setObjectName("fieldLabel")
        head.addWidget(self.title, 1)
        self.up_button = QPushButton("↑")
        self.down_button = QPushButton("↓")
        self.remove_button = QPushButton("Kaldır")
        self.up_button.setAccessibleName("Adımı yukarı taşı")
        self.down_button.setAccessibleName("Adımı aşağı taşı")
        for button in (self.up_button, self.down_button, self.remove_button):
            head.addWidget(button)
        layout.addLayout(head)
        self.tool = QComboBox()
        self.tool.setAccessibleName("Araç")
        names = list(tools)
        if step is not None and step.tool not in names:
            names.append(step.tool)  # kalkmış/yasak araç sessizce düşmesin; doğrulama bildirir
        for tool_name in names:  # görünen metin Türkçe ad + kimlik; kimlik itemData'da
            self.tool.addItem(tool_display(tool_name), tool_name)
        if step is not None:
            self.tool.setCurrentIndex(max(0, self.tool.findData(step.tool)))
        layout.addWidget(self.tool)
        self.about = QLabel("")
        self.about.setObjectName("meta")
        self.about.setWordWrap(True)
        layout.addWidget(self.about)
        self.args_holder = QWidget()
        self.args_layout = QVBoxLayout(self.args_holder)
        self.args_layout.setContentsMargins(0, 0, 0, 0)
        self.args_layout.setSpacing(8)
        layout.addWidget(self.args_holder)
        self._build_args(step.arguments if step is not None else {})
        self.tool.currentIndexChanged.connect(self._tool_changed)
        self.up_button.clicked.connect(self._up)
        self.down_button.clicked.connect(self._down)
        self.remove_button.clicked.connect(self._remove)

    def tool_id(self) -> str:
        data = self.tool.currentData()
        return data if isinstance(data, str) else self.tool.currentText()

    def set_position(self, number: int, count: int) -> None:
        self.title.setText(f"Adım {number}")
        self.up_button.setEnabled(number > 1)
        self.down_button.setEnabled(number < count)

    def _up(self) -> None:
        self.move_requested.emit(self, -1)

    def _down(self) -> None:
        self.move_requested.emit(self, 1)

    def _remove(self) -> None:
        self.remove_requested.emit(self)

    def _tool_changed(self, _index: int) -> None:
        self._build_args({})
        self.changed.emit()

    def _touched(self) -> None:
        self.changed.emit()

    def _build_args(self, initial: dict[str, Any]) -> None:
        while self.args_layout.count():
            item = self.args_layout.takeAt(0)
            old = item.widget() if item is not None else None
            if old is not None:
                old.deleteLater()
        self._fields, self._raw = [], None
        tool = self._registry.get(self.tool_id())
        if tool is None:
            self.about.setText("Bu araç kayıtlı değil.")
        else:
            self.about.setText(tool_summary(tool.name) or f"Araç kimliği: {tool.name}.")
        if tool is None:
            # Şema yok: ham argümanlar korunur, doğrulama hatayı söyler.
            self._raw = QPlainTextEdit(json.dumps(initial, ensure_ascii=False, indent=2))
            self._raw.setAccessibleName("Argümanlar (JSON)")
            self._raw.textChanged.connect(self._touched)
            self.args_layout.addWidget(self._raw)
            return
        schema = tool.args_model.model_json_schema()
        required = set(schema.get("required", []))
        defs = schema.get("$defs", {})
        for name, sub in schema.get("properties", {}).items():
            current = initial.get(name, _MISSING)
            field = _ArgField(name, sub, name in required, defs, current)
            field.touched.connect(self._touched)
            self._fields.append(field)
            self.args_layout.addWidget(field)
        if not self._fields:
            note = QLabel("Bu aracın argümanı yok.")
            note.setObjectName("meta")
            self.args_layout.addWidget(note)

    def step(self) -> RoutineStep:
        """Formdaki adım. Geçersiz JSON → ValueError (Türkçe mesaj)."""
        if self._raw is not None:
            try:
                arguments = json.loads(self._raw.toPlainText() or "{}")
            except ValueError as error:
                raise ValueError("Argümanlar geçerli JSON değil.") from error
            if not isinstance(arguments, dict):
                raise ValueError("Argümanlar bir JSON nesnesi olmalı.")
            return RoutineStep(tool=self.tool_id(), arguments=arguments)
        arguments = {}
        for field in self._fields:
            value = field.value()
            if value is not _MISSING:
                arguments[field.name] = value
        return RoutineStep(tool=self.tool_id(), arguments=arguments)


class RoutineDialog(FormDialog):
    def __init__(
        self,
        existing: Routine | None,
        others: list[Routine],
        registry: ToolRegistry,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__("Rutini düzenle" if existing else "Rutin ekle", 520, parent)
        self._existing = existing
        self._others = others
        self._registry = registry
        self.result_routine: Routine | None = None
        self.editors: list[_StepEditor] = []
        self._tools = sorted(
            (d.name for d in registry.definitions() if not d.name.startswith(FORBIDDEN_PREFIX)),
            key=lambda n: (tool_display(n).casefold(), n),
        )
        self.setWindowTitle("Rutini düzenle" if existing else "Rutin ekle")
        self.body_layout = self.form  # gövde FormDialog'un kaydırılan düzeni

        label = QLabel("Ad")
        label.setObjectName("fieldLabel")
        self.name = QLineEdit(existing.name if existing else "")
        self.name.setMaxLength(40)
        self.name.setAccessibleName("Ad")
        self.name.setMinimumWidth(280)
        self.body_layout.addWidget(label)
        self.body_layout.addWidget(self.name)
        steps_label = QLabel("Adımlar")
        steps_label.setObjectName("fieldLabel")
        self.body_layout.addLayout(with_help(
            steps_label,
            f"En çok {MAX_STEPS} adım. Adımlar sırayla, her biri normal onay kurallarıyla "
            "çalışır; onay isteyen adım yine sorar. Bir adım başarısız olursa rutin orada durur.",
        ))
        self.steps_layout = QVBoxLayout()
        self.steps_layout.setSpacing(12)
        self.body_layout.addLayout(self.steps_layout)
        self.add_step_button = QPushButton("Adım ekle")
        add_row = QHBoxLayout()
        add_row.addWidget(self.add_step_button)
        add_row.addStretch(1)
        self.body_layout.addLayout(add_row)
        self.body_layout.addStretch(1)

        self.error = QLabel("")
        self.error.setObjectName("warnNote")
        self.error.setWordWrap(True)
        self._outer.addWidget(self.error)
        self.add_buttons("Kaydet")
        self.save_button = self.ok_button

        initials: list[RoutineStep | None] = list(existing.steps) if existing else [None]
        for initial in initials:
            self._append_step(initial)
        self.add_step_button.clicked.connect(self._add_step)
        self.name.textChanged.connect(self._revalidate)
        self._renumber()
        self._revalidate()

    def _append_step(self, step: RoutineStep | None) -> None:
        editor = _StepEditor(self._registry, self._tools, step)
        editor.changed.connect(self._revalidate)
        editor.move_requested.connect(self._move)
        editor.remove_requested.connect(self._remove)
        self.editors.append(editor)
        self.steps_layout.addWidget(editor)

    def _add_step(self) -> None:
        if len(self.editors) < MAX_STEPS:
            self._append_step(None)
            self._renumber()
            self._revalidate()

    def _remove(self, editor: object) -> None:
        if isinstance(editor, _StepEditor) and editor in self.editors:
            self.editors.remove(editor)
            self.steps_layout.removeWidget(editor)
            editor.deleteLater()
            self._renumber()
            self._revalidate()

    def _move(self, editor: object, delta: int) -> None:
        if not isinstance(editor, _StepEditor) or editor not in self.editors:
            return
        index = self.editors.index(editor)
        target = index + delta
        if 0 <= target < len(self.editors):
            self.editors[index], self.editors[target] = self.editors[target], self.editors[index]
            self.steps_layout.removeWidget(editor)
            self.steps_layout.insertWidget(target, editor)
            self._renumber()
            self._revalidate()

    def _renumber(self) -> None:
        for number, editor in enumerate(self.editors, start=1):
            editor.set_position(number, len(self.editors))
        self.add_step_button.setEnabled(len(self.editors) < MAX_STEPS)

    def _check(self) -> tuple[str, list[RoutineStep]]:
        name = self.name.text().strip()
        try:
            Routine(name=name, steps=[RoutineStep(tool="x")])
        except ValidationError:
            return _NAME_PROBLEM, []
        if name_key(name) in {name_key(item.name) for item in self._others}:
            return "Bu ad zaten kullanılıyor.", []
        if self._existing is None and len(self._others) >= MAX_ROUTINES:
            return f"En çok {MAX_ROUTINES} rutin olabilir.", []
        if not self.editors:
            return "En az bir adım ekle.", []
        steps: list[RoutineStep] = []
        for number, editor in enumerate(self.editors, start=1):
            try:
                steps.append(editor.step())
            except ValueError as error:
                return f"Adım {number}: {error}", []
        try:
            return "", validate_steps(steps, self._registry)
        except ValueError as error:
            return str(error), []

    def _revalidate(self, *_args: object) -> None:
        problem, _steps = self._check()
        self.error.setText(problem)
        self.error.setVisible(bool(problem))
        self.save_button.setEnabled(not problem)

    def submit(self) -> None:
        problem, steps = self._check()
        self.error.setText(problem)
        self.error.setVisible(bool(problem))
        if problem:
            return
        schedule = self._existing.schedule if self._existing else None  # zaman burada düzenlenmez
        self.result_routine = Routine(name=self.name.text().strip(), steps=steps, schedule=schedule)
        self.accept()


def validate_raw(text: str, registry: ToolRegistry) -> tuple[list[Routine] | None, str]:
    """Ham JSON → (rutinler, "") ya da (None, Türkçe hata). Hata metni içerik taşımaz."""
    try:
        data = json.loads(text)
    except ValueError as error:
        line = getattr(error, "lineno", 0)
        column = getattr(error, "colno", 0)
        return None, f"JSON geçersiz (satır {line}, sütun {column}). Dosya yazılmadı."
    try:
        parsed = RoutineFile.model_validate(data)
    except ValidationError as error:
        place = ".".join(str(part) for part in error.errors()[0]["loc"]) or "dosya"
        return None, f"Şemaya uymuyor: {place}. Dosya yazılmadı."
    fixed: list[Routine] = []
    for routine in parsed.routines:
        try:
            steps = validate_steps(routine.steps, registry)
        except ValueError as error:
            return None, f"«{routine.name}»: {error} Dosya yazılmadı."
        fixed.append(routine.model_copy(update={"steps": steps}))
    return fixed, ""


class RoutineFileDialog(FormDialog):
    """Bozuk routines.json'u uygulama içinde düzeltme; dosya kendiliğinden silinmez/sıfırlanmaz.

    Kaydet, `RoutineFile` + `validate_steps` geçmeden hiçbir şey yazmaz (yazım `RoutineStore.save`,
    atomik)."""

    def __init__(self, store: RoutineStore, registry: ToolRegistry,
                 parent: QWidget | None = None) -> None:
        super().__init__("Rutin dosyasını düzelt", 560, parent)
        self._store = store
        self._registry = registry
        self.saved = False
        self.setWindowTitle("Rutin dosyasını düzelt")
        layout = self.form
        note = QLabel(
            "Dosya okunamadığı için rutinler kapalı. Aşağıdaki JSON'u düzeltip Kaydet'e bas; "
            "yalnız geçerliyse yazılır, aksi halde dosya olduğu gibi kalır."
        )
        note.setObjectName("meta")
        note.setWordWrap(True)
        layout.addWidget(note)
        text = ""
        try:
            raw = store.path.read_bytes()
        except FileNotFoundError:
            raw = b""
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("utf-8", errors="replace")
            note.setText(note.text() + " Dosya UTF-8 değildi; okunamayan karakterler değişti.")
        self.editor = QPlainTextEdit(text)
        self.editor.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.editor.setAccessibleName("Rutin dosyası (JSON)")
        self.editor.setMinimumHeight(260)
        layout.addWidget(self.editor, 1)
        self.error = QLabel("")
        self.error.setObjectName("warnNote")
        self.error.setWordWrap(True)
        self.error.hide()
        self._outer.addWidget(self.error)
        self.add_buttons("Kaydet")

    def submit(self) -> None:
        routines, problem = validate_raw(self.editor.toPlainText(), self._registry)
        if routines is None:
            self.error.setText(problem)
            self.error.show()
            return
        try:
            self._store.save(routines)
        except RoutinesError as error:
            self.error.setText(str(error))
            self.error.show()
            return
        self.saved = True
        self.accept()

