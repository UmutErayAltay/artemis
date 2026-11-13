"""Ayarlar > Beceriler: geçerli ve geçersiz beceri dosyaları, klasörü aç, yenile (ADR 0060).

Uygulama içi düzenleyici bilerek yoktur: beceri yalnız kullanıcının yazdığı dosyadır (güven sınırı).
Tek istisna "Yeni beceri": kullanıcı açıkça tıklarsa panel, paketle gelen sabit bir şablondan YENİ
bir dosya oluşturur ve kullanıcının düzenleyicisinde açar. Var olan dosyayı değiştirmez, üzerine
yazmaz, silmez; hiçbir araç ya da model bunu tetikleyemez. Klasör yalnız etkin ve güvenli
konumdaysa açılır ya da yazılır.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QInputDialog,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from artemis.channels.desktop.provider_section import button_row, group, meta_label
from artemis.channels.desktop.settings_lists import ListWithEmpty
from artemis.config.settings import Settings, load_settings
from artemis.skills import (
    NAME_PATTERN,
    ensure_example,
    load_skills,
    location_problem,
    skills_dir,
    templates,
)

_BLANK = "Boş beceri"
_BLANK_TEXT = """---
name: {name}
description: Bu becerinin ne zaman kullanılacağını tek cümleyle yaz.
---
Tarifi buraya yaz: Artemis'in hangi adımları sırayla izleyeceğini kısa ve düz anlat.
"""
_NAME_ROLE = Qt.ItemDataRole.UserRole


class SkillsSection(QWidget):
    # "Dene": beceri adı; sohbet sayfası komut kutusunu doldurur, göndermez.
    try_requested = Signal(str)

    def __init__(self, config_path: Path, *, demo: bool = False) -> None:
        super().__init__()
        self._config_path = config_path
        self._demo = demo
        self._settings: Settings | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(24)
        valid_box, valid = group("Geçerli beceriler", divided=False)
        layout.addWidget(valid_box)
        self.valid_list = QListWidget()
        self.valid_list.setAccessibleName("Geçerli beceriler")
        self.valid_area = ListWithEmpty(
            self.valid_list, "Henüz beceri yok", "", "Klasörü aç",
        )
        if self.valid_area.empty.action_button is not None:
            self.valid_area.empty.action_button.setEnabled(not demo)
            self.valid_area.empty.action_button.clicked.connect(self._open_folder)
        valid.addWidget(self.valid_area)
        invalid_box, invalid = group("Geçersiz dosyalar")
        layout.addWidget(invalid_box)
        self.invalid_list = QListWidget()
        self.invalid_list.setAccessibleName("Geçersiz beceri dosyaları")
        self.invalid_area = ListWithEmpty(self.invalid_list, "Geçersiz dosya yok", height=120)
        invalid.addWidget(self.invalid_area)
        self.new_button = QPushButton("Yeni beceri…")
        self.try_button = QPushButton("Dene")
        self.try_button.setToolTip("Sohbet kutusuna /beceri-adı yazar, göndermez")
        self.try_button.setEnabled(False)
        valid.addLayout(button_row(self.try_button, self.new_button))
        self.open_button = QPushButton("Klasörü aç")
        self.choose_button = QPushButton("Klasör seç…")
        self.refresh_button = QPushButton("Yenile")
        invalid.addWidget(meta_label("Beceri dosyalarını bu klasöre .md olarak koy:"))
        self.folder_label = meta_label("")
        self.folder_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        invalid.addWidget(self.folder_label)
        invalid.addLayout(button_row(self.choose_button, self.open_button, self.refresh_button))
        invalid.addWidget(meta_label(
            "Beceri metni yerel bir dosyadır; bulut modele giden istemin parçası olur, "
            "başka bir kullanıcı metni gibi. Beceri izin vermez: her adım normal onaydan geçer."
        ))
        self.notice = meta_label("")
        layout.addWidget(self.notice)
        self.open_button.clicked.connect(self._open_folder)
        self.new_button.clicked.connect(self._new_skill)
        self.try_button.clicked.connect(self._try_selected)
        self.valid_list.itemDoubleClicked.connect(self._try_selected)
        self.valid_list.currentItemChanged.connect(self._selection_changed)
        self.choose_button.clicked.connect(self._choose_folder)
        self.refresh_button.clicked.connect(self.refresh)
        self.refresh()

    def refresh(self) -> None:
        self.valid_list.clear()
        self.invalid_list.clear()
        self.notice.setText("")
        self.folder_label.setText("")
        try:
            self._settings = load_settings(self._config_path)
        except (OSError, ValueError):
            self._settings = None
            self.notice.setText("Ayar dosyası okunamadı.")
            self.valid_area.show_empty("Beceriler okunamadı")
            self.invalid_area.show_empty("Beceriler okunamadı")
        else:
            if not self._settings.skills.enabled:
                self.valid_area.show_empty("Beceriler kapalı")
                self.invalid_area.show_empty("Beceriler kapalı")
            else:
                self.folder_label.setText(str(skills_dir(self._settings)))
                catalog = load_skills(self._settings)
                for skill in catalog.skills:
                    item = QListWidgetItem(f"{skill.name}: {skill.description}")
                    item.setData(_NAME_ROLE, skill.name)
                    self.valid_list.addItem(item)
                for name, reason in catalog.invalid:
                    self.invalid_list.addItem(f"{name}: {reason}")
                self.notice.setText(" ".join(
                    n for n in (catalog.refused, self._legacy_notice(self._settings)) if n
                ))
                if self.valid_list.count():
                    self.valid_area.show_list()
                else:
                    self.valid_area.show_empty("Henüz beceri yok", action=True)
                if self.invalid_list.count():
                    self.invalid_area.show_list()
                else:
                    self.invalid_area.show_empty("Geçersiz dosya yok")
        enabled = self._settings is not None and self._settings.skills.enabled
        self.open_button.setEnabled(enabled and not self._demo)
        self.new_button.setEnabled(enabled and not self._demo)
        self.try_button.setEnabled(False)
        self.choose_button.setEnabled(self._settings is not None and not self._demo)
        self.refresh_button.setEnabled(not self._demo)

    @staticmethod
    def _legacy_notice(settings: Settings) -> str:
        """Eski klasör güvensiz ve .md içeriyorsa bunu söyler; taşımayı kullanıcı yapar."""
        legacy = settings.state_dir / "beceriler"
        try:
            if (
                legacy.is_dir() and location_problem(settings, legacy)
                and any(legacy.glob("*.md"))
            ):
                return (
                    f"Eski klasör ({legacy}) güvenli olmadığı için kullanılmıyor. İçindeki "
                    f"dosyaları {skills_dir(settings)} klasörüne kendin taşı."
                )
        except OSError:
            pass
        return ""

    def _selection_changed(self) -> None:
        self.try_button.setEnabled(self.valid_list.currentItem() is not None)

    def _try_selected(self) -> None:
        item = self.valid_list.currentItem()
        if item is not None:
            self.try_requested.emit(str(item.data(_NAME_ROLE)))

    def _pick_template(self) -> str | None:
        """Şablon seçtirir: boş beceri için '', şablon için adı; vazgeçilirse None."""
        dialog = QDialog(self)
        dialog.setWindowTitle("Yeni beceri")
        layout = QVBoxLayout(dialog)
        choices = QListWidget()
        choices.setAccessibleName("Beceri şablonları")
        choices.addItem(f"{_BLANK}: sıfırdan kendin yaz")
        for name, text in templates():
            description = yaml.safe_load(text.split("---")[1]).get("description", "")
            choices.addItem(f"{name}: {description}")
        choices.setCurrentRow(0)
        layout.addWidget(choices)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        choices.itemDoubleClicked.connect(dialog.accept)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        row = choices.currentRow()
        return "" if row <= 0 else templates()[row - 1][0]

    def _ask_name(self) -> str | None:
        name, ok = QInputDialog.getText(
            self, "Yeni beceri", "Dosya adı (küçük harf, rakam, tire):"
        )
        return name.strip() if ok else None

    def _new_skill(self) -> None:
        """Seçilen şablondan YENİ dosya yazar (asla üzerine yazmaz) ve düzenleyicide açar."""
        settings = self._settings
        if settings is None or not settings.skills.enabled or self._demo:
            return
        problem = location_problem(settings)
        if problem:
            self.notice.setText(problem)
            return
        picked = self._pick_template()
        if picked is None:
            return
        if picked:
            name, text = picked, dict(templates())[picked]
        else:
            asked = self._ask_name()
            if asked is None:
                return
            if not NAME_PATTERN.fullmatch(asked):
                self.notice.setText(
                    "Dosya adı 1-40 karakter olmalı; yalnız küçük harf, rakam ve tire kullan."
                )
                return
            name, text = asked, _BLANK_TEXT.format(name=asked)
        folder = skills_dir(settings)
        path = folder / f"{name}.md"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            with path.open("x", encoding="utf-8", newline="") as stream:
                stream.write(text)
        except FileExistsError:
            answer = QMessageBox.question(
                self, "Yeni beceri",
                f"{name}.md zaten var; üzerine yazılmaz. Mevcut dosyayı açayım mı?",
            )
            if answer == QMessageBox.StandardButton.Yes:
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
            return
        except OSError:
            self.notice.setText("Beceri dosyası oluşturulamadı.")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        self.refresh()

    def _open_folder(self) -> None:
        settings = self._settings
        if settings is None or not settings.skills.enabled:
            return
        problem = location_problem(settings)
        if problem:
            self.notice.setText(problem)
            return
        try:
            ensure_example(settings)
            folder = skills_dir(settings)
            folder.mkdir(parents=True, exist_ok=True)
        except OSError:
            self.notice.setText("Klasör oluşturulamadı.")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _choose_folder(self) -> None:
        settings = self._settings
        if settings is None:
            return
        chosen = QFileDialog.getExistingDirectory(
            self, "Beceri klasörünü seç", str(skills_dir(settings)),
        )
        if not chosen:
            return
        folder = Path(chosen)
        problem = location_problem(settings, folder)
        if problem:
            self.notice.setText(problem)
            return
        # Ayar paneliyle aynı doğrulamalı, atomik yazım (diğer anahtarlar korunur).
        from artemis.channels.desktop.settings_panel import _write_validated

        try:
            raw = yaml.safe_load(self._config_path.read_text(encoding="utf-8")) or {}
            section = raw.get("skills") or {}
            section["folder"] = str(folder)
            raw["skills"] = section
            _write_validated(self._config_path, raw)
        except (OSError, ValueError, yaml.YAMLError):
            self.refresh()
            self.notice.setText("Klasör kaydedilemedi.")
            return
        self.refresh()
