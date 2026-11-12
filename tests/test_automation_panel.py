"""Otomasyon sayfası: kart durumları, kapılı eylem yolu ve gezinme sırası (offscreen)."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfoNotFoundError

import pytest
import yaml

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QLabel

from artemis.channels.desktop import automation_panel as module
from artemis.channels.desktop import themes
from artemis.channels.desktop.app import build_window
from artemis.channels.desktop.automation_panel import AutomationPanel, load_snapshot
from artemis.tools.base import ToolResult
from artemis.tools.research_jobs import SCHEMA


@pytest.fixture
def app() -> QApplication:
    current = QApplication.instance()
    return current if isinstance(current, QApplication) else QApplication([])


def _config(tmp_path: Path, **extra: Any) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({
        "workspace": "desktop", "state_dir": "state", "audit_path": "audit.jsonl",
        "primary_model": "first",
        "models": {"first": {"provider": "openrouter", "model": "first-model",
                             "input_per_million": "0", "output_per_million": "0"}},
        **extra,
    }), encoding="utf-8")
    return path


def _load(app: QApplication, panel: AutomationPanel) -> None:
    panel.refresh()
    end = time.monotonic() + 3
    while time.monotonic() < end:
        app.processEvents()
        if panel.routines.stack.currentIndex() != 2 or panel.routines.disabled_label.text() != (
            "Yükleniyor…"
        ):
            return
        time.sleep(0.005)
    raise AssertionError("sayfa yüklenmedi")


def _routine(state: Path, name: str, **extra: Any) -> None:
    state.mkdir(parents=True, exist_ok=True)
    (state / "routines.json").write_text(json.dumps({"routines": [
        {"name": name, "steps": [{"tool": "get_time", "arguments": {}}], **extra}
    ]}), encoding="utf-8")


def _job(state: Path, job_id: str, status: str) -> None:
    state.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(state / "research.sqlite3")
    with db:
        db.execute(SCHEMA)
        db.execute(
            "INSERT INTO research_jobs (id, topic, status, max_searches, searches, created) "
            "VALUES (?,?,?,?,?,?)", (job_id, "Konu başlığı", status, 12, 3, time.time()),
        )
    db.close()


def test_everything_disabled_shows_one_line_and_settings_link(
    app: QApplication, tmp_path: Path
) -> None:
    panel = AutomationPanel(config_path=_config(tmp_path))
    asked: list[str] = []
    panel.settings_requested.connect(asked.append)
    _load(app, panel)
    for card in (panel.routines, panel.research, panel.summaries, panel.notices):
        assert card.stack.currentIndex() == 2
        assert "kapalı" in card.disabled_label.text()
    panel.routines.link.click()
    panel.research.link.click()
    panel.summaries.link.click()
    panel.notices.link.click()
    assert asked == ["Rutinler", "Araştırma", "Gün özeti", "Tetikleyiciler"]


def test_enabled_but_empty_shows_empty_states(app: QApplication, tmp_path: Path) -> None:
    panel = AutomationPanel(config_path=_config(tmp_path, **{
        "routines": {"enabled": True}, "research": {"enabled": True},
        "daily_summary": {"enabled": True},
    }))
    _load(app, panel)
    assert panel.routines.stack.currentIndex() == 1
    assert panel.routines.empty.title_label.text() == "Henüz rutin yok"
    assert panel.research.stack.currentIndex() == 1
    assert panel.summaries.stack.currentIndex() == 1


def test_run_button_goes_through_the_gated_tool_path(app: QApplication, tmp_path: Path) -> None:
    config = _config(tmp_path, routines={"enabled": True})
    _routine(tmp_path / "state", "İşe başla", schedule={"at": "08:30", "days": [0, 1]})
    panel = AutomationPanel(config_path=config)
    calls: list[tuple[str, Any]] = []
    panel.tool_requested.connect(lambda name, args: calls.append((name, args)))
    _load(app, panel)
    assert panel.routines.stack.currentIndex() == 0
    panel.run_buttons["İşe başla"].click()
    assert [(n, a.name) for n, a in calls] == [("routine_run", "İşe başla")]
    panel.set_busy(True)
    assert not panel.run_buttons["İşe başla"].isEnabled()


def test_research_cancel_uses_gated_tool_and_report_opens(
    app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, research={"enabled": True})
    state = tmp_path / "state"
    _job(state, "a" * 12, "running")
    _job(state, "b" * 12, "done")
    (state / "arastirma").mkdir()
    (state / "arastirma" / f"{'b' * 12}.md").write_text("# Rapor", encoding="utf-8")
    opened: list[str] = []
    monkeypatch.setattr(module.QDesktopServices, "openUrl", lambda url: opened.append(url.path()))
    panel = AutomationPanel(config_path=config)
    calls: list[tuple[str, Any]] = []
    panel.tool_requested.connect(lambda name, args: calls.append((name, args)))
    _load(app, panel)
    assert set(panel.cancel_buttons) == {"a" * 12}
    assert set(panel.report_buttons) == {"b" * 12}
    panel.cancel_buttons["a" * 12].click()
    assert [(n, a.job_id) for n, a in calls] == [("research_cancel", "a" * 12)]
    panel.report_buttons["b" * 12].click()
    assert opened and opened[0].endswith(f"{'b' * 12}.md")
    # Salt okunur: kayıtlar değişmedi (kurucu 'uncertain' yazmaz).
    snap = load_snapshot(config)
    assert {j["status"] for j in snap["research"]["items"]} == {"running", "done"}


def test_summaries_list_last_seven_with_first_line(tmp_path: Path) -> None:
    config = _config(tmp_path, daily_summary={"enabled": True})
    folder = tmp_path / "state" / "gunluk"
    folder.mkdir(parents=True)
    for day in range(1, 10):
        (folder / f"2026-01-{day:02d}.md").write_text(f"# Gün {day}\n\nayrıntı", encoding="utf-8")
    (folder / "not-a-day.md").write_text("x", encoding="utf-8")
    items = load_snapshot(config)["summaries"]["items"]
    assert [i["day"] for i in items] == [f"2026-01-{d:02d}" for d in range(9, 2, -1)]
    assert items[0]["line"] == "Gün 9"


def test_demo_shows_sample_content_without_enabled_actions(app: QApplication) -> None:
    panel = AutomationPanel(demo=True)
    _load(app, panel)
    assert panel.routines.stack.currentIndex() == 0
    assert panel.run_buttons and not any(b.isEnabled() for b in panel.run_buttons.values())
    assert panel.cancel_buttons
    assert panel.report_buttons and panel.summary_buttons


def test_cancel_result_is_shown_and_page_refreshed(app: QApplication) -> None:
    panel = AutomationPanel(demo=True)
    panel.accept_result("research_cancel", ToolResult(message="İptal istendi."))
    assert panel.notice.text() == "İptal istendi."


def test_window_nav_order_and_settings_link(app: QApplication, tmp_path: Path) -> None:
    window = build_window(tmp_path / "ignored.yaml", demo=True, app=app)
    try:
        labels = [button.text() for button in window.nav_buttons.values()]
        assert labels == ["Sohbet", "Projeler", "Otomasyon", "Petler", "Geçmiş", "Ayarlar"]
        window.automation_panel.settings_requested.emit("Araştırma")
        assert window.tabs.currentWidget() is window.settings_scroll
        names = window.settings_panel.section_names()
        assert window.settings_panel.sections.currentRow() == names.index("Araştırma")
    finally:
        window._request_exit()
        end = time.monotonic() + 3
        while time.monotonic() < end and not window.bridge.stopped:
            app.processEvents()
            time.sleep(0.005)


def test_research_status_is_plain_text_with_theme_dot(app: QApplication, tmp_path: Path) -> None:
    config = _config(tmp_path, research={"enabled": True})
    _job(tmp_path / "state", "a" * 12, "running")
    panel = AutomationPanel(config_path=config)
    _load(app, panel)
    # Durum rozeti (kutu/arka plan) yok; durum düz metin ve nokta olarak satırda.
    assert panel.findChildren(QLabel, "statusChip") == []
    texts = [label.text() for label in panel.findChildren(QLabel)]
    assert any(t.startswith("Çalışıyor · 3/12 arama · Bugün ") for t in texts)
    dots = [label for label in panel.findChildren(QLabel) if label.text() == "●"]
    assert len(dots) == 1
    assert panel._palette["accent"] in dots[0].styleSheet()


def test_dot_colour_follows_theme(app: QApplication, tmp_path: Path) -> None:
    config = _config(tmp_path, research={"enabled": True})
    _job(tmp_path / "state", "a" * 12, "running")
    panel = AutomationPanel(config_path=config)
    _load(app, panel)
    panel.set_theme(themes.THEMES["dark"])
    dots = [label for label in panel.findChildren(QLabel) if label.text() == "●"]
    assert dots and themes.THEMES["dark"]["accent"] in dots[0].styleSheet()


def test_human_date_helpers() -> None:
    now = datetime(2026, 1, 15, 20, 0)
    assert module._human_when(datetime(2026, 1, 15, 10, 42), now) == "Bugün 10:42"
    assert module._human_when(datetime(2026, 1, 14, 18, 5), now) == "14 Oca 18:05"
    assert module._human_day(date(2026, 1, 14)) == "14 Ocak Çarşamba"
    day = date(2026, 1, 14)
    assert module._summary_subtitle("14 Ocak: 23 araç çağrısı, 2 onay", day) == (
        "23 araç çağrısı, 2 onay"
    )
    assert module._summary_subtitle("2026-01-14 Gün özeti", day) == "Gün özeti"
    assert module._summary_subtitle("14 Ocak Çarşamba", day) == "14 Ocak Çarşamba"
    # Başka güne ait tarih silinmez; yalnız yinelenen tarih atılır.
    assert module._summary_subtitle("13 Ocak: 11 çağrı", day) == "13 Ocak: 11 çağrı"


def test_summary_rows_show_human_day_title(app: QApplication) -> None:
    panel = AutomationPanel(demo=True)
    _load(app, panel)
    texts = [label.text() for label in panel.findChildren(QLabel)]
    assert "14 Ocak Çarşamba" in texts
    assert "23 araç çağrısı, 2 onay" in texts


@pytest.mark.parametrize("error", [
    ZoneInfoNotFoundError("Europe/Nowhere"), KeyError("x"), OverflowError("big"),
    OSError("disk"), ValueError("bad"),
])
def test_snapshot_failure_shows_error_not_loading(
    app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception,
) -> None:
    def broken(_path: Path) -> dict[str, Any]:
        raise error

    monkeypatch.setattr(module, "load_snapshot", broken)
    panel = AutomationPanel(config_path=_config(tmp_path))
    _load(app, panel)
    for card in (panel.routines, panel.research, panel.summaries, panel.notices):
        assert card.disabled_label.text() == "Otomasyon verisi okunamadı."
