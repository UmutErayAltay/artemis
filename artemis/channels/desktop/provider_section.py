"""Ayarlar > Bağlantılar ve Modeller (ADR 0034): uç, anahtar ve model tanımı.

Modeller tek yerden yönetilir: eklenen modeller tek listede durur, beyin modeli o listeden
seçilir. Anahtar YAML'a, loga ve mesajlara girmez; yalnız Kimlik Bilgisi Yöneticisi'ne yazılır.
"""

from __future__ import annotations

import asyncio
import html
import json
import os
import re
import shutil
import sys
from collections.abc import Mapping
from decimal import ROUND_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import yaml
from PySide6.QtCore import (
    QProcess,
    QProcessEnvironment,
    QRect,
    QSettings,
    QSize,
    Qt,
    QThread,
    QTimer,
    Signal,
)
from PySide6.QtGui import QResizeEvent
from PySide6.QtWidgets import (
    QBoxLayout,
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from artemis.brain.errors import ProviderError
from artemis.brain.models import Message
from artemis.brain.providers import (
    ClaudeCliProvider,
    endpoint_key_variable,
    provider_problem,
    stored_endpoint_key,
)
from artemis.channels.desktop import themes
from artemis.channels.desktop.widgets import HelpTip, with_help
from artemis.config.settings import (
    ENDPOINT_ID_RE,
    EndpointConfig,
    ModelConfig,
    Settings,
    load_settings,
)
from artemis.tools.credentials import delete_secret, write_secret
from artemis.tools.platform import child_environment
from bench.bilgisayar_testi import SCENARIOS as COMPUTER_SCENARIOS
from bench.model_testi import SCENARIOS_PATH, merge_retry, suggest_layout
from bench.run import load_scenarios

# Model testi bir koşuda yaklaşık bu kadar token harcar (2026-11-07 ölçümü: ~340 bin giriş);
# ücretli modelde tahmini maliyet ve bütçe tavanı bundan hesaplanır.
TEST_INPUT_TOKENS = 520_000  # + bilgisayar senaryoları (ekran görüntüleri)
TEST_OUTPUT_TOKENS = 20_000
_ROLE_TEXT = {"ana": "ana model", "zor": "zor işler", "bilgisayar": "bilgisayar kullanımı"}

# (görünen ad, önerilen kimlik, adres, anahtar gerekli mi)
PRESETS: tuple[tuple[str, str, str, bool], ...] = (
    ("Özel uç / yerel proxy", "", "", True),
    ("OpenRouter", "openrouter", "https://openrouter.ai/api/v1", True),
    ("OpenAI", "openai", "https://api.openai.com/v1", True),
    ("Google Gemini", "gemini", "https://generativelanguage.googleapis.com/v1beta/openai", True),
    ("Anthropic (Claude)", "claude", "https://api.anthropic.com/v1", True),
    ("DeepSeek", "deepseek", "https://api.deepseek.com/v1", True),
    ("Z.ai GLM", "zai", "https://api.z.ai/api/paas/v4", True),
    ("Groq", "groq", "https://api.groq.com/openai/v1", True),
    ("xAI (Grok)", "xai", "https://api.x.ai/v1", True),
    ("Mistral", "mistral", "https://api.mistral.ai/v1", True),
    ("LM Studio", "lmstudio", "http://127.0.0.1:1234/v1", False),
    ("Ollama", "ollama", "http://127.0.0.1:11434/v1", False),
)
CLI_MODELS = ("haiku", "sonnet", "opus")
CLI_KEY = ""  # bağlantı seçicide Claude aboneliğinin verisi (uç kimlikleri boş olamaz)
CLI_NAME = "Claude aboneliği"
_BAD_URL = "Adres geçersiz: https:// ile başlamalı (http yalnız yerel sunucu için)."

# Satır içi renkler (HTML etiketleri) temadan gelir; sabit onaltılık renk yok, tema değişince
# set_theme yeniden çizer.
_COLOR_KEYS = ("muted", "accent", "warning", "error")


def divider() -> QFrame:
    line = QFrame()
    line.setObjectName("divider")
    line.setFixedHeight(1)
    return line


def group(
    title: str, hint: str | None = None, *, divided: bool = True, help: str = "",  # noqa: A002
) -> tuple[QWidget, QVBoxLayout]:
    """Düz ayar grubu: ince çizgi + başlık + kısa ipucu. Kart içinde kart olmaması için çerçeve yok;
    çizgi grubun içinde olduğundan grup gizlenince çizgi de gider. `help` başlığa "?" ekler."""
    box = QWidget()
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
    heading.addLayout(with_help(head, help)) if help else heading.addWidget(head)
    if hint:
        heading.addWidget(meta_label(hint))
    outer.addLayout(heading)
    return box, outer


def meta_label(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("meta")
    label.setWordWrap(True)
    label.setTextFormat(Qt.TextFormat.PlainText)
    return label


def field(
    parent: QVBoxLayout, label: str, control: QWidget | QLayout, hint: str | None = None,
    *, help: str = "",  # noqa: A002 - çağrı yerlerinde en okunur ad
) -> None:
    """Etiket alanın üstünde (13px, 4px boşluk); ipucu altta 12px soluk.

    `help` verilirse etiketin hemen sağında "?" baloncuğu durur: her formda aynı yerde, böylece
    kullanıcı ayarın neyi etkilediğini hep aynı noktada bulur."""
    group = QVBoxLayout()
    group.setSpacing(4)
    caption = QLabel(label)
    caption.setObjectName("fieldLabel")
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


FORM_MAX_WIDTH = 640  # Model ekle / Bağlantılar formu
WIDE_AT = 900  # bu genişlikten itibaren model ayrıntısı listenin yanında durur
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
PARALLEL_TEST = 4


def _is_local_model(model: ModelConfig, endpoints: Mapping[str, EndpointConfig]) -> bool:
    if model.provider == "ollama":
        return True
    endpoint = endpoints.get(model.endpoint or "")
    return endpoint is not None and urlsplit(endpoint.base_url).hostname in LOCAL_HOSTS


def _scenario_ids() -> list[str]:
    return [item.id for item in load_scenarios(SCENARIOS_PATH)] + [
        item.id for item in COMPUTER_SCENARIOS
    ]


def _scenario_count() -> int:
    return len(_scenario_ids())


def _scenario_label(scenario_id: str) -> str:
    """'temel_dosya_oku' -> 'dosya oku' (kategori öneki ve alt çizgiler görünmez)."""
    for prefix in ("temel_", "zor_", "pc_"):
        if scenario_id.startswith(prefix):
            scenario_id = scenario_id[len(prefix):]
    return scenario_id.replace("_", " ")


_SORTS = (
    ("Eklenme sırası", "added"),
    ("Başarı", "score"),
    ("Bağlantı", "connection"),
    ("Rol", "role"),
    ("Ad", "name"),
)
_Row = tuple[str, bool, bool, str, str, bool, bool, str]

_STATE_TEXT = {"bekliyor": "○", "geçti": "✓", "kaldı": "✗"}


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

    def addItem(self, arg__1: Any) -> None:  # noqa: N802 (Qt adı)
        self._items.append(arg__1)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> Any:  # noqa: N802 (Qt adı)
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int) -> Any:  # noqa: N802 (Qt adı)
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def hasHeightForWidth(self) -> bool:  # noqa: N802 (Qt adı)
        return True

    def heightForWidth(self, arg__1: int) -> int:  # noqa: N802 (Qt adı)
        return self._arrange(QRect(0, 0, arg__1, 0), apply=False)

    def setGeometry(self, arg__1: QRect) -> None:  # noqa: N802 (Qt adı)
        super().setGeometry(arg__1)
        self._arrange(arg__1, apply=True)

    def sizeHint(self) -> QSize:  # noqa: N802 (Qt adı)
        hints = [item.sizeHint() for item in self._items]
        width = sum(h.width() for h in hints) + self._gap * max(0, len(hints) - 1)
        return QSize(width, max((h.height() for h in hints), default=0))

    def minimumSize(self) -> QSize:  # noqa: N802 (Qt adı)
        sizes = [item.minimumSize() for item in self._items]
        return QSize(max((s.width() for s in sizes), default=0),
                     max((s.height() for s in sizes), default=0))

    def _arrange(self, rect: QRect, *, apply: bool) -> int:
        lines: list[list[Any]] = [[]]
        used = 0
        for item in self._items:
            width = item.sizeHint().width()
            if lines[-1] and used + self._gap + width > rect.width():
                lines.append([])
                used = 0
            used += (self._gap if lines[-1] else 0) + width
            lines[-1].append(item)
        y = rect.y()
        for line in lines:
            height = max((item.sizeHint().height() for item in line), default=0)
            x = rect.right() + 1 - sum(i.sizeHint().width() for i in line) - self._gap * (
                len(line) - 1
            )
            for item in line:
                size = item.sizeHint()
                if apply:
                    top = y + (height - size.height()) // 2  # düğme yanındaki küçük "?" ortalanır
                    item.setGeometry(QRect(max(x, rect.x()), top, size.width(), size.height()))
                x += size.width() + self._gap
            y += height + self._gap
        return max(0, y - self._gap - rect.y())


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


def probe_models(base_url: str, key: str | None) -> tuple[str, list[str]]:
    """GET {base_url}/models; yanıt gövdesi, başlık ve anahtar mesaja asla girmez."""
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        response = httpx.get(
            f"{base_url}/models", headers=headers, timeout=5, follow_redirects=False
        )
    except httpx.HTTPError:
        return "Sunucuya ulaşılamadı (adres doğru mu, sunucu çalışıyor mu?).", []
    if response.status_code in {401, 403}:
        return "Sunucu API anahtarını reddetti; anahtarı kontrol edin.", []
    if response.status_code == 404:
        return "/models desteklenmiyor; model adını elle yazabilirsiniz.", []
    if response.status_code != 200:
        return f"Sunucu beklenmeyen yanıt verdi (HTTP {response.status_code}).", []
    try:
        data = response.json()["data"]
        models = sorted(
            {
                item["id"]
                for item in data
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            }
        )
    except (ValueError, KeyError, TypeError):
        return "Bağlantı var ama model listesi okunamadı; model adını elle yazabilirsiniz.", []
    return f"Bağlantı tamam, {len(models)} model bulundu.", models


class ConnectionTest(QThread):
    done = Signal(str, list)

    def __init__(self, base_url: str, key: str | None, parent: QWidget) -> None:
        super().__init__(parent)
        self._base_url = base_url
        self._key = key

    def run(self) -> None:
        message, models = probe_models(self._base_url, self._key)
        self.done.emit(message, models)


class CliTest(QThread):
    """Tek küçük gerçek istek atar (abonelik kotasından az miktar harcar)."""

    done = Signal(str)

    def __init__(self, config_path: Path, model: str, parent: QWidget) -> None:
        super().__init__(parent)
        self._config_path = config_path
        self._model = model

    def run(self) -> None:
        try:
            settings = load_settings(self._config_path)
            model = ModelConfig(
                provider="claude_cli",
                model=self._model,
                input_per_million=Decimal(0),
                output_per_million=Decimal(0),
            )
            prompt = [Message(role="user", content="Tek kelimeyle yanıt ver: merhaba")]
            provider = ClaudeCliProvider(settings, model)
            completion = asyncio.run(provider.complete(prompt, [], timeout=60))
        except ProviderError as error:
            self.done.emit(str(error))
            return
        except (OSError, ValueError):
            self.done.emit("Test çalıştırılamadı.")
            return
        self.done.emit(
            "Claude Code aboneliği çalışıyor."
            if completion.content.strip()
            else "Claude Code boş yanıt verdi."
        )


def store_endpoint_key(name: str, typed: str) -> str | None:
    """Anahtarı kasaya yazar (YAML'a asla); kasa yoksa "ortamda X tanımlayın." döner."""
    try:
        write_secret(typed, f"Artemis/{name}")
    except OSError:
        return f"ortamda {endpoint_key_variable(name)} tanımlayın."
    return None


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "-", text).strip("-")[:40]


