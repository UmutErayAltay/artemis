"""Sayfa iskeleti parçaları: başlık, kaydırılan sayfa, kart, boş durum, katlanır bölüm."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QSize, Qt, Signal
from PySide6.QtGui import QResizeEvent, QShowEvent
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from artemis.channels.desktop.widgets import HelpTip, with_help


def make_label(text: str, name: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName(name)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


class AutoHideLabel(QLabel):
    """Metin boşken gizlenen `meta` etiketi; boş durum satırı kartta boşluk bırakmaz."""

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("meta")
        self.setWordWrap(True)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.hide()

    def setText(self, text: str) -> None:
        super().setText(text)
        self.setVisible(bool(text))


class PathHint(QLabel):
    """Tek satır, ortadan kısaltılan yol ipucu: uzun yol kartı büyütmez, tam yol ipucundadır.

    Sarmalı etiket boşluksuz uzun yolda ya taşar ya satırı katlar ve altındaki eylem satırını
    kıvrımın altına iter (Projeler 1280x850 sözleşmesi).
    """

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("meta")
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.full_text = ""

    def set_full_text(self, text: str, tooltip: str = "") -> None:
        self.full_text = text
        self.setToolTip(tooltip or text)
        self._elide()

    def _elide(self) -> None:
        width = max(self.width(), 0)
        shown = self.fontMetrics().elidedText(self.full_text, Qt.TextElideMode.ElideMiddle, width)
        super().setText(shown)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._elide()


def make_field(caption: str, widget: QWidget, help: str = "") -> QWidget:  # noqa: A002
    """Etiket alanın üstünde kalıcıdır; placeholder yalnız örnek verir. `help` varsa "?" etiketin
    hemen yanında durur."""
    box = QWidget()
    lay = QVBoxLayout(box)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(4)
    label = make_label(caption, "fieldLabel")
    if help:
        lay.addLayout(with_help(label, help))
    else:
        lay.addWidget(label)
    lay.addWidget(widget)
    return box


def make_page_header(
    title: str,
    subtitle: str | None = None,
    actions: tuple[QWidget | tuple[QWidget, str], ...] = (),
) -> QWidget:
    """Her sayfanın aynı başlık satırı: 48px sabit, sağda eylemler, 4px altında alt başlık.

    Sayfalar arası başlık konumu bu tek yerden gelir; sayfa düzenindeki 16px aralık
    ilk karta kadar olan boşluğu verir.
    """
    host = QWidget()
    column = QVBoxLayout(host)
    column.setContentsMargins(0, 0, 0, 0)
    column.setSpacing(4)
    row = QHBoxLayout()
    row.setSpacing(8)
    heading = QLabel(title)
    heading.setObjectName("pageTitle")
    heading.setTextFormat(Qt.TextFormat.PlainText)
    heading.setFixedHeight(48)
    row.addWidget(heading)
    row.addStretch(1)
    for action in actions:
        if isinstance(action, tuple):  # (düğme, yardım): "?" düğmenin hemen sağında
            row.addWidget(action[0])
            row.addWidget(HelpTip(action[1]), 0, Qt.AlignmentFlag.AlignVCenter)
            row.setSpacing(8)
        else:
            row.addWidget(action)
    column.addLayout(row)
    if subtitle:
        column.addWidget(make_label(subtitle, "meta"))
    return host


class CurrentPageStack(QWidget):
    """Boyutu yalnız GÖRÜNEN sayfadan gelen yığın (QStackedWidget'ın küçük API'si).

    QStackedLayout sizeHint, minimumSize VE heightForWidth'te tüm sayfaların en büyüğünü
    döndürür; kaydırma bölgesinin içinde kısa sayfa uzun sayfa kadar boş kaydırma bırakır
    (boyut ipuçlarını ezmek yetmedi: kaydırma alanı yerleşimin heightForWidth'ini sorar).
    Gizli widget yerleşimden düştüğü için burada yalnız mevcut sayfa gösterilir.
    """

    currentChanged = Signal(int)

    def __init__(self) -> None:
        super().__init__()
        self._pages: list[QWidget] = []
        self._current = -1
        self._box = QVBoxLayout(self)
        self._box.setContentsMargins(0, 0, 0, 0)
        # İçteki sayfalarda görünürlük veya sarılan etiket metni değiştiğinde yığın en az
        # aktif sayfanın anlık minimum/heightForWidth yüksekliği kadar büyümeli; aksi hâlde
        # QScrollArea eski boyutu korur ve alt widget'lar minimumSizeHint'in altına sıkışır.
        self._box.setSizeConstraint(QLayout.SizeConstraint.SetMinAndMaxSize)

    def hasHeightForWidth(self) -> bool:
        page = self.currentWidget()
        return page.hasHeightForWidth() if page is not None else super().hasHeightForWidth()

    def heightForWidth(self, width: int) -> int:
        page = self.currentWidget()
        if page is not None:
            # QScrollArea.setWidgetResizable(True) içerik yüksekliğini seçerken `heightForWidth`
            # değerini `minimumSizeHint` yerine kullanır. İçteki bazı çocuklarda (ör. `_WrapLabel`
            # veya maximumWidth=640 olan ipuçları) gerçek sarma yüksekliği `minimumSizeHint`'e
            # yansırken, üst `QBoxLayout.totalHeightForWidth(width)` çocuklara 880/640 yerine
            # tam kaydırma genişliğini geçirip daha küçük bir sarma yüksekliği hesaplayabilir.
            # Bu yüzden `heightForWidth` asla `minimumSizeHint` yüksekliğinin altına düşmemelidir.
            min_h = page.minimumSizeHint().height()
            if page.hasHeightForWidth():
                return max(min_h, page.heightForWidth(width))
            return min_h
        return super().heightForWidth(width)

    def sizeHint(self) -> QSize:
        page = self.currentWidget()
        if page is not None:
            hint = page.sizeHint().expandedTo(page.minimumSizeHint())
            if page.hasHeightForWidth() and self.width() > 0:
                hfw = page.heightForWidth(self.width())
                if hfw > 0:
                    hint.setHeight(max(hint.height(), hfw))
            return hint
        return super().sizeHint()

    def minimumSizeHint(self) -> QSize:
        page = self.currentWidget()
        if page is not None:
            hint = page.minimumSizeHint()
            if page.hasHeightForWidth() and self.width() > 0:
                hfw = page.heightForWidth(self.width())
                if hfw > 0:
                    hint.setHeight(max(hint.height(), hfw))
            return hint
        return super().minimumSizeHint()

    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.LayoutRequest:
            self._box.invalidate()
            self.updateGeometry()
        return super().event(event)

    def addWidget(self, page: QWidget) -> int:
        self._pages.append(page)
        self._box.addWidget(page)
        page.setVisible(len(self._pages) == 1)
        if self._current < 0:
            self._current = 0
        return len(self._pages) - 1

    def count(self) -> int:
        return len(self._pages)

    def widget(self, index: int) -> QWidget | None:
        return self._pages[index] if 0 <= index < len(self._pages) else None

    def currentIndex(self) -> int:
        return self._current

    def currentWidget(self) -> QWidget | None:
        return self.widget(self._current)

    def setCurrentWidget(self, page: QWidget) -> None:
        self.setCurrentIndex(self._pages.index(page))

    def setCurrentIndex(self, index: int) -> None:
        if not 0 <= index < len(self._pages) or index == self._current:
            return
        self._pages[self._current].hide()
        self._current = index
        self._pages[index].show()
        self._box.invalidate()
        self.updateGeometry()
        self.currentChanged.emit(index)


class SettingsTabWidget(QTabWidget):
    """Yalnız etkin sekmenin minimum/heightForWidth yüksekliğini yansıtan QTabWidget."""

    def __init__(self) -> None:
        super().__init__()
        self.setUsesScrollButtons(True)
        self.currentChanged.connect(self._on_tab_changed)

    def _bar_height(self) -> int:
        bar = self.tabBar()
        if bar is None or bar.isHidden():
            return 0
        return bar.sizeHint().height()

    def _on_tab_changed(self, index: int) -> None:
        for i in range(self.count()):
            page = self.widget(i)
            if page is not None:
                policy = (
                    QSizePolicy.Policy.Preferred
                    if i == index
                    else QSizePolicy.Policy.Ignored
                )
                page.setSizePolicy(QSizePolicy.Policy.Preferred, policy)
        self.updateGeometry()

    def addTab(self, widget: QWidget, label: str) -> int:  # type: ignore[override]
        idx = super().addTab(widget, label)
        self._on_tab_changed(self.currentIndex())
        return idx

    def hasHeightForWidth(self) -> bool:
        page = self.currentWidget()
        return page.hasHeightForWidth() if page is not None else super().hasHeightForWidth()

    def heightForWidth(self, width: int) -> int:
        page = self.currentWidget()
        if page is not None:
            bar_h = self._bar_height()
            min_h = page.minimumSizeHint().height()
            page_h = max(min_h, page.heightForWidth(width)) if page.hasHeightForWidth() else min_h
            return bar_h + page_h
        return super().heightForWidth(width)

    def minimumSizeHint(self) -> QSize:
        page = self.currentWidget()
        if page is not None:
            bar_h = self._bar_height()
            hint = page.minimumSizeHint()
            width = page.width() if page.width() > 0 else self.width()
            if page.hasHeightForWidth() and width > 0:
                hfw = page.heightForWidth(width)
                if hfw > hint.height():
                    hint.setHeight(hfw)
            return QSize(
                max(super().minimumSizeHint().width(), hint.width()),
                bar_h + hint.height(),
            )
        return super().minimumSizeHint()

    def sizeHint(self) -> QSize:
        page = self.currentWidget()
        if page is not None:
            bar_h = self._bar_height()
            hint = page.sizeHint().expandedTo(page.minimumSizeHint())
            width = page.width() if page.width() > 0 else self.width()
            if page.hasHeightForWidth() and width > 0:
                hfw = page.heightForWidth(width)
                if hfw > hint.height():
                    hint.setHeight(hfw)
            return QSize(
                max(super().sizeHint().width(), hint.width()),
                bar_h + hint.height(),
            )
        return super().sizeHint()

    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.LayoutRequest:
            self.updateGeometry()
        return super().event(event)


# Tüm ana sekmelerin (Projeler, Otomasyon, Geçmiş, Petler, Ayarlar) ortak sayfa kabuğu: içerik en
# çok bu genişlikte, ortada; başlık bloğun sol kenarında. Sekme değişince başlık zıplamasın (UX-02).
PAGE_MAX_WIDTH = 1240


class PageBody(QWidget):
    """Sayfa gövdesi: en çok PAGE_MAX_WIDTH, kendi asgari genişliğinin altına asla sıkıştırılmaz.

    Sabit üst sınır, asgarisi 1240px'i aşan bir sayfada (Projeler'de iki sütun + uzun durum metni)
    çocukları üst üste bindirirdi; sınır asgariye göre yükselir, taşma eskisi gibi kaydırılır.
    """

    def __init__(self) -> None:
        super().__init__()
        self.setMaximumWidth(PAGE_MAX_WIDTH)

    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.LayoutRequest:
            self.setMaximumWidth(max(PAGE_MAX_WIDTH, self.minimumSizeHint().width()))
        return super().event(event)


def center_page(scroll: QScrollArea) -> None:
    """Kaydırma alanındaki sayfa PAGE_MAX_WIDTH'i aşmaz ve yatayda ortalanır (üstten hizalı)."""
    scroll.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)


