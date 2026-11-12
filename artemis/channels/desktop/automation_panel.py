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

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QShowEvent
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from artemis.channels.desktop import themes
from artemis.channels.desktop.projects_panel import (
    CurrentPageStack,
    EmptyState,
    make_card,
    make_label,
    make_page_header,
    make_scroll_page,
)
from artemis.config.settings import load_settings
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
    when = "her gün" if len(days) == 7 else " ".join(_DAYS[day] for day in days)
    return f"{at} · {when}"


def _human_day(day: date) -> str:
    """'14 Ocak Çarşamba'; ay ve gün adları sabit tabloda, yerel ayara bağlı değil."""
    return f"{day.day} {_MONTHS[day.month - 1]} {_WEEKDAYS[day.weekday()]}"


def _human_when(moment: datetime, now: datetime) -> str:
    """'Bugün 10:42' ya da '15 Oca 10:42'."""
    clock = moment.strftime("%H:%M")
    if moment.date() == now.date():
        return f"Bugün {clock}"
    return f"{moment.day} {_MONTHS[moment.month - 1][:3]} {clock}"


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
        return "Dosya okunamadı"
    for line in head.splitlines():
        text = line.strip().lstrip("#").strip()
        if text:
            return text[:120]
    return "Boş özet"


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
        return {"error": "Ayar dosyası okunamadı."}
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
            {"name": "İşe başla", "steps": 3, "schedule": "08:30 · Pzt Sal Çar Per Cum"},
            {"name": "Günü kapat", "steps": 2, "schedule": ""},
        ]},
        "research": {"enabled": True, "items": [
            {"id": "a" * 12, "topic": "Yerel dil modelleri karşılaştırması", "status": "running",
             "searches": 4, "max_searches": 12, "when": "15 Oca 10:42", "report": ""},
            {"id": "b" * 12, "topic": "Windows bildirim API'leri", "status": "done",
             "searches": 9, "max_searches": 12, "when": "14 Oca 18:05", "report": "demo"},
        ]},
        "summaries": {"enabled": True, "items": [
            {"day": "2026-01-14", "line": "14 Ocak: 23 araç çağrısı, 2 onay", "path": ""},
            {"day": "2026-01-13", "line": "13 Ocak: 11 araç çağrısı, 1 onay", "path": ""},
        ]},
        "notices": {"enabled": True, "proactive_off": False, "items": [
            {"name": "İndirilenler", "when": "2026-01-14 17:20"},
        ]},
    }


