"""MCP sunucusu ekleme/düzenleme penceresi ve saf alan doğrulaması (ADR 0028).

Doğrulamanın tek kaynağı `MCPServerConfig`tir; burada ek olarak benzersiz sunucu adı,
varsayılansız içerik türü seçimi, çakışmayan araç risk listeleri ve yalnızca büyük harfli
ortam değişkeni adları (`=` veya sır değeri olmadan) Türkçe hatalarla denetlenir.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from pathlib import Path

from pydantic import ValidationError
from PySide6.QtCore import QEvent
from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from artemis.channels.desktop.dialog_shell import FormDialog
from artemis.channels.desktop.widgets import with_help
from artemis.config.settings import ENV_NAME_RE, MCPServerConfig
from artemis.i18n import t

_NAME_RE = re.compile(r"^[a-z0-9_]{1,24}$")
_FIELD_ERRORS = {
    "name": "Ad 1-24 küçük harf, rakam veya _ olmalı.",
    "command": "Komut gerekli.",
    "cwd": "Geçerli bir çalışma klasörü yolu yaz.",
    "content": "İçerik türünü seç: dosya verisi buluta ve Telegram'a gitmez.",
    "timeout_seconds": "Zaman aşımı 1 ile 300 saniye arasında olmalı.",
    "read_only_tools": "Salt okunur araç listesi geçersiz.",
    "confirm_tools": "Onay gerektiren araç listesi geçersiz.",
    "env_passthrough": "Ortam değişkeni adları yalnız büyük harf, rakam ve _ içermeli.",
}


def _split_names(raw_text: str) -> tuple[str, ...]:
    parts = [part.strip() for part in re.split(r"[,\s]+", raw_text.strip()) if part.strip()]
    return tuple(dict.fromkeys(parts))


def _parse_args(args_text: str) -> tuple[str, ...]:
    return tuple(line.strip() for line in args_text.splitlines() if line.strip())


def validate_mcp_server(
    *,
    name: str,
    command: str,
    args_text: str,
    cwd: str,
    content: str | None,
    timeout_seconds: float,
    read_only_tools_text: str,
    confirm_tools_text: str,
    env_passthrough_text: str,
    taken: Iterable[str],
) -> tuple[MCPServerConfig | None, dict[str, str]]:
    """Form alanlarını doğrular; `(MCPServerConfig | None, {alan: Türkçe hata})` döndürür."""
    errors: dict[str, str] = {}
    clean_name = name.strip()
    clean_command = command.strip()
    clean_cwd = cwd.strip()

    if not _NAME_RE.fullmatch(clean_name):
        errors["name"] = t(_FIELD_ERRORS["name"])
    elif clean_name.casefold() in {item.casefold() for item in taken}:
        errors["name"] = t("Bu ad zaten kullanılıyor.")

    if not clean_command:
        errors["command"] = t("Komut gerekli.")

    if "\x00" in clean_cwd:
        errors["cwd"] = t(_FIELD_ERRORS["cwd"])
    cwd_path = Path(clean_cwd) if clean_cwd and "cwd" not in errors else None

    if content not in {"file", "text"}:
        errors["content"] = t(_FIELD_ERRORS["content"])

    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or timeout_seconds < 1
        or timeout_seconds > 300
    ):
        errors["timeout_seconds"] = t(_FIELD_ERRORS["timeout_seconds"])

    read_only_tools = _split_names(read_only_tools_text)
    confirm_tools = _split_names(confirm_tools_text)
    overlap = set(read_only_tools) & set(confirm_tools)
    if overlap:
        errors["confirm_tools"] = t(
            "Aynı araç hem salt okunur hem onay gerektiren listede olamaz: {tools}"
        ).format(tools=", ".join(sorted(overlap)))

    env_passthrough = _split_names(env_passthrough_text)
    for item in env_passthrough:
        if "=" in item:
            errors["env_passthrough"] = t(
                "Değer değil yalnız ortam değişkeni adı yaz (= ve sır değeri yazılmaz)."
            )
            break
        if not ENV_NAME_RE.fullmatch(item):
            errors["env_passthrough"] = t(
                "Ortam değişkeni adı büyük harf, rakam ve _ içermeli: {name}"
            ).format(name=item)
            break

    if errors:
        return None, errors

    content_literal: str = "file" if content == "file" else "text"
    try:
        server = MCPServerConfig(
            name=clean_name,
            command=clean_command,
            args=_parse_args(args_text),
            cwd=cwd_path,
            content=content_literal,  # type: ignore[arg-type]
            timeout_seconds=float(timeout_seconds),
            read_only_tools=read_only_tools,
            confirm_tools=confirm_tools,
            env_passthrough=env_passthrough,
        )
    except ValidationError as error:
        for detail in error.errors():
            key = str(detail["loc"][0]) if detail["loc"] else "name"
            key = key if key in _FIELD_ERRORS else "name"
            errors.setdefault(key, t(_FIELD_ERRORS[key]))
        return None, errors
    return server, {}


class _DoubleSpinBox(QDoubleSpinBox):
    """QSS min-height ile Segoe UI minimumSizeHint farkında sıkışmayı önler."""

    def _sync_min_height(self) -> None:
        hint_h = self.minimumSizeHint().height()
        if hint_h > self.minimumHeight():
            self.setMinimumHeight(hint_h)

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() in (
            QEvent.Type.StyleChange,
            QEvent.Type.FontChange,
            QEvent.Type.Polish,
        ):
            self._sync_min_height()

    def showEvent(self, event: QShowEvent) -> None:
        self._sync_min_height()
        super().showEvent(event)


class MCPServerDialog(FormDialog):
    """Stdio MCP sunucusu ekleme ve düzenleme formu (ADR 0028)."""

    def __init__(
        self,
        existing: MCPServerConfig | None,
        taken: Iterable[str],
        parent: QWidget | None = None,
    ) -> None:
        title = t("MCP sunucusunu düzenle") if existing else t("MCP sunucusu ekle")
        super().__init__(title, 520, parent)
        self._taken = tuple(taken)
        self.result_server: MCPServerConfig | None = None
        self.setWindowTitle(title)
        self._errors: dict[str, QLabel] = {}
        layout = self.form

        self.name_edit = QLineEdit(existing.name if existing else "")
        self.name_edit.setMaxLength(24)
        self.name_edit.setAccessibleName(t("Ad"))
        self.name_edit.setPlaceholderText("notlar")
        self._add(
            layout,
            t("Ad"),
            self.name_edit,
            "name",
            t("1-24 küçük harf, rakam veya _; araçlar mcp_<sunucu>_<araç> olarak adlanır."),
        )

        self.command_edit = QLineEdit(existing.command if existing else "")
        self.command_edit.setAccessibleName(t("Komut"))
        self.command_edit.setPlaceholderText("npx")
        self._add(
            layout,
            t("Komut"),
            self.command_edit,
            "command",
            t("Örn. npx veya Artemis'in kendi Python ortamı için tam olarak {python}."),
        )

        self.args_edit = QPlainTextEdit("\n".join(existing.args) if existing else "")
        self.args_edit.setMaximumHeight(76)
        self.args_edit.setTabChangesFocus(True)
        self.args_edit.setAccessibleName(t("Argümanlar"))
        self.args_edit.setPlaceholderText(t("Her satıra bir argüman"))
        self._add(
            layout,
            t("Argümanlar"),
            self.args_edit,
            "args",
            t("Her satır ayrı bir argüman olur; boşluk içeren yollar tırnaksız yazılır."),
        )

        self.cwd_edit = QLineEdit(str(existing.cwd) if existing and existing.cwd else "")
        self.cwd_edit.setAccessibleName(t("Çalışma klasörü"))
        self.cwd_edit.setPlaceholderText(t("İsteğe bağlı"))
        self.cwd_browse = QPushButton(t("Seç…"))
        self.cwd_browse.clicked.connect(self._browse_cwd)
        cwd_row = QHBoxLayout()
        cwd_row.setSpacing(8)
        cwd_row.addWidget(self.cwd_edit, 1)
        cwd_row.addWidget(self.cwd_browse)
        self._add(layout, t("Çalışma klasörü"), cwd_row, "cwd")

        self.content_combo = QComboBox()
        self.content_combo.setAccessibleName(t("İçerik türü"))
        # ADR 0028: içerik türünde gizliliği düşüren sessiz varsayılan konmaz.
        self.content_combo.addItem(t("İçerik türü seçin…"), None)
        self.content_combo.addItem(t("Dosya / kişisel veri (yalnız yerel)"), "file")
        self.content_combo.addItem(t("Genel metin (buluta ve Telegram'a gidebilir)"), "text")
        if existing is not None:
            idx = self.content_combo.findData(existing.content)
            self.content_combo.setCurrentIndex(max(0, idx))
        self._add(
            layout,
            t("İçerik türü"),
            self.content_combo,
            "content",
            t("Dosya seçimi sunucu çıktılarını yerel tutar; bulut izni olmadan dışarı göndermez."),
            help=t(
                "Dosya seçimi kişisel not/dosya içeriklerini buluta ve Telegram'a göndermez. "
                "Genel metin seçimi ise genel metin çıktıları içindir."
            ),
        )

        self.timeout_spin = _DoubleSpinBox()
        self.timeout_spin.setAccessibleName(t("Zaman aşımı (sn)"))
        self.timeout_spin.setRange(1.0, 300.0)
        self.timeout_spin.setDecimals(1)
        self.timeout_spin.setValue(float(existing.timeout_seconds) if existing else 15.0)
        self._add(layout, t("Zaman aşımı (sn)"), self.timeout_spin, "timeout_seconds")

        self.read_only_edit = QLineEdit(
            ", ".join(existing.read_only_tools) if existing else ""
        )
        self.read_only_edit.setAccessibleName(t("Salt okunur araçlar"))
        self.read_only_edit.setPlaceholderText("read_text_file, list_directory")
        self._add(
            layout,
            t("Salt okunur araçlar"),
            self.read_only_edit,
            "read_only_tools",
            t("Virgül veya satır sonuyla ayrılmış ham araç adları (ön ek olmadan)."),
            help=t(
                "Yalnız gerçekten okuma yapan ve güvendiğin ham araç adlarını yaz. "
                "Sunucunun kendi readOnly beyanı riski düşürmez."
            ),
        )

        self.confirm_edit = QLineEdit(
            ", ".join(existing.confirm_tools) if existing else ""
        )
        self.confirm_edit.setAccessibleName(t("Onay gerektiren araçlar"))
        self.confirm_edit.setPlaceholderText("write_file, move_file")
        self._add(
            layout,
            t("Onay gerektiren araçlar"),
            self.confirm_edit,
            "confirm_tools",
            t("Her çağrıda onay penceresi açılacak ham araç adları."),
        )

        self.env_edit = QLineEdit(
            ", ".join(existing.env_passthrough) if existing else ""
        )
        self.env_edit.setAccessibleName(t("Ortam değişkeni adları"))
        self.env_edit.setPlaceholderText("GITHUB_TOKEN")
        self._add(
            layout,
            t("Ortam değişkeni adları"),
            self.env_edit,
            "env_passthrough",
            t("Değer değil yalnız büyük harfli değişken adları; değerler ortamdan okunur."),
            help=t(
                "Buraya sır değeri değil yalnız ortam değişkeni adı yaz (ör. GITHUB_TOKEN). "
                "Değer ayar dosyasına yazılmaz, yalnız çalıştırmada bu sunucuya aktarılır."
            ),
        )

        self.add_buttons(t("Kaydet") if existing else t("Ekle"))

    def _add(
        self,
        layout: QVBoxLayout,
        caption: str,
        control: QWidget | QLayout,
        key: str,
        hint: str | None = None,
        help: str = "",  # noqa: A002
    ) -> None:
        label = QLabel(caption)
        label.setObjectName("fieldLabel")
        layout.addLayout(with_help(label, help)) if help else layout.addWidget(label)
        if isinstance(control, QWidget):
            label.setBuddy(control)
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

    def _browse_cwd(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, t("Klasör seç"), self.cwd_edit.text())
        if chosen:
            self.cwd_edit.setText(chosen)

    def submit(self) -> None:
        raw_content = self.content_combo.currentData()
        server, errors = validate_mcp_server(
            name=self.name_edit.text(),
            command=self.command_edit.text(),
            args_text=self.args_edit.toPlainText(),
            cwd=self.cwd_edit.text(),
            content=str(raw_content) if raw_content is not None else None,
            timeout_seconds=self.timeout_spin.value(),
            read_only_tools_text=self.read_only_edit.text(),
            confirm_tools_text=self.confirm_edit.text(),
            env_passthrough_text=self.env_edit.text(),
            taken=self._taken,
        )
        for key, label in self._errors.items():
            label.setText(errors.get(key, ""))
            label.setVisible(key in errors)
        if server is None:
            return
        self.result_server = server
        self.accept()
