"""Masaüstü kabul denetimleri: Dosya arama (Y:176), Araştırma (Y:144) ve Geçmiş/Hafıza (W:148)."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from artemis.brain.conversation import TurnRecord
from artemis.brain.models import Completion, ToolCall
from artemis.channels.desktop.app import DesktopWindow
from artemis.channels.desktop.approval_dialog import ApprovalDialog
from artemis.memory.store import MemoryStore
from artemis.tools.file_index import FileIndex
from artemis.tools.research_jobs import ResearchJobs
from scripts.kabul_masaustu_checks.common import (
    DEMO_DOCS_DIR,
    GlobalTimeoutError,
    RecordingProvider,
    RowResult,
    assert_no_leaked_windows,
    check_deadline,
    close_window_cleanly,
    fake_provider_bridge,
    grab_neutralized,
    intercept_toasts,
    make_temp_config,
    open_real_window,
    read_audit_events,
    wait_until,
    wait_window_ready,
)

_CHECK_ERRORS = (
    AssertionError,
    OSError,
    ValueError,
    RuntimeError,
    TimeoutError,
    sqlite3.Error,
    yaml.YAMLError,
    ValidationError,
)


def _check_y176(
    app: QApplication,
    tmp_root: Path,
    out_dir: Path,
    *,
    deadline: float | None = None,
) -> RowResult:
    """Y:176 — Yerel dosya aramasında tam dosya yerine <=200 karakterlik alıntı ve lekeleme."""
    check_deadline(deadline, "Y:176")
    sub = tmp_root / "y176"
    docs_dir = sub / "belgeler"
    docs_dir.mkdir(parents=True, exist_ok=True)
    long_text = (
        "BASLANGIC-GOZUKMEMELI "
        + ("dolgu metin satırı " * 45)
        + " BILINEN-IFADE-176 burada yer alır. "
        + ("sonraki metin satırı " * 45)
        + " BITIS-GOZUKMEMELI"
    )
    (docs_dir / "rapor_uzun.txt").write_text(long_text, encoding="utf-8")

    window: DesktopWindow | None = None
    try:
        cfg, _ws, state_dir = make_temp_config(
            sub,
            overrides={"file_search": {"enabled": True, "roots": [str(docs_dir)]}},
        )
        provider = RecordingProvider(
            responses=[
                Completion(
                    content="",
                    tool_calls=[
                        ToolCall(
                            id="tc-176",
                            name="file_search",
                            arguments={"query": "BILINEN-IFADE-176"},
                        )
                    ],
                    provider="fake",
                    model="fake-acceptance-model",
                ),
                Completion(
                    content="Aranan ifade rapor_uzun.txt dosyasında bulundu.",
                    provider="fake",
                    model="fake-acceptance-model",
                ),
            ]
        )
        with fake_provider_bridge(lambda: provider):
            window = open_real_window(app, cfg, deadline=deadline)
            assert window.bridge._runtime is not None
            file_index = window.bridge._runtime.context.services["file_index"]
            assert isinstance(file_index, FileIndex)
            wait_until(
                app,
                lambda: not file_index.running and int(file_index.status()["files"]) >= 1,
                desc="Y:176 dosya indeksi hazır",
                deadline=deadline,
            )

            window.prompt.setPlainText("Belgelerimde 'BILINEN-IFADE-176' geçen dosya hangisi")
            QTest.mouseClick(window.send_button, Qt.MouseButton.LeftButton)
            wait_window_ready(app, window, deadline=deadline)

            tool_events = [
                event
                for event in read_audit_events(state_dir / "audit.jsonl", "tool_result")
                if event.get("tool") == "file_search"
            ]
            assert tool_events, "audit.jsonl içinde file_search tool_result olayı yok"
            fs_event = tool_events[-1]
            result = fs_event["result"]
            assert result["success"] is True
            assert result["external"] is True
            assert result["content_kind"] == "file"
            assert fs_event["tainted"] is True

            hits = result["data"]["results"]
            assert len(hits) == 1, f"Beklenen 1 sonuç, gelen: {len(hits)}"
            hit = hits[0]
            assert Path(hit["path"]).name == "rapor_uzun.txt"
            assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", hit["modified"])
            assert 0 < len(hit["snippet"]) <= 200
            assert "BILINEN-IFADE-176" in hit["snippet"]
            assert "BASLANGIC-GOZUKMEMELI" not in hit["snippet"]
            assert "BITIS-GOZUKMEMELI" not in hit["snippet"]

            assert len(provider.requests) >= 2
            tool_messages = [msg for msg in provider.requests[1] if msg.role == "tool"]
            assert tool_messages, "İkinci sağlayıcı isteğinde role='tool' mesajı yok"
            tool_content = tool_messages[-1].content
            assert "VERİDİR" in tool_content
            assert "BILINEN-IFADE-176" in tool_content
            assert "BASLANGIC-GOZUKMEMELI" not in tool_content
            assert "BITIS-GOZUKMEMELI" not in tool_content

            shot = grab_neutralized(
                app,
                window,
                out_dir,
                "Y176_file_search_result.png",
                real_roots={docs_dir: DEMO_DOCS_DIR},
            )
            return RowResult(
                "Y:176",
                "PASS",
                "RecordingProvider ile file_search çağrısı senaryolandırıldı; gerçek FTS5 "
                "FileIndex, SafetyGate lekeleme (external=True, content_kind='file', "
                "tainted=True) ve <=200 karakter alıntı üreten FileSearch çıktısı doğrulandı.",
                (shot.name,),
            )
    except GlobalTimeoutError:
        raise
    except _CHECK_ERRORS as error:
        return RowResult("Y:176", "FAIL", f"{type(error).__name__}: {error}")
    finally:
        if window is not None:
            close_window_cleanly(app, window)


def _check_y144(
    app: QApplication,
    tmp_root: Path,
    out_dir: Path,
    *,
    deadline: float | None = None,
) -> RowResult:
    """Y:144 — Arka plan araştırması tamamlanınca rapor dosyası, toast ve Otomasyon kartı."""
    check_deadline(deadline, "Y:144")
    sub = tmp_root / "y144"
    window: DesktopWindow | None = None
    try:
        cfg, _ws, state_dir = make_temp_config(
            sub,
            overrides={"research": {"enabled": True, "max_searches": 3}},
        )
        provider = RecordingProvider(
            responses=[
                Completion(
                    content="",
                    tool_calls=[
                        ToolCall(
                            id="r-144",
                            name="web_search",
                            arguments={"query": "elektrikli araç pazarı"},
                        )
                    ],
                    provider="fake",
                    model="fake-acceptance-model",
                ),
                Completion(
                    content="# Araştırma Özeti\nElektrikli araç pazarı büyüdü.",
                    provider="fake",
                    model="fake-acceptance-model",
                ),
            ]
        )

        async def _fake_web_search(_query: str) -> list[dict[str, str]]:
            return [
                {
                    "title": "EV Raporu",
                    "url": "https://example.invalid/ev-2025",
                    "snippet": "Satışlar arttı.",
                }
            ]

        def _on_runtime(runtime: Any) -> None:
            runtime.context.services["web_search"] = _fake_web_search

        with (
            fake_provider_bridge(lambda: provider, on_runtime=_on_runtime),
            intercept_toasts("ok") as toasts,
        ):
            window = open_real_window(app, cfg, deadline=deadline)
            assert window.bridge._runtime is not None
            jobs = window.bridge._runtime.context.services["research_jobs"]
            assert isinstance(jobs, ResearchJobs)

            job_id = jobs.start("Elektrikli araç pazarı", 2)
            wait_until(
                app,
                lambda: (
                    (job := jobs.get(job_id)) is not None
                    and job["status"] == "done"
                    and len(toasts) >= 1
                    and any(
                        ev.get("title") == "Araştırma bitti: Elektrikli araç pazarı"
                        for ev in read_audit_events(
                            state_dir / "audit.jsonl", "desktop_notification"
                        )
                    )
                ),
                desc="Y:144 araştırma tamamlanması",
                deadline=deadline,
            )

            assert toasts[0].title == "Araştırma bitti: Elektrikli araç pazarı"
            assert toasts[0].body == "Raporu sohbetten isteyin."
            report_file = state_dir / "arastirma" / f"{job_id}.md"
            assert report_file.is_file()
            assert "Elektrikli" not in report_file.name

            notif_events = read_audit_events(state_dir / "audit.jsonl", "desktop_notification")
            assert any(
                ev.get("title") == "Araştırma bitti: Elektrikli araç pazarı"
                and ev.get("status") == "ok"
                for ev in notif_events
            )

            window.tabs.setCurrentWidget(window.automation_page)
            app.processEvents()
            window.automation_panel.refresh()
            wait_until(
                app,
                lambda: window is not None and job_id in window.automation_panel.report_buttons,
                desc="Y:144 otomasyon rapor düğmesi",
                deadline=deadline,
            )

            shot = grab_neutralized(app, window, out_dir, "Y144_research_done_automation.png")
            return RowResult(
                "Y:144",
                "PASS",
                "Sahte web_search, senaryolu RecordingProvider, yakalanan toast ve onay "
                "tıklaması olmadan ResearchJobs.start kullanıldı; gerçek ResearchJobs arka "
                "plan işçisi, rapor dosyası, toast yükü, desktop_notification denetim kaydı "
                "ve Otomasyon paneli doğrulandı.",
                (shot.name,),
            )
    except GlobalTimeoutError:
        raise
    except _CHECK_ERRORS as error:
        return RowResult("Y:144", "FAIL", f"{type(error).__name__}: {error}")
    finally:
        if window is not None:
            close_window_cleanly(app, window)


def _check_w148(
    app: QApplication,
    tmp_root: Path,
    out_dir: Path,
    *,
    deadline: float | None = None,
) -> RowResult:
    """W:148 — Geçmiş saklama süresi ve silme onay akışı (onay tıklanmadan SKIPPED)."""
    check_deadline(deadline, "W:148")
    sub = tmp_root / "w148"
    window: DesktopWindow | None = None
    skip_note = (
        "Geçmiş sekmesinde saklama=30 ve 'SAKLI-CUMLE-W148' okundu; 0 gün saklama ve "
        "tüm geçmişi silme onay pencereleri açılıp Esc ile reddedilerek veritabanının "
        "korunduğu doğrulandı. Onay diyaloğunda Evet/Onayla tıklama yasağı nedeniyle "
        "saklama/silme işleminin uygulanması ve 'SAKLI-CUMLE-W148' metninin "
        "memory.sqlite3 içinden temizlendiğinin doğrulanması atlandı."
    )
    try:
        cfg, _ws, state_dir = make_temp_config(sub, overrides={"persist_memory": True})
        db_path = state_dir / "memory.sqlite3"
        store = MemoryStore(db_path)
        store.set_retention(30)
        store.add_turn(
            TurnRecord(
                turn_id="t-w148",
                user_text="SAKLI-CUMLE-W148",
                steps=(),
                answer="Yanıt W148",
                answer_private=False,
                tainted=False,
            )
        )
        store.add_note("KALICI-OZ-NOT-W148", turn_id="t-w148", max_notes=50, private=False)

        with fake_provider_bridge(RecordingProvider):
            window = open_real_window(app, cfg, deadline=deadline)
            window.tabs.setCurrentWidget(window.data_page)
            app.processEvents()
            wait_until(
                app,
                lambda: (
                    window is not None
                    and window.data_panel._history_loaded
                    and window.data_panel._memory_loaded
                    and not window.data_panel._memory_pending
                    and not window.data_panel._busy
                    and not window._busy
                    and window.data_panel.days.isEnabled()
                ),
                desc="W:148 geçmiş ve kurallar yüklendi",
                deadline=deadline,
            )

            panel = window.data_panel
            assert panel.days.value() == 30
            assert "SAKLI-CUMLE-W148" in panel.preview.toPlainText()
            assert not panel.retention.isEnabled()

            panel.days.setValue(0)
            app.processEvents()
            assert panel.retention.isEnabled()

            QTest.mouseClick(panel.retention, Qt.MouseButton.LeftButton)
            wait_until(
                app,
                lambda: window is not None and isinstance(window._approval_dialog, ApprovalDialog),
                desc="W:148 saklama onay diyaloğu",
                deadline=deadline,
            )
            dialog = window._approval_dialog
            assert isinstance(dialog, ApprovalDialog)
            assert dialog.item.tool_name == "conversation_history"
            assert dialog.item.arguments.get("action") == "retention"
            assert dialog.item.arguments.get("retention_days") == 0
            assert dialog.no_button.isDefault()

            QTest.keyClick(dialog, Qt.Key.Key_Escape)
            wait_until(
                app,
                lambda: window is not None and window._approval_dialog is None,
                desc="W:148 saklama onayı kapandı",
                deadline=deadline,
            )
            wait_window_ready(app, window, deadline=deadline)
            assert MemoryStore(db_path).history_status()["retention_days"] == 30

            QTest.mouseClick(panel.delete, Qt.MouseButton.LeftButton)
            wait_until(
                app,
                lambda: window is not None and isinstance(window._approval_dialog, ApprovalDialog),
                desc="W:148 silme onay diyaloğu",
                deadline=deadline,
            )
            dialog = window._approval_dialog
            assert isinstance(dialog, ApprovalDialog)
            assert dialog.item.tool_name == "conversation_history"
            assert dialog.item.arguments.get("action") == "delete"
            assert dialog.no_button.isDefault()

            shot = grab_neutralized(
                app, dialog, out_dir, "W148_memory_panel_retention_delete.png"
            )

            QTest.keyClick(dialog, Qt.Key.Key_Escape)
            wait_until(
                app,
                lambda: window is not None and window._approval_dialog is None,
                desc="W:148 silme onayı kapandı",
                deadline=deadline,
            )
            wait_window_ready(app, window, deadline=deadline)

            after_store = MemoryStore(db_path)
            assert any(
                "SAKLI-CUMLE-W148" in row["user"] for row in after_store.history_page()
            ), "Esc sonrası SAKLI-CUMLE-W148 geçmişte korunmalıydı"
            assert any(
                "KALICI-OZ-NOT-W148" in note.text for note in after_store.list_notes()
            ), "Esc sonrası KALICI-OZ-NOT-W148 öz notlarda korunmalıydı"

            return RowResult("W:148", "SKIPPED", skip_note, (shot.name,))
    except GlobalTimeoutError:
        raise
    except _CHECK_ERRORS as error:
        return RowResult("W:148", "FAIL", f"{type(error).__name__}: {error}")
    finally:
        if window is not None:
            close_window_cleanly(app, window)


def run_search_research_memory_checks(
    app: QApplication,
    tmp_root: Path,
    out_dir: Path,
    *,
    deadline: float | None = None,
) -> list[RowResult]:
    """Dosya arama, arka plan araştırması ve geçmiş/hafıza kabul senaryolarını çalıştırır."""
    results: list[RowResult] = []
    for label, check_fn in (
        ("Y:176", _check_y176),
        ("Y:144", _check_y144),
        ("W:148", _check_w148),
    ):
        results.append(check_fn(app, tmp_root, out_dir, deadline=deadline))
        assert_no_leaked_windows(app, f"{label} sonrası", max_visible=2, max_desktop=0)
    return results
