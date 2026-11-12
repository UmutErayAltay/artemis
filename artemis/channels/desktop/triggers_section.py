"""Ayarlar > Tetikleyiciler: yapılandırılmış tetikleyiciler ve son bildirim zamanı (ADR 0061).

Salt okunurdur: tetikleyici yalnız config'te kullanıcının elle yazdığı bir otomasyondur; bu panel
ve hiçbir araç onu eklemez ya da değiştirmez.
"""

from __future__ import annotations

from pathlib import Path
from zoneinfo import ZoneInfo

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QListWidget, QPushButton, QVBoxLayout, QWidget

from artemis.channels.desktop.provider_section import button_row, group, meta_label
from artemis.channels.desktop.settings_lists import ListWithEmpty
from artemis.config.settings import load_settings
from artemis.proactive.schedule import format_local
from artemis.proactive.store import ProactiveStore, ProactiveStoreError


class TriggersSection(QWidget):
    def __init__(self, config_path: Path, *, demo: bool = False) -> None:
        super().__init__()
        self._config_path = config_path
        self._demo = demo
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(24)
        box, listing = group("Tanımlı tetikleyiciler", divided=False)
        layout.addWidget(box)
        self.trigger_list = QListWidget()
        self.trigger_list.setAccessibleName("Tanımlı tetikleyiciler")
        self.trigger_area = ListWithEmpty(
            self.trigger_list, "Tanımlı tetikleyici yok", "", "Ayar dosyasını aç", height=200,
        )
        if self.trigger_area.empty.action_button is not None:
            self.trigger_area.empty.action_button.setEnabled(not demo)
            self.trigger_area.empty.action_button.clicked.connect(self._open_config)
        listing.addWidget(self.trigger_area)
        self.refresh_button = QPushButton("Yenile")
        listing.addLayout(button_row(self.refresh_button))
        listing.addWidget(meta_label(
            "Tetikleyiciler ayar dosyasında elle tanımlanır. "
            "Bildirim yalnız sorar: düğmeye basmadan hiçbir şey çalışmaz."
        ))
        self.notice = meta_label("")
        layout.addWidget(self.notice)
        self.refresh_button.clicked.connect(self.refresh)
        self.refresh_button.setEnabled(not demo)
        self.refresh()

    def refresh(self) -> None:
        self.trigger_list.clear()
        self.notice.setText("")
        try:
            settings = load_settings(self._config_path)
        except (OSError, ValueError):
            self.notice.setText("Ayar dosyası okunamadı.")
            self.trigger_area.show_empty("Tetikleyiciler okunamadı")
            return
        if settings.triggers.enabled and not settings.proactive.enabled:
            self.notice.setText("Tetikleyiciler için Zamanlı işler de açık olmalı.")
        store = None
        if settings.proactive.enabled and not self._demo:
            try:
                store = ProactiveStore(settings.state_dir / "proactive.sqlite3")
            except ProactiveStoreError:
                store = None
        tz = ZoneInfo(settings.proactive.timezone)
        for item in settings.triggers.items:
            last = "henüz yok"
            if store is not None:
                try:
                    moment = store.last_delivered(f"trigger:{item.name}:")
                except ProactiveStoreError:
                    moment = None
                if moment is not None:
                    last = format_local(moment, tz)
            button = f" · düğme: {'rutin ' + item.routine if item.routine else 'öneri'}"
            has_button = item.routine or item.suggest
            self.trigger_list.addItem(
                f"{item.name} · {item.folder} ({item.pattern}){button if has_button else ''}"
                f" · son bildirim: {last}"
            )
        if not settings.triggers.enabled:
            self.trigger_area.show_empty("Tetikleyiciler kapalı")
        elif self.trigger_list.count():
            self.trigger_area.show_list()
        else:
            self.trigger_area.show_empty("Tanımlı tetikleyici yok", action=True)

    def _open_config(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._config_path)))
