"""Ayarlar bölümlerinin listeleri için ortak boş durum: liste boşken ya da özellik kapalıyken
listenin yerinde kısa, ortalı bir ileti gösterilir (Projeler'deki EmptyState ile aynı görünüm)."""

from __future__ import annotations

from PySide6.QtWidgets import QListWidget, QStackedWidget

from artemis.channels.desktop.page_kit import EmptyState


class ListWithEmpty(QStackedWidget):
    """`listing` ile bir EmptyState'i üst üste tutar; biri görünür. Liste nesnesi aynı kalır,
    yani eski kod ve bağlar (`count()`, `item()`, `model()`) etkilenmez."""

    def __init__(
        self, listing: QListWidget, title: str, body: str = "", action_text: str | None = None,
        *, height: int = 160,
    ) -> None:
        super().__init__()
        self.listing = listing
        self.empty = EmptyState(title, body, action_text)
        self.addWidget(listing)
        self.addWidget(self.empty)
        self.setMinimumHeight(min(height, 112))
        self.setMaximumHeight(height)
        listing.setMaximumHeight(height)

    def show_list(self) -> None:
        self.setCurrentWidget(self.listing)

    def show_empty(self, title: str, body: str = "", *, action: bool = False) -> None:
        self.empty.set_text(title, body)
        self.empty.body_label.setVisible(bool(body))
        if self.empty.action_button is not None:
            self.empty.action_button.setVisible(action)
        self.setCurrentWidget(self.empty)
