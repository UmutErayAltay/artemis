"""Kod ajanı ayarlarının yazımı ve ajan profili formu (ADR 0049 arayüzü).

Yazım tek mekanizmadan geçer: `settings_panel.save_code_agent` (doğrula, aynı dizinde atomik
değiştir, bilinmeyen anahtarları koru). Burada yalnız hangi değerin yazılacağı hesaplanır.
Arayüz yol yazmaz/çözmez: klasörü kullanıcı seçer, ajan/klasör için model yalnız takma ad görür.
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QVBoxLayout,
)

from artemis.config.settings import (
    ALIAS_RE,
    BUILTIN_AGENTS,
    ENV_NAME_RE,
    OWN_REPO_ALIAS,
    AgentProfile,
)

# Yalnız okunur: önbellek doluysa sonucu gösterir, PowerShell hiçbir koşulda çalıştırılmaz.
from artemis.tools.code_agent_backends import _LOCATE_CACHE


def _raw_code_agent(path: Path) -> dict[str, Any]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    section = raw.get("code_agent")
    return dict(section) if isinstance(section, dict) else {}


def _save(path: Path, values: dict[str, Any]) -> None:
    # Döngüsel içe aktarmayı önlemek için geç: settings_panel zaten projects_panel'i içe aktarır.
    from artemis.channels.desktop.settings_panel import save_code_agent

    save_code_agent(path, values)


def slug_alias(folder_name: str) -> str:
    """Klasör adından önerilen takma ad; geçerli değilse boş döner."""
    slug = re.sub(r"[^a-z0-9_-]+", "-", folder_name.lower()).strip("-")[:32]
    return slug if ALIAS_RE.fullmatch(slug) else ""


def workdir_alias_error(alias: str, taken: Iterable[str]) -> str | None:
    if not ALIAS_RE.fullmatch(alias):
        return "Takma ad 1-32 küçük harf, rakam, _ veya - olmalı."
    if alias == OWN_REPO_ALIAS:
        return f"'{OWN_REPO_ALIAS}' ayrılmış addır (projenin kendi klasörü)."
    if alias in set(taken):
        return "Bu takma ad zaten kullanılıyor."
    return None


def guess_project_path(repos_dir: Path | None, alias: str) -> Path | None:
    """GitHub eşitlemesinden gelen projenin klasörü: yalnız dizin listesi, git çalıştırılmaz.

    Arka uç aynı adlandırmayı kullanır (küçük harf, geçersiz karakter '-'); tek eşleşme yoksa
    None döner ve varsayılan yazılmaz.
    """
    if repos_dir is None:
        return None
    try:
        children = sorted(repos_dir.iterdir())
    except OSError:
        return None
    found = [
        child
        for child in children
        if child.is_dir() and re.sub(r"[^a-z0-9_-]", "-", child.name.lower())[:32] == alias
    ]
    return found[0] if len(found) == 1 else None


def save_project_default(
    path: Path,
    alias: str,
    *,
    agent: str,
    workdir: str,
    default_agent: str,
    project_path: Path | None,
    mode: str | None = None,
    default_mode: str = "background",
) -> None:
    """Projenin varsayılanını yazar; eski `alias: yol` yalnız gerektiğinde sözlüğe çevrilir.

    `mode` None ise projenin kayıtlı kipine dokunulmaz."""
    projects = dict(_raw_code_agent(path).get("projects") or {})
    existing = projects.get(alias)
    if isinstance(existing, dict):
        entry = dict(existing)
    elif existing is not None:
        entry = {"path": str(existing)}
    elif project_path is not None:
        entry = {"path": str(project_path)}
    else:
        raise ValueError("Projenin klasörü bilinmiyor.")
    # Varsayılanla aynı seçim anahtar olarak yazılmaz: proje genel varsayılanı izlemeye devam eder.
    choices = [("agent", agent, default_agent), ("workdir", workdir, OWN_REPO_ALIAS)]
    if mode is not None:
        choices.append(("mode", mode, default_mode))
    for key, value, fallback in choices:
        if value == fallback:
            entry.pop(key, None)
        else:
            entry[key] = value
    projects[alias] = entry["path"] if set(entry) == {"path"} else entry
    _save(path, {"projects": projects})


def add_workdir(path: Path, alias: str, folder: Path) -> None:
    workdirs = dict(_raw_code_agent(path).get("workdirs") or {})
    workdirs[alias] = str(folder)
    _save(path, {"workdirs": workdirs})


def command_status(command: str | None) -> str:
    """Komutun çözülebilirliği; hiçbir şey başlatılmaz.

    Yalnız `shutil.which` ve (varsa) süreç önbelleği bakılır. `locate_codex` PowerShell
    çalıştırabildiği için arayüz iş parçacığında çağrılmaz.
    """
    if not command:
        return "komut yok"
    if command in _LOCATE_CACHE:
        return "bulundu" if _LOCATE_CACHE[command] else "bulunamadı"
    if Path(command).is_absolute():
        return "bulundu" if Path(command).is_file() else "bulunamadı"
    if shutil.which(command):
        return "bulundu"
    if command == "codex":
        return "PATH'te yok (ilk kullanımda ayrıca aranır)"
    return "PATH'te yok"


class AgentFormError(ValueError):
    """Formdaki hatanın kullanıcıya gösterilecek Türkçe metni."""


def parse_agent_form(
    name: str,
    command: str,
    args_text: str,
    prompt_via: str,
    env_text: str,
    taken: Iterable[str],
    visible_args_text: str = "",
) -> tuple[str, dict[str, Any]]:
    """Özel (generic) ajan formunu arka uç AgentProfile modeliyle doğrular."""
    name = name.strip()
    if not ALIAS_RE.fullmatch(name):
        raise AgentFormError("Ad 1-32 küçük harf, rakam, _ veya - olmalı.")
    if name in BUILTIN_AGENTS or name in set(taken):
        raise AgentFormError("Bu ad zaten kullanılıyor.")
    command = command.strip()
    if not command:
        raise AgentFormError("Komut gerekli.")
    args = tuple(line for line in args_text.splitlines() if line.strip())
    visible_args = tuple(line for line in visible_args_text.splitlines() if line.strip())
    env = tuple(part for part in re.split(r"[,\s]+", env_text.strip()) if part)
    for variable in env:
        if not ENV_NAME_RE.fullmatch(variable):
            raise AgentFormError(f"Ortam değişkeni adı büyük harfli olmalı: {variable}")
    try:
        profile = AgentProfile(
            kind="generic",
            command=command,
            args=args,
            prompt_via=prompt_via,  # type: ignore[arg-type]  # değer arka uç doğrulamasından geçer
            env=env,
            visible_args=visible_args,
        )
    except ValidationError as error:
        messages = [str(item["msg"]).removeprefix("Value error, ") for item in error.errors()]
        raise AgentFormError("\n".join(messages)) from error
    return name, profile.model_dump(mode="json", exclude_defaults=True)


class AgentDialog(QDialog):
    """Özel ajan ekleme penceresi; doğrulama `parse_agent_form`da, hata satır içi gösterilir."""

    def __init__(self, taken: Iterable[str], parent: Any = None) -> None:
        super().__init__(parent)
        self._taken = tuple(taken)
        self.result_profile: tuple[str, dict[str, Any]] | None = None
        self.setWindowTitle("Özel kod ajanı ekle")
        self.setMinimumWidth(480)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)
        title = QLabel("Özel kod ajanı")
        title.setObjectName("title")
        layout.addWidget(title)
        self.name = QLineEdit()
        self.name.setPlaceholderText("Örn. aider")
        self.command = QLineEdit()
        self.command.setPlaceholderText("Örn. aider")
        self.args = QPlainTextEdit()
        self.args.setPlaceholderText(
            "Her satır bir argüman. Yer tutucular: {prompt_file} {workdir} {repo}"
        )
        self.args.setFixedHeight(72)
        self.visible_args = QPlainTextEdit()
        self.visible_args.setPlaceholderText(
            "Boşsa görünür kip kapalı. Yer tutucular: {prompt_file} {prompt} {workdir} {repo}"
        )
        self.visible_args.setFixedHeight(72)
        self.prompt_via = QComboBox()
        self.prompt_via.addItem("Standart girdi (stdin)", "stdin")
        self.prompt_via.addItem("Geçici dosya ({prompt_file})", "file")
        self.env = QLineEdit()
        self.env.setPlaceholderText("Örn. OPENAI_API_KEY, AIDER_MODEL")
        for caption, widget, hint in (
            ("Ad", self.name, "Küçük harf, rakam, _ veya -"),
            ("Komut", self.command, None),
            ("Argümanlar", self.args, None),
            ("İstem biçimi", self.prompt_via, None),
            (
                "Görünür pencere argümanları (isteğe bağlı)",
                self.visible_args,
                "Satır başına bir argüman. {prompt} kısa yönerge, {prompt_file} tam istemdir.",
            ),
            (
                "Ortam değişkeni adları (isteğe bağlı)",
                self.env,
                "Yalnız adlar saklanır; değerler asla yazılmaz. Değeri kendi ortamında tanımla.",
            ),
        ):
            widget.setAccessibleName(caption)
            label = QLabel(caption)
            label.setObjectName("fieldLabel")
            layout.addWidget(label)
            layout.addWidget(widget)
            if hint:
                note = QLabel(hint)
                note.setObjectName("meta")
                note.setWordWrap(True)
                layout.addWidget(note)
        # Hata satırı gizli başlar; yalnız doğrulama başarısızsa görünür (warning tonu).
        self.error = QLabel("")
        self.error.setObjectName("warnNote")
        self.error.setWordWrap(True)
        self.error.hide()
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Ekle")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Vazgeç")
        buttons.accepted.connect(self.submit)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def submit(self) -> None:
        try:
            self.result_profile = parse_agent_form(
                self.name.text(),
                self.command.text(),
                self.args.toPlainText(),
                str(self.prompt_via.currentData()),
                self.env.text(),
                self._taken,
                self.visible_args.toPlainText(),
            )
        except AgentFormError as error:
            self.error.setText(str(error))
            self.error.show()
            return
        self.accept()
