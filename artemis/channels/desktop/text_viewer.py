"""Salt okunur uygulama içi metin görüntüleyici (araştırma raporu, gün özeti).

Metin dış kaynaktan (web araştırması) gelebilir; bu yüzden YALNIZ veri olarak gösterilir:
bağlantılar tıklanınca hiçbir şey açılmaz (ne tarayıcı ne dosya), görseller yüklenmez. Böylece
"raporu aç" eylemi, rapordaki bir bağlantı ya da `file:` görseli yüzünden kendiliğinden bir yan
etki üretemez; kullanıcı adresi isterse kopyalayıp kendisi açar.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)


class _DataBrowser(QTextBrowser):
    def loadResource(self, type: int, name: Any) -> Any:  # noqa: A002 - Qt imzası
        # Markdown içindeki görsel/`file:` kaynağı diskten ya da ağdan çekilmez.
        return None


class TextViewerDialog(QDialog):
    def __init__(
        self, title: str, text: str, *, markdown: bool = True, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._text = text
        self.setWindowTitle(title)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)
        heading = QLabel(title)
        heading.setObjectName("title")
        heading.setWordWrap(True)
        layout.addWidget(heading)
        self.browser = _DataBrowser()
        self.browser.setReadOnly(True)
        self.browser.setOpenLinks(False)  # tıklama sayfayı değiştirmez
        self.browser.setOpenExternalLinks(False)  # ve dışarıda bir şey açmaz
        self.browser.setAccessibleName(title)
        # Koyu temada varsayılan mavi bağlantı okunmaz; vurgu rengi temadan gelir.
        link = self.browser.palette().highlight().color().name()
        self.browser.document().setDefaultStyleSheet(f"a {{ color: {link}; }}")
        if markdown:
            self.browser.setMarkdown(text)
        else:
            self.browser.setPlainText(text)
        layout.addWidget(self.browser, 1)
        self.copy_button = QPushButton("Kopyala")
        self.close_button = QPushButton("Kapat")
        self.close_button.setObjectName("primary")
        self.close_button.setDefault(True)
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addStretch(1)
        row.addWidget(self.copy_button)
        row.addWidget(self.close_button)
        layout.addLayout(row)
        self.copy_button.clicked.connect(self.copy_text)
        self.close_button.clicked.connect(self.accept)
        screen = QGuiApplication.primaryScreen()
        area = screen.availableGeometry() if screen is not None else None
        width = min(720, area.width() - 80) if area is not None else 720
        height = min(640, area.height() - 80) if area is not None else 640
        self.resize(max(width, 320), max(height, 240))

    def copy_text(self) -> None:
        # Panoya görüntülenen değil ham metin gider (Markdown işaretleri korunur).
        clipboard = QGuiApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(self._text)


_MAX_BYTES = 1_000_000  # dev rapor/özet pencereyi dondurmasın


def open_text_file(path: Path, title: str, parent: QWidget | None = None) -> None:
    """Dosyayı (sınırlı, UTF-8, bozuk baytlar değiştirilerek) uygulama içinde gösterir.

    Dış uygulama açılmaz; okunamazsa kullanıcıya mesaj verilir, uygulama çökmez."""
    try:
        with path.open("rb") as handle:
            raw = handle.read(_MAX_BYTES + 1)
    except OSError:
        QMessageBox.warning(parent, title, "Dosya okunamadı.")
        return
    text = raw[:_MAX_BYTES].decode("utf-8", errors="replace")
    if len(raw) > _MAX_BYTES:
        text += "\n\n… (dosya çok uzun; baştaki 1 MB gösterildi)"
    TextViewerDialog(title, text, parent=parent).exec()
