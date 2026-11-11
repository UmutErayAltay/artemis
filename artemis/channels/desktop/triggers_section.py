"""Ayarlar > Tetikleyiciler: yapılandırılmış tetikleyiciler ve son bildirim zamanı (ADR 0061).

Salt okunurdur: tetikleyici yalnız config'te kullanıcının elle yazdığı bir otomasyondur; bu panel
ve hiçbir araç onu eklemez ya da değiştirmez.
"""

from __future__ import annotations

from pathlib import Path
from zoneinfo import ZoneInfo

from PySide6.QtWidgets import QListWidget, QPushButton, QVBoxLayout, QWidget

from artemis.channels.desktop.provider_section import button_row, group, meta_label
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
        self.trigger_list.setMaximumHeight(200)
        self.trigger_list.setAccessibleName("Tanımlı tetikleyiciler")
        listing.addWidget(self.trigger_list)
        self.refresh_button = QPushButton("Yenile")
        listing.addLayout(button_row(self.refresh_button))
        listing.addWidget(meta_label(
            "Tetikleyiciler config.yaml içinde `triggers.items` altında elle tanımlanır. "
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
            return
        if not settings.triggers.enabled:
            self.notice.setText("Tetikleyiciler kapalı: yukarıdaki kutuyu aç ve Uygula'ya bas.")
        elif not settings.proactive.enabled:
            self.notice.setText("Tetikleyiciler için proactive.enabled de açık olmalı.")
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
