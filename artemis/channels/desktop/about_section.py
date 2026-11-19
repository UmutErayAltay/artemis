"""Ayarlar > Hakkında bölümü: sürüm, bağlantılar, lisans ve sistem bilgisi."""

from __future__ import annotations

import platform
import sys
from collections.abc import Callable
from pathlib import Path

import PySide6
from PySide6.QtCore import Qt, QUrl, qVersion
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from artemis.channels.desktop.form_kit import button_row, group, meta_label
from artemis.channels.desktop.page_kit import make_card
from artemis.channels.desktop.text_viewer import open_text_file
from artemis.i18n import t
from artemis.update_check import current_version

_REPO_URL = "https://github.com/UmutErayAltay/artemis"
_RELEASES_URL = f"{_REPO_URL}/releases"
_ISSUES_URL = f"{_REPO_URL}/issues/new"


def _resource(name: str) -> Path | None:
    """Paketlenmiş çalıştırmada ya da kaynak ağacında kök dosyayı bulur."""
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    else:
        base = Path(__file__).resolve().parents[3]
    candidate = base / name
    return candidate if candidate.is_file() else None


def _default_open_url(url: str) -> object:
    return QDesktopServices.openUrl(QUrl(url))


def _selectable_meta(text: str) -> QLabel:
    label = meta_label(text)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


class AboutSection(QWidget):
    """Uygulama kimliği, dış bağlantılar, lisans dosyaları ve ortam özeti."""

    def __init__(
        self,
        config_path: Path,
        parent: QWidget | None = None,
        *,
        open_url: Callable[[str], object] | None = None,
    ) -> None:
        super().__init__(parent)
        self.config_path = Path(config_path)
        self._open_url: Callable[[str], object] = open_url or _default_open_url

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(24)

        layout.addWidget(self._build_header())
        layout.addWidget(self._build_links_group())
        layout.addWidget(self._build_license_group())
        layout.addWidget(self._build_system_group())

    def _build_header(self) -> QWidget:
        # Dairesel içe aktarmayı önlemek için uygulama simgesi burada yüklenir.
        from artemis.channels.desktop.app import app_icon

        card, card_layout = make_card()
        row = QHBoxLayout()
        row.setSpacing(16)

        self.icon_label = QLabel()
        self.icon_label.setFixedSize(64, 64)
        pixmap = app_icon().pixmap(64, 64)
        if not pixmap.isNull():
            self.icon_label.setPixmap(pixmap)
        row.addWidget(
            self.icon_label,
            0,
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft,
        )

        text_col = QVBoxLayout()
        text_col.setSpacing(4)
        self.title_label = QLabel("Artemis")
        self.title_label.setObjectName("sectionTitle")
        self.version_label = _selectable_meta(
            t("Sürüm {version}").format(version=current_version())
        )
        self.description_label = meta_label(
            t("Türkçe konuşan, onay odaklı kişisel masaüstü asistanı.")
        )
        text_col.addWidget(self.title_label)
        text_col.addWidget(self.version_label)
        text_col.addWidget(self.description_label)
        row.addLayout(text_col, 1)

        card_layout.addLayout(row)
        return card

    def _build_links_group(self) -> QWidget:
        box, body = group(t("Bağlantılar"))
        self.github_button = QPushButton(t("GitHub deposu"))
        self.github_button.clicked.connect(lambda: self._open_url(_REPO_URL))
        self.releases_button = QPushButton(t("Sürüm notları"))
        self.releases_button.clicked.connect(lambda: self._open_url(_RELEASES_URL))
        self.issues_button = QPushButton(t("Sorun bildir"))
        self.issues_button.clicked.connect(lambda: self._open_url(_ISSUES_URL))
        body.addLayout(
            button_row(self.github_button, self.releases_button, self.issues_button)
        )
        return box

    def _build_license_group(self) -> QWidget:
        box, body = group(t("Lisans"))
        self.license_summary_label = meta_label(
            t("Apache Lisansı 2.0 ile dağıtılır.")
        )
        body.addWidget(self.license_summary_label)

        self._license_path = _resource("LICENSE")
        self.license_button = QPushButton(t("Lisansı göster"))
        self.license_button.setEnabled(self._license_path is not None)
        self.license_button.clicked.connect(self._show_license)

        self._notices_path = _resource("THIRD_PARTY_NOTICES.md")
        self.notices_button = QPushButton(t("Üçüncü taraf bildirimleri"))
        self.notices_button.setEnabled(self._notices_path is not None)
        self.notices_button.clicked.connect(self._show_notices)

        body.addLayout(button_row(self.license_button, self.notices_button))
        return box

    def _build_system_group(self) -> QWidget:
        box, body = group(t("Sistem"))
        py_ver = platform.python_version()
        qt_text = f"PySide6 {PySide6.__version__} (Qt {qVersion()})"
        os_text = platform.platform(terse=True)

        self.python_label = _selectable_meta(
            t("Python: {version}").format(version=py_ver)
        )
        self.qt_label = _selectable_meta(t("Arayüz: {qt}").format(qt=qt_text))
        self.os_label = _selectable_meta(
            t("İşletim sistemi: {os}").format(os=os_text)
        )
        self.config_label = _selectable_meta(
            t("Ayar klasörü: {path}").format(path=str(self.config_path.parent))
        )
        for label in (
            self.python_label,
            self.qt_label,
            self.os_label,
            self.config_label,
        ):
            body.addWidget(label)

        self.open_config_dir_button = QPushButton(t("Ayar klasörünü aç"))
        self.open_config_dir_button.clicked.connect(self._open_config_dir)
        body.addLayout(button_row(self.open_config_dir_button))
        return box

    def _show_license(self) -> None:
        if self._license_path is not None:
            open_text_file(self._license_path, t("Lisans"), self)

    def _show_notices(self) -> None:
        if self._notices_path is not None:
            open_text_file(
                self._notices_path, t("Üçüncü taraf bildirimleri"), self
            )

    def _open_config_dir(self) -> None:
        folder_url = QUrl.fromLocalFile(str(self.config_path.parent)).toString()
        self._open_url(folder_url)
