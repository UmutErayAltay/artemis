"""Projeler ve Geçmiş sayfaları (sıcak yeniden tasarım dalga 2): akış, nedenler, boş durumlar."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication, QLabel, QScrollArea

from artemis.channels.desktop import data_panel, projects_panel, themes
from artemis.channels.desktop.data_panel import DataPanel
from artemis.channels.desktop.projects_panel import (
    LIST_MAX_ROWS,
    EmptyState,
    ProjectsPanel,
    make_page_header,
)
from artemis.tools.base import ToolResult
from artemis.tools.plugins.history import HistoryArguments


@pytest.fixture
def app() -> QApplication:
    current = QApplication.instance()
    instance = current if isinstance(current, QApplication) else QApplication([])
    instance.setQuitOnLastWindowClosed(False)
    return instance


def _ok(data: dict[str, Any]) -> ToolResult:
    return ToolResult(success=True, status="ok", message="ok", data=data)


def _spec(task: str = "Görev", n: int = 0) -> dict[str, Any]:
    return {
        "spec_id": f"s{n}",
        "project": "p",
        "task": task,
        "scope": ["a"],
        "acceptance": ["b"],
    }


def _status(panel: ProjectsPanel, drafts: list[Any], jobs: list[Any]) -> None:
    panel.accept_result(
        "code_agent_status", _ok({"projects": ["p"], "drafts": drafts, "jobs": jobs})
    )


def _states(panel: ProjectsPanel) -> list[str]:
    return [str(label.property("state")) for label in panel._flow_labels]


# --- Projeler ----------------------------------------------------------------------------


def test_flow_header_follows_selection(app: QApplication) -> None:
    panel = ProjectsPanel()
    panel.set_busy(False)
    assert _states(panel) == ["current", "todo", "todo"]
    _status(panel, [_spec()], [])
    assert _states(panel) == ["done", "current", "todo"]
    job = {"job_id": "j", "project": "p", "status": "running", "spec": _spec()}
    _status(panel, [], [job])
    assert _states(panel) == ["done", "done", "current"]
    _status(panel, [], [{**job, "status": "done"}])
    assert _states(panel) == ["done", "done", "done"]
    _status(panel, [], [])
    assert _states(panel) == ["current", "todo", "todo"]


def test_disabled_reasons_are_shown_and_cleared(app: QApplication) -> None:
    panel = ProjectsPanel()
    assert "meşgul" in panel.prepare_reason.text()
    panel.set_busy(False)
    _status(panel, [], [])
    assert not panel.prepare_button.isEnabled()
    assert panel.prepare_reason.text() == "Önce görev yaz."
    panel.task.setPlainText("x")
    assert "Kapsam" in panel.prepare_reason.text()
    panel.scope.setPlainText("y")
    assert "Kabul" in panel.prepare_reason.text()
    panel.acceptance.setPlainText("z")
    assert panel.prepare_button.isEnabled() and panel.prepare_reason.isHidden()
    assert not panel.start_button.isEnabled() and "taslak" in panel.start_reason.text()
    _status(panel, [_spec()], [])
    assert panel.start_button.isEnabled() and panel.start_reason.isHidden()


def test_demo_reasons(app: QApplication) -> None:
    panel = ProjectsPanel(demo=True)
    panel.set_busy(False)
    assert "Demo" in panel.prepare_reason.text() and "Demo" in panel.start_reason.text()


def test_preview_escapes_user_text(app: QApplication) -> None:
    panel = ProjectsPanel()
    panel.set_busy(False)
    _status(panel, [_spec("<b>x</b> & <img src='f'>")], [])
    text = panel.preview.toPlainText()
    assert "<b>x</b> & <img src='f'>" in text
    assert "Kabul ölçütleri" in text and "Kapsam" in text
    assert "<img" not in panel.preview.toHtml().replace("&lt;img", "")


def test_collapsible_defaults_closed_and_toggles(app: QApplication) -> None:
    panel = ProjectsPanel()
    assert panel.hint.isHidden() and not panel.rules_toggle.isChecked()
    assert "▸" in panel.rules_toggle.text()
    panel.rules_toggle.click()
    assert not panel.hint.isHidden() and "▾" in panel.rules_toggle.text()
    panel.rules_toggle.click()
    assert panel.hint.isHidden()


def test_list_height_bounds_and_empty_state(app: QApplication) -> None:
    panel = ProjectsPanel()
    panel.set_busy(False)
    assert panel.list_stack.currentWidget() is not panel.items
    heights = {}
    for n in (1, 2, 6, 10):
        _status(panel, [_spec(n=i) for i in range(n)], [])
        heights[n] = panel.list_stack.height()
    assert heights[1] < heights[2] < heights[6]
    # 1 taslak = 1 satır yüksekliği (+ 12px çerçeve payı); iki satır rezerve edilmez.
    _status(panel, [_spec()], [])
    row = max(panel.items.sizeHintForRow(0), 36)
    assert panel.list_stack.height() == row + 12
    _status(panel, [], [])
    assert panel.list_stack.currentWidget() is not panel.items
    assert heights[10] == heights[6]
    assert LIST_MAX_ROWS == 6


def test_status_chip_and_cancel_separated(app: QApplication) -> None:
    panel = ProjectsPanel()
    panel.set_busy(False)
    _status(panel, [], [])
    assert panel.status_chip.isHidden()
    job = {"job_id": "j", "project": "p", "status": "waiting", "question_id": "q", "spec": {}}
    _status(panel, [], [job])
    assert panel.status_chip.text() == "Cevap bekliyor"
    assert panel.cancel_button.objectName() == "danger" and not panel.cancel_button.isHidden()
    assert panel.cancel_button.parent() is panel.start_button.parent()
    _status(panel, [_spec()], [])
    assert panel.status_chip.text() == "Taslak" and panel.cancel_button.isHidden()


def test_status_chip_labels_are_distinct_but_keep_error_tone(app: QApplication) -> None:
    panel = ProjectsPanel()
    panel.set_busy(False)
    seen = {}
    for status in ("error", "timeout", "interrupted", "cleanup_pending"):
        job = {"job_id": "j", "project": "p", "status": status, "spec": {}}
        _status(panel, [], [job])
        seen[status] = panel.status_chip.text()
        assert panel.status_chip.property("tone") == "error", status
    assert len(set(seen.values())) == 4, seen
    assert seen["timeout"] == "Zaman aşımı" and seen["interrupted"] == "Kesildi"
    assert seen["cleanup_pending"] == "Temizleniyor"


def test_prepare_action_row_visible_at_1280x850(app: QApplication) -> None:
    """1280x850 pencerede sayfa görünümü ~1000x700: düğme ve nedeni kıvrımın altına düşmemeli."""
    panel = ProjectsPanel()
    panel.setStyleSheet(themes.stylesheet(themes.DEFAULT_THEME))
    panel.resize(1000, 700)
    panel.show()
    panel.set_busy(False)
    _status(panel, [], [])  # neden satırı görünür: düğme pasif
    app.processEvents()
    viewport = panel.findChild(QScrollArea).viewport()
    for widget in (panel.prepare_button, panel.prepare_reason):
        bottom = widget.mapTo(viewport, QPoint(0, widget.height())).y()
        assert 0 < bottom <= viewport.height(), (widget, bottom, viewport.height())
    # Düğme son editörün hemen altındadır (araya germe girmez).
    gap = panel.prepare_button.mapTo(panel, QPoint(0, 0)).y() - panel.acceptance.mapTo(
        panel, QPoint(0, panel.acceptance.height())
    ).y()
    assert 0 < gap <= 24, gap
    panel.close()


def test_editors_start_at_72_and_grow_to_120(app: QApplication) -> None:
    panel = ProjectsPanel()
    panel.show()
    assert panel.task.height() == 72
    panel.task.setPlainText("satır\n" * 30)
    app.processEvents()
    assert panel.task.height() == 120
    panel.task.clear()
    assert panel.task.height() == 72
    panel.close()


# --- Geçmiş ------------------------------------------------------------------------------


def _loaded(panel: DataPanel, days: int = 30, turns: list[Any] | None = None) -> None:
    panel.accept_result(
        "conversation_history",
        _ok({"turns": len(turns or []), "retention_days": days, "recent_turns": turns or []}),
    )


def test_history_unloaded_vs_loaded_empty(app: QApplication) -> None:
    panel = DataPanel()
    panel.set_busy(False)
    empty = panel.history_empty
    assert empty.title_label.text() == "Henüz yüklenmedi"
    assert empty.action_button is not None and not empty.action_button.isHidden()
    _loaded(panel, turns=[])
    assert empty.title_label.text() == "Kayıt yok"
    assert empty.action_button is not None and empty.action_button.isHidden()


def test_autoload_only_when_allowed(app: QApplication) -> None:
    def run(panel: DataPanel, show: bool, busy: bool) -> list[Any]:
        seen: list[Any] = []
        panel.tool_requested.connect(lambda name, args: seen.append((name, args)))
        panel.set_busy(busy)
        if show:
            panel.show()
        app.processEvents()
        app.processEvents()
        panel.close()
        return seen

    seen = run(DataPanel(), show=True, busy=False)
    assert seen == [("conversation_history", HistoryArguments(action="list"))]
    assert run(DataPanel(demo=True), show=True, busy=False) == []
    assert run(DataPanel(), show=True, busy=True) == []
    assert run(DataPanel(), show=False, busy=False) == []
    # Meşgulken görünen sayfa, boşalınca bir kez yükler; ikinci set_busy tekrar istemez.
    panel = DataPanel()
    seen2: list[Any] = []
    panel.tool_requested.connect(lambda name, args: seen2.append(name))
    panel.set_busy(True)
    panel.show()
    app.processEvents()
    assert seen2 == []
    panel.set_busy(False)
    app.processEvents()
    panel.set_busy(False)
    app.processEvents()
    assert seen2 == ["conversation_history"]
    panel.close()


def test_delete_block_separated_and_emits_typed_delete(app: QApplication) -> None:
    panel = DataPanel()
    panel.set_busy(False)
    assert panel.delete.parent() is panel.danger_zone
    assert panel.delete.parent() is not panel.history.parent()
    assert not panel.delete.isEnabled() and "yükle" in panel.delete_reason.text()
    seen: list[Any] = []
    panel.tool_requested.connect(lambda name, args: seen.append((name, args)))
    _loaded(panel)
    assert panel.delete.isEnabled() and panel.delete_reason.isHidden()
    panel.delete.click()
    # Onay SafetyGate'te (CONFIRM_REQUIRED); panel yalnız tipli isteği iletir.
    assert seen == [("conversation_history", HistoryArguments(action="delete"))]


def test_retention_apply_disabled_until_changed(app: QApplication) -> None:
    panel = DataPanel()
    panel.set_busy(False)
    assert not panel.retention.isEnabled()
    _loaded(panel, days=30)
    assert not panel.retention.isEnabled()
    panel.days.setValue(7)
    assert panel.retention.isEnabled()
    panel.days.setValue(30)
    assert not panel.retention.isEnabled()
    panel.days.setValue(0)
    assert panel.retention.isEnabled()


def test_retry_label_short_with_separate_warning(app: QApplication) -> None:
    panel = DataPanel()
    panel.set_busy(False)
    assert panel.retry.text() == "Yeniden dene" and panel.retry_warning.isHidden()
    panel.accept_result(
        "reminder_list",
        _ok({"reminders": [{"reminder_id": 1, "state": "failed", "due_local": "d", "text": "t"}]}),
    )
    panel.items.setCurrentRow(0)
    assert panel.retry.isEnabled() and not panel.retry_warning.isHidden()
    assert panel.retry_warning.text() == "Çift bildirim olabilir"
    # Düğme tek başına neyi denediğini ipucunda ve erişilebilir adında söyler; uyarı ayrı kalır.
    assert "seçili hatırlatma teslimini" in panel.retry.toolTip()
    assert "Seçili hatırlatma" in panel.retry.accessibleName()
    assert "çift bildirim" not in panel.retry.text().lower()


def test_history_search_is_first_class_and_content_capped(app: QApplication) -> None:
    panel = DataPanel()
    assert panel.search.placeholderText().startswith("Konuşmalarda ara")
    assert panel.MAX_CONTENT_WIDTH == 960
    assert panel.search.accessibleName() == "Geçmişte ara"


# --- Ortak ---------------------------------------------------------------------------------


def test_no_hardcoded_hex_in_list_panels() -> None:
    for module in (projects_panel, data_panel):
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert not re.search(r"#[0-9a-fA-F]{3,8}\b", source), module.__name__


def test_shared_helpers_keep_old_signatures(app: QApplication) -> None:
    assert make_page_header("T") is not None
    assert make_page_header("T", "alt", ()) is not None
    box = EmptyState("a", "b")
    assert box.action_button is None
    assert any(lbl.text() == "a" for lbl in box.findChildren(QLabel))


def test_list_qss_uses_tokens_in_every_theme() -> None:
    for name in themes.THEMES:
        css = themes.stylesheet(name)
        for selector in ("QLabel#statusChip", "QFrame#dangerZone", "QPushButton#disclosure"):
            assert selector in css
