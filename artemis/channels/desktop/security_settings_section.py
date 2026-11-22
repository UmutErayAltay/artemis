"""Ayarlar > Uygulamalar ve Güvenlik bölümü gizlilik/şifreleme ve onaysız kapatma yardımcıları."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QLabel, QListWidgetItem, QVBoxLayout

from artemis.channels.desktop.form_kit import group, meta_label
from artemis.i18n import t

if TYPE_CHECKING:
    from artemis.channels.desktop.settings_panel import SettingsPanel

_FRIENDLY_APPS = {
    "brave": "Brave",
    "notepad": "Not Defteri",
    "paint": "Paint",
    "calculator": "Hesap Makinesi",
    "explorer": "Dosya Gezgini",
}


def _hint_label(text: str) -> QLabel:
    label = meta_label(text)
    label.setContentsMargins(24, 0, 0, 0)
    label.setMaximumWidth(640)
    return label


def build_privacy_security_group(panel: SettingsPanel, parent_layout: QVBoxLayout) -> None:
    """Gizlilik ve güvenlik ayar kartını kurar ve panel özniteliklerine bağlar."""
    privacy_box, privacy = group(t("Gizlilik ve güvenlik"))
    parent_layout.addWidget(privacy_box)

    panel.allow_cloud_file_content = QCheckBox(
        t("Dosya ve not içeriğinin bulut modele gitmesine izin ver")
    )
    panel.allow_cloud_file_content.setAccessibleName(
        t("Dosya ve not içeriğinin bulut modele gitmesine izin ver")
    )
    privacy.addWidget(panel.allow_cloud_file_content)
    privacy.addWidget(
        _hint_label(t("Kapalıyken dosya araması ve kasa notları yalnız yerel modele gider."))
    )

    panel.allow_cloud_clipboard = QCheckBox(
        t("Pano içeriğinin bulut modele gitmesine izin ver")
    )
    panel.allow_cloud_clipboard.setAccessibleName(
        t("Pano içeriğinin bulut modele gitmesine izin ver")
    )
    privacy.addWidget(panel.allow_cloud_clipboard)
    privacy.addWidget(
        _hint_label(
            t("Kapalıyken seçili metin eylemleri pano içeriğini yalnız yerel modele gönderir.")
        )
    )

    panel.memory_encrypt_at_rest = QCheckBox(
        t("Konuşma hafızasını diskte şifrele (Windows DPAPI)")
    )
    panel.memory_encrypt_at_rest.setAccessibleName(
        t("Konuşma hafızasını diskte şifrele (Windows DPAPI)")
    )
    privacy.addWidget(panel.memory_encrypt_at_rest)
    privacy.addWidget(
        _hint_label(
            t(
                "Açıkken yeni kalıcı hafıza kayıtları Windows kullanıcı hesabına bağlı "
                "şifrelenir; değişiklik yeniden başlatmada uygulanır."
            )
        )
    )


def refresh_close_without_confirmation(panel: SettingsPanel) -> None:
    """Onaysız kapatma listesini güncel `panel.applications` ile eşler."""
    widget = panel.close_without_confirmation_list
    checked = {
        str(widget.item(i).data(Qt.ItemDataRole.UserRole))
        for i in range(widget.count())
        if widget.item(i).checkState() == Qt.CheckState.Checked
    }
    blocked = widget.blockSignals(True)
    try:
        widget.clear()
        for alias, executable in panel.applications.items():
            label = _FRIENDLY_APPS.get(alias)
            shown = f"{t(label)} ({alias})" if label else alias
            item = QListWidgetItem(shown)
            item.setFlags(
                Qt.ItemFlag.ItemIsUserCheckable
                | Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
            )
            item.setCheckState(
                Qt.CheckState.Checked if alias in checked else Qt.CheckState.Unchecked
            )
            item.setData(Qt.ItemDataRole.UserRole, alias)
            item.setToolTip(executable)
            widget.addItem(item)
    finally:
        widget.blockSignals(blocked)


def sync_close_without_confirmation_access(panel: SettingsPanel) -> None:
    """Tüm kurulu uygulamalar açıkken veya liste boşken onaysız kapatma listesini uyarlar."""
    manual = not panel.all_applications.isChecked() and not panel._demo
    has_apps = bool(panel.applications)
    panel.close_without_confirmation_list.setEnabled(manual and has_apps)
    panel.close_without_confirmation_list.setVisible(manual)