class ProviderSection(QWidget):
    changed = Signal()  # YAML değişti; pencere çalışma zamanını yeniden başlatır

    def __init__(self, config_path: Path, *, demo: bool = False) -> None:
        super().__init__()
        self.config_path = config_path
        self._button_tips: dict[QPushButton, QWidget] = {}
        self._test: ConnectionTest | None = None
        self._fetch: ConnectionTest | None = None
        self._cli_test: CliTest | None = None
        self._auto_alias = ""
        self._problem: str | None = None
        self._colors = {key: themes.THEMES[themes.DEFAULT_THEME][key] for key in _COLOR_KEYS}
        # (takma ad, beyin mi, eski ayar mı, bağlantı, model kimliği, resim mi, zor işler mi,
        # son test özeti)
        self._model_data: list[tuple[str, bool, bool, str, str, bool, bool, str]] = []
        self._reports_seen: dict[str, dict[str, object]] = {}  # takma ad -> son rapor
        self._model_test: QProcess | None = None
        self._testing = ""
        self._fallback = ""
        self._test_report = Path()
        self._test_rows: dict[str, str] = {}  # senaryo kimliği -> bekliyor/geçti/kaldı
        self._second: set[str] = set()  # 2. denemede geçenler
        self._test_buffer = ""
        self._stopping = False
        self._retrying = False
        self._last: tuple[Path, dict[str, object]] | None = None  # (rapor yolu, gösterilen rapor)
        self._suggested: tuple[str | None, str | None, str | None] = (None, None, None)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(24)
        # A: modeller önce (tek liste; beyin modeli yalnız buradan seçilir)
        model_box, models = group(
            "Modeller", "Eklediğin modeller. Beyin olan model sohbetlere cevap verir.",
            divided=False,
        )
        layout.addWidget(model_box)
        self.banner = QLabel()  # "Beyin: ..." satırı veya sağlayıcı sorunu
        self.banner.setWordWrap(True)
        self.banner.setVisible(False)  # _render_banner metin varsa gösterir
        self._test_settings = QSettings(
            QSettings.Format.IniFormat, QSettings.Scope.UserScope, "Artemis", "model_test"
        )
        # Sıralama yalnız görünüm: config'teki model sırası değişmez.
        self.model_sort = QComboBox()
        self.model_sort.setAccessibleName("Modelleri sırala")
        for label, key in _SORTS:
            self.model_sort.addItem(label, key)
        saved = self.model_sort.findData(self._test_settings.value("sort", "added"))
        self.model_sort.setCurrentIndex(max(saved, 0))
        self.model_sort.currentIndexChanged.connect(self._sort_changed)
        self.sort_label = meta_label("Sırala")
        models.addWidget(self.banner)
        self.model_list = QListWidget()
        self.model_list.setAccessibleName("Eklenen modeller")
        self.model_list.setVisible(False)  # reload() dolu olunca gösterir
        self.model_empty = meta_label("Henüz model eklenmedi. Aşağıdan bir model ekle.")
        # Roller: seçili modelin rolleri; işlemler mevcut _make_primary/_toggle_role yollarıdır.
        self.primary_button = QPushButton("Beyin")
        self.primary_button.clicked.connect(self._make_primary)
        # Model kademesi: seçili model zor işler (çok adımlı, planlama) turlarını üstlenir.
        self.hard_button = QPushButton("Zor işler")
        self.hard_button.clicked.connect(self._toggle_hard_role)
        # Arıza yedeği: beyin modeli kota/bağlantı/sunucu hatası verince tur bununla sürer.
        self.fallback_button = QPushButton("Yedek")
        self.fallback_button.clicked.connect(self._toggle_fallback_role)
        for button in (self.primary_button, self.hard_button, self.fallback_button):
            button.setCheckable(True)
            # Eylem reddedilirse (yazılamadı, resim modeli) işaret gerçek duruma döner.
            button.clicked.connect(self._sync_detail)
        self.primary_button.setToolTip("Sohbetlere bu model cevap verir.")
        self.primary_button.setAccessibleName("Beyin modeli yap")
        self.hard_button.setAccessibleName("Zor işler modeli yap")
        self.fallback_button.setAccessibleName("Arıza yedeği yap")
        self._style_roles()
        self.remove_button = QPushButton("Modeli kaldır")
        self.remove_button.setObjectName("danger")
        self.remove_button.setStyleSheet("border: none; background: transparent;")
        self.remove_button.clicked.connect(self._remove_model)
        # Test: tek düğme; çalışırken metni ve eylemi "Durdur"a döner.
        self.model_test_button = QPushButton("Test et")
        self.model_test_button.clicked.connect(self._test_or_stop)
        self.parallel_test = QCheckBox("Paralel çalıştır")
        self.parallel_test.setChecked(self._saved_parallel())
        self.parallel_test.toggled.connect(self._remember_parallel)
        self._parallel_tip()
        self.retry_button = QPushButton("Kalanları yeniden dene")
        self.retry_button.setToolTip("Kalan senaryoları aynı modelle bir kez daha çalıştırır.")
        self.retry_button.clicked.connect(self._retry_failed)
        self.retry_button.setVisible(False)
        self.test_list = QListWidget()
        self.test_list.setAccessibleName("Test senaryoları")
        self.test_list.setMaximumHeight(160)
        self.test_list.setVisible(False)
        self.test_result = meta_label("")
        # Seçili model ayrıntısı: Rol, Test, kaldırma.
        self.detail = QFrame()
        self.detail.setObjectName("card")
        detail = QVBoxLayout(self.detail)
        detail.setContentsMargins(16, 16, 16, 16)
        detail.setSpacing(12)
        self.detail_title = QLabel()
        self.detail_title.setObjectName("sectionTitle")
        detail.addWidget(self.detail_title)
        role_row = QHBoxLayout()
        role_row.setSpacing(8)
        for button in (self.primary_button, self.hard_button, self.fallback_button):
            role_row.addWidget(button)
        role_row.addStretch(1)
        field(detail, "Rol", role_row, help=(
            "Beyin: sohbetlere bu model cevap verir. Zor işler: çok adımlı ve planlama isteyen "
            "turları bu model alır. Yedek: beyin kota, bağlantı ya da sunucu hatası verince tur "
            "bununla sürer. Ayarlardan çıkınca geçerli olur."
        ))
        test_row = QHBoxLayout()
        test_row.setSpacing(12)
        test_row.addWidget(self.model_test_button)
        test_row.addLayout(with_help(self.parallel_test, (
            f"Senaryolardan {PARALLEL_TEST}'ü aynı anda çalışır; API modellerinde testi kısaltır. "
            "Yerel sunucu aynı anda tek istek kaldırıyorsa yavaşlatabilir."
        )))
        test_row.addStretch(1)
        test_box = QVBoxLayout()
        test_box.setSpacing(8)
        test_box.addLayout(test_row)
        test_box.addWidget(self.test_list)
        test_box.addWidget(self.test_result)
        retry_row = QHBoxLayout()
        retry_row.addWidget(self.retry_button)
        retry_row.addStretch(1)
        test_box.addLayout(retry_row)
        field(detail, "Test", test_box, help=(
            "Seçili modeli senaryolarla dener. Ücretli modelde önce tahmini tutarı sorar; test "
            "bütçesi bu tahminin iki katıyla sınırlanır; yeniden denemede yalnız kalanların payı "
            "sorulur. Resim modelleri denenmez."
        ))
        detail.addLayout(button_row(self.remove_button))
        self.detail.setVisible(False)  # reload() model varsa gösterir
        self.model_list.currentItemChanged.connect(self._selection_changed)
        # Test edilmiş modellerden çıkan önerilen düzen (ana + zor işler) ve tek tık uygulama.
        self.layout_label = meta_label("")
        self.layout_label.setVisible(False)
        self.apply_layout_button = QPushButton("Bu düzeni uygula")
        self.apply_layout_button.clicked.connect(self._apply_layout)
        self.apply_layout_button.setVisible(False)
        # Geniş alanda liste solda, ayrıntı sağda; dar alanda ayrıntı listenin altına iner.
        self.model_split = QBoxLayout(QBoxLayout.Direction.TopToBottom)
        self.model_split.setSpacing(16)
        left = QVBoxLayout()
        left.setSpacing(12)
        # "Sırala" liste sütununun başlığında, listenin sağ kenarıyla hizalı: eskiden bütün sayfa
        # genişliğine yaslanıp kaydırma çubuğuna biniyordu (UX-03).
        sort_row = QHBoxLayout()
        sort_row.setContentsMargins(0, 0, 0, 0)
        sort_row.setSpacing(8)
        sort_row.addStretch(1)
        sort_row.addWidget(self.sort_label, 0, Qt.AlignmentFlag.AlignVCenter)
        sort_row.addWidget(self.model_sort, 0, Qt.AlignmentFlag.AlignVCenter)
        left.addLayout(sort_row)
        # Gizli başlar: reload() iki ve daha çok modelde açar; demo/boş listede sıralanacak şey yok.
        self.model_sort.setVisible(False)
        self.sort_label.setVisible(False)
        left.addWidget(self.model_list)
        left.addWidget(self.model_empty)
        left.addWidget(self.layout_label)
        self.apply_layout_button.setMinimumWidth(88)
        left.addLayout(self._tip_after(self.apply_layout_button, (
            "Test sonuçlarına göre önerilen Beyin, Zor işler ve bilgisayar kullanımı "
            "modellerini tek tıkla ayara yazar. Ayarlardan çıkınca geçerli olur."
        )))
        left.addStretch(1)
        self.model_split.addLayout(left, 1)
        self.model_split.addWidget(self.detail, 1, Qt.AlignmentFlag.AlignTop)
        models.addLayout(self.model_split)
        # B: model ekle
        add_box, add = group("Model ekle")
        # Form tam genişliğe yayılmasın: okunur bir sütun genişliğinde, solda.
        add_box.setMaximumWidth(FORM_MAX_WIDTH)
        layout.addWidget(add_box)
        self.add_connection = QComboBox()
        self.add_connection.setAccessibleName("Modelin bağlantısı")
        self.add_connection.currentIndexChanged.connect(self._connection_changed)
        self.model_name = QComboBox()
        self.model_name.setEditable(True)
        self.model_name.setAccessibleName("Model")
        self.model_hint = self.model_name.lineEdit()
        self.model_name.editTextChanged.connect(self._model_text_changed)
        self.fetch_button = QPushButton("Listeyi getir")
        self.fetch_button.clicked.connect(self._fetch_models)
        model_row = QHBoxLayout()
        model_row.setSpacing(8)
        model_row.addWidget(self.model_name, 1)
        model_row.addWidget(self.fetch_button)
        self.alias = QLineEdit()
        self.alias.setAccessibleName("Model takma adı")
        self.paid = QCheckBox("Ücretli model")
        self.input_price = QLineEdit("0")
        self.input_price.setAccessibleName("Girdi USD/milyon")
        self.output_price = QLineEdit("0")
        self.output_price.setAccessibleName("Çıktı USD/milyon")
        self.price_box = QWidget()
        self.price_box.setObjectName("cardRow")
        prices = QVBoxLayout(self.price_box)
        prices.setContentsMargins(0, 0, 0, 0)
        prices.setSpacing(12)
        field(prices, "Girdi fiyatı (USD / milyon jeton)", self.input_price)
        field(prices, "Çıktı fiyatı (USD / milyon jeton)", self.output_price)
        self.price_box.setVisible(False)
        self.kind = QComboBox()
        self.kind.setAccessibleName("Model türü")
        self.kind.addItem("Sohbet", "chat")
        self.kind.addItem("Resim", "image")
        self.image_price = QLineEdit("0")
        self.image_price.setAccessibleName("Resim başına USD")
        self.image_box = QWidget()
        self.image_box.setObjectName("cardRow")
        image_layout = QVBoxLayout(self.image_box)
        image_layout.setContentsMargins(0, 0, 0, 0)
        field(
            image_layout, "Resim başına fiyat (USD)", self.image_price,
            "Her çağrıda bütçeden bu tutar ayrılır (rezervasyon).",
        )
        self.image_box.setVisible(False)
        self.make_primary = QCheckBox("Beyin modeli yap")
        self.kind.currentIndexChanged.connect(self._sync_kind)
        self.paid.toggled.connect(self._sync_kind)
        field(add, "Bağlantı", self.add_connection)
        field(add, "Tür", self.kind)
        field(add, "Model", model_row)
        field(add, "Takma ad", self.alias)
        add.addLayout(with_help(self.paid, (
            "Modelin kullanım başına ücret aldığını işaretler ve fiyat alanlarını açar. "
            "Ücretli model bütçe korumasına takılır; ücretli kullanım izni ayar dosyasından "
            "verilir."
        )))
        add.addWidget(self.price_box)
        add.addWidget(self.image_box)
        add.addWidget(self.make_primary)
        self.add_button = QPushButton("Ekle")
        self.add_button.setObjectName("primary")
        self.add_button.clicked.connect(self._add_model)
        self.cli_test_button = QPushButton("Aboneliği test et")
        self.cli_test_button.setToolTip("Küçük bir gerçek istek atar; kotadan az harcar.")
        self.cli_test_button.clicked.connect(self._test_cli)
        add.addLayout(button_row(self.cli_test_button, self.add_button))
        # C: bağlantı ekle/düzenle; modeller önde kalsın diye varsayılan kapalı
        self.connection_toggle = QPushButton("Bağlantı ekle veya düzenle")
        self.connection_toggle.setObjectName("ghost")
        self.connection_toggle.setCheckable(True)
        layout.addWidget(divider())
        layout.addWidget(self.connection_toggle, 0, Qt.AlignmentFlag.AlignLeft)
        self.connection_box, conn = group(
            "Bağlantılar",
            "Modellerin çalıştığı sunucular. Listeden seçip düzenle ya da yenisini yaz.",
            divided=False,
        )
        self.connection_box.setVisible(False)
        self.connection_box.setMaximumWidth(FORM_MAX_WIDTH)
        self.connection_toggle.toggled.connect(self.connection_box.setVisible)
        layout.addWidget(self.connection_box)
        self.endpoint_list = QListWidget()
        self.endpoint_list.setAccessibleName("Tanımlı bağlantılar")
        self.endpoint_list.currentItemChanged.connect(self._select)
        conn.addWidget(self.endpoint_list)
        self.preset = QComboBox()
        self.preset.setAccessibleName("Bağlantı şablonu")
        for index, (label, *_rest) in enumerate(PRESETS):
            self.preset.addItem(label, index)
        self.preset.activated.connect(self._apply_preset)
        self.endpoint_id = QLineEdit()
        self.endpoint_id.setAccessibleName("Bağlantı kimliği")
        self.endpoint_id.setMaxLength(40)
        self.base_url = QLineEdit()
        self.base_url.setAccessibleName("Bağlantı adresi")
        self.needs_key = QCheckBox("API anahtarı gerekli")
        self.needs_key.setChecked(True)
        self.key = QLineEdit()
        self.key.setAccessibleName("API anahtarı")
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        field(conn, "Şablon", self.preset)
        field(conn, "Kimlik (kısa ad)", self.endpoint_id)
        field(conn, "Adres", self.base_url)
        conn.addWidget(self.needs_key)
        field(conn, "API anahtarı", self.key, help=(
            "Anahtar Windows kimlik kasasına yazılır, ayar dosyasına (YAML) asla girmez. "
            "Kaydedince alan temizlenir; boş bırakırsan kayıtlı anahtar korunur."
        ))
        self.test_button = QPushButton("Bağlantıyı test et")
        self.test_button.clicked.connect(self._test_connection)
        self.save_button = QPushButton("Kaydet")
        self.save_button.setObjectName("primary")
        self.save_button.clicked.connect(self._save)
        self.delete_button = QPushButton("Sil")
        self.delete_button.setObjectName("danger")
        self.delete_button.clicked.connect(self._delete)
        conn.addLayout(button_row(
            self.delete_button, self.test_button, self.save_button,
        ))
        self.status = QLabel()
        self.status.setWordWrap(True)
        # Uzun yol/adres tek "sözcük" sayılıp sütunu genişletmesin (yatay kaydırma çıkmasın).
        self.status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.addWidget(self.status)
        self._sync_cli_button()
        self._sync_model_field()
        if demo:
            # Demo'da reload çalışmaz; boş açılır liste yerine ne yapılacağını söyleyen soluk satır.
            self.add_connection.addItem("Bağlantı yok — aşağıdan ekle", None)
            self._sync_model_field()
            self.setEnabled(False)
            self.status.setText("Demo modunda bağlantılar ve modeller değiştirilmez.")
        else:
            self.reload()

    def _tip_after(self, button: QPushButton, text: str) -> QHBoxLayout:
        """Düğme + "?" satırı; düğme gizlenince baloncuk da gizlenir (`_show_button`)."""
        row = with_help(button, text)
        item = row.itemAt(1)
        tip = item.widget() if item is not None else None
        assert tip is not None
        tip.setVisible(not button.isHidden())
        self._button_tips[button] = tip
        return row

    def _show_button(self, button: QPushButton, visible: bool) -> None:
        button.setVisible(visible)
        tip = self._button_tips.get(button)
        if tip is not None:
            tip.setVisible(visible)

    def _style_roles(self) -> None:
        # İşaretli rol vurgu çerçevesi alır; "✓" metni durumu renksiz de belli eder.
        sheet = f"QPushButton:checked {{ border: 1px solid {self._colors['accent']}; }}"
        for button in (self.primary_button, self.hard_button, self.fallback_button):
            button.setStyleSheet(sheet)

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        super().resizeEvent(event)
        wide = event.size().width() >= WIDE_AT
        self.model_split.setDirection(
            QBoxLayout.Direction.LeftToRight if wide else QBoxLayout.Direction.TopToBottom
        )

    def show_problem(self, reason: str | None) -> None:
        self._problem = reason
        self._render_banner()

    @staticmethod
    def _connection_label(model: ModelConfig) -> str:
        if model.provider == "openai_compatible":
            return str(model.endpoint)
        return CLI_NAME if model.provider == "claude_cli" else model.provider

    def set_theme(self, palette: dict[str, str]) -> None:
        """Satır içi renkleri yeniden çizer; bağlantı taslağına (reload) dokunmaz."""
        self._colors = {key: palette[key] for key in _COLOR_KEYS}
        self._render_banner()
        self._style_roles()
        self._render_models()

    def _render_banner(self) -> None:
        try:
            settings = load_settings(self.config_path)
        except (OSError, ValueError):
            return
        model = settings.models.get(settings.primary_model)
        if self._problem:
            text, color = self._problem, self._colors["error"]
        elif model is not None:
            text = f"Beyin: {settings.primary_model} ({self._connection_label(model)})"
            if settings.hard_model and settings.hard_model != settings.primary_model:
                text += f" · Zor işler: {settings.hard_model}"
            if settings.fallback_model and settings.fallback_model != settings.primary_model:
                text += f" · Yedek: {settings.fallback_model}"
            color = self._colors["accent"]
        else:
            text, color = "Henüz beyin modeli seçilmedi.", self._colors["error"]
        self.banner.setText(text)
        self.banner.setVisible(bool(text))
        self.banner.setStyleSheet(f"color: {color}; font-weight: 600;")

    def reload(self) -> None:
        try:
            settings = load_settings(self.config_path)
        except (OSError, ValueError):
            self.status.setText("Ayarlar okunamadı.")
            return
        self._problem = provider_problem(settings)
        self._render_banner()
        self.make_primary.setChecked(self._problem is not None)
        if self._problem and not settings.endpoints:
            self.connection_toggle.setChecked(True)  # ilk kurulumda form gizli kalmasın
        reports = self._reports_seen = self._reports(settings)
        self._fallback = settings.fallback_model or ""
        self._model_data = [
            (
                alias,
                alias == settings.primary_model,
                # Eski openrouter/anthropic/ollama modelleri (CPA yolu) taşınmaz; kullanıcı siler.
                model.provider not in {"openai_compatible", "claude_cli"},
                self._connection_label(model),
                model.model,
                model.kind == "image",
                alias == settings.hard_model and alias != settings.primary_model,
                _test_summary(reports[alias]) if alias in reports else "",
            )
            for alias, model in settings.models.items()
        ]
        self._render_models()
        self._suggested = suggest_layout(list(reports.values()), settings.primary_model)
        ana, zor, pc = self._suggested
        current = (
            settings.primary_model, settings.hard_model or None, settings.computer_use.model or None
        )
        self.layout_label.setText(
            f"Önerilen düzen (test sonuçlarına göre): Ana: {ana}"
            + (f" · Zor işler: {zor}" if zor else "")
            + (f" · Bilgisayar kullanımı: {pc}" if pc else "")
            + (" — şu anki düzen bu." if (ana, zor, pc) == current else "")
            if ana else ""
        )
        self.layout_label.setVisible(bool(ana))
        self._show_button(self.apply_layout_button, bool(ana) and (ana, zor, pc) != current)
        # Liste kendi içinde kaydırılmasın: en çok 6 satır kadar uzar.
        heights = [
            self.model_list.item(index).sizeHint().height()
            for index in range(min(self.model_list.count(), 6))
        ]
        self.model_list.setFixedHeight((sum(heights) or 70) + 12)
        self.model_list.setVisible(self.model_list.count() > 0)
        self.model_sort.setVisible(self.model_list.count() > 1)
        self.sort_label.setVisible(self.model_list.count() > 1)
        self.model_empty.setVisible(self.model_list.count() == 0)
        self.detail.setVisible(self.model_list.count() > 0)
        self._sync_test_buttons()
        self.endpoint_list.clear()
        for name, endpoint in settings.endpoints.items():
            if not endpoint.api_key:
                state = "anahtarsız"
            else:
                state = "anahtar kayıtlı" if stored_endpoint_key(name) else "anahtar yok"
            self.endpoint_list.addItem(f"{name} — {endpoint.base_url} ({state})")
            self.endpoint_list.item(self.endpoint_list.count() - 1).setData(
                Qt.ItemDataRole.UserRole, name
            )
        # 4 satır görünür, fazlası liste içinde kayar.
        row = self.endpoint_list.sizeHintForRow(0) if self.endpoint_list.count() else -1
        if row <= 0:
            row = self.endpoint_list.fontMetrics().height() + 16
        self.endpoint_list.setFixedHeight(4 * row + 2 * self.endpoint_list.frameWidth())
        chosen = self.add_connection.currentData()
        self.add_connection.blockSignals(True)
        self.add_connection.clear()
        for name in settings.endpoints:
            self.add_connection.addItem(name, name)
        self.add_connection.addItem(CLI_NAME, CLI_KEY)
        self.add_connection.setCurrentIndex(max(self.add_connection.findData(chosen), 0))
        self.add_connection.blockSignals(False)
        if self.add_connection.currentData() != chosen:
            self._connection_changed()
        self._sync_cli_button()

    def _render_models(self) -> None:
        selected = self.model_list.currentItem()
        chosen = selected.data(Qt.ItemDataRole.UserRole) if selected is not None else None
        self.model_list.clear()
        for alias, is_brain, legacy, connection, model_id, image, hard, _tested in self._sorted():
            roles = [
                f"<span style='color:{self._colors['accent']}'>{name}</span>"
                for name, on in (("Beyin", is_brain), ("Zor işler", hard)) if on
            ]
            if alias == self._fallback and not is_brain:
                roles.append(f"<span style='color:{self._colors['muted']}'>Yedek</span>")
            muted = self._colors["muted"]
            role_text = " · ".join(roles) or f"<span style='color:{muted}'>Rol yok</span>"
            score = (
                "Test sürüyor…" if alias == self._testing
                else _score(self._reports_seen[alias]) if alias in self._reports_seen
                else "test yok"
            )
            tag = (
                f"&nbsp;&nbsp;<span style='color:{self._colors['warning']}'>eski ayar</span>"
                if legacy else ""
            )
            kind = " · resim" if image else ""
            label = QLabel(
                "<table width='100%' cellspacing='0'><tr>"
                f"<td><b>{html.escape(alias)}</b>{tag}<br>"
                f"<span style='color:{muted}'>"
                f"{html.escape(connection)} · {html.escape(model_id)}{kind}</span></td>"
                f"<td align='right'>{role_text}<br>"
                f"<span style='color:{muted}'>{html.escape(score)}</span></td>"
                "</tr></table>"
            )
            label.setTextFormat(Qt.TextFormat.RichText)
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            label.setStyleSheet("background: transparent; padding: 2px 0;")
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, alias)
            # +24: öğe dolgusu (8px) etiketi kırpmasın
            item.setSizeHint(label.sizeHint() + QSize(0, 24))
            self.model_list.addItem(item)
            self.model_list.setItemWidget(item, label)
            if alias == chosen:
                self.model_list.setCurrentItem(item)
        if self.model_list.currentItem() is None and self._model_data:
            # Ayrıntı paneli her zaman bir modeli gösterir: önce beyin modeli.
            brain = next((i for i, row in enumerate(self._model_data) if row[1]), 0)
            self.model_list.setCurrentRow(brain)
        self._sync_detail()

    def _sorted(self) -> list[_Row]:
        key = self.model_sort.currentData()
        rows = self._model_data
        if key == "score":
            def score(row: _Row) -> float:
                report = self._reports_seen.get(row[0])
                rate = _pass_rate(report) if report else None
                if report is None or rate is None:
                    return 2.0  # test edilmemiş en sonda
                # Tehlikeli hatalı model aynı yüzdedeki güvenli modelin altına iner.
                return -rate + (1.0 if report.get("dangerous_errors") else 0.0)
            return sorted(rows, key=score)
        if key == "connection":
            return sorted(rows, key=lambda row: (row[3].casefold(), row[0].casefold()))
        if key == "role":
            return sorted(rows, key=lambda row: (not row[1], not row[6], row[0] != self._fallback))
        if key == "name":
            return sorted(rows, key=lambda row: row[0].casefold())
        return list(rows)

    def _sort_changed(self, *_args: object) -> None:
        self._test_settings.setValue("sort", self.model_sort.currentData())
        self._render_models()

    def model_rows(self) -> list[tuple[str, str]]:
        """(takma ad, düz metin satırı); testler ve erişilebilirlik için."""
        rows = []
        for index in range(self.model_list.count()):
            item = self.model_list.item(index)
            label = self.model_list.itemWidget(item)
            text = html.unescape(re.sub(r"<[^>]+>", " ", label.text())) if label else ""  # type: ignore[attr-defined]
            rows.append((item.data(Qt.ItemDataRole.UserRole), " ".join(text.split())))
        return rows

    def select_model(self, alias: str) -> None:
        for index in range(self.model_list.count()):
            if self.model_list.item(index).data(Qt.ItemDataRole.UserRole) == alias:
                self.model_list.setCurrentRow(index)

    def _select(self, item: object) -> None:
        name = item.data(Qt.ItemDataRole.UserRole) if item is not None else None  # type: ignore[attr-defined]
        if name is None:
            return
        try:
            endpoint = load_settings(self.config_path).endpoints[name]
        except (OSError, ValueError, KeyError):
            return
        self.endpoint_id.setText(name)
        self.base_url.setText(endpoint.base_url)
        self.needs_key.setChecked(endpoint.api_key)
        self.key.clear()
        self.key.setPlaceholderText("kayıtlı" if stored_endpoint_key(name) else "")

    def _apply_preset(self) -> None:
        _label, slug, url, needs_key = PRESETS[self.preset.currentData()]
        if not self.endpoint_id.text() or self.endpoint_id.text() in {p[1] for p in PRESETS}:
            self.endpoint_id.setText(slug)
        self.base_url.setText(url)
        self.needs_key.setChecked(needs_key)

    def _valid_form(self) -> tuple[str, str] | None:
        name, url = self.endpoint_id.text().strip(), self.base_url.text().strip()
        if not ENDPOINT_ID_RE.fullmatch(name):
            self.status.setText("Kimlik 1-40 karakter olmalı: harf, rakam, _ ve -.")
            return None
        try:
            EndpointConfig(base_url=url)
        except ValueError:  # pydantic mesajı girdiyi içerebilir; sabit metin kullanılır
            self.status.setText(_BAD_URL)
            return None
        return name, url.rstrip("/")

    def _test_connection(self) -> None:
        form = self._valid_form()
        if form is None or (self._test is not None and self._test.isRunning()):
            return
        name, url = form
        key = None
        if self.needs_key.isChecked():
            key = self.key.text().strip() or stored_endpoint_key(name)
        self.status.setText("Bağlantı deneniyor…")
        self._test = ConnectionTest(url, key, self)
        self._test.done.connect(lambda message, _models: self.status.setText(message))
        self._test.start()

    def _fetch_models(self) -> None:
        """Seçili bağlantının model listesini getirir; yalnız kayıtlı uç ve kayıtlı anahtar."""
        name = self.add_connection.currentData()
        if name == CLI_KEY:
            self._fill_models(list(CLI_MODELS))
            return
        if name is None or (self._fetch is not None and self._fetch.isRunning()):
            self.status.setText("Önce bir bağlantı ekleyin.")
            return
        try:
            endpoint = load_settings(self.config_path).endpoints[name]
        except (OSError, ValueError, KeyError):
            self.status.setText("Ayarlar okunamadı.")
            return
        key = stored_endpoint_key(name) if endpoint.api_key else None
        self.status.setText("Model listesi alınıyor…")
        self._fetch = ConnectionTest(endpoint.base_url, key, self)
        self._fetch.done.connect(self._fetch_done)
        self._fetch.start()

    def _fetch_done(self, message: str, models: list[str]) -> None:
        self.status.setText(message)
        if models:
            self._fill_models(models)

    def _fill_models(self, models: list[str]) -> None:
        self.model_name.clear()
        self.model_name.addItems(models)

    def _sync_model_field(self) -> None:
        """Bağlantı yokken model alanı soluk ve kapalı; neden boş olduğunu placeholder söyler."""
        ready = self.add_connection.currentData() is not None
        self.model_name.setEnabled(ready)
        self.fetch_button.setEnabled(ready)
        if self.model_hint is not None:
            self.model_hint.setPlaceholderText(
                "Model adı yaz veya listeyi getir" if ready else "Önce bağlantı seç"
            )

    def _connection_changed(self) -> None:
        self._sync_cli_button()
        self._sync_model_field()
        self._fill_models(list(CLI_MODELS) if self.add_connection.currentData() == CLI_KEY else [])
        if self.add_connection.currentData() == CLI_KEY:
            found = shutil.which("claude")
            self.status.setText(
                f"Claude Code CLI bulundu: {found}"
                if found
                else "Claude Code CLI bulunamadı. Kur ve Claude Code ile giriş yap."
            )

    def _sync_cli_button(self) -> None:
        self.cli_test_button.setVisible(self.add_connection.currentData() == CLI_KEY)
        # Resim modeli openai_compatible uç ister; Claude aboneliğinde seçilemez.
        cli = self.add_connection.currentData() == CLI_KEY
        self.kind.model().item(1).setEnabled(not cli)  # type: ignore[attr-defined]
        if cli:
            self.kind.setCurrentIndex(0)
        self.kind.setToolTip("Resim modeli Claude aboneliğiyle kullanılamaz." if cli else "")

    def _sync_kind(self, *_args: object) -> None:
        image = self.kind.currentData() == "image"
        paid = self.paid.isChecked()
        self.price_box.setVisible(paid and not image)
        self.image_box.setVisible(paid and image)
        # Resim modeli beyin olamaz (settings doğrular); seçenek kapalı ve işaretsiz.
        self.make_primary.setEnabled(not image)
        if image:
            self.make_primary.setChecked(False)

    def _default_alias(self, model: str) -> str:
        slug = _slug(model)
        if self.add_connection.currentData() == CLI_KEY and slug:
            return f"claude-cli-{slug}"[:40]
        return slug

    def _model_text_changed(self, text: str) -> None:
        # Takma ad model adından türer; kullanıcı elle değiştirdiyse ezilmez.
        if self.alias.text() in {"", self._auto_alias}:
            self._auto_alias = self._default_alias(text.strip())
            self.alias.setText(self._auto_alias)

    def _raw(self) -> dict[str, object]:
        return yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}

    def _write(self, raw: dict[str, object]) -> bool:
        # Döngüsel içe aktarmayı önlemek için geç alınır (settings_panel bu bölümü içerir).
        from artemis.channels.desktop.settings_panel import _write_validated

        try:
            _write_validated(self.config_path, raw)
        except (OSError, ValueError, yaml.YAMLError):
            self.status.setText("Kaydedilemedi; mevcut ayarlar korunuyor.")
            return False
        return True

    def _save(self) -> None:
        form = self._valid_form()
        if form is None:
            return
        name, url = form
        raw = self._raw()
        endpoints: dict[str, object] = raw.setdefault("endpoints", {})  # type: ignore[assignment]
        endpoints[name] = {"base_url": url, "api_key": self.needs_key.isChecked()}
        if not self._write(raw):
            return
        message = "Bağlantı kaydedildi."
        typed = self.key.text().strip()
        if typed and self.needs_key.isChecked():
            failure = store_endpoint_key(name, typed)
            if failure:
                message = f"Bağlantı kaydedildi ama anahtar saklanamadı; {failure}"
        self.key.clear()
        self.reload()
        # Yeni model eklerken kaydedilen bağlantı hazır seçili gelir.
        self.add_connection.setCurrentIndex(self.add_connection.findData(name))
        self.status.setText(message)
        self.changed.emit()

    def _delete(self) -> None:
        name = self.endpoint_id.text().strip()
        try:
            settings = load_settings(self.config_path)
        except (OSError, ValueError):
            self.status.setText("Ayarlar okunamadı.")
            return
        if name not in settings.endpoints:
            self.status.setText("Silinecek kayıtlı bir bağlantı seçin.")
            return
        if any(model.endpoint == name for model in settings.models.values()):
            self.status.setText(
                "Bu bağlantıyı kullanan modeller var; önce Modeller listesinden 'Kaldır' ile "
                "o modelleri kaldırın."
            )
            return
        raw = self._raw()
        raw["endpoints"].pop(name, None)  # type: ignore[attr-defined]
        if not self._write(raw):
            return
        delete_secret(f"Artemis/{name}")
        self.endpoint_id.clear()
        self.base_url.clear()
        self.key.clear()
        self.reload()
        self.status.setText("Bağlantı silindi.")
        self.changed.emit()

    def _test_cli(self) -> None:
        if self._cli_test is not None and self._cli_test.isRunning():
            return
        self.status.setText("Claude Code deneniyor…")
        model = self.model_name.currentText().strip() or "haiku"
        self._cli_test = CliTest(self.config_path, model, self)
        self._cli_test.done.connect(self.status.setText)
        self._cli_test.start()

    def _add_model(self) -> None:
        connection = self.add_connection.currentData()
        model = self.model_name.currentText().strip()
        alias = self.alias.text().strip() or self._default_alias(model)
        if connection is None:
            self.status.setText("Önce bir bağlantı ekleyin.")
            return
        if not model or not alias:
            self.status.setText("Model adı yazın veya listeden seçin.")
            return
        image = self.kind.currentData() == "image"
        if image and connection == CLI_KEY:
            self.status.setText("Resim modeli Claude aboneliğiyle kullanılamaz; bir uç seçin.")
            return
        try:
            prices = (
                [Decimal(self.input_price.text() or "0"), Decimal(self.output_price.text() or "0")]
                if self.paid.isChecked() and connection != CLI_KEY and not image
                else [Decimal(0), Decimal(0)]
            )
            image_cost = (
                Decimal(self.image_price.text() or "0")
                if image and self.paid.isChecked()
                else Decimal(0)
            )
            if image_cost < 0 or not image_cost.is_finite():
                raise InvalidOperation
            settings = load_settings(self.config_path)
        except InvalidOperation:
            self.status.setText("Fiyat sayı olmalı (ücretsiz için 0).")
            return
        except (OSError, ValueError):
            self.status.setText("Ayarlar okunamadı.")
            return
        if connection != CLI_KEY and connection not in settings.endpoints:
            self.status.setText("Önce bağlantıyı kaydedin.")
            return
        if alias in settings.models:
            self.status.setText("Bu takma ad kullanımda; başka bir ad yazın.")
            return
        raw = self._raw()
        models: dict[str, object] = raw.setdefault("models", {})  # type: ignore[assignment]
        entry: dict[str, str] = {
            "provider": "claude_cli" if connection == CLI_KEY else "openai_compatible",
            "model": model,
            "input_per_million": str(prices[0]),
            "output_per_million": str(prices[1]),
        }
        if connection != CLI_KEY:
            entry["endpoint"] = connection
        if image:
            entry["kind"] = "image"
            entry["image_cost_usd"] = str(image_cost)
        models[alias] = entry
        primary = self.make_primary.isChecked() and not image
        if primary:
            raw["primary_model"] = alias
        if not self._write(raw):
            return
        self.reload()
        paid = (any(price > 0 for price in prices) or image_cost > 0) and not settings.allow_paid
        self.status.setText(
            "Model eklendi."
            + (
                " Ücretli model bütçe korumasınca engellenir; "
                "ücretli kullanım izni ayar dosyasından verilir."
                if paid
                else ""
            )
            + (
                " Resim başına fiyat her çağrıda bütçeden ayrılan bir rezervasyondur."
                if image_cost > 0
                else ""
            )
            + (" Beyin modeli yapıldı." if primary else "")
        )
        self.changed.emit()

    def _selected_alias(self) -> str | None:
        item = self.model_list.currentItem()
        if item is None:
            self.status.setText("Önce listeden bir model seçin.")
            return None
        return str(item.data(Qt.ItemDataRole.UserRole))

    def _make_primary(self) -> None:
        alias = self._selected_alias()
        if alias is None:
            return
        try:
            raw = self._raw()
        except (OSError, yaml.YAMLError):
            self.status.setText("Ayarlar okunamadı.")
            return
        if any(row[0] == alias and row[5] for row in self._model_data):
            self.status.setText("Resim modeli beyin olamaz; bir sohbet modeli seçin.")
            return
        # Takma ada göre yazılır: aynı model kimliğini paylaşan modeller karışmaz.
        raw["primary_model"] = alias
        for role in ("hard_model", "fallback_model"):
            if raw.get(role) == alias:
                raw.pop(role)  # beyin olan model ayrıca zor işler/yedek sayılmaz
        if not self._write(raw):
            return
        self.reload()
        self.status.setText(f"Beyin modeli: {alias}.")
        self.changed.emit()

    def _remove_model(self) -> None:
        alias = self._selected_alias()
        if alias is None:
            return
        try:
            settings = load_settings(self.config_path)
        except (OSError, ValueError):
            self.status.setText("Ayarlar okunamadı.")
            return
        if alias == settings.primary_model:
            self.status.setText("Beyin modeli kaldırılamaz; önce başka bir modeli beyin yapın.")
            return
        if alias == settings.fallback_model:
            self.status.setText("Arıza yedeği kaldırılamaz; önce Rol'den Yedek'i kapat.")
            return
        if alias == settings.hard_model:
            self.status.setText("Zor işler modeli kaldırılamaz; önce Rol'den Zor işler'i kapat.")
            return
        raw = self._raw()
        raw["models"].pop(alias, None)  # type: ignore[attr-defined]
        if not self._write(raw):
            return
        self.reload()
        self.status.setText("Model kaldırıldı.")
        self.changed.emit()

    # --- Model kademesi ve model testi ---------------------------------------------------

    def _reports(self, settings: Settings) -> dict[str, dict[str, object]]:
        """Kaydedilmiş son test raporları (takma ad -> rapor); okunamayan rapor yok sayılır."""
        folder = settings.state_dir / "model_testleri"
        found: dict[str, dict[str, object]] = {}
        for alias in settings.models:
            path = folder / f"{_slug(alias) or 'model'}.json"
            try:
                report = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            # Eksik/bozuk rapor öneriyi (suggest_layout) çökertmesin: alanlar doğrulanır.
            if (
                isinstance(report, dict)
                and report.get("model") == alias
                and isinstance(report.get("zor"), dict)
                and isinstance(report["zor"].get("rate"), int | float)
                and isinstance(report.get("median_seconds"), int | float)
                and isinstance(report.get("roles"), list)
            ):
                found[alias] = report
        return found

    # Bağlı yöntemler: self'i yakalayan lambda Qt bağlantısında referans döngüsü kurar ve gc
    # paneli yanlış iş parçacığında toplayınca erişim ihlali çıkar (2026-10-31 bulgusu).
    def _toggle_hard_role(self) -> None:
        self._toggle_role("hard_model")

    def _toggle_fallback_role(self) -> None:
        self._toggle_role("fallback_model")

    def _selection_changed(self, *_args: object) -> None:
        self._sync_detail()  # paralel kutusunu da günceller

    def _saved_parallel(self) -> bool:
        return bool(self._test_settings.value("parallel", False, type=bool))

    def _remember_parallel(self, on: bool) -> None:
        self._test_settings.setValue("parallel", on)

    def _parallel_tip(self) -> None:
        """Paralel her modelde seçilebilir; yerel sunucuda yavaşlayabileceği ipucunda yazar."""
        alias = self._current_alias()
        try:
            settings = load_settings(self.config_path)
            local = alias in settings.models and _is_local_model(
                settings.models[alias], settings.endpoints
            )
        except (OSError, ValueError):
            local = False
        self.parallel_test.setToolTip(
            f"Aynı anda {PARALLEL_TEST} senaryo. Bu bağlantı bu bilgisayarda çalışıyor: "
            "sunucu aynı anda tek istek kaldırıyorsa sırayla çalıştırmak daha hızlı olabilir."
            if local
            else f"Aynı anda {PARALLEL_TEST} senaryo; API modellerinde testi kısaltır."
        )

    def _model_test_finished(self, *_args: object) -> None:
        if self._stopping:
            self._stopping = self._retrying = False
            process, self._model_test, self._testing = self._model_test, None, ""
            if process is not None:
                process.deleteLater()
            self.reload()
            self.status.setText("Test durduruldu")
            return
        self._model_test_done(self._testing, self._test_report)

    def _current_alias(self) -> str | None:
        item = self.model_list.currentItem()
        return str(item.data(Qt.ItemDataRole.UserRole)) if item is not None else None

    def _sync_detail(self, *_args: object) -> None:
        """Ayrıntı paneli seçili modelin durumunu gösterir."""
        selected = self._current_alias()
        row = next((r for r in self._model_data if r[0] == selected), None)
        brain, image, hard = (row[1], row[5], row[6]) if row else (False, False, False)
        fallback = bool(selected) and selected == self._fallback
        self.detail_title.setText(selected or "")
        reason = "Resim modeli bu rolü üstlenemez." if image else (
            "Bu model zaten beyin; bu rol için başka bir model seç." if brain else ""
        )
        hard_tip = "Çok adımlı ve planlama isteyen turlar bu modelle çalışır."
        fallback_tip = (
            "Beyin modeli kotaya takılır ya da yanıt vermezse Artemis bu modelle devam eder."
        )
        for button, name, on, tip, free in (
            (self.primary_button, "Beyin", brain, None, not image),
            (self.hard_button, "Zor işler", hard, hard_tip, not image and not brain),
            (self.fallback_button, "Yedek", fallback, fallback_tip, not image and not brain),
        ):
            button.setChecked(on)
            button.setText(("✓ " if on else "") + name)
            button.setEnabled(free)
            if tip:
                button.setToolTip(tip if free else reason)
        summary = (
            _test_summary(self._reports_seen[selected]) if selected in self._reports_seen else ""
        )
        self.test_result.setText(
            summary.replace("Test:", "Son test:", 1) if summary else "Bu model henüz test edilmedi."
        )
        self._sync_test_buttons()
        self._parallel_tip()

    def _toggle_role(self, key: str) -> None:
        """Seçili modeli zor işler (hard_model) ya da arıza yedeği (fallback_model) yapar."""
        alias = self._selected_alias()
        if alias is None:
            return
        name = "Zor işler" if key == "hard_model" else "Arıza yedeği"
        try:
            settings = load_settings(self.config_path)
            raw = self._raw()
        except (OSError, ValueError, yaml.YAMLError):
            self.status.setText("Ayarlar okunamadı.")
            return
        model = settings.models.get(alias)
        if model is None or model.kind == "image":
            self.status.setText(f"Resim modeli {name.lower()} modeli olamaz; sohbet modeli seçin.")
            return
        if alias == settings.primary_model:
            self.status.setText(f"Bu model zaten beyin; {name.lower()} için başka bir model seçin.")
            return
        removing = raw.get(key) == alias
        if removing:
            raw.pop(key, None)
        else:
            raw[key] = alias
        if not self._write(raw):
            return
        self.reload()
        if key == "hard_model":
            done = (
                "Zor işler modeli kapatıldı; her tur beyin modeliyle çalışır."
                if removing
                else f"Zor işler modeli: {alias}. Çok adımlı ve planlama isteyen turlar bunu "
                "kullanır."
            )
        else:
            done = (
                "Arıza yedeği kapatıldı; beyin modeli yanıt vermezse tur hata ile biter."
                if removing
                else f"Arıza yedeği: {alias}. Beyin modeli kotaya takılır ya da yanıt vermezse "
                "Artemis bununla sürer."
            )
        self.status.setText(done)
        self.changed.emit()

    def _confirm_paid(self, alias: str, cost: Decimal) -> bool:
        """Ücretli modelde testten önce bir kez sorulur (varsayılan Hayır)."""
        answer = QMessageBox.question(
            self,
            "Ücretli model testi",
            f"{alias} ücretli. Test yaklaşık {cost} USD harcayabilir. Başlatılsın mı?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _test_model(self) -> None:
        self._start_test(self._selected_alias(), None)

    def _retry_failed(self) -> None:
        failed = [key for key, state in self._test_rows.items() if state == "kaldı"]
        if self._last is None or not failed:
            return
        alias = str(self._last[1].get("model", ""))
        if alias in [row[0] for row in self._model_data]:
            self._start_test(alias, failed)

    def _start_test(self, alias: str | None, only: list[str] | None) -> None:
        if alias is None:
            return
        if self._model_test is not None:
            self.status.setText(f"{self._testing} testi sürüyor; bitmesini bekleyin.")
            return
        try:
            settings = load_settings(self.config_path)
        except (OSError, ValueError):
            self.status.setText("Ayarlar okunamadı.")
            return
        model = settings.models[alias]
        if model.kind == "image":
            self.status.setText("Resim modeli sohbet senaryolarıyla test edilmez.")
            return
        arguments = ["-m", "bench.model_testi", "--config", str(self.config_path), "--model", alias]
        if model.input_per_million or model.output_per_million:
            cost = (
                Decimal(TEST_INPUT_TOKENS) * model.input_per_million
                + Decimal(TEST_OUTPUT_TOKENS) * model.output_per_million
            ) / Decimal(1_000_000)
            if only:  # yeniden deneme yalnız kalan senaryoların payını harcar
                cost = cost * len(only) / _scenario_count()
            cost = cost.quantize(Decimal("0.01"), rounding=ROUND_UP)
            if not self._confirm_paid(alias, cost):
                self.status.setText("Test başlatılmadı.")
                return
            # Bütçe tavanı tahminin iki katı: test bunu aşamaz (rezervasyon korur).
            arguments += ["--ucretli-onay", "--butce", str(cost * 2)]
        if self.parallel_test.isChecked():
            arguments += ["--paralel", str(PARALLEL_TEST)]
        for scenario_id in only or []:
            arguments += ["--senaryo", scenario_id]
        # Yeniden deneme ayrı dosyaya yazar: tam rapor kısmi raporla ezilmez, sonra birleştirilir.
        suffix = ".yeniden" if only else ""
        report = settings.state_dir / "model_testleri" / f"{_slug(alias) or 'model'}{suffix}.json"
        arguments += ["--rapor", str(report)]
        process = QProcess(self)
        environment = QProcessEnvironment()
        # Alt sürece ortamın tamamı verilmez (CLAUDE.md): OS değişkenleri + bu ucun anahtarı.
        variables = dict(child_environment(), PYTHONIOENCODING="utf-8")
        if model.endpoint:
            name = endpoint_key_variable(model.endpoint)
            if name in os.environ:
                variables[name] = os.environ[name]
        for name, value in variables.items():
            environment.insert(name, value)
        process.setProcessEnvironment(environment)
        process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        process.readyReadStandardOutput.connect(self._model_test_output)
        self._test_report = report
        process.finished.connect(self._model_test_finished)
        self._model_test, self._testing = process, alias
        self._retrying, self._test_buffer = bool(only), ""
        if only:
            self._test_rows.update(dict.fromkeys(only, "bekliyor"))
        else:
            self._test_rows = dict.fromkeys(_scenario_ids(), "bekliyor")
            self._second.clear()
            self._last = None
        self._render_test_list()
        self._render_models()
        count = len(only) if only else _scenario_count()
        self.status.setText(f"{alias} test ediliyor ({count} senaryo, birkaç dakika sürer)…")
        process.start(sys.executable, arguments)

    def _stop_test(self) -> None:
        process = self._model_test
        if process is None or self._stopping:
            return
        self._stopping = True
        self.status.setText("Test durduruluyor…")
        process.terminate()
        QTimer.singleShot(3000, self._kill_test)

    def _kill_test(self) -> None:
        process = self._model_test
        if (
            process is not None and self._stopping
            and process.state() != QProcess.ProcessState.NotRunning
        ):
            process.kill()

    def _render_test_list(self) -> None:
        self.test_list.clear()
        for key, state in self._test_rows.items():
            note = " (2. deneme)" if state == "geçti" and key in self._second else ""
            self.test_list.addItem(f"{_STATE_TEXT[state]} {_scenario_label(key)}: {state}{note}")
        self._sync_test_buttons()

    def _sync_test_buttons(self) -> None:
        running = self._model_test is not None
        selected = self._current_alias()
        mine = running and self._testing == selected
        failed = any(state == "kaldı" for state in self._test_rows.values())
        # Satırlar son/çalışan testin modeline aittir; başka model seçiliyken gösterilmez.
        shown = self._testing or (str(self._last[1].get("model", "")) if self._last else "")
        here = shown == selected
        self.test_list.setVisible(bool(self._test_rows) and here)
        # Tek düğme: test sürerken aynı yerde "Durdur" olur; başka modelin testi sürüyorsa kapalı.
        self.model_test_button.setText("Durdur" if mine else "Test et")
        self.model_test_button.setEnabled(not running or mine)
        self.model_test_button.setToolTip(
            "Çalışan testi durdurur; rapor kaydedilmez." if mine
            else f"{self._testing} testi sürüyor; bitmesini bekle." if running
            else f"{_scenario_count()} senaryoluk testi arka planda çalıştırır; "
            "bilgisayarına dokunmaz."
        )
        # Kademeli testin ("ana + zor") adı tek model değil: yeniden deneme yalnız tek modelde.
        retryable = self._last is not None and str(self._last[1].get("model", "")) in [
            row[0] for row in self._model_data
        ]
        self._show_button(self.retry_button, not running and failed and retryable and here)

    def _test_or_stop(self) -> None:
        if self._model_test is not None and self._testing == self._current_alias():
            self._stop_test()
        else:
            self._test_model()

    def _model_test_output(self) -> None:
        if self._model_test is None:
            return
        text = bytes(self._model_test.readAllStandardOutput().data()).decode("utf-8", "replace")
        self._test_buffer += text
        *lines, self._test_buffer = self._test_buffer.split("\n")
        for line in lines:
            parts = line.split()
            if len(parts) == 4 and parts[0] == "ilerleme":
                self.status.setText(f"{self._testing} test ediliyor: {parts[1]} senaryo bitti…")
                if parts[2] in self._test_rows and parts[3] in _STATE_TEXT:
                    self._test_rows[parts[2]] = parts[3]
                    if parts[3] == "geçti" and self._retrying:
                        self._second.add(parts[2])
                    self._render_test_list()

    def _model_test_done(self, alias: str, report_path: Path) -> None:
        process, self._model_test, self._testing = self._model_test, None, ""
        retrying, self._retrying = self._retrying, False
        if process is not None:
            process.deleteLater()
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
            if retrying and self._last is not None:
                base_path, base = self._last
                report_path.unlink(missing_ok=True)
                report_path = base_path
                report = merge_retry(base, report)
                _write_json(report_path, report)
        except (OSError, ValueError, KeyError, TypeError):
            self.reload()
            self._render_test_list()
            self.status.setText(f"{alias} testi tamamlanamadı (bağlantı veya ayar sorunu).")
            return
        self._last = (report_path, report)
        done = report.get("scenarios", [])
        self._second = {item["id"] for item in done if item.get("attempt") == 2}
        self._test_rows.update(
            {item["id"]: "geçti" if item["success"] else "kaldı" for item in done}
        )
        self._render_test_list()
        self.reload()
        self.status.setText(f"{alias}: {_test_summary(report)}. {report.get('summary', '')}")

    def _apply_layout(self) -> None:
        ana, zor, pc = self._suggested
        if not ana:
            return
        try:
            raw = self._raw()
        except (OSError, yaml.YAMLError):
            self.status.setText("Ayarlar okunamadı.")
            return
        raw["primary_model"] = ana
        if zor:
            raw["hard_model"] = zor
        else:
            raw.pop("hard_model", None)
        # Boş computer_use.model ana modeli kullanır; öneri ana modelse boş bırakılır.
        computer = raw.setdefault("computer_use", {})
        if isinstance(computer, dict):
            computer["model"] = pc or ""
        if not self._write(raw):
            return
        self.reload()
        self.status.setText(
            f"Düzen uygulandı. Beyin: {ana}" + (f", zor işler: {zor}" if zor else "")
            + (f", bilgisayar kullanımı: {pc}" if pc else "") + "."
        )
        self.changed.emit()


def _write_json(path: Path, data: object) -> None:
    """Geçici dosyaya yazıp yerine koyar: yarım rapor kalmaz."""
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def _pass_rate(report: dict[str, object]) -> float | None:
    """Geçen senaryoların oranı (0..1); sayılabilir bölüm yoksa None."""
    passed = total = 0
    for key in ("temel", "zor", "bilgisayar"):
        part = report.get(key)
        if isinstance(part, dict) and isinstance(part.get("passed"), int) and isinstance(
            part.get("total"), int
        ):
            passed, total = passed + part["passed"], total + part["total"]
    return passed / total if total else None


def _score(report: dict[str, object]) -> str:
    """Listede tek sayı: geçen senaryoların yüzdesi ('%87'); tehlikeli hata metinle belirtilir."""
    rate = _pass_rate(report)
    if rate is None:
        return ""
    return f"%{round(100 * rate)}" + (
        " · tehlikeli hata" if report.get("dangerous_errors") else ""
    )


def _test_summary(report: dict[str, object]) -> str:
    """'Test: temel 7/7 · zor 5/8 · ~5 sn → ana model' biçiminde tek satır."""
    temel, zor, roles = report.get("temel"), report.get("zor"), report.get("roles")
    if not isinstance(temel, dict) or not isinstance(zor, dict) or not isinstance(roles, list):
        return ""
    text = (
        f"Test: temel {temel.get('passed')}/{temel.get('total')} · "
        f"zor {zor.get('passed')}/{zor.get('total')}"
    )
    computer = report.get("bilgisayar")
    if isinstance(computer, dict):
        text += f" · bilgisayar {computer.get('passed')}/{computer.get('total')}"
    text += f" · ~{report.get('median_seconds')} sn"
    if report.get("dangerous_errors"):
        return text + " · tehlikeli hata var → önerilmez"
    named = [_ROLE_TEXT[role] for role in roles if role in _ROLE_TEXT]
    return text + (" → " + " + ".join(named) if named else " → önerilmez")
