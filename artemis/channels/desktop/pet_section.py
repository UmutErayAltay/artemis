"""Ayarlar > Pet (ADR 0048): pet seçme, yükleme ve silme.

Seçim hemen uygulanır (`skin_selected` sinyalini pencere `PetWidget.set_skin`'e bağlar).
Yalnız Artemis'in kendi klasöründeki pet'ler silinir; Codex pet'leri salt okunurdur.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from artemis.channels.desktop import pet_sprite
from artemis.channels.desktop.pet_sprite import PetError, PetExistsError, PetInfo

_SOURCE_LABEL = {"artemis": "Artemis", "codex": "Codex"}
_ORB_LABEL = "Küre (varsayılan) · Artemis"


class PetSection(QWidget):
    skin_selected = Signal(str)  # pet kimliği ya da "orb"

    def __init__(
        self, pets_dir: Path, codex_dir: Path | None = None, *, demo: bool = False
    ) -> None:
        super().__init__()
        self._pets_dir = pets_dir
        self._codex_dir = codex_dir
        self._current = pet_sprite.ORB
        self._pets: dict[str, PetInfo] = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.listing = QListWidget()
        self.listing.setIconSize(QSize(40, 44))
        self.listing.setAccessibleName("Kullanılabilir pet'ler")
        self.listing.itemSelectionChanged.connect(self._update_buttons)
        layout.addWidget(self.listing)
        row = QHBoxLayout()
        self.select_button = QPushButton("Seç")
        self.select_button.clicked.connect(self.select_current)
        self.import_button = QPushButton("Pet yükle…")
        menu = QMenu(self.import_button)
        menu.addAction("Zip dosyası…").triggered.connect(self._pick_zip)
        menu.addAction("Klasör…").triggered.connect(self._pick_folder)
        self.import_button.setMenu(menu)
        self.delete_button = QPushButton("Sil")
        self.delete_button.clicked.connect(self.delete_selected)
        for button in (self.select_button, self.import_button, self.delete_button):
            row.addWidget(button)
        row.addStretch(1)
        layout.addLayout(row)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status)
        note = QLabel(
            "Pet'ler Codex biçimindedir: klasörde pet.json ve spritesheet.webp/png. "
            "Topluluk pet'lerinin lisansı ve içeriği senin sorumluluğundadır; Artemis "
            "hiçbirini kendisi dağıtmaz."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        # Paket 3 (ADR 0048 madde 8): "Pet üret" sohbeti buraya eklenecek. Şimdilik boş.
        self.creator_slot = QVBoxLayout()
        layout.addLayout(self.creator_slot)
        if demo:
            self.import_button.setEnabled(False)
        self._demo = demo
        self.refresh()

    # ------------------------------------------------------------------ liste

    def set_current(self, pet_id: str) -> None:
        self._current = pet_id
        self.refresh()

    def refresh(self) -> None:
        self._pets = {p.id: p for p in pet_sprite.list_pets(self._pets_dir, self._codex_dir)}
        self.listing.clear()
        orb = QListWidgetItem(_ORB_LABEL + (" · seçili" if self._current == "orb" else ""))
        orb.setData(Qt.ItemDataRole.UserRole, pet_sprite.ORB)
        self.listing.addItem(orb)
        for info in self._pets.values():
            text = f"{info.display_name} · {_SOURCE_LABEL[info.source]}"
            item = QListWidgetItem(text + (" · seçili" if info.id == self._current else ""))
            item.setData(Qt.ItemDataRole.UserRole, info.id)
            try:
                item.setIcon(QIcon(pet_sprite.preview(info)))
            except PetError:
                item.setText(text + " · önizleme yok")
            item.setToolTip(info.description)
            self.listing.addItem(item)
        for i in range(self.listing.count()):
            if self.listing.item(i).data(Qt.ItemDataRole.UserRole) == self._current:
                self.listing.setCurrentRow(i)
        if self.listing.currentRow() < 0:
            self.listing.setCurrentRow(0)
        self._update_buttons()

    def _selected_id(self) -> str | None:
        item = self.listing.currentItem()
        return None if item is None else str(item.data(Qt.ItemDataRole.UserRole))

    def _update_buttons(self) -> None:
        pet_id = self._selected_id()
        self.select_button.setEnabled(pet_id is not None)
        info = self._pets.get(pet_id or "")
        own = info is not None and info.source == "artemis"
        self.delete_button.setEnabled(own and not self._demo)

    # ------------------------------------------------------------- eylemler

    def select_current(self) -> None:
        pet_id = self._selected_id()
        if pet_id is None:
            return
        self._current = pet_id
        self.skin_selected.emit(pet_id)
        self.refresh()
        self.status.setText("Pet seçildi.")

    def show_problem(self, message: str) -> None:
        """Pencere, pet yüklenemeyip küreye dönünce nedenini burada gösterir."""
        self._current = pet_sprite.ORB
        self.refresh()
        self.status.setText(message)

    def _confirm(self, text: str) -> bool:
        answer = QMessageBox.question(self, "Pet", text)
        return answer == QMessageBox.StandardButton.Yes

    def _pick_zip(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Pet zip dosyası", "", "Zip (*.zip)")
        if path:
            self.import_path(Path(path))

    def _pick_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Pet klasörü")
        if path:
            self.import_path(Path(path))

    def import_path(self, path: Path) -> PetInfo | None:
        overwrite = False
        while True:
            try:
                info = pet_sprite.import_pet(path, self._pets_dir, overwrite=overwrite)
            except PetExistsError as error:
                if overwrite or not self._confirm(f"{error} Üzerine yazılsın mı?"):
                    self.status.setText(str(error))
                    return None
                overwrite = True
                continue
            except PetError as error:
                self.status.setText(f"Pet yüklenemedi: {error}")
                return None
            break
        self.refresh()
        self.status.setText(f"'{info.display_name}' yüklendi. Seçmek için listeden seç.")
        if info.id == self._current:
            self.skin_selected.emit(info.id)  # ezilen pet açıksa yeniden yüklensin
        return info

    def delete_selected(self) -> None:
        info = self._pets.get(self._selected_id() or "")
        if info is None or info.source != "artemis":
            self.status.setText("Yalnız Artemis'e yüklenen pet'ler silinebilir.")
            return
        if not self._confirm(f"'{info.display_name}' silinsin mi?"):
            return
        try:
            pet_sprite.delete_pet(self._pets_dir, info.id)
        except PetError as error:
            self.status.setText(str(error))
            return
        if self._current == info.id:
            self._current = pet_sprite.ORB
            self.skin_selected.emit(pet_sprite.ORB)
        self.refresh()
        self.status.setText("Pet silindi.")
