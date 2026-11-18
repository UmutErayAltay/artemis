"""Otomasyon sayfası: rutinler, araştırmalar, gün özetleri ve son tetikleyici bildirimleri.

Günlük kullanılan şeyler Ayarlar'a girmeden tek yerde. Sayfa yalnız OKUR; eylemler kendi
araç çağrısıdır ve `tool_requested` ile pencereye gider, pencere onları ayarlardaki Çalıştır
düğmesiyle aynı köprü ve SafetyGate yolundan gönderir (ikinci onay ya da çalıştırma yolu yok).
Rutin ve araştırma verisi dosya/SQLite okur; okuma iş parçacığında yapılır, GUI'ye sonuç
sinyalle döner.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from collections.abc import Sequence
from contextlib import closing, suppress
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from artemis.channels.desktop import themes
from artemis.channels.desktop.page_kit import (
    CurrentPageStack,
    EmptyState,
    make_card,
    make_label,
    make_page_header,
    make_scroll_page,
)
from artemis.channels.desktop.text_viewer import open_text_file
from artemis.channels.desktop.widgets import with_help
from artemis.config.settings import load_settings
from artemis.i18n import t
from artemis.proactive.daily_summary import parse_day, summary_path
from artemis.proactive.routines import RoutinesError, RoutineStore
from artemis.proactive.schedule import format_local
from artemis.proactive.store import ProactiveStore, ProactiveStoreError
from artemis.tools.base import ToolResult
from artemis.tools.plugins.research import JobArguments
from artemis.tools.plugins.routines import RoutineNameArguments
from artemis.tools.research_jobs import REPORT_DIR

_DAYS = ("Pzt", "Sal", "Çar", "Per", "Cum", "Cmt", "Paz")
_MONTHS = ("Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos", "Eylül",
           "Ekim", "Kasım", "Aralık")
_WEEKDAYS = ("Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar")
SHOWN = 7  # özet ve bildirim kartlarında en çok bu kadar satır
JOBS_SHOWN = 8
# Durum -> (etiket, tema jetonu). Nokta rengi jetondan gelir; etiket rengi tek başına bırakmaz.
_JOB_STATES = {
    "running": ("Çalışıyor", "accent"),
    "cancelling": ("Durduruluyor", "warning"),
    "done": ("Bitti", "success"),
    "cancelled": ("Durduruldu", "muted"),
    "timeout": ("Zaman aşımı", "error"),
    "error": ("Hata", "error"),
    "uncertain": ("Doğrulanamadı", "error"),
}
_JOB_ID_CHARS = frozenset("0123456789abcdef")


def _schedule_text(at: str, days: tuple[int, ...]) -> str:
    when = t("her gün") if len(days) == 7 else " ".join(t(_DAYS[day]) for day in days)
    return f"{at} · {when}"


def _human_day(day: date) -> str:
    """'14 Ocak Çarşamba'; ay ve gün adları sabit tabloda, yerel ayara bağlı değil."""
    return f"{day.day} {t(_MONTHS[day.month - 1])} {t(_WEEKDAYS[day.weekday()])}"


def _human_when(moment: datetime, now: datetime) -> str:
    """'Bugün 10:42' ya da '15 Oca 10:42'."""
    clock = moment.strftime("%H:%M")
    if moment.date() == now.date():
        return t("Bugün {clock}").format(clock=clock)
    return f"{moment.day} {t(_MONTHS[moment.month - 1])[:3]} {clock}"


def _summary_subtitle(line: str, day: date) -> str:
    """Özetin ilk satırı; başındaki aynı günün tarihi (ISO ya da '14 Ocak ...') atılır."""
    month = _MONTHS[day.month - 1]
    weekday = _WEEKDAYS[day.weekday()]
    pattern = (rf"^\s*(?:{re.escape(day.isoformat())}"
               rf"|{day.day} {month}(?: \d{{4}})?(?: {weekday})?)\s*[:\-|·,]?\s*")
    rest = re.sub(pattern, "", line, count=1, flags=re.IGNORECASE).strip()
    return rest or line


def _first_line(path: Path) -> str:
    """Özetin ilk anlamlı satırı; başlık işaretleri atılır, dosya sınırlı okunur."""
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            head = handle.read(2000)
    except OSError:
        return t("Dosya okunamadı")
    for line in head.splitlines():
        text = line.strip().lstrip("#").strip()
        if text:
            return text[:120]
    return t("Boş özet")


def _jobs(db_path: Path) -> list[dict[str, Any]]:
    """İşleri SALT OKUNUR açar: `ResearchJobs` kurucusu kayıt günceller, burada olmaz."""
    if not db_path.is_file():
        return []
    try:
        with closing(sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True, timeout=2)) as db:
            rows = db.execute(
                "SELECT id, topic, status, max_searches, searches, created, finished "
                "FROM research_jobs ORDER BY created DESC LIMIT ?",
                (JOBS_SHOWN,),
            ).fetchall()
    except sqlite3.Error:
        return []
    keys = ("id", "topic", "status", "max_searches", "searches", "created", "finished")
    return [dict(zip(keys, row, strict=True)) for row in rows]


def load_snapshot(config_path: Path) -> dict[str, Any]:
    """Dört kartın verisi; her kart `enabled` ve (açıksa) `items` taşır.

    Yalnız ayar ve rutin hataları burada yakalanır; kalanı (saat dilimi, bozuk zaman) çağıran
    iş parçacığında `error` anlık görüntüsüne dönüştürülür.
    """
    try:
        settings = load_settings(config_path)
    except (OSError, ValueError):
        return {"error": t("Ayar dosyası okunamadı.")}
    state = settings.state_dir
    snap: dict[str, Any] = {"error": ""}

    routines: dict[str, Any] = {"enabled": settings.routines.enabled, "items": [], "error": ""}
    if settings.routines.enabled:
        try:
            for routine in RoutineStore(state / "routines.json").load():
                days = routine.schedule.days if routine.schedule else ()
                routines["items"].append({
                    "name": routine.name,
                    "steps": len(routine.steps),
                    "schedule": _schedule_text(routine.schedule.at, days)
                    if routine.schedule else "",
                })
        except RoutinesError as error:
            routines["error"] = str(error)
    snap["routines"] = routines

    research: dict[str, Any] = {"enabled": settings.research.enabled, "items": []}
    if settings.research.enabled:
        for job in _jobs(state / "research.sqlite3"):
            report = state / REPORT_DIR / f"{job['id']}.md"
            valid = len(job["id"]) == 12 and set(job["id"]) <= _JOB_ID_CHARS
            when = job["finished"] or job["created"]
            job["when"] = _human_when(datetime.fromtimestamp(when), datetime.now())
            job["report"] = str(report) if valid and job["status"] == "done" and report.is_file() \
                else ""
            research["items"].append(job)
    snap["research"] = research

    summaries: dict[str, Any] = {"enabled": settings.daily_summary.enabled, "items": []}
    if settings.daily_summary.enabled:
        try:
            listed = sorted(
                (day for p in (state / "gunluk").glob("*.md") if (day := parse_day(p.stem))),
                reverse=True,
            )[:SHOWN]
        except OSError:
            listed = []
        for day in listed:
            path = summary_path(state, day)
            summaries["items"].append(
                {"day": day.isoformat(), "line": _first_line(path), "path": str(path)}
            )
    snap["summaries"] = summaries

    ready = settings.triggers.enabled and settings.proactive.enabled
    notices: dict[str, Any] = {
        "enabled": ready, "items": [], "proactive_off": settings.triggers.enabled and not ready,
    }
    if ready:
        found: list[tuple[datetime, dict[str, str]]] = []
        # Yalnız tetikleyici adı ve son teslim zamanı; dosya adları (kişisel) hiç okunmaz.
        try:
            store = ProactiveStore(state / "proactive.sqlite3")
            tz = ZoneInfo(settings.proactive.timezone)
            for item in settings.triggers.items:
                moment = store.last_delivered(f"trigger:{item.name}:")
                if moment is not None:
                    found.append((moment, {"name": item.name, "when": format_local(moment, tz)}))
        except ProactiveStoreError:
            pass
        found.sort(key=lambda pair: pair[0], reverse=True)
        notices["items"] = [row for _, row in found[:SHOWN]]
    snap["notices"] = notices
    return snap


def demo_snapshot() -> dict[str, Any]:
    """Gerçek veri değil: demo için uydurma örnek satırlar."""
    return {
        "error": "",
        "routines": {"enabled": True, "error": "", "items": [
            {"name": t("İşe başla"), "steps": 3, "schedule": t("08:30 · Pzt Sal Çar Per Cum")},
            {"name": t("Günü kapat"), "steps": 2, "schedule": ""},
        ]},
        "research": {"enabled": True, "items": [
            {"id": "a" * 12, "topic": t("Yerel dil modelleri karşılaştırması"), "status": "running",
             "searches": 4, "max_searches": 12, "when": t("15 Oca 10:42"), "report": ""},
            {"id": "b" * 12, "topic": t("Windows bildirim API'leri"), "status": "done",
             "searches": 9, "max_searches": 12, "when": t("14 Oca 18:05"), "report": "demo"},
        ]},
        "summaries": {"enabled": True, "items": [
            {"day": "2026-01-14", "line": t("14 Ocak: 23 araç çağrısı, 2 onay"), "path": ""},
            {"day": "2026-01-13", "line": t("13 Ocak: 11 araç çağrısı, 1 onay"), "path": ""},
        ]},
        "notices": {"enabled": True, "proactive_off": False, "items": [
            {"name": t("İndirilenler"), "when": "2026-01-14 17:20"},
        ]},
    }


_HELP_RESEARCH = (
    "Durdur: çalışan araştırmayı iptal eder. Raporu aç: raporu uygulama içinde gösterir; rapor "
    "web verisidir, içindeki bağlantılar açılmaz. Araştırma yalnız web aramasıyla çalışır."
)
_HELP_SUMMARIES = (
    "Aç: o günün özetini uygulama içinde gösterir. Özet, denetim kaydındaki sayılardan yerelde "
    "üretilir; konuşma metni içermez ve Telegram'a gitmez."
)


class _Card:
    """Kart gövdesi: tek anda ya satırlar, ya boş durum, ya da kapalı satırı görünür."""

    def __init__(
        self, title: str, empty: tuple[str, str], settings_section: str, help: str = "",  # noqa: A002
    ) -> None:
        self.frame, lay = make_card()
        heading = make_label(title, "sectionTitle")
        lay.addLayout(with_help(heading, help)) if help else lay.addWidget(heading)
        self.rows_box = QWidget()
        self.rows = QVBoxLayout(self.rows_box)
        self.rows.setContentsMargins(0, 0, 0, 0)
        self.rows.setSpacing(8)
        self.empty = EmptyState(*empty)
        self.disabled = QWidget()
        off = QHBoxLayout(self.disabled)
        off.setContentsMargins(0, 0, 0, 0)
        off.setSpacing(8)
        self.disabled_label = make_label("", "meta")
        off.addWidget(self.disabled_label, 1)
        self.link = QPushButton(t("Ayarlarda aç"))
        self.link.setAccessibleName(t("{title} ayarlarını aç").format(title=title))
        self.link.setProperty("section", settings_section)  # panelin tek yuvası okur
        off.addWidget(self.link)
        self.stack = CurrentPageStack()
        for page in (self.rows_box, self.empty, self.disabled):
            self.stack.addWidget(page)
        lay.addWidget(self.stack)

    def _clear(self) -> None:
        while self.rows.count():
            child = self.rows.takeAt(0)
            widget = child.widget() if child is not None else None
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

    def show_rows(self, rows: Sequence[QWidget]) -> None:
        self._clear()
        for row in rows:
            self.rows.addWidget(row)
        self.stack.setCurrentIndex(0)

    def show_empty(self, title: str, body: str) -> None:
        self._clear()
        self.empty.set_text(title, body)
        self.stack.setCurrentIndex(1)

    def show_disabled(self, text: str, *, link: bool = True) -> None:
        self._clear()
        self.disabled_label.setText(text)
        self.link.setVisible(link)
        self.stack.setCurrentIndex(2)


def _row(
    title: str, meta: str, *, dot_color: str | None = None,
    buttons: tuple[QPushButton, ...] = (),
) -> QFrame:
    """Satır: başlık, ikincil satır (varsa renkli nokta + düz metin) ve eylem düğmeleri."""
    row = QFrame()
    row.setObjectName("card")
    lay = QHBoxLayout(row)
    lay.setContentsMargins(12, 8, 12, 8)
    lay.setSpacing(8)
    text = QVBoxLayout()
    text.setSpacing(2)
    head = make_label(title, "fieldLabel")
    text.addWidget(head)
    if dot_color is None:
        text.addWidget(make_label(meta, "meta"))
    else:
        # Durum kelimesi metinde; nokta yalnız ek ipucu.
        line = QHBoxLayout()
        line.setSpacing(6)
        dot = QLabel("●")
        dot.setStyleSheet(f"color: {dot_color};")
        line.addWidget(dot)
        line.addWidget(make_label(meta, "meta"), 1)
        text.addLayout(line)
    lay.addLayout(text, 1)
    for button in buttons:
        lay.addWidget(button)
    return row


class _Relay(QObject):
    """İş parçacığı bunu tutar, paneli değil: panel ölünce bağlantı kendiliğinden düşer."""

    loaded = Signal(int, object)


class AutomationPanel(QWidget):
    tool_requested = Signal(str, object)
    settings_requested = Signal(str)  # Ayarlar bölüm adı

    def __init__(self, *, demo: bool = False, config_path: Path | None = None) -> None:
        super().__init__()
        self._demo = demo
        self._relay = _Relay()  # (nesil, anlık görüntü): iş parçacığından GUI'ye
        self._config_path = config_path
        self._busy = False
        self._generation = 0
        self._palette = themes.THEMES[themes.DEFAULT_THEME]
        self._snap: dict[str, Any] | None = None  # son görüntü; tema değişince yeniden çizilir
        self.run_buttons: dict[str, QPushButton] = {}
        self.cancel_buttons: dict[str, QPushButton] = {}
        self.report_buttons: dict[str, QPushButton] = {}
        self.summary_buttons: dict[str, QPushButton] = {}
        outer = make_scroll_page(self)
        layout = outer  # genişlik sınırı ve ortalama ortak kabuktan (make_scroll_page) gelir
        layout.setSpacing(16)
        self.refresh_button = QPushButton(t("Yenile"))
        self.refresh_button.setAccessibleName(t("Otomasyon sayfasını yenile"))
        self.refresh_button.clicked.connect(self.refresh)
        layout.addWidget(make_page_header(
            t("Otomasyon"),
            t("Rutinlerini, araştırmalarını ve gün özetlerini buradan takip et."),
            (self.refresh_button,),
        ))
        self.routines = _Card(
            t("Rutinler"),
            (t("Henüz rutin yok"), t("Sohbette “işe başla adında bir rutin kaydet” de.")),
            "Rutinler",
        )
        self.research = _Card(
            t("Araştırmalar"),
            (t("Henüz araştırma yok"), t("Sohbette bir konu söyle ve araştırmasını iste.")),
            "Araştırma",
            t(_HELP_RESEARCH),
        )
        self.summaries = _Card(
            t("Gün özetleri"),
            (t("Henüz özet yok"), t("Gün sonu özeti her akşam bir kez yazılır.")),
            "Gün özeti",
            t(_HELP_SUMMARIES),
        )
        self.notices = _Card(
            t("Son bildirimler"),
            (t("Henüz bildirim yok"), t("Bir tetikleyici çalışınca burada görünür.")),
            "Tetikleyiciler",
        )
        self.notice = make_label("", "meta")
        # Aynı konu iki yerde bölünmesin: izleme burada, kurulum Ayarlar'da (UX-06). Küçük
        # bağlantılar ilgili Ayarlar bölümüne geçirir; kartlardaki "Ayarlarda aç" yalnız
        # kapalıyken çıkar.
        links = QHBoxLayout()
        links.setSpacing(4)
        links.addWidget(make_label(t("Ayarlar:"), "meta"))
        self.settings_links: dict[str, QPushButton] = {}
        for section in ("Rutinler", "Tetikleyiciler", "Gün özeti", "Araştırma"):
            label = t(section)
            link = QPushButton(label)
            link.setObjectName("ghost")
            link.setAccessibleName(t("{title} ayarlarını aç").format(title=label))
            link.setProperty("section", section)
            link.clicked.connect(self._open_settings)
            self.settings_links[section] = link
            links.addWidget(link)
        links.addStretch(1)
        layout.addLayout(links)
        for card in (self.routines, self.research, self.summaries, self.notices):
            layout.addWidget(card.frame)
        layout.addWidget(self.notice)
        layout.addStretch(1)
        self._relay.loaded.connect(self._apply)
        for card in (self.routines, self.research, self.summaries, self.notices):
            card.link.clicked.connect(self._open_settings)
        self._set_loading()  # ilk yükleme sayfa ilk görününce (showEvent) başlar

    # --- yükleme -------------------------------------------------------------------

    def _set_loading(self) -> None:
        for card in (self.routines, self.research, self.summaries, self.notices):
            card.show_disabled(t("Yükleniyor…"), link=False)

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self.refresh()

    def refresh(self) -> None:
        self._generation += 1
        generation = self._generation
        if self._demo or self._config_path is None:
            self._apply(generation, demo_snapshot() if self._demo else {"error": t("Ayar yok.")})
            return

        def work(path: Path, relay: _Relay) -> None:
            try:
                snap = load_snapshot(path)
            except (
                ZoneInfoNotFoundError, KeyError, OverflowError, OSError, TypeError, ValueError,
            ):
                # Bozuk saat dilimi, beklenmedik veri tipi (ör. SQLite None/metin zamanı) ya da
                # zaman değeri: kartlar "Yükleniyor…" durumunda kalmasın.
                snap = {"error": t("Otomasyon verisi okunamadı.")}
            with suppress(RuntimeError):  # pencere okuma bitmeden kapandı
                relay.loaded.emit(generation, snap)

        threading.Thread(target=work, args=(self._config_path, self._relay), daemon=True).start()

    def set_theme(self, palette: dict[str, str]) -> None:
        """Nokta renkleri tema jetonlarından gelir; açık görüntü yeni renkle yeniden çizilir."""
        self._palette = palette
        if self._snap is not None:
            self._apply(self._generation, self._snap)

    def _apply(self, generation: int, snap: dict[str, Any]) -> None:
        if generation != self._generation:  # daha yeni bir yenileme başladı
            return
        self._snap = snap
        self.run_buttons.clear()
        self.cancel_buttons.clear()
        self.report_buttons.clear()
        self.summary_buttons.clear()
        if snap.get("error"):
            for card in (self.routines, self.research, self.summaries, self.notices):
                card.show_disabled(snap["error"], link=False)
            return
        self._render_routines(snap["routines"])
        self._render_research(snap["research"])
        self._render_summaries(snap["summaries"])
        self._render_notices(snap["notices"])
        self.set_busy(self._busy)

    # --- kartlar -------------------------------------------------------------------

    def _button(
        self, text: str, name: str, action: str, value: str, *, primary: bool = False,
    ) -> QPushButton:
        button = QPushButton(text)
        button.setAccessibleName(name)
        button.setToolTip(name)
        if primary:
            button.setObjectName("primary")
        button.setProperty("action", action)  # tek bağlı yuva sender()'dan okur: lambda döngüsü yok
        button.setProperty("value", value)
        button.clicked.connect(self._row_clicked)
        return button

    def _row_clicked(self) -> None:
        button = self.sender()
        if button is None:
            return
        action, value = str(button.property("action")), str(button.property("value"))
        {"run": self._run, "cancel": self._cancel, "open": self._open}[action](value)

    def _open_settings(self) -> None:
        if (button := self.sender()) is not None:
            self.settings_requested.emit(str(button.property("section")))

    def _render_routines(self, data: dict[str, Any]) -> None:
        if not data["enabled"]:
            self.routines.show_disabled(t("Rutinler kapalı."))
            return
        if data["error"]:
            self.routines.show_disabled(data["error"])
            return
        if not data["items"]:
            self.routines.show_empty(t("Henüz rutin yok"), t("Sohbette “işe başla adında bir rutin "
                                     "kaydet” de."))
            return
        rows = []
        for item in data["items"]:
            name = item["name"]
            run = self._button(
                t("Çalıştır"), t("Rutini çalıştır: {name}").format(name=name),
                "run", name, primary=True,
            )
            self.run_buttons[name] = run
            steps = t("{steps} adım").format(steps=item["steps"])
            meta = steps + (f" · {item['schedule']}" if item["schedule"] else "")
            rows.append(_row(name, meta, buttons=(run,)))
        self.routines.show_rows(rows)

    def _render_research(self, data: dict[str, Any]) -> None:
        if not data["enabled"]:
            self.research.show_disabled(t("Araştırma kapalı."))
            return
        if not data["items"]:
            self.research.show_empty(t("Henüz araştırma yok"), t("Sohbette bir konu söyle ve "
                                     "araştırmasını iste."))
            return
        rows = []
        for job in data["items"]:
            buttons: list[QPushButton] = []
            if job["status"] == "running":
                stop = self._button(
                    t("Durdur"), t("Araştırmayı durdur: {topic}").format(topic=job["topic"]),
                    "cancel", job["id"],
                )
                self.cancel_buttons[job["id"]] = stop
                buttons.append(stop)
            if job["report"]:
                path = job["report"]
                opener = self._button(
                    t("Raporu aç"), t("Raporu aç: {topic}").format(topic=job["topic"]),
                    "open", path,
                )
                self.report_buttons[job["id"]] = opener
                buttons.append(opener)
            label, token = _JOB_STATES.get(job["status"], ("Bilinmiyor", "muted"))
            meta = t("{label} · {searches}/{max_searches} arama · {when}").format(
                label=t(label),
                searches=job["searches"],
                max_searches=job["max_searches"],
                when=job["when"],
            )
            rows.append(_row(
                job["topic"], meta, dot_color=self._palette[token], buttons=tuple(buttons),
            ))
        self.research.show_rows(rows)

    def _render_summaries(self, data: dict[str, Any]) -> None:
        if not data["enabled"]:
            self.summaries.show_disabled(t("Gün özeti kapalı."))
            return
        if not data["items"]:
            self.summaries.show_empty(
                t("Henüz özet yok"), t("Gün sonu özeti her akşam bir kez yazılır."),
            )
            return
        rows = []
        for item in data["items"]:
            opener = self._button(
                t("Aç"), t("Özeti aç: {day}").format(day=item["day"]),
                "open", item["path"],
            )
            self.summary_buttons[item["day"]] = opener
            day = date.fromisoformat(item["day"])  # parse_day doğruladı; satır yalnız tarih taşır
            rows.append(_row(
                _human_day(day), _summary_subtitle(item["line"], day), buttons=(opener,),
            ))
        self.summaries.show_rows(rows)

    def _render_notices(self, data: dict[str, Any]) -> None:
        if not data["enabled"]:
            text = (t("Bildirimler için proaktif olaylar da açık olmalı.")
                    if data["proactive_off"] else t("Tetikleyiciler kapalı."))
            self.notices.show_disabled(text)
            return
        if not data["items"]:
            self.notices.show_empty(t("Henüz bildirim yok"), t("Bir tetikleyici çalışınca burada "
                                    "görünür."))
            return
        self.notices.show_rows([_row(i["name"], i["when"]) for i in data["items"]])

    # --- eylemler ------------------------------------------------------------------

    def _run(self, name: str) -> None:
        self.tool_requested.emit("routine_run", RoutineNameArguments(name=name))

    def _cancel(self, job_id: str) -> None:
        self.tool_requested.emit("research_cancel", JobArguments(job_id=job_id))

    def _open(self, path: str) -> None:
        if path and not self._demo:
            file = Path(path)
            open_text_file(file, file.name, self)  # rapor web verisi: uygulama içinde, bağlantısız

    def set_busy(self, busy: bool) -> None:
        """Köprü meşgulken ya da demoda eylem düğmeleri kapanır; salt okuma sürer."""
        self._busy = busy
        tip = t("Demo modunda kapalı.") if self._demo else ""
        for button in (*self.run_buttons.values(), *self.cancel_buttons.values()):
            button.setEnabled(not busy and not self._demo)
        for button in (*self.report_buttons.values(), *self.summary_buttons.values()):
            button.setEnabled(not self._demo)
        if tip:
            for button in (*self.run_buttons.values(), *self.cancel_buttons.values(),
                           *self.report_buttons.values(), *self.summary_buttons.values()):
                button.setToolTip(tip)

    def accept_result(self, tool_name: str, result: ToolResult) -> None:
        self.notice.setText(result.message)
        if tool_name == "research_cancel":
            self.refresh()
