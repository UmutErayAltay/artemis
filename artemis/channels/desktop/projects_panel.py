"""Projeler düz metin sunumudur; eylem sahipliği CommandRunner'da kalır."""

from __future__ import annotations

import contextlib
import html
import json
from pathlib import Path
from typing import Any, cast

from pydantic import ValidationError
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QResizeEvent, QShowEvent, QStandardItemModel
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
from artemis.channels.desktop.page_kit import (
    PAGE_MAX_WIDTH as PAGE_MAX_WIDTH,  # eski içe aktarma yolu
)
from artemis.channels.desktop.page_kit import (
    AutoHideLabel,
    CollapsibleSection,
    EmptyState,
    PathHint,
    make_card,
    make_field,
    make_label,
    make_page_header,
    make_scroll_page,
    repolish,
)
from artemis.channels.desktop.widgets import HelpTip
from artemis.config.settings import (
    OWN_REPO_ALIAS,
    CodeAgentConfig,
    Settings,
    load_settings,
)
from artemis.i18n import t
from artemis.tools.base import ToolResult
from artemis.tools.code_agent_backends import visible_supported
from artemis.tools.code_agent_jobs import FINISHED
from artemis.tools.plugins.code_agent import (
    AnswerArguments,
    CancelArguments,
    ClearFinishedArguments,
    DiscardArguments,
    PrepareArguments,
    ReleaseArguments,
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
    "done": "Bitti",
    "error": "Başarısız",
    "interrupted": "Kesintiye uğradı",
    "cleanup_pending": "Süreç temizliği belirsiz",
}

# "done" yalnız süreç bitti demektir; "Doğrulandı" ancak Artemis'in bağımsız kontrolleri geçtiyse.
DONE_LABELS = {
    "accepted": "Doğrulandı",
    "failed": "Bitti, kontrol başarısız",
    "unverified": "Bitti (doğrulanmadı)",
}


def status_label(record: dict[str, Any]) -> str:
    status = record.get("status", "")
    if status == "done":
        ver = record.get("verification", "unverified")
        return t(DONE_LABELS.get(ver, DONE_LABELS["unverified"]))
    return t(STATUS_LABELS.get(status, "Bilinmiyor"))


def checks_text(record: dict[str, Any]) -> str:
    rows = [
        t("{status}: {command} (çıkış {exit_code})").format(
            status=t("geçti") if c.get("passed") else t("BAŞARISIZ"),
            command=c.get("command", ""),
            exit_code=c.get("exit_code"),
        )
        for c in record.get("checks", [])
    ]
    return "; ".join(rows) or t("yok")




# Durum çipi: (etiket, ton). Ton QSS'te `tone` özelliğiyle renklenir; renk kodu burada yok.
STATUS_CHIPS = {
    "queued": ("Hazırlanıyor", "running"),
    "running": ("Çalışıyor", "running"),
    "waiting": ("Cevap bekliyor", "waiting"),
    "cancelling": ("Durduruluyor", "waiting"),
    "cancelled": ("Durduruldu", "neutral"),
    "timeout": ("Zaman aşımı", "error"),
    "done": ("Bitti", "done"),  # çip için status_chip() doğrulamaya göre ezer
    "error": ("Hata", "error"),
    "interrupted": ("Kesildi", "error"),
    "cleanup_pending": ("Temizleniyor", "error"),
}
DONE_CHIPS = {
    "accepted": ("Doğrulandı", "done"),
    "failed": ("Bitti, kontrol başarısız", "error"),
    "unverified": ("Bitti (doğrulanmadı)", "neutral"),
}


def status_chip(record: dict[str, Any]) -> tuple[str, str]:
    status = record.get("status", "")
    if status == "done":
        label, tone = DONE_CHIPS.get(
            record.get("verification", "unverified"), DONE_CHIPS["unverified"]
        )
        return t(label), tone
    label, tone = STATUS_CHIPS.get(status, ("Bilinmiyor", "neutral"))
    return t(label), tone


