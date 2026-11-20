"""Ayar bölümlerinin ortak form parçaları: çizgi, grup, alan, meta etiketi, sarılan düğme satırı."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import (
    QRect,
    QSize,
    Qt,
)
from PySide6.QtWidgets import (
    QFrame,
    QLabel,
    QLayout,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from artemis.channels.desktop.retranslate import set_i18n_context
from artemis.channels.desktop.widgets import HelpTip, with_help


def divider() -> QFrame:
    line = QFrame()
    line.setObjectName("divider")
    line.setFixedHeight(1)
    return line


class _HeightForWidthBox(QWidget):
    """Sarılan alt etiketlerin `heightForWidth` değerini `minimumSizeHint` içine yansıtan kutu.

    Neden: `QVBoxLayout` bir çocuk QWidget'a yer ayırırken `minimumSize()` ile `heightForWidth`
    değerini karşılaştırmaz; çocuk kutunun içinde dinamik olarak büyüyen/gösterilen alanlar ve
    sarılan `QLabel` varsa üst yerleşim kutuyu `minimumSizeHint` altında bırakabiliyordu.
    Ayrıca üst yerleşim `heightForWidth(w)` sorduğunda içteki daraltılmış (ör. maxWidth=640)
    etiketlerin `minimumSizeHint` yüksekliğinin altına inmemesi için `heightForWidth` de
    en az `minimumSizeHint().height()` döndürür.
    """

    def heightForWidth(self, width: int) -> int:
        hfw = super().heightForWidth(width)
        min_h = super().minimumSizeHint().height()
        return max(hfw, min_h)

    def minimumSizeHint(self) -> QSize:
        hint = super().minimumSizeHint()
        if self.hasHeightForWidth() and self.width() > 0:
            hfw = super().heightForWidth(self.width())
            if hfw > hint.height():
                hint.setHeight(hfw)
        return hint

    def sizeHint(self) -> QSize:
        hint = super().sizeHint().expandedTo(self.minimumSizeHint())
        return hint


def group(
    title: str,
    hint: str | None = None,
    *,
    divided: bool = True,
    help: str = "",  # noqa: A002
    title_context: str | None = None,
) -> tuple[QWidget, QVBoxLayout]:
    """Düz ayar grubu: ince çizgi + başlık + kısa ipucu. Kart içinde kart olmaması için çerçeve yok;
    çizgi grubun içinde olduğundan grup gizlenince çizgi de gider. `help` başlığa "?" ekler."""
    box = _HeightForWidthBox()
    box.setObjectName("group")
    outer = QVBoxLayout(box)
    outer.setContentsMargins(0, 0, 0, 0)
    outer.setSpacing(12)
    if divided:
        outer.addWidget(divider())
        outer.addSpacing(12)  # çizginin altı da üstü gibi 24px (sayfa aralığı 12 + bu 12)
    heading = QVBoxLayout()
    heading.setSpacing(4)
    head = QLabel(title)
    head.setObjectName("sectionTitle")
    if title_context:
        set_i18n_context(head, title_context)
    heading.addLayout(with_help(head, help)) if help else heading.addWidget(head)
    if hint:
        heading.addWidget(meta_label(hint))
    outer.addLayout(heading)
    return box, outer


class _WrapLabel(QLabel):
    """Genişliği belli olduğunda `heightForWidth` değerini asgari boyuta taşıyan sarma etiketi.

    Neden: Qt'nin `QLabel.minimumSizeHint()` metodu kelime sarmalı etiketlerde mevcut genişlikteki
    satır sayısını hesaba katmaz; ayrıca `QBoxLayout.totalHeightForWidth(w)` çağrısı çocuğun
    `maximumWidth()` sınırını (ör. `_check_hint` için 640px) dikkate almadan `w` genişliğini
    doğrudan `heightForWidth(w)`'ye iletir. Etiket `heightForWidth` içinde kendi `maximumWidth`
    ve mevcut genişlik sınırını uygularsa üst yerleşim her zaman gerçek sarma yüksekliğini alır.
    """

    def heightForWidth(self, width: int) -> int:
        eff_w = min(width, self.maximumWidth())
        if self.width() > 0:
            eff_w = min(eff_w, self.width())
        return super().heightForWidth(max(1, eff_w))

    def minimumSizeHint(self) -> QSize:
        hint = super().minimumSizeHint()
        if self.wordWrap() and self.width() > 0:
            hfw = self.heightForWidth(self.width())
            if hfw > hint.height():
                hint.setHeight(hfw)
        return hint

    def sizeHint(self) -> QSize:
        return super().sizeHint().expandedTo(self.minimumSizeHint())


def meta_label(text: str) -> QLabel:
    label = _WrapLabel(text)
    label.setObjectName("meta")
    label.setWordWrap(True)
    label.setTextFormat(Qt.TextFormat.PlainText)
    return label


def field(
    parent: QVBoxLayout, label: str, control: QWidget | QLayout, hint: str | None = None,
    *, help: str = "",  # noqa: A002 - çağrı yerlerinde en okunur ad
    label_context: str | None = None,
) -> None:
    """Etiket alanın üstünde (13px, 4px boşluk); ipucu altta 12px soluk.

    `help` verilirse etiketin hemen sağında "?" baloncuğu durur: her formda aynı yerde, böylece
    kullanıcı ayarın neyi etkilediğini hep aynı noktada bulur."""
    group = QVBoxLayout()
    group.setSpacing(4)
    caption = QLabel(label)
    caption.setObjectName("fieldLabel")
    if label_context:
        set_i18n_context(caption, label_context)
    if help:
        group.addLayout(with_help(caption, help))
    else:
        group.addWidget(caption)
    if isinstance(control, QWidget):
        caption.setBuddy(control)
        group.addWidget(control)
    else:
        group.addLayout(control)
    if hint:
        group.addWidget(meta_label(hint))
    parent.addLayout(group)


class _FlowRow(QLayout):
    """Sağa yaslı düğme satırı; sığmayınca düğmeler alt satıra sarılır.

    Dar pencerede (720px) uzun etiketli düğmeler tek satıra sığmıyor, kırpılıyordu ya da
    sayfayı yatay kaydırıyordu (UX-01). Asgari genişlik en geniş tek düğmedir; ihtiyaç
    olursa yükseklik genişliğe göre büyür (heightForWidth).
    """

    def __init__(self) -> None:
        super().__init__()
        self._items: list[Any] = []
        self._gap = 8
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, arg__1: Any) -> None:
        self._items.append(arg__1)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> Any:
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int) -> Any:
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self) -> Qt.Orientation:
        # Dikey büyümesin: arkaşsız QVBoxLayout içinde düğme satırı boşluğu yemesin.
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, arg__1: int) -> int:
        return self._arrange(QRect(0, 0, arg__1, 0), apply=False)

    def setGeometry(self, arg__1: QRect) -> None:
        super().setGeometry(arg__1)
        self._arrange(arg__1, apply=True)

    def _visible_items(self) -> list[Any]:
        """Gizli (`.isHidden()`) çocukları dışlar; üst widget gösterilmeden `isVisible()` False
        kaldığı için özellikle `not widget.isHidden()` denetlenir."""
        visible: list[Any] = []
        for item in self._items:
            widget = item.widget()
            if widget is not None and widget.isHidden():
                continue
            visible.append(item)
        return visible

    def sizeHint(self) -> QSize:
        hints = [item.sizeHint() for item in self._visible_items()]
        if not hints:
            return QSize(0, 0)
        margins = self.contentsMargins()
        width = (
            sum(h.width() for h in hints)
            + self._gap * max(0, len(hints) - 1)
            + margins.left()
            + margins.right()
        )
        height = max(h.height() for h in hints) + margins.top() + margins.bottom()
        return QSize(width, height)

    def minimumSize(self) -> QSize:
        sizes = [item.minimumSize() for item in self._visible_items()]
        if not sizes:
            return QSize(0, 0)
        margins = self.contentsMargins()
        return QSize(
            max(s.width() for s in sizes) + margins.left() + margins.right(),
            max(s.height() for s in sizes) + margins.top() + margins.bottom(),
        )

    def _arrange(self, rect: QRect, *, apply: bool) -> int:
        visible = self._visible_items()
        if not visible:
            return 0
        margins = self.contentsMargins()
        area = self.contentsRect() if apply else rect.marginsRemoved(margins)
        lines: list[list[Any]] = [[]]
        used = 0
        for item in visible:
            width = item.sizeHint().width()
            if lines[-1] and used + self._gap + width > area.width():
                lines.append([])
                used = 0
            used += (self._gap if lines[-1] else 0) + width
            lines[-1].append(item)
        y = area.y()
        for line in lines:
            height = max((item.sizeHint().height() for item in line), default=0)
            x = area.right() + 1 - sum(i.sizeHint().width() for i in line) - self._gap * (
                len(line) - 1
            )
            for item in line:
                size = item.sizeHint()
                if apply:
                    top = y + (height - size.height()) // 2  # düğme yanındaki küçük "?" ortalanır
                    item.setGeometry(QRect(max(x, area.x()), top, size.width(), size.height()))
                x += size.width() + self._gap
            y += height + self._gap
        content_height = max(0, y - self._gap - area.y())
        return content_height + margins.top() + margins.bottom()


def button_row(*buttons: QPushButton, help: str = "") -> QLayout:  # noqa: A002
    """Sağa yaslı düğme satırı; birincil düğme en sonda verilir, sığmazsa alt satıra sarılır.

    `help` verilirse son düğmenin sağında "?" baloncuğu durur (satırdaki düğmeleri anlatır)."""
    row = _FlowRow()
    for button in buttons:
        button.setMinimumWidth(88)
        row.addWidget(button)
    if help:
        row.addWidget(HelpTip(help))
    return row
