"""Ayarlar > Beceriler: geçerli ve geçersiz beceri dosyaları, klasörü aç, yenile (ADR 0060).

Uygulama içi düzenleyici bilerek yoktur: beceri yalnız kullanıcının elle yazdığı dosyadır, hiçbir
araç ya da bu panel onu yazmaz (güven sınırı). Klasör yalnız etkin ve güvenli konumdaysa açılır.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QListWidget, QPushButton, QVBoxLayout, QWidget

from artemis.channels.desktop.provider_section import button_row, group, meta_label
from artemis.config.settings import Settings, load_settings
from artemis.skills import ensure_example, load_skills, skills_dir


class SkillsSection(QWidget):
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
        self.valid_list.setMaximumHeight(160)
        self.valid_list.setAccessibleName("Geçerli beceriler")
        valid.addWidget(self.valid_list)
        invalid_box, invalid = group("Geçersiz dosyalar")
        layout.addWidget(invalid_box)
        self.invalid_list = QListWidget()
        self.invalid_list.setMaximumHeight(120)
        self.invalid_list.setAccessibleName("Geçersiz beceri dosyaları")
        invalid.addWidget(self.invalid_list)
        self.open_button = QPushButton("Klasörü aç")
        self.refresh_button = QPushButton("Yenile")
        invalid.addLayout(button_row(self.open_button, self.refresh_button))
        invalid.addWidget(meta_label(
            "Beceri metni yerel bir dosyadır; bulut modele giden istemin parçası olur, "
            "başka bir kullanıcı metni gibi. Beceri izin vermez: her adım normal onaydan geçer."
        ))
        self.notice = meta_label("")
        layout.addWidget(self.notice)
        self.open_button.clicked.connect(self._open_folder)
        self.refresh_button.clicked.connect(self.refresh)
        self.refresh()

    def refresh(self) -> None:
        self.valid_list.clear()
        self.invalid_list.clear()
        self.notice.setText("")
        try:
            self._settings = load_settings(self._config_path)
        except (OSError, ValueError):
            self._settings = None
            self.notice.setText("Ayar dosyası okunamadı.")
        else:
            if not self._settings.skills.enabled:
                self.notice.setText("Beceriler kapalı: yukarıdaki kutuyu aç ve Uygula'ya bas.")
            else:
                catalog = load_skills(self._settings)
                for skill in catalog.skills:
                    self.valid_list.addItem(f"{skill.name}: {skill.description}")
                for name, reason in catalog.invalid:
                    self.invalid_list.addItem(f"{name}: {reason}")
                if catalog.refused:
                    self.notice.setText(catalog.refused)
        enabled = self._settings is not None and self._settings.skills.enabled
        self.open_button.setEnabled(enabled and not self._demo)
        self.refresh_button.setEnabled(not self._demo)

    def _open_folder(self) -> None:
        settings = self._settings
        if settings is None or not settings.skills.enabled:
            return
        try:
            ensure_example(settings)
        except OSError:
            self.notice.setText("Klasör oluşturulamadı.")
            return
        folder = skills_dir(settings)
        if not folder.is_dir():
            self.notice.setText("Klasör açılamadı (güvenli konumda değil ya da yok).")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))
