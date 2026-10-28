"""Petler sayfası (ADR 0048): seçili pet (hero), kart galerisi, yükleme/silme ve atölye.

Seçim hemen uygulanır (`skin_selected` sinyalini pencere `PetWidget.set_skin`'e bağlar).
Yalnız Artemis'in kendi klasöründeki pet'ler silinir; Codex pet'leri salt okunurdur.
Renkler yalnız temadan gelir (themes.py `wave2-pets`); bu dosyada sabit renk yoktur.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QMetaMethod, QSize, Qt, Signal
from PySide6.QtGui import QBrush, QFocusEvent, QIcon, QPixmap, QResizeEvent, QShowEvent
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from artemis.channels.desktop import pet_sprite
from artemis.channels.desktop.pet_creator_panel import CreatorPanel
from artemis.channels.desktop.pet_sprite import PetError, PetExistsError, PetInfo
from artemis.channels.desktop.projects_panel import (
    AutoHideLabel,
    CurrentPageStack,
    make_page_header,
)

_SOURCE_LABEL = {"artemis": "Artemis", "codex": "Codex"}
_ORB_LABEL = "Küre · Artemis"
_ORB_NAME = "Küre"
_MAX_WIDTH = 960  # içerik okunur genişlikte kalır; fazlası kenar boşluğu olur
_CARD_W, _CARD_H, _GAP = 140, 120, 8
_ICON = Path(__file__).with_name("artemis.ico")


class _ElidedLabel(QLabel):
    """Uzun ad kartı taşırmasın; kısaltma stil uygulandıktan sonraki yazıyla yapılır."""

    def __init__(self, text: str) -> None:
        super().__init__()
        self._full = text
        self.setToolTip(text)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._elide()

    def _elide(self) -> None:
        width = max(self.width() - 4, 0) or 100
        self.setText(self.fontMetrics().elidedText(self._full, Qt.TextElideMode.ElideRight, width))

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802 (Qt)
        super().resizeEvent(event)
        self._elide()


def _pet_pixmap(info: PetInfo | None, height: int) -> QPixmap:
    """Galeri ve hero aynı kaynağı kullanır: idle satırının ilk karesi (küre: uygulama simgesi)."""
    if info is not None:
        try:
            return pet_sprite.preview(info, height)
        except PetError:
            return QPixmap()
    # ICO büyük boyut döndürebilir; kart/hero kutusuna sığsın diye ölçeklenir.
    icon = QIcon(str(_ICON)).pixmap(256, 256)
    return icon.scaled(
        height, height, Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )


class _PetCard(QFrame):
    """Galeri kartı (DESIGN.md galleryCard): görünüm tamamen temadan gelir.

    Fare olayları listeye geçsin diye kart saydamdır; seçim ve klavye QListWidget'ta kalır.
    """

    def __init__(self, name: str, source: str, pixmap: QPixmap) -> None:
        super().__init__()
        self.setObjectName("galleryCard")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 6)
        lay.setSpacing(2)
        self.image = QLabel()
        self.image.setFixedHeight(64)
        self.image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if pixmap.isNull():
            self.image.setObjectName("meta")
            self.image.setText("önizleme yok")
        else:
            self.image.setPixmap(pixmap)
        lay.addWidget(self.image)
        self.name = _ElidedLabel(name)
        self.name.setObjectName("petName")
        self.name.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.name)
        self.source = QLabel(source)
        self.source.setObjectName("meta")
        self.source.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.source)
        self.chip = QLabel("Seçili", self)
        self.chip.setObjectName("selectedChip")
        self.chip.hide()
        self.set_state(selected=False, highlighted=False, focused=False)

    def set_state(self, *, selected: bool, highlighted: bool, focused: bool = False) -> None:
        for key, value in (
            ("selected", selected), ("highlighted", highlighted), ("focused", focused)
        ):
            self.setProperty(key, "true" if value else "false")
        self.chip.setVisible(selected)
        self.style().unpolish(self)
        self.style().polish(self)
        self.chip.adjustSize()
        self.chip.move(max(0, self.width() - self.chip.width() - 6), 6)

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802 (Qt)
        super().resizeEvent(event)
        self.chip.move(max(0, self.width() - self.chip.width() - 6), 6)


class _GalleryList(QListWidget):
    """Sarmalı kart ızgarası; kendi kaydırıcısı yoktur, yüksekliği satır sayısından gelir."""

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("galleryList")
        # IconMode öğe widget'ını yalnız metin alanına sığdırır; ListMode + sarma widget'a tüm
        # hücreyi verir ve ızgara yine satırlara sarılır.
        self.setViewMode(QListView.ViewMode.ListMode)
        self.setFlow(QListView.Flow.LeftToRight)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setMovement(QListView.Movement.Static)
        self.setWrapping(True)
        self.setUniformItemSizes(True)
        # Simge öğede kalır (erişilebilirlik) ama çizilmez; kart hücreyi örter.
        self.setIconSize(QSize(0, 0))
        self.setGridSize(QSize(_CARD_W + _GAP, _CARD_H + _GAP))
        self.setSpacing(0)
        self.setFrameShape(QFrame.Shape.NoFrame)
        # Sayfada tek kaydırma bölgesi var: liste kendi içinde kaymaz, sayfa kayar.
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

    # Klavye odağı kartta değil listede durur; odak değişince kartlar yeniden boyanır.
    focus_changed = Signal()
    keyboard_focus = False

    def focusInEvent(self, event: QFocusEvent) -> None:  # noqa: N802 (Qt)
        super().focusInEvent(event)
        self.keyboard_focus = True
        self.focus_changed.emit()

    def focusOutEvent(self, event: QFocusEvent) -> None:  # noqa: N802 (Qt)
        super().focusOutEvent(event)
        self.keyboard_focus = False
        self.focus_changed.emit()

    def fit_height(self) -> None:
        per_row = max(1, self.viewport().width() // (_CARD_W + _GAP))
        rows = max(1, -(-self.count() // per_row))
        self.setFixedHeight(rows * (_CARD_H + _GAP) + 4)

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802 (Qt)
        super().resizeEvent(event)
        self.fit_height()


class PetSection(QWidget):
    skin_selected = Signal(str)  # pet kimliği ya da "orb"
    model_settings_requested = Signal()  # boş durumdaki "Ayarlar'da model ekle"

    def __init__(
        self, pets_dir: Path, codex_dir: Path | None = None, *, demo: bool = False,
        config_path: Path | None = None,
    ) -> None:
        super().__init__()
        self._pets_dir = pets_dir
        self._codex_dir = codex_dir
        self._current = pet_sprite.ORB
        self._pets: dict[str, PetInfo] = {}
        self._demo = demo
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 24)  # sayfa altında tutarlı 24px nefes payı
        layout.setSpacing(16)
        layout.addWidget(
            make_page_header("Petler", "Masaüstü arkadaşını seç ya da yenisini üret.")
        )
        self.stack = CurrentPageStack()
        layout.addWidget(self.stack, 1)
        self.stack.addWidget(self._build_gallery_page())
        # Atölye ayrı görünümdür: aşama çubuğu kaydırılan gövdenin dışında sabit kalır
        # (sayfada tek kaydırma bölgesi + sabit başlık); app.py'deki dış kaydırıcı boşta kalır.
        self.workshop_page = QWidget()
        workshop = QVBoxLayout(self.workshop_page)
        workshop.setContentsMargins(0, 0, 0, 0)
        workshop.setSpacing(8)
        back_row = QHBoxLayout()
        self.back_button = QPushButton("‹ Petlerime dön")
        self.back_button.setObjectName("ghost")
        self.back_button.clicked.connect(self.show_gallery)
        back_row.addWidget(self.back_button)
        back_row.addStretch(1)
        workshop.addLayout(back_row)
        self.stack.addWidget(self.workshop_page)
        # Pet üretici (ADR 0048 madde 8): config yoksa (yalnız liste kullanan testler) eklenmez.
        self.creator: CreatorPanel | None = None
        if config_path is not None:
            self.creator = CreatorPanel(config_path, pets_dir, demo=demo)
            self.creator.installed.connect(self._creator_installed)
            self.creator.model_settings_requested.connect(self.model_settings_requested)
            # Kart içerik yüksekliğinde ve üstten hizalı; yan boşluklar modelsiz kompakt kartı
            # (azami 480px) ortalar, modelli kartı ise (büyük ağırlık) neredeyse hiç daraltmaz.
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.addStretch(1)
            row.addWidget(self.creator, 1000)
            row.addStretch(1)
            workshop.addLayout(row)
        workshop.addStretch(1)  # kart ekranı doldurmaya zorlanmaz: ölü krem alan oluşmaz
        self.new_button.setVisible(self.creator is not None)
        if demo:
            self.import_button.setEnabled(False)
        self.refresh()

    def _build_gallery_page(self) -> QWidget:
        # Kaydırıcı yok: Petler sayfasının tek kaydırma bölgesi app.py'deki pet_scroll'dur.
        content = QWidget()
        content.setObjectName("flat")
        content.setMaximumWidth(_MAX_WIDTH)
        page = QVBoxLayout(content)
        page.setContentsMargins(0, 0, 0, 0)
        page.setSpacing(16)

        # Hero: seçili pet büyük; masaüstündeki arkadaşın yüzü sayfanın yıldızı.
        hero = QFrame()
        hero.setObjectName("petHero")
        row = QHBoxLayout(hero)
        row.setContentsMargins(24, 24, 24, 24)
        row.setSpacing(24)
        self.hero_image = QLabel()
        self.hero_image.setObjectName("petStage")
        self.hero_image.setFixedSize(144, 144)
        self.hero_image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        row.addWidget(self.hero_image)
        text = QVBoxLayout()
        text.setSpacing(4)
        text.addStretch(1)
        self.hero_name = QLabel()
        self.hero_name.setObjectName("greeting")
        self.hero_source = QLabel()
        self.hero_source.setObjectName("meta")
        self.hero_caption = QLabel()
        self.hero_caption.setObjectName("fieldLabel")
        self.hero_caption.setWordWrap(True)
        for label in (self.hero_name, self.hero_source, self.hero_caption):
            text.addWidget(label)
        text.addStretch(1)
        row.addLayout(text, 1)
        actions = QVBoxLayout()
        actions.setSpacing(8)
        actions.addStretch(1)
        self.hero_select_button = QPushButton("Masaüstüne al")
        self.hero_select_button.setObjectName("primary")
        self.hero_select_button.clicked.connect(self.select_current)
        self.new_button = QPushButton("Yeni pet üret")
        self.new_button.clicked.connect(self.show_workshop)
        for button in (self.hero_select_button, self.new_button):
            button.setMinimumWidth(128)
            actions.addWidget(button)
        actions.addStretch(1)
        row.addLayout(actions)
        page.addWidget(hero)

        gallery = QFrame()
        gallery.setObjectName("card")
        card = QVBoxLayout(gallery)
        card.setContentsMargins(16, 16, 16, 16)
        card.setSpacing(12)
        head = QHBoxLayout()
        head.setSpacing(8)
        heading = QLabel("Yüklü petler")
        heading.setObjectName("sectionTitle")
        head.addWidget(heading)
        head.addStretch(1)
        self.delete_button = QPushButton("Sil")
        self.delete_button.setObjectName("danger")
        self.delete_button.clicked.connect(self.delete_selected)
        # Düz etiket + ok işareti: menü açtığı belli olsun (ok QSS'te menu-indicator).
        self.import_button = QPushButton("Pet yükle…")
        self.import_button.setObjectName("importButton")
        menu = QMenu(self.import_button)
        menu.addAction("Zip dosyası…").triggered.connect(self._pick_zip)
        menu.addAction("Klasör…").triggered.connect(self._pick_folder)
        self.import_button.setMenu(menu)
        self.import_button.setToolTip(
            "Pet'ler Codex biçimindedir: klasörde pet.json ve spritesheet.webp/png. "
            "Topluluk pet'lerinin lisansı ve içeriği senin sorumluluğundadır; Artemis "
            "hiçbirini kendisi dağıtmaz."
        )
        for button in (self.delete_button, self.import_button):
            button.setMinimumWidth(88)
            head.addWidget(button)
        card.addLayout(head)
        self.listing = _GalleryList()
        self.listing.setAccessibleName("Kullanılabilir pet'ler")
        self.listing.itemSelectionChanged.connect(self._update_buttons)
        self.listing.focus_changed.connect(self._sync_cards)
        self.listing.itemDoubleClicked.connect(lambda _item: self.select_current())
        card.addWidget(self.listing)
        self.status = AutoHideLabel()
        card.addWidget(self.status)
        page.addWidget(gallery)
        page.addStretch(1)
        return content

    # ------------------------------------------------------------------ görünümler

    def show_workshop(self) -> None:
        if self.creator is not None:
            self.stack.setCurrentWidget(self.workshop_page)

    def show_gallery(self) -> None:
        self.stack.setCurrentIndex(0)

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 (Qt)
        super().showEvent(event)
        self.sync_model_hook()

    def sync_model_hook(self) -> None:
        """Boş durum düğmesi yalnız pencere sinyali bağladıysa görünür (çalışmayan düğme olmaz)."""
        if self.creator is not None:
            hooked = self.isSignalConnected(
                QMetaMethod.fromSignal(self.model_settings_requested)
            )
            self.creator.set_model_hook(hooked)

    # ------------------------------------------------------------------ liste

    def _creator_installed(self, pet_id: str) -> None:
        self.refresh()
        self._current = pet_id
        self.skin_selected.emit(pet_id)
        self.refresh()
        self.show_gallery()  # yeni pet hero'da görünsün

    def set_current(self, pet_id: str) -> None:
        self._current = pet_id
        self.refresh()

    def refresh(self) -> None:
        self._pets = {p.id: p for p in pet_sprite.list_pets(self._pets_dir, self._codex_dir)}
        self.listing.clear()
        self._add_item(_ORB_LABEL, pet_sprite.ORB, _ORB_NAME, "Artemis", None, "")
        for info in self._pets.values():
            self._add_item(
                f"{info.display_name} · {_SOURCE_LABEL[info.source]}", info.id,
                info.display_name, _SOURCE_LABEL[info.source], info, info.description,
            )
        for i in range(self.listing.count()):
            if self.listing.item(i).data(Qt.ItemDataRole.UserRole) == self._current:
                self.listing.setCurrentRow(i)
        if self.listing.currentRow() < 0:
            self.listing.setCurrentRow(0)
        self.listing.fit_height()
        self._update_buttons()

    def _add_item(
        self, label: str, pet_id: str, name: str, source: str, info: PetInfo | None, tip: str
    ) -> None:
        pixmap = _pet_pixmap(info, 64)
        chosen = pet_id == self._current
        # Metin ve simge sınama/erişilebilirlik için öğede kalır; kart widget'ı üstünü örter.
        item = QListWidgetItem(label + ("\nseçili" if chosen else ""))
        item.setData(Qt.ItemDataRole.UserRole, pet_id)
        item.setForeground(QBrush(Qt.GlobalColor.transparent))
        item.setSizeHint(QSize(_CARD_W, _CARD_H))
        item.setToolTip(tip or name)
        if pixmap.isNull():
            item.setText(label + "\nönizleme yok")
        else:
            item.setIcon(QIcon(pixmap))
        self.listing.addItem(item)
        self.listing.setItemWidget(item, _PetCard(name, source, pixmap))

    def _selected_id(self) -> str | None:
        item = self.listing.currentItem()
        return None if item is None else str(item.data(Qt.ItemDataRole.UserRole))

    def _name_of(self, pet_id: str) -> str:
        info = self._pets.get(pet_id)
        return _ORB_NAME if info is None else info.display_name

    def _sync_cards(self) -> None:
        current_item = self.listing.currentItem()
        for i in range(self.listing.count()):
            item = self.listing.item(i)
            card = self.listing.itemWidget(item)
            if not isinstance(card, _PetCard):
                continue
            pet_id = str(item.data(Qt.ItemDataRole.UserRole))
            chosen = pet_id == self._current
            is_current = item is current_item
            card.set_state(
                selected=chosen, highlighted=is_current,
                focused=is_current and self.listing.keyboard_focus,
            )
            name = self._name_of(pet_id)
            spoken = f"{name}, seçili" if chosen else name
            card.setAccessibleName(spoken)
            item.setData(Qt.ItemDataRole.AccessibleTextRole, spoken)

    def _update_hero(self) -> None:
        info = self._pets.get(self._current)
        name = _ORB_NAME if info is None else info.display_name
        source = "Artemis" if info is None else _SOURCE_LABEL[info.source]
        pixmap = _pet_pixmap(info, 120)
        if pixmap.isNull():
            self.hero_image.setText("önizleme yok")
        else:
            self.hero_image.setPixmap(pixmap)
        self.hero_name.setText(name)
        self.hero_source.setText(source)
        self.hero_image.setAccessibleName(f"Seçili pet: {name}")
        self.hero_caption.setText(
            "Klasik küre masaüstünde duruyor; bir pet seçersen yerini o alır."
            if info is None
            else "Masaüstünde seninle duruyor."
        )
        self.hero_image.setToolTip("" if info is None else info.description)

    def _update_buttons(self) -> None:
        pet_id = self._selected_id()
        info = self._pets.get(pet_id or "")
        differs = pet_id is not None and pet_id != self._current
        own = info is not None and info.source == "artemis" and not self._demo
        # Bağlamsal eylemler: Masaüstüne al yalnız başka pet vurgulanınca, Sil yalnız silinebilirde.
        self.hero_select_button.setVisible(differs)
        self.hero_select_button.setEnabled(differs)
        if differs and pet_id is not None:
            self.hero_select_button.setAccessibleName(f"{self._name_of(pet_id)} masaüstüne al")
        self.delete_button.setVisible(own)
        self.delete_button.setEnabled(own)
        self._sync_cards()
        self._update_hero()

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