def make_scroll_page(panel: QWidget) -> QVBoxLayout:
    """Sayfada tek kaydırma bölgesi: dar pencerede kartlar sıkışmak yerine kayar."""
    outer = QVBoxLayout(panel)
    outer.setContentsMargins(0, 0, 0, 0)
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    center_page(scroll)
    content = PageBody()
    scroll.setWidget(content)
    outer.addWidget(scroll)
    layout = QVBoxLayout(content)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(16)
    return layout


def make_card() -> tuple[QFrame, QVBoxLayout]:
    card = QFrame()
    card.setObjectName("card")
    lay = QVBoxLayout(card)
    lay.setContentsMargins(16, 16, 16, 16)
    lay.setSpacing(12)
    return card, lay


class EmptyState(QFrame):
    """Boş durum kutusu; başlık ve gövde etiketleri açık alan olarak tutulur."""

    def __init__(self, title: str, body: str, action_text: str | None = None) -> None:
        super().__init__()
        self.setObjectName("emptyState")
        lay = QVBoxLayout(self)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.setSpacing(4)
        self.title_label = make_label(title, "emptyTitle")
        self.body_label = make_label(body, "emptyBody")
        for label in (self.title_label, self.body_label):
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lay.addWidget(label)
        # Yalnız "yüklenmedi" durumunda eylem sunulur (DESIGN: yüklendi-boş durumunda eylem yok).
        self.action_button: QPushButton | None = None
        if action_text:
            self.action_button = QPushButton(action_text)
            lay.addWidget(self.action_button, 0, Qt.AlignmentFlag.AlignCenter)
        self.set_text(title, body)

    def set_text(self, title: str, body: str) -> None:
        self.title_label.setText(title)
        self.body_label.setText(body)
        self._fit_width()

    def _fit_width(self) -> None:
        """Sarmalı etiket dar ipucu verir ("Göster'e / bas."); gövde ~360px'e kadar tek satır kalır.

        Ölçü stil uygulandıktan sonraki yazı tipiyle alınmalı; bu yüzden gösterimde de çağrılır.
        """
        wide = self.body_label.fontMetrics().horizontalAdvance(self.body_label.text()) + 8
        self.body_label.setFixedWidth(min(360, wide))
        title = self.title_label.fontMetrics().horizontalAdvance(self.title_label.text())
        self.title_label.setMinimumWidth(title + 8)

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self._fit_width()