class _Card:
    """Kart gövdesi: tek anda ya satırlar, ya boş durum, ya da kapalı satırı görünür."""

    def __init__(
        self, title: str, empty: tuple[str, str], settings_section: str,
    ) -> None:
        self.frame, lay = make_card()
        lay.addWidget(make_label(title, "sectionTitle"))
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
        self.link = QPushButton("Ayarlarda aç")
        self.link.setAccessibleName(f"{title} ayarlarını aç")
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
    MAX_CONTENT_WIDTH = 960

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
        body = QWidget()
        body.setMaximumWidth(self.MAX_CONTENT_WIDTH)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)
        holder = QHBoxLayout()
        holder.setContentsMargins(0, 0, 0, 0)
        holder.addWidget(body, 1)
        outer.addLayout(holder)
        outer.addStretch(1)
        self.refresh_button = QPushButton("Yenile")
        self.refresh_button.setAccessibleName("Otomasyon sayfasını yenile")
        self.refresh_button.clicked.connect(self.refresh)
        layout.addWidget(make_page_header(
            "Otomasyon",
            "Rutinlerini, araştırmalarını ve gün özetlerini buradan takip et.",
            (self.refresh_button,),
        ))
        self.routines = _Card(
            "Rutinler",
            ("Henüz rutin yok", "Sohbette “işe başla adında bir rutin kaydet” de."),
            "Rutinler",
        )
        self.research = _Card(
            "Araştırmalar",
            ("Henüz araştırma yok", "Sohbette bir konu söyle ve araştırmasını iste."),
            "Araştırma",
        )
        self.summaries = _Card(
            "Gün özetleri",
            ("Henüz özet yok", "Gün sonu özeti her akşam bir kez yazılır."),
            "Gün özeti",
        )
        self.notices = _Card(
            "Son bildirimler",
            ("Henüz bildirim yok", "Bir tetikleyici çalışınca burada görünür."),
            "Tetikleyiciler",
        )
        self.notice = make_label("", "meta")
        for card in (self.routines, self.research, self.summaries, self.notices):
            layout.addWidget(card.frame)
        layout.addWidget(self.notice)
        self._relay.loaded.connect(self._apply)
        for card in (self.routines, self.research, self.summaries, self.notices):
            card.link.clicked.connect(self._open_settings)
        self._set_loading()  # ilk yükleme sayfa ilk görününce (showEvent) başlar

    # --- yükleme -------------------------------------------------------------------

    def _set_loading(self) -> None:
        for card in (self.routines, self.research, self.summaries, self.notices):
            card.show_disabled("Yükleniyor…", link=False)

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 (Qt adı)
        super().showEvent(event)
        self.refresh()

    def refresh(self) -> None:
        self._generation += 1
        generation = self._generation
        if self._demo or self._config_path is None:
            self._apply(generation, demo_snapshot() if self._demo else {"error": "Ayar yok."})
            return

        def work(path: Path, relay: _Relay) -> None:
            try:
                snap = load_snapshot(path)
            except (ZoneInfoNotFoundError, KeyError, OverflowError, OSError, ValueError):
                # Bozuk saat dilimi ya da zaman değeri: kartlar yüklenmiş kalmasın.
                snap = {"error": "Otomasyon verisi okunamadı."}
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
            self.routines.show_disabled("Rutinler kapalı.")
            return
        if data["error"]:
            self.routines.show_disabled(data["error"])
            return
        if not data["items"]:
            self.routines.show_empty("Henüz rutin yok", "Sohbette “işe başla adında bir rutin "
                                     "kaydet” de.")
            return
        rows = []
        for item in data["items"]:
            name = item["name"]
            run = self._button(
                "Çalıştır", f"Rutini çalıştır: {name}",
                "run", name, primary=True,
            )
            self.run_buttons[name] = run
            meta = f"{item['steps']} adım" + (f" · {item['schedule']}" if item["schedule"] else "")
            rows.append(_row(name, meta, buttons=(run,)))
        self.routines.show_rows(rows)

    def _render_research(self, data: dict[str, Any]) -> None:
        if not data["enabled"]:
            self.research.show_disabled("Araştırma kapalı.")
            return
        if not data["items"]:
            self.research.show_empty("Henüz araştırma yok", "Sohbette bir konu söyle ve "
                                     "araştırmasını iste.")
            return
        rows = []
        for job in data["items"]:
            buttons: list[QPushButton] = []
            if job["status"] == "running":
                stop = self._button(
                    "Durdur", f"Araştırmayı durdur: {job['topic']}",
                    "cancel", job["id"],
                )
                self.cancel_buttons[job["id"]] = stop
                buttons.append(stop)
            if job["report"]:
                path = job["report"]
                opener = self._button(
                    "Raporu aç", f"Raporu aç: {job['topic']}",
                    "open", path,
                )
                self.report_buttons[job["id"]] = opener
                buttons.append(opener)
            label, token = _JOB_STATES.get(job["status"], ("Bilinmiyor", "muted"))
            meta = f"{label} · {job['searches']}/{job['max_searches']} arama · {job['when']}"
            rows.append(_row(
                job["topic"], meta, dot_color=self._palette[token], buttons=tuple(buttons),
            ))
        self.research.show_rows(rows)

    def _render_summaries(self, data: dict[str, Any]) -> None:
        if not data["enabled"]:
            self.summaries.show_disabled("Gün özeti kapalı.")
            return
        if not data["items"]:
            self.summaries.show_empty("Henüz özet yok", "Gün sonu özeti her akşam bir kez yazılır.")
            return
        rows = []
        for item in data["items"]:
            opener = self._button(
                "Aç", f"Özeti aç: {item['day']}",
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
            text = ("Bildirimler için proaktif olaylar da açık olmalı."
                    if data["proactive_off"] else "Tetikleyiciler kapalı.")
            self.notices.show_disabled(text)
            return
        if not data["items"]:
            self.notices.show_empty("Henüz bildirim yok", "Bir tetikleyici çalışınca burada "
                                    "görünür.")
            return
        self.notices.show_rows([_row(i["name"], i["when"]) for i in data["items"]])

    # --- eylemler ------------------------------------------------------------------

    def _run(self, name: str) -> None:
        self.tool_requested.emit("routine_run", RoutineNameArguments(name=name))

    def _cancel(self, job_id: str) -> None:
        self.tool_requested.emit("research_cancel", JobArguments(job_id=job_id))

    def _open(self, path: str) -> None:
        if path and not self._demo:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def set_busy(self, busy: bool) -> None:
        """Köprü meşgulken ya da demoda eylem düğmeleri kapanır; salt okuma sürer."""
        self._busy = busy
        tip = "Demo modunda kapalı." if self._demo else ""
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
