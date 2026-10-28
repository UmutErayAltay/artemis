"""Projeler düz metin sunumudur; eylem sahipliği CommandRunner'da kalır."""

from __future__ import annotations

import contextlib
import html
import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QResizeEvent, QShowEvent
from PySide6.QtWidgets import (
    QBoxLayout,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from artemis.channels.desktop.code_agent_config import (
    add_workdir,
    guess_project_path,
    save_project_default,
    slug_alias,
    workdir_alias_error,
)
from artemis.config.settings import (
    BUILTIN_AGENT,
    OWN_REPO_ALIAS,
    CodeAgentConfig,
    Settings,
    load_settings,
)
from artemis.tools.base import ToolResult
from artemis.tools.plugins.code_agent import (
    AnswerArguments,
    CancelArguments,
    PrepareArguments,
    StartArguments,
    StatusArguments,
)
from artemis.tools.project_spec import AUTHORITY_BOUNDARY, ProjectSpec, approval_text

STATUS_LABELS = {
    "queued": "Sırada",
    "running": "Çalışıyor",
    "waiting": "Cevap bekliyor",
    "cancelling": "Durduruluyor",
    "cancelled": "İptal edildi",
    "timeout": "Süre doldu",
    "done": "Doğrulandı",
    "error": "Başarısız",
    "interrupted": "Kesintiye uğradı",
    "cleanup_pending": "Süreç temizliği belirsiz",
}


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

    def setText(self, text: str) -> None:  # noqa: N802 - Qt imzası
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

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802 (Qt adı)
        super().resizeEvent(event)
        self._elide()


def make_field(caption: str, widget: QWidget) -> QWidget:
    """Etiket alanın üstünde kalıcıdır; placeholder yalnız örnek verir."""
    box = QWidget()
    lay = QVBoxLayout(box)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(4)
    lay.addWidget(make_label(caption, "fieldLabel"))
    lay.addWidget(widget)
    return box


def make_page_header(
    title: str, subtitle: str | None = None, actions: tuple[QWidget, ...] = ()
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

    def addWidget(self, page: QWidget) -> int:  # noqa: N802 (Qt)
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

    def currentIndex(self) -> int:  # noqa: N802 (Qt)
        return self._current

    def currentWidget(self) -> QWidget | None:  # noqa: N802 (Qt)
        return self.widget(self._current)

    def setCurrentWidget(self, page: QWidget) -> None:  # noqa: N802 (Qt)
        self.setCurrentIndex(self._pages.index(page))

    def setCurrentIndex(self, index: int) -> None:  # noqa: N802 (Qt)
        if not 0 <= index < len(self._pages) or index == self._current:
            return
        self._pages[self._current].hide()
        self._current = index
        self._pages[index].show()
        self.currentChanged.emit(index)


def make_scroll_page(panel: QWidget) -> QVBoxLayout:
    """Sayfada tek kaydırma bölgesi: dar pencerede kartlar sıkışmak yerine kayar."""
    outer = QVBoxLayout(panel)
    outer.setContentsMargins(0, 0, 0, 0)
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    content = QWidget()
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


# Durum çipi: (etiket, ton). Ton QSS'te `tone` özelliğiyle renklenir; renk kodu burada yok.
STATUS_CHIPS = {
    "queued": ("Hazırlanıyor", "running"),
    "running": ("Çalışıyor", "running"),
    "waiting": ("Cevap bekliyor", "waiting"),
    "cancelling": ("Durduruluyor", "waiting"),
    "cancelled": ("Durduruldu", "neutral"),
    "timeout": ("Zaman aşımı", "error"),
    "done": ("Bitti", "done"),
    "error": ("Hata", "error"),
    "interrupted": ("Kesildi", "error"),
    "cleanup_pending": ("Temizleniyor", "error"),
}
# Yeniden kurulumun iptal edeceği, henüz bitmemiş işler.
ACTIVE_JOB_STATUSES = frozenset({"queued", "running", "waiting", "cancelling", "cleanup_pending"})
WORKDIR_WARNING = (
    "Ajan bu klasörde başlar ve oradaki kuralları yükler; dosyaları değiştirebilir."
)
FLOW_STEPS = ("Hazırla", "İncele", "Başlat")
LIST_MIN_ROWS = 1  # tek taslak tek satır yüksekliği alır; boşken EmptyState gösterilir
LIST_MAX_ROWS = 6
EDITOR_MIN_HEIGHT = 72
EDITOR_MAX_HEIGHT = 120


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


def spec_to_html(kind: str, record: dict[str, Any]) -> str:
    """Taslak/iş ayrıntısı: kalın başlıklar + madde listeleri.

    Kullanıcı ve model metni html.escape'ten geçer; ham HTML asla yorumlanmaz.
    """
    spec = record if kind == "draft" else record.get("spec", {})
    esc = html.escape

    def section(title: str, text: str) -> str:
        return f"<p><b>{title}</b><br>{esc(text).replace(chr(10), '<br>')}</p>"

    def bullets(title: str, items: list[Any]) -> str:
        rows = "".join(f"<li>{esc(str(item))}</li>" for item in items) or "<li>—</li>"
        return f"<p><b>{title}</b></p><ul>{rows}</ul>"

    # İlk blok: ajan, başlangıç klasörü ve hedef repo. İş kaydındaki alanlar (agent, workdir,
    # workdir_path) çalışan işin gerçeğidir; onay metni arka uçtaki aynı işlevden gelir.
    where = {**spec, **{k: record[k] for k in ("agent", "workdir", "workdir_path") if k in record}}
    parts = [
        section("Ajan ve başlangıç klasörü", approval_text(where)),
        section("Proje", str(spec.get("project", ""))),
        section("Görev", str(spec.get("task", ""))),
        bullets("Kapsam", list(spec.get("scope", []))),
        bullets("Kabul ölçütleri", list(spec.get("acceptance", []))),
    ]
    if kind == "job":
        commits = json.dumps(record.get("commits", []), ensure_ascii=False)
        parts += [
            section("Durum", STATUS_LABELS.get(record.get("status", ""), "Bilinmiyor")),
            section("Özet", str(record.get("summary", ""))),
            section("Soru", str(record.get("question") or "—")),
            section("Commit'ler", commits),
            section("Kalan değişiklikler", str(record.get("leftover_changes"))),
            section("İş kimliği", str(record.get("job_id", ""))),
        ]
    return "".join(parts)


class ProjectsPanel(QWidget):
    tool_requested = Signal(str, object)
    # Yapılandırma dosyası yazıldı; pencere çalışma zamanını yeniden kurup yeni ayarı devreye alır.
    config_saved = Signal()

    # Bu içerik genişliğinin altında iki sütun alt alta dizilir.
    WIDE_BREAKPOINT = 1000

    def __init__(self, *, demo: bool = False, config_path: Path | None = None) -> None:
        super().__init__()
        self._demo = demo
        self._config_path = config_path
        self._settings: Settings | None = None
        self._items: list[tuple[str, dict[str, Any]]] = []
        self._busy = True
        # Her durum sonucunda artar; pencere "liste taze mi" diye buna bakar.
        self.status_serial = 0
        layout = make_scroll_page(self)
        layout.addWidget(
            make_page_header(
                "Projeler",
                "Yerel bir depoda iş taslağı hazırla, kapsamını incele, onaylayıp başlat.",
            )
        )
        # Üç adımlı akış yalnız seçili taslak/iş durumundan türer (_update_flow); durum tutmaz.
        self.flow_bar = QFrame()
        self.flow_bar.setObjectName("stageBar")
        flow_row = QHBoxLayout(self.flow_bar)
        flow_row.setContentsMargins(0, 0, 0, 0)
        flow_row.setSpacing(8)
        self._flow_labels: list[QLabel] = []
        for i, name in enumerate(FLOW_STEPS):
            label = make_label(name, "stage")
            label.setWordWrap(False)
            self._flow_labels.append(label)
            flow_row.addWidget(label)
            if i < len(FLOW_STEPS) - 1:
                flow_row.addWidget(make_label("→", "meta"))

        # Güvenlik metni silinmedi/yumuşatılmadı; yalnız varsayılan olarak katlı.
        self.rules = CollapsibleSection(
            "Bu işler nasıl çalışır?",
            "Taslağı hazırlayın, tam kapsamı inceleyin ve onayla başlatın. " + AUTHORITY_BOUNDARY,
            trailing=self.flow_bar,
        )
        self.rules_toggle = self.rules.toggle
        self.hint = self.rules.body
        layout.addWidget(self.rules)

        # Sol kart: yeni iş formu.
        left, left_lay = make_card()
        left_lay.addWidget(make_label("Yeni iş", "sectionTitle"))
        self.project = QComboBox()
        self.task = QPlainTextEdit()
        self.task.setPlaceholderText("Örn. Giriş formundaki doğrulamayı düzelt")
        self.scope = QPlainTextEdit()
        self.scope.setPlaceholderText("Örn. Her satır bir madde: yalnız login modülü")
        self.acceptance = QPlainTextEdit()
        self.acceptance.setPlaceholderText("Örn. Her satır bir madde: testler geçiyor")
        for editor in (self.task, self.scope, self.acceptance):
            # 1280x850'de "Taslak hazırla" kıvrımın altına düşmesin: 72px, içerikle 120px'e büyür.
            editor.setFixedHeight(EDITOR_MIN_HEIGHT)
            editor.textChanged.connect(lambda e=editor: self._grow_editor(e))
            editor.textChanged.connect(lambda: self.set_busy(self._busy))
        self.project.setAccessibleName("Proje")
        left_lay.addWidget(make_field("Proje", self.project))
        left_lay.addWidget(self._build_agent_row())
        for caption, widget in (
            ("Görev", self.task),
            ("Kapsam", self.scope),
            ("Kabul ölçütleri", self.acceptance),
        ):
            widget.setAccessibleName(caption)
            left_lay.addWidget(make_field(caption, widget))
        self.prepare_button = QPushButton("Taslak hazırla")
        # Pasif düğmenin nedeni düğmenin hemen yanında muted metindir: ayrı satır olsaydı
        # 1280x850'de yeni seçiciler yüzünden kıvrımın altına düşerdi.
        self.prepare_reason = AutoHideLabel()
        prepare_row = QHBoxLayout()
        prepare_row.setSpacing(12)
        prepare_row.addWidget(self.prepare_button)
        prepare_row.addWidget(self.prepare_reason, 1)
        left_lay.addLayout(prepare_row)
        left_lay.addStretch()  # boşluk eylem satırlarının altında kalır; düğme son editöre yapışık

        # Sağ kart: liste, ayrıntı, eylemler.
        right, right_lay = make_card()
        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        title_row.addWidget(make_label("Taslaklar ve işler", "sectionTitle"))
        self.status_chip = QLabel()
        self.status_chip.setObjectName("statusChip")
        self.status_chip.setTextFormat(Qt.TextFormat.PlainText)
        self.status_chip.setVisible(False)
        title_row.addWidget(self.status_chip)
        title_row.addStretch()
        right_lay.addLayout(title_row)
        self.items = QListWidget()
        self.items.setAccessibleName("Taslaklar ve işler")
        self.items.currentRowChanged.connect(self._select)
        self.list_stack = QStackedWidget()
        self.list_stack.addWidget(self.items)
        self.list_stack.addWidget(
            EmptyState("Henüz taslak yok", "Soldan bir görev yazıp taslak hazırla.")
        )
        right_lay.addWidget(self.list_stack)
        self.preview = QTextBrowser()
        self.preview.setOpenLinks(False)
        self.preview.setOpenExternalLinks(False)
        self.preview.setAccessibleName("Taslak ayrıntısı")
        self.preview.setPlaceholderText("Bir taslak veya iş seç; ayrıntısı burada görünür.")
        self.preview.setMinimumHeight(120)
        right_lay.addWidget(self.preview, 1)
        # Bekleyen soru alanı yalnız seçili iş "waiting" iken görünür (set_busy).
        self.question_box = QWidget()
        q_lay = QVBoxLayout(self.question_box)
        q_lay.setContentsMargins(0, 0, 0, 0)
        q_lay.setSpacing(4)
        self.answer = QPlainTextEdit()
        self.answer.setPlaceholderText("Örn. Mavi olsun")
        self.answer.setAccessibleName("Bekleyen soru")
        self.answer.setMinimumHeight(72)
        self.answer.setMaximumHeight(96)
        q_lay.addWidget(make_label("Bekleyen soru", "fieldLabel"))
        q_lay.addWidget(self.answer)
        self.answer_button = QPushButton("Cevabı onayla ve devam et")
        answer_row = QHBoxLayout()
        answer_row.addStretch()
        answer_row.addWidget(self.answer_button)
        q_lay.addLayout(answer_row)
        right_lay.addWidget(self.question_box)
        self.refresh_button = QPushButton("Durumu yenile")
        self.start_button = QPushButton("Taslağı onayla ve başlat")
        self.start_button.setObjectName("primary")
        actions = QHBoxLayout()
        actions.setSpacing(8)
        actions.addStretch()
        actions.addWidget(self.refresh_button)
        actions.addWidget(self.start_button)
        right_lay.addLayout(actions)
        self.start_reason = AutoHideLabel()
        self.start_reason.setAlignment(Qt.AlignmentFlag.AlignRight)
        right_lay.addWidget(self.start_reason)
        # Yıkıcı eylem birincil eylemden ayrı satırda ve yalnız iş seçiliyken görünür.
        self.cancel_button = QPushButton("İşi durdur")
        self.cancel_button.setObjectName("danger")
        cancel_row = QHBoxLayout()
        cancel_row.addWidget(self.cancel_button)
        cancel_row.addStretch()
        right_lay.addSpacing(8)
        right_lay.addLayout(cancel_row)
        self.notice = make_label(
            "Demo modunda proje süreçleri kapalı." if demo else "Durum bekleniyor.", "meta"
        )
        right_lay.addWidget(self.notice)

        self._columns = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        self._columns.setSpacing(16)
        self._columns.addWidget(left, 5)
        self._columns.addWidget(right, 6)
        layout.addLayout(self._columns, 1)
        self.prepare_button.clicked.connect(self._prepare)
        self.start_button.clicked.connect(self._start)
        self.refresh_button.clicked.connect(self.refresh)
        self.answer_button.clicked.connect(self._answer)
        self.cancel_button.clicked.connect(self._cancel)
        self.project.currentTextChanged.connect(self._apply_project_defaults)
        self.agent_combo.currentIndexChanged.connect(self._sync_agent_ui)
        self.workdir_combo.currentIndexChanged.connect(self._sync_agent_ui)
        self.add_folder_button.clicked.connect(self._add_folder)
        self.default_button.clicked.connect(self._make_default)
        self.reload_config()
        self._sync_list()
        self.set_busy(True)

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802 (Qt adı)
        super().resizeEvent(event)
        wide = event.size().width() >= self.WIDE_BREAKPOINT
        self._columns.setDirection(
            QBoxLayout.Direction.LeftToRight if wide else QBoxLayout.Direction.TopToBottom
        )
        # İki seçici geniş düzende yan yana, dar düzende alt alta (aynı eşik).
        self._agent_row.setDirection(
            QBoxLayout.Direction.LeftToRight if wide else QBoxLayout.Direction.TopToBottom
        )

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 (Qt adı)
        super().showEvent(event)
        # Ayarlar'da Uygula ile eklenen ajan/klasörler sayfaya dönünce görünür.
        self.reload_config()

    # --- Kod ajanı ve başlangıç klasörü seçimi (ADR 0049) -------------------------------------

    def _build_agent_row(self) -> QWidget:
        host = QWidget()
        column = QVBoxLayout(host)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(4)
        self.agent_combo = QComboBox()
        self.agent_combo.setAccessibleName("Kod ajanı")
        self.workdir_combo = QComboBox()
        self.workdir_combo.setAccessibleName("Başlangıç klasörü")
        for combo in (self.agent_combo, self.workdir_combo):
            combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(8)
        self.add_folder_button = QPushButton("Klasör ekle…")
        folder_box = QWidget()
        folder_row = QHBoxLayout(folder_box)
        folder_row.setContentsMargins(0, 0, 0, 0)
        folder_row.setSpacing(8)
        folder_row.addWidget(self.workdir_combo, 1)
        folder_row.addWidget(self.add_folder_button)
        self._agent_row = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        self._agent_row.setSpacing(12)
        self._agent_row.addWidget(make_field("Kod ajanı", self.agent_combo), 2)
        self._agent_row.addWidget(make_field("Başlangıç klasörü", folder_box), 3)
        column.addLayout(self._agent_row)
        # Çözülmüş yol 12px soluk ipucudur; uyarı yalnız proje dışı klasörde görünür.
        self.workdir_hint = PathHint()
        self.workdir_warning = make_label("", "chipWarning")
        self.workdir_warning.hide()
        self.agent_reason = AutoHideLabel()
        # İkincil eylem yol ipucunun satırında (ince, düz düğme): ayrı satır olsaydı çipli durumda
        # 1280x850'de kıvrımın altına düşerdi. Yol dar kalırsa ortadan kısalır, tam yol ipucundadır.
        self.default_button = QPushButton("Bunu bu proje için varsayılan yap")
        self.default_button.setObjectName("ghost")
        # Genel düğme stili 32px asgari yükseklik verir; satır ipucu kadar ince kalsın.
        self.default_button.setStyleSheet("min-height: 0px; padding: 0px 8px;")
        self.default_button.setFixedHeight(20)
        hint_row = QHBoxLayout()
        hint_row.setSpacing(8)
        hint_row.addWidget(self.workdir_hint, 1)
        hint_row.addWidget(self.default_button)
        column.addLayout(hint_row)
        for widget in (self.workdir_warning, self.agent_reason):
            column.addWidget(widget)
        return host

    def _config(self) -> CodeAgentConfig:
        return self._settings.code_agent if self._settings else CodeAgentConfig()

    def _vault(self) -> Path | None:
        return self._settings.memory.vault_path if self._settings else None

    def reload_config(self) -> None:
        """Seçenekleri yapılandırmadan okur; okunamazsa önceki liste kalır (iş durmaz)."""
        if self._config_path is not None:
            with contextlib.suppress(OSError, ValueError):
                self._settings = load_settings(self._config_path)
        config = self._config()
        agent, workdir = self.agent_combo.currentData(), self.workdir_combo.currentData()
        self.agent_combo.blockSignals(True)
        self.workdir_combo.blockSignals(True)
        self.agent_combo.clear()
        only_claude = not config.agents
        for name in (BUILTIN_AGENT, *sorted(config.agents)):
            self.agent_combo.addItem(f"{name} (yalnız kurulu)" if only_claude else name, name)
        self.agent_combo.setToolTip(
            "Yalnız Claude kurulu. Diğer ajanlar Ayarlar > GitHub bölümünden eklenir."
            if only_claude
            else ""
        )
        self.workdir_combo.clear()
        for name in config.workdir_choices(self._vault()):
            self.workdir_combo.addItem("Proje klasörü" if name == OWN_REPO_ALIAS else name, name)
        self.agent_combo.blockSignals(False)
        self.workdir_combo.blockSignals(False)
        # Yeniden kurulum kullanıcının seçimini bozmasın; yoksa projenin varsayılanı gelir.
        if agent is not None and workdir is not None:
            self._pick(self.agent_combo, agent)
            self._pick(self.workdir_combo, workdir)
            self._sync_agent_ui()
        else:
            self._apply_project_defaults()

    @staticmethod
    def _pick(combo: QComboBox, alias: str) -> None:
        combo.setCurrentIndex(max(0, combo.findData(alias)))

    def _saved_choice(self) -> tuple[str, str]:
        """Seçili projenin kayıtlı varsayılanı: (ajan, klasör takma adı)."""
        config, project = self._config(), self.project.currentText()
        return config.project_agent(project), config.project_workdir(project)

    def _apply_project_defaults(self, *_args: object) -> None:
        agent, workdir = self._saved_choice()
        self.agent_combo.blockSignals(True)
        self.workdir_combo.blockSignals(True)
        self._pick(self.agent_combo, agent)
        self._pick(self.workdir_combo, workdir)
        self.agent_combo.blockSignals(False)
        self.workdir_combo.blockSignals(False)
        self._sync_agent_ui()

    def selected_choice(self) -> tuple[str, str]:
        return str(self.agent_combo.currentData()), str(self.workdir_combo.currentData())

    def _project_repo(self) -> Path | None:
        config, project = self._config(), self.project.currentText()
        return config.project_path(project) or guess_project_path(config.repos_dir, project)

    def _workdir_path(self, workdir: str) -> Path | None:
        chosen = self._config().workdir_choices(self._vault()).get(workdir)
        return self._project_repo() if chosen is None else chosen

    def _sync_agent_ui(self, *_args: object) -> None:
        agent, workdir = self.selected_choice()
        repo, folder = self._project_repo(), self._workdir_path(workdir)
        self.workdir_hint.set_full_text(
            "Başlangıç yolu: " + (str(folder) if folder else "(çözülmedi)"), str(folder or "")
        )
        # Formda kısa uyarı: hedef repo zaten Proje seçicisinden bellidir. Tam backend
        # onay metni ön izleme bloğunda ve onay penceresinde durur (1280x850 sığma sözleşmesi).
        self.workdir_warning.setText(WORKDIR_WARNING)
        self.workdir_warning.setVisible(workdir != OWN_REPO_ALIAS)
        config = self._config()
        usable = self._config_path is not None and not self._demo
        # Neden satırı yalnız eyleme engel olan durumlarda görünür (1280x850'de "Taslak hazırla"
        # kıvrımın altına düşmesin); "yalnız Claude kurulu" bilgisi seçicinin kendisinde durur.
        reason = "Demo modunda ajan ve klasör seçimi kapalı." if self._demo else ""
        saved = self._saved_choice()
        changed = (agent, workdir) != saved
        can_default = usable and changed and bool(self.project.currentText()) and repo is not None
        if usable and changed and repo is None:
            reason = "Bu projenin klasörü bulunamadı; varsayılan yazılamaz."
        self.agent_reason.setText(reason)
        self.agent_combo.setEnabled(usable and len(config.agents) > 0)
        self.workdir_combo.setEnabled(usable)
        self.add_folder_button.setEnabled(usable)
        self.default_button.setEnabled(can_default)

    def _after_config_write(self, message: str) -> None:
        self.reload_config()
        self.notice.setText(message)
        self.config_saved.emit()

    def _make_default(self) -> None:
        if self._config_path is None or self._demo:
            return
        agent, workdir = self.selected_choice()
        alias = self.project.currentText()
        try:
            save_project_default(
                self._config_path,
                alias,
                agent=agent,
                workdir=workdir,
                default_agent=self._config().default_agent,
                project_path=self._project_repo(),
            )
        except (OSError, ValueError):
            self.notice.setText("Varsayılan kaydedilemedi; mevcut ayarlar korunuyor.")
            return
        self._after_config_write(f"{alias} için varsayılan kaydedildi: {agent}, {workdir}.")

    def _add_folder(self) -> None:
        if self._config_path is None or self._demo:
            return
        # Klasörü kullanıcı arayüzde seçer; model yol veremez (ADR 0049).
        chosen = QFileDialog.getExistingDirectory(self, "Başlangıç klasörü seç", str(Path.home()))
        if not chosen:
            return
        folder = Path(chosen)
        alias, accepted = QInputDialog.getText(
            self, "Klasör takma adı", "Takma ad (küçük harf, rakam, _ veya -):",
            text=slug_alias(folder.name),
        )
        if not accepted:
            return
        alias = alias.strip()
        taken = self._config().workdir_choices(self._vault())
        problem = workdir_alias_error(alias, taken)
        if problem:
            self.notice.setText(problem)
            return
        try:
            add_workdir(self._config_path, alias, folder)
        except (OSError, ValueError):
            self.notice.setText("Klasör kaydedilemedi; mevcut ayarlar korunuyor.")
            return
        self._after_config_write(f"'{alias}' klasörü eklendi.")
        self._pick(self.workdir_combo, alias)

    @staticmethod
    def _grow_editor(editor: QPlainTextEdit) -> None:
        """Yükseklik içeriği izler (72..120px); QPlainTextEdit belge boyutu satır sayısıdır."""
        lines = max(editor.document().size().height(), 1)
        want = int(lines * editor.fontMetrics().lineSpacing()) + 24  # dikey dolgu + çerçeve payı
        editor.setFixedHeight(min(max(want, EDITOR_MIN_HEIGHT), EDITOR_MAX_HEIGHT))

    def _sync_list(self) -> None:
        """Boş liste yerine açıklayıcı durum göster; liste yüksekliği satır sayısına uyar."""
        count = self.items.count()
        self.list_stack.setCurrentIndex(0 if count else 1)
        if count:
            # Liste satır sayısına uyar (en az 1, en çok 6); fazlası kaydırılır.
            row = max(self.items.sizeHintForRow(0), 36)
            rows = min(max(count, LIST_MIN_ROWS), LIST_MAX_ROWS)
            self.list_stack.setFixedHeight(rows * row + 12)
        else:
            self.list_stack.setFixedHeight(120)

    def _update_flow(self, kind: str, record: dict[str, Any]) -> None:
        """Hazırla → İncele → Başlat: taslak yoksa 0, taslak seçiliyse 1, iş seçiliyse 2."""
        current = 0 if not kind else 1 if kind == "draft" else 2
        finished = kind == "job" and record.get("status") == "done"
        for i, label in enumerate(self._flow_labels):
            state = "done" if finished or i < current else "current" if i == current else "todo"
            label.setText(("✓ " if state == "done" else "") + FLOW_STEPS[i])
            if label.property("state") != state:
                label.setProperty("state", state)
                repolish(label)

    def _update_chip(self, kind: str, record: dict[str, Any]) -> None:
        if not kind:
            self.status_chip.setVisible(False)
            return
        text, tone = (
            ("Taslak", "neutral")
            if kind == "draft"
            else STATUS_CHIPS.get(record.get("status", ""), ("Bilinmiyor", "neutral"))
        )
        self.status_chip.setText(text)
        if self.status_chip.property("tone") != tone:
            self.status_chip.setProperty("tone", tone)
            repolish(self.status_chip)
        self.status_chip.setVisible(True)

    def _set_prepare_primary(self, primary: bool) -> None:
        name = "primary" if primary else ""
        if self.prepare_button.objectName() != name:
            self.prepare_button.setObjectName(name)
            repolish(self.prepare_button)

    def active_job_count(self) -> int:
        """Arka planda sürmekte olan iş sayısı (son durum listesine göre)."""
        return sum(
            1 for kind, item in self._items
            if kind == "job" and item.get("status") in ACTIVE_JOB_STATUSES
        )

    def refresh(self) -> None:
        if not self._busy and not self._demo:
            self.tool_requested.emit("code_agent_status", StatusArguments())

    def _prepare_block(self) -> str:
        """Taslak düğmesi pasifse kullanıcıya gösterilecek tek satır neden ('' = açık)."""
        if self._demo:
            return "Demo modunda taslak hazırlanamaz."
        if self._busy:
            return "Artemis meşgul; hazır olunca açılır."
        if not self.project.currentText():
            return "Önce bir proje tanımla."
        if not self.task.toPlainText().strip():
            return "Önce görev yaz."
        if not self.scope.toPlainText().strip():
            return "Kapsamı yaz (her satır bir madde)."
        if not self.acceptance.toPlainText().strip():
            return "Kabul ölçütünü yaz (her satır bir madde)."
        return ""

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        enabled = not busy and not self._demo
        prepare_reason = self._prepare_block()
        self.prepare_button.setEnabled(not prepare_reason)
        self.prepare_reason.setText(prepare_reason)
        self.refresh_button.setEnabled(enabled)
        selected = self._selected()
        kind, record = selected if selected else ("", {})
        self.start_button.setEnabled(enabled and kind == "draft")
        if self._demo:
            start_reason = "Demo modunda proje süreçleri kapalı."
        elif busy:
            start_reason = "Artemis meşgul; hazır olunca açılır."
        elif kind == "job":
            start_reason = "Bu bir iş; yeni iş için bir taslak seç."
        elif kind != "draft":
            start_reason = "Önce bir taslak hazırla veya listeden seç."
        else:
            start_reason = ""
        self.start_reason.setText(start_reason)
        self._set_prepare_primary(kind != "draft")
        self.question_box.setVisible(record.get("status") == "waiting")
        self.answer_button.setEnabled(enabled and record.get("status") == "waiting")
        self.cancel_button.setVisible(kind == "job")
        self.cancel_button.setEnabled(
            enabled
            and record.get("status")
            in {
                "queued",
                "running",
                "waiting",
                "cancelling",
            }
        )
        self._update_flow(kind, record)
        self._update_chip(kind, record)

    def _selected(self) -> tuple[str, dict[str, Any]] | None:
        row = self.items.currentRow()
        return self._items[row] if 0 <= row < len(self._items) else None

    def _select(self, row: int) -> None:
        selected = self._selected()
        if selected:
            self.preview.setHtml(spec_to_html(*selected))
        else:
            self.preview.clear()
        self.set_busy(self._busy)

    def accept_result(self, tool_name: str, result: ToolResult) -> None:
        self.notice.setText(result.message)
        if tool_name == "code_agent_status" and result.success:
            self.status_serial += 1
            selected = self._selected()
            identity = (
                None if selected is None else selected[1].get("job_id", selected[1].get("spec_id"))
            )
            self.project.blockSignals(True)
            current_project = self.project.currentText()
            projects = result.data.get("projects", [])
            if projects != [self.project.itemText(i) for i in range(self.project.count())]:
                self.project.clear()
                self.project.addItems(projects)
                self.project.setCurrentText(current_project)
            self.project.blockSignals(False)
            # Yalnız proje değiştiyse (ilk yükleme) varsayılanlar gelir; 2 sn'lik yenileme
            # kullanıcının iş başına seçimini ezmemeli.
            if self.project.currentText() != current_project:
                self._apply_project_defaults()
            self.items.blockSignals(True)
            self.items.clear()
            self._items = [("draft", item) for item in result.data.get("drafts", [])]
            self._items += [("job", item) for item in result.data.get("jobs", [])]
            row = 0
            for i, (kind, item) in enumerate(self._items):
                self.items.addItem(
                    f"{item['project']} · "
                    + (
                        "Taslak"
                        if kind == "draft"
                        else STATUS_LABELS.get(item.get("status", ""), "Bilinmiyor")
                    )
                )
                if item.get("job_id", item.get("spec_id")) == identity:
                    row = i
            self.items.setCurrentRow(row if self._items else -1)
            self.items.blockSignals(False)
            self._sync_list()
            self._select(row)
        elif tool_name == "code_agent_prepare" and result.success:
            spec = result.data["spec"]
            self._items.append(("draft", spec))
            self.items.addItem(spec["project"] + " · Taslak")
            self.items.setCurrentRow(len(self._items) - 1)
            self._sync_list()

    def _prepare(self) -> None:
        try:
            arguments = PrepareArguments(
                project=self.project.currentText(),
                task=self.task.toPlainText(),
                scope=tuple(self.scope.toPlainText().splitlines()),
                acceptance=tuple(self.acceptance.toPlainText().splitlines()),
                # Yalnız takma ad gider; yol hiçbir zaman istekte yer almaz.
                agent=str(self.agent_combo.currentData() or "") or None,
                workdir=str(self.workdir_combo.currentData() or "") or None,
            )
        except ValidationError:
            self.notice.setText("Proje, görev, kapsam ve kabul ölçütleri gerekli.")
            return
        self.tool_requested.emit("code_agent_prepare", arguments)

    def _start(self) -> None:
        selected = self._selected()
        if selected and selected[0] == "draft":
            self.tool_requested.emit(
                "code_agent_start", StartArguments(spec=ProjectSpec.model_validate(selected[1]))
            )

    def _answer(self) -> None:
        selected = self._selected()
        if selected and selected[1].get("status") == "waiting":
            record = selected[1]
            try:
                arguments = AnswerArguments(
                    job_id=record["job_id"],
                    question_id=record["question_id"],
                    answer=self.answer.toPlainText(),
                )
            except ValidationError:
                self.notice.setText("Cevap 1-4000 karakter olmalı.")
                return
            self.tool_requested.emit("code_agent_answer", arguments)

    def _cancel(self) -> None:
        selected = self._selected()
        if selected and selected[0] == "job":
            self.tool_requested.emit(
                "code_agent_cancel", CancelArguments(job_id=selected[1]["job_id"])
            )