# Yeniden kurulumun iptal edeceği, henüz bitmemiş işler.
ACTIVE_JOB_STATUSES = frozenset({"queued", "running", "waiting", "cancelling", "cleanup_pending"})
WORKDIR_WARNING = "Ajan bu klasörde başlar ve oradaki kuralları yükler; dosyaları değiştirebilir."
FLOW_STEPS = ("Hazırla", "İncele", "Başlat")
_HELP_PROJECT = (
    "İşin yapılacağı, Ayarlar'da tanımlı yerel depo. Ajan dosyaları bu depoda düzenler ve "
    "yerel commit atabilir; push yapmaz."
)
_HELP_MODE = (
    "Arka planda: pencere açılmaz, durumu buradan izlersin. Görünür pencerede: ajan kendi "
    "penceresinde çalışır; pencereyi kapatırsan iş kesilir ve Artemis soru-cevap yapamaz."
)
_HELP_START = (
    "Durumu yenile: listeyi şimdi yeniden okur. Taslağı onayla ve başlat: seçili taslağı ajanda "
    "çalıştırır; onay penceresi tam kapsamı gösterir. Ajan Artemis kapısı dışında da çalışabilir."
)
_HELP_ROW_ACTIONS = (
    "İşi durdur: yarım dosya değişiklikleri kalır, geri alınmaz. Taslağı sil: başlamamış taslağı "
    "siler. Kilidi bırak: Artemis çöktüyse kalan depo kilidini açar. Biten işleri temizle: biten "
    "kayıtları listeden siler; repoya dokunmaz."
)
# Seçicide gösterilen ad; saklanan takma ad değişmez (claude).
AGENT_LABELS = {"claude": "Claude Code"}
# Proje listesi boşken combo boş ve etiketsiz görünürdü; devre dışı satır nedenini söyler
# (Ayarlar > Model'deki "Bağlantı yok — aşağıdan ekle" ile aynı dil). Seçilemez ve gönderilmez.
NO_PROJECT_TEXT = "Proje yok — önce bir depo ekle"
CLAUDE_TIP = "Claude Code komut satırı aracı; Claude masaüstü uygulaması iş alamaz."
AGENT_MISSING_TIP = "Bulunamadı. Ayarlar > GitHub > Kod ajanları bölümünden komutu belirtin."
MODE_LABELS = (("Arka planda", "background"), ("Görünür pencerede", "visible"))
LIST_MIN_ROWS = 1  # tek taslak tek satır yüksekliği alır; boşken EmptyState gösterilir
LIST_MAX_ROWS = 6
# 60: üç düzenleyici, "?" baloncuklu etiket satırları (18px) ve 36 px kontrollerle birincil düğme
# 1000x700 pencerede görünür kalır.
EDITOR_MIN_HEIGHT = 60
EDITOR_MAX_HEIGHT = 120




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
    keys = ("agent", "workdir", "workdir_path", "mode")
    where = {**spec, **{k: record[k] for k in keys if k in record}}
    parts = [
        section(t("Ajan ve başlangıç klasörü"), approval_text(where)),
        section(t("Proje"), str(spec.get("project", ""))),
        section(t("Görev"), str(spec.get("task", ""))),
        bullets(t("Kapsam"), list(spec.get("scope", []))),
        bullets(t("Kabul ölçütleri"), list(spec.get("acceptance", []))),
    ]
    if kind == "job":
        commits = json.dumps(record.get("commits", []), ensure_ascii=False)
        parts += [
            section(t("Durum"), status_label(record)),
            section(t("Özet"), str(record.get("summary", ""))),
            section(t("Soru"), str(record.get("question") or "—")),
            section(t("Commit'ler"), commits),
            section(t("Kalan değişiklikler"), str(record.get("leftover_changes"))),
            section(t("Değişiklik"), str(record.get("changes", "none"))),
            section(t("Bağımsız kontroller"), checks_text(record)),
            section(t("İş kimliği"), str(record.get("job_id", ""))),
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
        self._selected_identity: tuple[str, str, str] | None = None
        self._busy = True
        # Her durum sonucunda artar; pencere "liste taze mi" diye buna bakar.
        self.status_serial = 0
        self._force_update = False
        self._last_status: dict[str, Any] | None = None
        # Araçtan gelen {ajan: {found, visible}}; boşken (ilk yükleme) ajanlar bulunmuş sayılır.
        self._agent_status: dict[str, dict[str, bool]] = {}
        layout = make_scroll_page(self)
        layout.addWidget(
            make_page_header(
                t("Projeler"),
                t("Yerel bir depoda iş taslağı hazırla, kapsamını incele, onaylayıp başlat."),
            )
        )
        # Demo notu sayfa başında bir kez verilir; her eylemin altında tekrarlanmaz (UX-11).
        self.demo_note = make_label(
            t(
                "Demo modunda proje süreçleri kapalı: taslak hazırlama ve başlatma etkin sürümde "
                "çalışır."
            ),
            "meta",
        )
        self.demo_note.setVisible(demo)
        layout.addWidget(self.demo_note)
        # Üç adımlı akış yalnız seçili taslak/iş durumundan türer (_update_flow); durum tutmaz.
        self.flow_bar = QFrame()
        self.flow_bar.setObjectName("stageBar")
        flow_row = QHBoxLayout(self.flow_bar)
        flow_row.setContentsMargins(0, 0, 0, 0)
        flow_row.setSpacing(8)
        self._flow_labels: list[QLabel] = []
        for i, name in enumerate(FLOW_STEPS):
            label = make_label(t(name), "stage")
            label.setWordWrap(False)
            self._flow_labels.append(label)
            flow_row.addWidget(label)
            if i < len(FLOW_STEPS) - 1:
                flow_row.addWidget(make_label("→", "meta"))

        # Güvenlik metni silinmedi/yumuşatılmadı; yalnız varsayılan olarak katlı.
        self.rules = CollapsibleSection(
            t("Bu işler nasıl çalışır?"),
            t("Taslağı hazırlayın, tam kapsamı inceleyin ve onayla başlatın. ")
            + t(AUTHORITY_BOUNDARY),
            trailing=self.flow_bar,
        )
        self.rules_toggle = self.rules.toggle
        self.hint = self.rules.body
        layout.addWidget(self.rules)

        # Sol kart: yeni iş formu.
        left, left_lay = make_card()
        self._left_card = left
        left_lay.addWidget(make_label(t("Yeni iş"), "sectionTitle"))
        self.project = QComboBox()
        self.task = QPlainTextEdit()
        self.task.setPlaceholderText(t("Örn. Giriş formundaki doğrulamayı düzelt"))
        self.scope = QPlainTextEdit()
        self.scope.setPlaceholderText(t("Örn. Her satır bir madde: yalnız login modülü"))
        self.acceptance = QPlainTextEdit()
        self.acceptance.setPlaceholderText(t("Örn. Her satır bir madde: testler geçiyor"))
        for editor in (self.task, self.scope, self.acceptance):
            # 1280x850'de "Taslak hazırla" kıvrımın altına düşmesin: 72px, içerikle 120px'e büyür.
            editor.setFixedHeight(EDITOR_MIN_HEIGHT)
            # Lambda self'i yakalayıp panel -> editör -> bağlantı -> panel döngüsü kuruyordu;
            # gc döngüyü rastgele sırada çözünce Qt silinmiş çocuklara erişip çöküyordu.
            # Bağlı yöntem döngü kurmaz.
            editor.textChanged.connect(self._editor_text_changed)
        self.project.setAccessibleName(t("Proje"))
        self._set_projects([])
        left_lay.addWidget(make_field(t("Proje"), self.project, t(_HELP_PROJECT)))
        left_lay.addWidget(self._build_agent_row())
        for caption, widget in (
            (t("Görev"), self.task),
            (t("Kapsam"), self.scope),
            (t("Kabul ölçütleri"), self.acceptance),
        ):
            widget.setAccessibleName(caption)
            left_lay.addWidget(make_field(caption, widget))
        self.prepare_button = QPushButton(t("Taslak hazırla"))
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
        self._right_card = right
        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        title_row.addWidget(make_label(t("Taslaklar ve işler"), "sectionTitle"))
        self.status_chip = QLabel()
        self.status_chip.setObjectName("statusChip")
        self.status_chip.setTextFormat(Qt.TextFormat.PlainText)
        self.status_chip.setVisible(False)
        title_row.addWidget(self.status_chip)
        title_row.addStretch()
        right_lay.addLayout(title_row)
        self.items = QListWidget()
        self.items.setAccessibleName(t("Taslaklar ve işler"))
        self.items.currentRowChanged.connect(self._select)
        self.list_stack = QStackedWidget()
        self.list_stack.addWidget(self.items)
        self.list_stack.addWidget(
            EmptyState(t("Henüz taslak yok"), t("Soldan bir görev yazıp taslak hazırla."))
        )
        right_lay.addWidget(self.list_stack)
        self.preview = QTextBrowser()
        self.preview.setOpenLinks(False)
        self.preview.setOpenExternalLinks(False)
        self.preview.setAccessibleName(t("Taslak ayrıntısı"))
        self.preview.setPlaceholderText(t("Bir taslak veya iş seç; ayrıntısı burada görünür."))
        self.preview.setMinimumHeight(120)
        right_lay.addWidget(self.preview, 1)
        right_lay.addWidget(self._build_question_box())
        self.refresh_button = QPushButton(t("Durumu yenile"))
        self.start_button = QPushButton(t("Taslağı onayla ve başlat"))
        self.start_button.setObjectName("primary")
        actions = QHBoxLayout()
        actions.setSpacing(8)
        actions.addStretch()
        actions.addWidget(self.refresh_button)
        actions.addWidget(self.start_button)
        actions.addWidget(HelpTip(t(_HELP_START)), 0, Qt.AlignmentFlag.AlignVCenter)
        right_lay.addLayout(actions)
        self.start_reason = AutoHideLabel()
        self.start_reason.setAlignment(Qt.AlignmentFlag.AlignRight)
        right_lay.addWidget(self.start_reason)
        # Yıkıcı eylem birincil eylemden ayrı satırda ve yalnız iş seçiliyken görünür.
        self.cancel_button = QPushButton(t("İşi durdur"))
        self.cancel_button.setObjectName("danger")
        # Taslağı sil: yalnız taslak seçiliyken görünür (iptal düğmesiyle aynı satır, ikisi
        # birlikte görünmez); yıkıcı olduğu için birincil eylemden ayrı satırdadır.
        self.discard_button = QPushButton(t("Taslağı sil"))
        # Çökme sonrası kalan belirsiz depo kilidini (cleanup_pending) onayla bırakır.
        self.release_button = QPushButton(t("Kilidi bırak"))
        self.release_button.setObjectName("danger")
        self.discard_button.setObjectName("danger")
        # Listeyi sadeleştirir; yalnız bitmiş ve kilidi serbest iş varken etkin.
        self.clear_button = QPushButton(t("Biten işleri temizle"))
        cancel_row = QHBoxLayout()
        cancel_row.addWidget(self.cancel_button)
        cancel_row.addWidget(self.release_button)
        cancel_row.addWidget(self.discard_button)
        cancel_row.addWidget(self.clear_button)
        # Düğmeler seçime göre görünür/gizlenir; tek "?" satırın sonunda kalır ve hepsini anlatır.
        cancel_row.addWidget(HelpTip(t(_HELP_ROW_ACTIONS)), 0, Qt.AlignmentFlag.AlignVCenter)
        cancel_row.addStretch()
        right_lay.addSpacing(8)
        right_lay.addLayout(cancel_row)
        self.notice = make_label("" if demo else t("Durum bekleniyor."), "meta")
        right_lay.addWidget(self.notice)

        self._columns = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        self._columns.setSpacing(16)
        self._columns.addWidget(left, 5)
        self._columns.addWidget(right, 6)
        layout.addLayout(self._columns, 1)
        self.prepare_button.clicked.connect(self._prepare)
        self.start_button.clicked.connect(self._start)
        self.refresh_button.clicked.connect(self._manual_refresh)
        self.answer_button.clicked.connect(self._answer)
        self.cancel_button.clicked.connect(self._cancel)
        self.release_button.clicked.connect(self._release)
        self.discard_button.clicked.connect(self._discard)
        self.clear_button.clicked.connect(self._clear_finished)
        self.project.currentTextChanged.connect(self._apply_project_defaults)
        self.agent_combo.currentIndexChanged.connect(self._sync_agent_ui)
        self.workdir_combo.currentIndexChanged.connect(self._sync_agent_ui)
        self.mode_combo.currentIndexChanged.connect(self._sync_agent_ui)
        self.add_folder_button.clicked.connect(self._add_folder)
        self.default_button.clicked.connect(self._make_default)
        self.reload_config()
        self._sync_list()
        self.set_busy(True)

    def _sync_responsive_layout(self, width: int) -> None:
        wide = width >= self.WIDE_BREAKPOINT
        # İki seçici geniş düzende yan yana, dar düzende alt alta (aynı eşik).
        self._agent_row.setDirection(
            QBoxLayout.Direction.LeftToRight if wide else QBoxLayout.Direction.TopToBottom
        )
        # Yan yana iki sütun mevcut genişliğe sığmazsa alt alta dizilir; 1280x850 gibi
        # pencerelerde yatay kaydırma çubuğu çıkıp sol sütunu kesmez.
        needed = (
            self._left_card.minimumSizeHint().width()
            + self._right_card.minimumSizeHint().width()
            + self._columns.spacing()
        )
        available = max(0, width - 24)
        columns_wide = wide and needed <= available
        self._columns.setDirection(
            QBoxLayout.Direction.LeftToRight if columns_wide else QBoxLayout.Direction.TopToBottom
        )

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._sync_responsive_layout(event.size().width())

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        # Ayarlar'da Uygula ile eklenen ajan/klasörler sayfaya dönünce görünür.
        self.reload_config()
        self._sync_responsive_layout(self.width())

    # --- Kod ajanı ve başlangıç klasörü seçimi (ADR 0049) -------------------------------------

    def _build_question_box(self) -> QWidget:
        """Bekleyen soru alanı; yalnız seçili iş 'waiting' iken görünür (set_busy)."""
        self.question_box = QWidget()
        q_lay = QVBoxLayout(self.question_box)
        q_lay.setContentsMargins(0, 0, 0, 0)
        q_lay.setSpacing(4)
        self.answer = QPlainTextEdit()
        self.answer.setPlaceholderText(t("Örn. Mavi olsun"))
        self.answer.setAccessibleName(t("Bekleyen soru"))
        self.answer.setMinimumHeight(72)
        self.answer.setMaximumHeight(96)
        q_lay.addWidget(make_label(t("Bekleyen soru"), "fieldLabel"))
        q_lay.addWidget(self.answer)
        self.answer_button = QPushButton(t("Cevabı onayla ve devam et"))
        answer_row = QHBoxLayout()
        answer_row.addStretch()
        answer_row.addWidget(self.answer_button)
        q_lay.addLayout(answer_row)
        return self.question_box

    def _build_agent_row(self) -> QWidget:
        host = QWidget()
        column = QVBoxLayout(host)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(4)
        self.agent_combo = QComboBox()
        self.agent_combo.setAccessibleName(t("Kod ajanı"))
        self.workdir_combo = QComboBox()
        self.workdir_combo.setAccessibleName(t("Başlangıç klasörü"))
        self.mode_combo = QComboBox()
        self.mode_combo.setAccessibleName(t("Çalışma kipi"))
        for label, value in MODE_LABELS:
            self.mode_combo.addItem(t(label), value)
        # Üç seçici 1280x850'de (~1000px sayfa) tek satıra sığmalı ve en uzun seçenek metni
        # ("Görünür pencerede", "Proje klasörü") kırpılmadan okunmalı. Karakter sayıları metinden
        # birkaç harf bol: 'x' ortalama harften geniştir, açılır ok ve dolgu da payı yer.
        # Kullanıcının eklediği uzun klasör adı yine de yalnız kendi satırında "…" ile kısalır.
        fit = QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        sizes = ((self.agent_combo, 8), (self.mode_combo, 13), (self.workdir_combo, 8))
        for combo, chars in sizes:
            combo.setSizeAdjustPolicy(fit)
            combo.setMinimumContentsLength(chars)
        self.add_folder_button = QPushButton(t("Ekle…"))
        self.add_folder_button.setToolTip(t("Yeni başlangıç klasörü ekle"))
        self.add_folder_button.setAccessibleName(t("Klasör ekle"))
        folder_box = QWidget()
        folder_row = QHBoxLayout(folder_box)
        folder_row.setContentsMargins(0, 0, 0, 0)
        folder_row.setSpacing(8)
        folder_row.addWidget(self.workdir_combo, 1)
        folder_row.addWidget(self.add_folder_button)
        self._agent_row = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        self._agent_row.setSpacing(12)
        self._agent_row.addWidget(make_field(t("Kod ajanı"), self.agent_combo), 3)
        self._agent_row.addWidget(make_field(t("Çalışma kipi"), self.mode_combo, t(_HELP_MODE)), 3)
        self._agent_row.addWidget(
            make_field(t("Başlangıç klasörü"), folder_box), 4
        )
        column.addLayout(self._agent_row)
        # Çözülmüş yol 12px soluk ipucudur; uyarı yalnız proje dışı klasörde görünür.
        self.workdir_hint = PathHint()
        self.workdir_warning = make_label("", "chipWarning")
        self.workdir_warning.hide()
        self.agent_reason = AutoHideLabel()
        # İkincil eylem yol ipucunun satırında (ince, düz düğme): ayrı satır olsaydı çipli durumda
        # 1280x850'de kıvrımın altına düşerdi. Yol dar kalırsa ortadan kısalır, tam yol ipucundadır.
        self.default_button = QPushButton(t("Bunu bu proje için varsayılan yap"))
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
        self._fill_agent_combo()
        self.workdir_combo.blockSignals(True)
        self.workdir_combo.clear()
        for name in config.workdir_choices(self._vault()):
            self.workdir_combo.addItem(t("Proje klasörü") if name == OWN_REPO_ALIAS else name, name)
        self.workdir_combo.blockSignals(False)
        # Yeniden kurulum kullanıcının seçimini bozmasın; yoksa projenin varsayılanı gelir.
        if agent is not None and workdir is not None:
            self._pick(self.agent_combo, agent)
            self._pick(self.workdir_combo, workdir)
            self._sync_agent_ui()
        else:
            self._apply_project_defaults()

    def _agent_found(self, name: str) -> bool:
        return self._agent_status.get(name, {}).get("found", True)

    def _fill_agent_combo(self) -> None:
        """Claude + Codex + özel ajanlar. Bulunamayan ajan seçilemez (pasif) ve nedeni ipucundadır;
        seçim korunur. Durum araç iş parçacığından gelir, burada komut aranmaz."""
        keep = self.agent_combo.currentData()
        model = cast(QStandardItemModel, self.agent_combo.model())
        self.agent_combo.blockSignals(True)
        self.agent_combo.clear()
        for index, name in enumerate(self._config().agent_names()):
            found = self._agent_found(name)
            label = AGENT_LABELS.get(name, name)
            text = label if found else t("{label} (bulunamadı)").format(label=label)
            self.agent_combo.addItem(text, name)
            tips = [t(CLAUDE_TIP)] if name == "claude" else []
            if not found:
                tips.append(t(AGENT_MISSING_TIP))
            self.agent_combo.setItemData(index, " ".join(tips), Qt.ItemDataRole.ToolTipRole)
            item = model.item(index)
            if item is not None:
                item.setEnabled(found)
        self.agent_combo.setToolTip(t(CLAUDE_TIP))
        if keep is not None:
            self._pick(self.agent_combo, str(keep))
        self.agent_combo.blockSignals(False)

    @staticmethod
    def _pick(combo: QComboBox, alias: str) -> None:
        combo.setCurrentIndex(max(0, combo.findData(alias)))

    def _project_name(self) -> str:
        """Seçili gerçek proje; placeholder verisiz olduğundan '' döner ve hiçbir yere gitmez."""
        return str(self.project.currentData() or "")

    def _project_names(self) -> list[str]:
        return [
            str(self.project.itemData(i))
            for i in range(self.project.count())
            if self.project.itemData(i) is not None
        ]

    def _set_projects(self, projects: list[str]) -> None:
        self.project.clear()
        for name in projects:
            self.project.addItem(name, name)
        if not projects:
            self.project.addItem(t(NO_PROJECT_TEXT))
            item = cast(QStandardItemModel, self.project.model()).item(0)
            if item is not None:
                item.setEnabled(False)
        self.project.setEnabled(bool(projects))

    def _saved_choice(self) -> tuple[str, str]:
        """Seçili projenin kayıtlı varsayılanı: (ajan, klasör takma adı)."""
        config, project = self._config(), self._project_name()
        return config.project_agent(project), config.project_workdir(project)

    def _saved_mode(self) -> str:
        return self._config().project_mode(self._project_name())

    def _apply_project_defaults(self, *_args: object) -> None:
        agent, workdir = self._saved_choice()
        combos = (self.agent_combo, self.workdir_combo, self.mode_combo)
        for combo in combos:
            combo.blockSignals(True)
        self._pick(self.agent_combo, agent)
        self._pick(self.workdir_combo, workdir)
        self._pick(self.mode_combo, self._saved_mode())
        for combo in combos:
            combo.blockSignals(False)
        self._sync_agent_ui()

    def selected_choice(self) -> tuple[str, str]:
        return str(self.agent_combo.currentData()), str(self.workdir_combo.currentData())

    def selected_mode(self) -> str:
        return str(self.mode_combo.currentData() or "background")

    def _visible_block(self, agent: str) -> str:
        """Seçili ajanda görünür pencere kipi kapalıysa nedeni ('' = destekleniyor)."""
        try:
            _, profile = self._config().resolve_agent(agent)
        except ValueError:
            return t("Bilinmeyen ajan.")
        known = self._agent_status.get(agent, {}).get("visible")
        if known if known is not None else visible_supported(profile):
            return ""
        if profile.kind != "generic":
            return t("Görünür pencere kipi yalnız Windows'ta çalışır.")
        return t("Bu ajan için görünür kip tanımlı değil (Ayarlar > GitHub > Kod ajanları).")

    def _project_repo(self) -> Path | None:
        config, project = self._config(), self._project_name()
        return config.project_path(project) or guess_project_path(config.repos_dir, project)

    def _workdir_path(self, workdir: str) -> Path | None:
        chosen = self._config().workdir_choices(self._vault()).get(workdir)
        return self._project_repo() if chosen is None else chosen

    def _sync_agent_ui(self, *_args: object) -> None:
        agent, workdir = self.selected_choice()
        repo, folder = self._project_repo(), self._workdir_path(workdir)
        self.workdir_hint.set_full_text(
            t("Başlangıç klasörü: {folder}").format(folder=folder)
            if folder
            else t("Başlangıç klasörü henüz seçilmedi"),
            str(folder or ""),
        )
        # Formda kısa uyarı: hedef repo zaten Proje seçicisinden bellidir. Tam backend
        # onay metni ön izleme bloğunda ve onay penceresinde durur (1280x850 sığma sözleşmesi).
        self.workdir_warning.setText(t(WORKDIR_WARNING))
        self.workdir_warning.setVisible(workdir != OWN_REPO_ALIAS)
        usable = self._config_path is not None and not self._demo
        # Ajanda görünür kip yoksa seçenek pasif, nedeni görünür; seçili kip arka plana düşer.
        visible_reason = self._visible_block(agent)
        visible_item = cast(QStandardItemModel, self.mode_combo.model()).item(1)
        if visible_item is not None:
            visible_item.setEnabled(not visible_reason)
        self.mode_combo.setItemData(1, visible_reason, Qt.ItemDataRole.ToolTipRole)
        if visible_reason and self.selected_mode() == "visible":
            blocked = self.mode_combo.blockSignals(True)
            self.mode_combo.setCurrentIndex(0)
            self.mode_combo.blockSignals(blocked)
        mode = self.selected_mode()
        # Neden satırı yalnız eyleme engel olan durumlarda görünür (1280x850'de "Taslak hazırla"
        # kıvrımın altına düşmesin); açıklayıcı bilgi seçicilerin ipuçlarında durur.
        reason = t("Demo modunda ajan ve klasör seçimi kapalı.") if self._demo else ""
        saved = self._saved_choice()
        changed = (agent, workdir) != saved or mode != self._saved_mode()
        can_default = usable and changed and bool(self._project_name()) and repo is not None
        if usable and changed and repo is None:
            reason = t("Bu projenin klasörü bulunamadı; varsayılan yazılamaz.")
        elif visible_reason and not self._demo:
            reason = t("Görünür pencere kipi kapalı: {reason}").format(reason=visible_reason)
        self.agent_reason.setText(reason)
        self.mode_combo.setToolTip(
            t(
                "Arka planda: pencere açılmaz, Artemis ajana soru-cevap yapabilir (Claude). "
                "Görünür pencerede: ajan ayrı bir konsolda çalışır, pencereyi kapatırsan "
                "iş kesilir."
            )
        )
        self.agent_combo.setEnabled(usable)
        self.mode_combo.setEnabled(usable)
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
        alias = self._project_name()
        if not alias:
            return
        try:
            save_project_default(
                self._config_path,
                alias,
                agent=agent,
                workdir=workdir,
                default_agent=self._config().default_agent,
                project_path=self._project_repo(),
                mode=self.selected_mode(),
                default_mode=self._config().default_mode,
                default_workdir=self._config().default_workdir,
            )
        except (OSError, ValueError):
            self.notice.setText(t("Varsayılan kaydedilemedi; mevcut ayarlar korunuyor."))
            return
        self._after_config_write(
            t("{alias} için varsayılan kaydedildi: {agent}, {workdir}, {mode}.").format(
                alias=alias, agent=agent, workdir=workdir, mode=self.selected_mode()
            )
        )

    def _add_folder(self) -> None:
        if self._config_path is None or self._demo:
            return
        # Klasörü kullanıcı arayüzde seçer; model yol veremez (ADR 0049).
        chosen = QFileDialog.getExistingDirectory(
            self, t("Başlangıç klasörü seç"), str(Path.home())
        )
        if not chosen:
            return
        folder = Path(chosen)
        alias, accepted = QInputDialog.getText(
            self,
            t("Klasör takma adı"),
            t("Takma ad (küçük harf, rakam, _ veya -):"),
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
            self.notice.setText(t("Klasör kaydedilemedi; mevcut ayarlar korunuyor."))
            return
        self._after_config_write(t("'{alias}' klasörü eklendi.").format(alias=alias))
        self._pick(self.workdir_combo, alias)

    def _editor_text_changed(self) -> None:
        editor = self.sender()
        if isinstance(editor, QPlainTextEdit):
            self._grow_editor(editor)
        self.set_busy(self._busy)

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
            label.setText(("✓ " if state == "done" else "") + t(FLOW_STEPS[i]))
            if label.property("state") != state:
                label.setProperty("state", state)
                repolish(label)

    def _update_chip(self, kind: str, record: dict[str, Any]) -> None:
        if not kind:
            self.status_chip.setVisible(False)
            return
        text, tone = (t("Taslak"), "neutral") if kind == "draft" else status_chip(record)
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
            1
            for kind, item in self._items
            if kind == "job" and item.get("status") in ACTIVE_JOB_STATUSES
        )

    def _manual_refresh(self) -> None:
        self._force_update = True
        self.refresh()

    def refresh(self) -> None:
        if not self._busy and not self._demo:
            self.tool_requested.emit("code_agent_status", StatusArguments())

    def _prepare_block(self) -> str:
        """Taslak düğmesi pasifse kullanıcıya gösterilecek tek satır neden ('' = açık)."""
        if self._demo:
            return t("Demo modunda taslak hazırlanamaz.")
        if self._busy:
            return t("Artemis meşgul; hazır olunca açılır.")
        if not self._project_name():
            return t("Önce bir proje tanımla.")
        if not self._agent_found(str(self.agent_combo.currentData())):
            return t("Seçili ajan bulunamadı; Ayarlar > GitHub > Kod ajanları.")
        if not self.task.toPlainText().strip():
            return t("Önce görev yaz.")
        if not self.scope.toPlainText().strip():
            return t("Kapsamı yaz (her satır bir madde).")
        if not self.acceptance.toPlainText().strip():
            return t("Kabul ölçütünü yaz (her satır bir madde).")
        return ""

    def _lock_block(self, draft: dict[str, Any]) -> str:
        """Taslağın deposunda çalışan/kilitli iş varsa nedeni; sunucu kontrolü ayrıca durur,
        bu yalnız kullanıcıya boşuna onay penceresi göstermemek içindir."""
        for kind, job in self._items:
            if kind != "job" or job.get("project") != draft.get("project"):
                continue
            if job.get("cleanup_pending"):
                return t("Bu projede kilitli/çalışan iş var; önce Kilidi bırak.")
            if job.get("status") in ACTIVE_JOB_STATUSES:
                return t("Bu projede çalışan iş var; bitmesini bekleyin.")
        return ""

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        enabled = not busy and not self._demo
        prepare_reason = self._prepare_block()
        self.prepare_button.setEnabled(not prepare_reason)
        self.prepare_reason.setText("" if self._demo else prepare_reason)
        self.prepare_button.setToolTip(
            t("Demo modunda kapalı.") if self._demo else prepare_reason
        )
        self.refresh_button.setEnabled(enabled)
        selected = self._selected()
        kind, record = selected if selected else ("", {})
        lock_reason = self._lock_block(record) if kind == "draft" else ""
        self.start_button.setEnabled(enabled and kind == "draft" and not lock_reason)
        if self._demo:
            start_reason = ""  # neden sayfa başındaki demo notunda
        elif busy:
            start_reason = t("Artemis meşgul; hazır olunca açılır.")
        elif lock_reason:
            start_reason = lock_reason
        elif (
            kind == "job" and record.get("mode") == "visible" and record.get("status") == "running"
        ):
            start_reason = t(
                "Ajan penceresi açık: iş bitince pencereyi kapatın, sonuç o zaman kaydedilir."
            )
        elif kind == "job":
            start_reason = t("Bu bir iş; yeni iş için bir taslak seç.")
        elif kind != "draft":
            start_reason = t("Önce bir taslak hazırla veya listeden seç.")
        else:
            start_reason = ""
        self.start_reason.setText(start_reason)
        self.start_button.setToolTip(
            t("Demo modunda kapalı.") if self._demo else start_reason
        )
        self._set_prepare_primary(kind != "draft")
        self.question_box.setVisible(record.get("status") == "waiting")
        self.answer_button.setEnabled(enabled and record.get("status") == "waiting")
        self.clear_button.setEnabled(
            enabled
            and any(
                k == "job" and i.get("status") in FINISHED and not i.get("cleanup_pending")
                for k, i in self._items
            )
        )
        self.discard_button.setVisible(kind == "draft")
        self.discard_button.setEnabled(enabled and kind == "draft")
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
        self.release_button.setVisible(kind == "job")
        self.release_button.setEnabled(
            enabled
            and bool(record.get("cleanup_pending"))
            and record.get("status") not in {"queued", "running", "waiting", "cancelling"}
        )
        self._update_flow(kind, record)
        self._update_chip(kind, record)

    def _selected(self) -> tuple[str, dict[str, Any]] | None:
        row = self.items.currentRow()
        return self._items[row] if 0 <= row < len(self._items) else None

    def _select(self, row: int) -> None:
        selected = self._selected()
        if selected:
            kind, record = selected
            identity: tuple[str, str, str] | None = (
                kind,
                str(record.get("job_id", record.get("spec_id", ""))),
                str(record.get("question_id", "")),
            )
            self.preview.setHtml(spec_to_html(*selected))
        else:
            identity = None
            self.preview.clear()
        if identity != self._selected_identity:
            self._selected_identity = identity
            self.answer.clear()
        self.set_busy(self._busy)

    def accept_result(self, tool_name: str, result: ToolResult) -> None:
        forced, self._force_update = self._force_update, False
        poll = tool_name == "code_agent_status" and not forced
        # Sessiz yoklama bildirimi ezmez ve değişmeyen listeyi yeniden kurmaz (titreme).
        if not poll:
            self.notice.setText(result.message)
        if tool_name == "code_agent_status" and result.success:
            self.status_serial += 1
            if poll and result.data == self._last_status:
                return
            self._last_status = result.data
            status = result.data.get("agent_status")
            if isinstance(status, dict) and status != self._agent_status:
                self._agent_status = status
                self._fill_agent_combo()
                self._sync_agent_ui()
            selected = self._selected()
            identity = (
                None if selected is None else selected[1].get("job_id", selected[1].get("spec_id"))
            )
            self.project.blockSignals(True)
            current_project = self._project_name()
            projects = result.data.get("projects", [])
            if projects != self._project_names():
                self._set_projects(projects)
                self.project.setCurrentText(current_project)
            self.project.blockSignals(False)
            # Yalnız proje değiştiyse (ilk yükleme) varsayılanlar gelir; 2 sn'lik yenileme
            # kullanıcının iş başına seçimini ezmemeli.
            if self._project_name() != current_project:
                self._apply_project_defaults()
            self._items = [("draft", item) for item in result.data.get("drafts", [])]
            self._items += [("job", item) for item in result.data.get("jobs", [])]
            self._render_items(identity)
        elif tool_name == "code_agent_discard" and result.success:
            # Silinen taslak hemen listeden düşer, seçim aynı sıradaki (yoksa son) öğeye geçer;
            # önbellek boşaltılır ki sonraki yoklama listeyi kaynaktan yeniden kursun.
            gone, row = result.data.get("spec_id"), self.items.currentRow()
            self._items = [
                (kind, item)
                for kind, item in self._items
                if not (kind == "draft" and item.get("spec_id") == gone)
            ]
            self._last_status = None
            self._render_items(None, row)
        elif tool_name == "code_agent_clear_finished" and result.success:
            # Silinenler hemen listeden düşer; önbellek boşalır ki sonraki yoklama yeniden kursun.
            gone = set(result.data.get("job_ids", []))
            self._items = [
                (kind, item)
                for kind, item in self._items
                if not (kind == "job" and item.get("job_id") in gone)
            ]
            self._last_status = None
            self._render_items(None, self.items.currentRow())
        elif tool_name == "code_agent_prepare" and result.success:
            spec = result.data["spec"]
            self._items.append(("draft", spec))
            self.items.addItem(f"{spec['project']} · {t('Taslak')}")
            self.items.setCurrentRow(len(self._items) - 1)
            self._sync_list()
        elif tool_name == "code_agent_answer" and result.success:
            self.answer.clear()

    def _render_items(self, identity: str | None, fallback: int = 0) -> None:
        """Listeyi `_items`ten kurar; `identity` varsa o öğeyi, yoksa `fallback` sırasını seçer."""
        self.items.blockSignals(True)
        self.items.clear()
        row = fallback
        for i, (kind, item) in enumerate(self._items):
            self.items.addItem(
                f"{item['project']} · " + (t("Taslak") if kind == "draft" else status_label(item))
            )
            if identity is not None and item.get("job_id", item.get("spec_id")) == identity:
                row = i
        row = max(0, min(row, len(self._items) - 1))
        self.items.setCurrentRow(row if self._items else -1)
        self.items.blockSignals(False)
        self._sync_list()
        self._select(row)

    def _prepare(self) -> None:
        try:
            arguments = PrepareArguments(
                project=self._project_name(),
                task=self.task.toPlainText(),
                scope=tuple(self.scope.toPlainText().splitlines()),
                acceptance=tuple(self.acceptance.toPlainText().splitlines()),
                # Yalnız takma ad gider; yol hiçbir zaman istekte yer almaz.
                agent=str(self.agent_combo.currentData() or "") or None,
                workdir=str(self.workdir_combo.currentData() or "") or None,
                mode=self.selected_mode(),  # type: ignore[arg-type]  # iki sabit değerden biri
            )
        except ValidationError:
            self.notice.setText(t("Proje, görev, kapsam ve kabul ölçütleri gerekli."))
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
                self.notice.setText(t("Cevap 1-4000 karakter olmalı."))
                return
            self.tool_requested.emit("code_agent_answer", arguments)

    def _discard(self) -> None:
        selected = self._selected()
        if selected and selected[0] == "draft":
            self.tool_requested.emit(
                "code_agent_discard", DiscardArguments(spec_id=selected[1]["spec_id"])
            )

    def _clear_finished(self) -> None:
        self.tool_requested.emit("code_agent_clear_finished", ClearFinishedArguments())

    def _release(self) -> None:
        selected = self._selected()
        if selected and selected[0] == "job":
            self.tool_requested.emit(
                "code_agent_release", ReleaseArguments(job_id=selected[1]["job_id"])
            )

    def _cancel(self) -> None:
        selected = self._selected()
        if selected and selected[0] == "job":
            self.tool_requested.emit(
                "code_agent_cancel", CancelArguments(job_id=selected[1]["job_id"])
            )
