"""Ayarlar > Beceriler: geçerli ve geçersiz beceri dosyaları; ekle, düzenle, düzelt, sil (ADR 0060).

Beceri yalnız kullanıcının yazdığı talimattır (güven sınırı), bu yüzden dosyayı yalnız kullanıcının
açık tıklaması değiştirir: "Yeni beceri…"/"Düzenle"/"Düzelt" uygulama içi `SkillDialog`'u açar,
"Sil" onay sorar (varsayılan Hayır). Hiçbir araç ya da model bunu tetikleyemez; dış düzenleyici
açılmaz. Klasör yalnız etkin ve güvenli konumdaysa yazılır.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QFileDialog,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from artemis.channels.desktop.form_kit import button_row, group, meta_label
from artemis.channels.desktop.settings_io import _write_validated
from artemis.channels.desktop.settings_lists import ListWithEmpty
from artemis.channels.desktop.skill_dialog import SkillDialog, read_for_edit
from artemis.config.settings import Settings, load_settings
from artemis.i18n import t
from artemis.skills import (
    NAME_PATTERN,
    SkillWriteError,
    delete_skill,
    ensure_example,
    load_skills,
    location_problem,
    skills_dir,
)

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
        valid_box, valid = group(t("Geçerli beceriler"), divided=False)
        layout.addWidget(valid_box)
        self.valid_list = QListWidget()
        self.valid_list.setAccessibleName(t("Geçerli beceriler"))
        self.valid_area = ListWithEmpty(
            self.valid_list, t("Henüz beceri yok"), "", t("Yeni beceri…"),
        )
        if self.valid_area.empty.action_button is not None:
            self.valid_area.empty.action_button.setEnabled(not demo)
            self.valid_area.empty.action_button.clicked.connect(self._new_skill)
        valid.addWidget(self.valid_area)
        invalid_box, invalid = group(t("Geçersiz dosyalar"))
        layout.addWidget(invalid_box)
        self.invalid_list = QListWidget()
        self.invalid_list.setAccessibleName(t("Geçersiz beceri dosyaları"))
        self.invalid_area = ListWithEmpty(
            self.invalid_list, t("Geçersiz dosya yok"), height=120,
        )
        invalid.addWidget(self.invalid_area)
        self.new_button = QPushButton(t("Yeni beceri…"))
        self.new_button.setObjectName("primary")
        self.try_button = QPushButton(t("Dene"))
        self.try_button.setToolTip(t("Sohbet kutusuna /beceri-adı yazar, göndermez"))
        self.edit_button = QPushButton(t("Düzenle"))
        self.delete_button = QPushButton(t("Sil"))
        for button in (self.try_button, self.edit_button, self.delete_button):
            button.setEnabled(False)
        valid.addLayout(button_row(
            self.delete_button, self.edit_button, self.try_button, self.new_button,
        ))
        self.fix_button = QPushButton(t("Düzelt"))
        self.fix_button.setToolTip(t("Bozuk dosyayı uygulama içinde aç ve onar"))
        self.delete_invalid_button = QPushButton(t("Sil"))
        self.fix_button.setEnabled(False)
        self.delete_invalid_button.setEnabled(False)
        invalid.addLayout(button_row(
            self.delete_invalid_button, self.fix_button,
        ))
        # Klasörü göster yalnız dosya gezginini açar (düzenleyici değil); ikincil, düşük vurgu.
        self.open_button = QPushButton(t("Klasörü göster"))
        self.choose_button = QPushButton(t("Klasör seç…"))
        self.refresh_button = QPushButton(t("Yenile"))
        invalid.addWidget(meta_label(t("Beceri dosyalarını bu klasöre .md olarak koy:")))
        self.folder_label = meta_label("")
        self.folder_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        invalid.addWidget(self.folder_label)
        invalid.addLayout(button_row(
            self.choose_button, self.open_button, self.refresh_button,
        ))
        invalid.addWidget(meta_label(
            t("Beceri metni yerel bir dosyadır; bulut modele giden istemin parçası olur, "
              "başka bir kullanıcı metni gibi. Beceri izin vermez: her adım normal onaydan geçer."),
        ))
        self.notice = meta_label("")
        layout.addWidget(self.notice)
        self.open_button.clicked.connect(self._open_folder)
        self.new_button.clicked.connect(self._new_skill)
        self.try_button.clicked.connect(self._try_selected)
        self.edit_button.clicked.connect(self._edit_selected)
        self.delete_button.clicked.connect(self._delete_selected)
        self.fix_button.clicked.connect(self._fix_selected)
        self.delete_invalid_button.clicked.connect(self._delete_invalid)
        self.invalid_list.currentItemChanged.connect(self._selection_changed)
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
            self.notice.setText(t("Ayar dosyası okunamadı."))
            self.valid_area.show_empty(t("Beceriler okunamadı"))
            self.invalid_area.show_empty(t("Beceriler okunamadı"))
        else:
            if not self._settings.skills.enabled:
                self.valid_area.show_empty(t("Beceriler kapalı"))
                self.invalid_area.show_empty(t("Beceriler kapalı"))
            else:
                self.folder_label.setText(str(skills_dir(self._settings)))
                catalog = load_skills(self._settings)
                for skill in catalog.skills:
                    item = QListWidgetItem(f"{skill.name}: {skill.description}")
                    item.setData(_NAME_ROLE, skill.name)
                    self.valid_list.addItem(item)
                for name, reason in catalog.invalid:
                    invalid_item = QListWidgetItem(f"{name}: {reason}")
                    invalid_item.setData(_NAME_ROLE, name)
                    self.invalid_list.addItem(invalid_item)
                self.notice.setText(" ".join(
                    n for n in (catalog.refused, self._legacy_notice(self._settings)) if n
                ))
                if self.valid_list.count():
                    self.valid_area.show_list()
                else:
                    self.valid_area.show_empty(t("Henüz beceri yok"), action=True)
                if self.invalid_list.count():
                    self.invalid_area.show_list()
                else:
                    self.invalid_area.show_empty(t("Geçersiz dosya yok"))
        enabled = self._settings is not None and self._settings.skills.enabled
        self.open_button.setEnabled(enabled and not self._demo)
        self.new_button.setEnabled(enabled and not self._demo)
        self._selection_changed()
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
                return t(
                    "Eski klasör ({legacy}) güvenli olmadığı için kullanılmıyor. "
                    "İçindeki dosyaları {target} klasörüne kendin taşı."
                ).format(legacy=legacy, target=skills_dir(settings))
        except OSError:
            pass
        return ""

    def _selection_changed(self) -> None:
        live = not self._demo and self._settings is not None and self._settings.skills.enabled
        valid = self.valid_list.currentItem() is not None
        self.try_button.setEnabled(valid)
        self.edit_button.setEnabled(valid and live)
        self.delete_button.setEnabled(valid and live)
        broken = self.invalid_list.currentItem()
        self.delete_invalid_button.setEnabled(broken is not None and live)
        # Dosya adı geçersizse (örn. büyük harf) name==dosya adı kuralı yüzünden yerinde onarılamaz.
        self.fix_button.setEnabled(
            broken is not None and live
            and NAME_PATTERN.fullmatch(Path(str(broken.data(_NAME_ROLE))).stem) is not None
        )

    def _try_selected(self) -> None:
        item = self.valid_list.currentItem()
        if item is not None:
            self.try_requested.emit(str(item.data(_NAME_ROLE)))

    def _writable_settings(self) -> Settings | None:
        """Yazmaya hazır ayar; kapalıysa, demoda ya da konum güvensizse None (neden gösterilir)."""
        settings = self._settings
        if settings is None or not settings.skills.enabled or self._demo:
            return None
        problem = location_problem(settings)
        if problem:
            self.notice.setText(problem)
            return None
        return settings

    def _run_dialog(self, settings: Settings, name: str | None) -> None:
        original: bytes | None = None
        if name is not None:
            try:
                original = read_for_edit(settings, name)
            except SkillWriteError as error:
                self.notice.setText(str(error))
                return
        SkillDialog(settings, name, original, self).exec()
        self.refresh()  # iptal edilse de dış değişiklikler listede güncel görünsün

    def _new_skill(self) -> None:
        settings = self._writable_settings()
        if settings is not None:
            self._run_dialog(settings, None)

    def _edit_selected(self) -> None:
        item = self.valid_list.currentItem()
        settings = self._writable_settings()
        if item is not None and settings is not None:
            self._run_dialog(settings, str(item.data(_NAME_ROLE)))

    def _fix_selected(self) -> None:
        item = self.invalid_list.currentItem()
        settings = self._writable_settings()
        if item is not None and settings is not None:
            self._run_dialog(settings, Path(str(item.data(_NAME_ROLE))).stem)

    def _confirm_delete(self, file_name: str) -> None:
        settings = self._writable_settings()
        if settings is None:
            return
        answer = QMessageBox.question(
            self, t("Beceriyi sil"),
            t("{file_name} silinsin mi? Bu geri alınamaz.").format(file_name=file_name),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        problem = ""
        if answer == QMessageBox.StandardButton.Yes:
            try:
                delete_skill(settings, file_name)
            except SkillWriteError as error:
                problem = str(error)
        self.refresh()
        if problem:  # refresh bildirimi sildiği için hata ondan sonra yazılır
            self.notice.setText(problem)

    def _delete_selected(self) -> None:
        item = self.valid_list.currentItem()
        if item is not None:
            self._confirm_delete(f"{item.data(_NAME_ROLE)}.md")

    def _delete_invalid(self) -> None:
        item = self.invalid_list.currentItem()
        if item is not None:
            self._confirm_delete(str(item.data(_NAME_ROLE)))

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
            self.notice.setText(t("Klasör oluşturulamadı."))
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _choose_folder(self) -> None:
        settings = self._settings
        if settings is None:
            return
        chosen = QFileDialog.getExistingDirectory(
            self, t("Beceri klasörünü seç"), str(skills_dir(settings)),
        )
        if not chosen:
            return
        folder = Path(chosen)
        problem = location_problem(settings, folder)
        if problem:
            self.notice.setText(problem)
            return
        try:
            raw = yaml.safe_load(self._config_path.read_text(encoding="utf-8")) or {}
            section = raw.get("skills") or {}
            section["folder"] = str(folder)
            raw["skills"] = section
            _write_validated(self._config_path, raw)
        except (OSError, ValueError, yaml.YAMLError):
            self.refresh()
            self.notice.setText(t("Klasör kaydedilemedi."))
            return
        self.refresh()