def repolish(widget: QWidget) -> None:
    """Dinamik özellik seçicileri (tone/state) yalnız unpolish+polish ile güncellenir."""
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)


class CollapsibleSection(QWidget):
    """Başlık satırı (ok + başlık) ve katlı gövde; varsayılan kapalı.

    Güvenlik metni silinmez, yalnız katlı durur; bu yüzden gövde etiketi ayrı tutulur.
    """

    def __init__(self, title: str, body: str, trailing: QWidget | None = None) -> None:
        super().__init__()
        self._title = title
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        self.toggle = QPushButton()
        self.toggle.setObjectName("disclosure")
        self.toggle.setCheckable(True)
        self.toggle.setAccessibleName(title)
        self.body = make_label(body, "fieldLabel")
        self.body.setVisible(False)
        self.toggle.toggled.connect(self._sync)
        self._sync(False)
        row = QHBoxLayout()
        row.addWidget(self.toggle)
        row.addStretch()
        if trailing is not None:
            row.addWidget(trailing)  # başlık satırının sağı: ayrı satır olsaydı dikey yer yerdi
        lay.addLayout(row)
        lay.addWidget(self.body)

    def _sync(self, opened: bool) -> None:
        self.body.setVisible(opened)
        self.toggle.setText(("▾  " if opened else "▸  ") + self._title)
