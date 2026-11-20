"""Masaüstü kabul denetimleri: Rutinler, Beceriler, Sihirbaz, Geçmiş, Projeler ve Ayarlar."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

import yaml
from pydantic import ValidationError
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, Qt, QUrl
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from artemis.channels.desktop import skills_section as skills_section_mod
from artemis.channels.desktop import themes
from artemis.channels.desktop.app import DesktopWindow
from artemis.channels.desktop.approval_dialog import ApprovalDialog
from artemis.channels.desktop.connect_wizard import ConnectWizard
from artemis.config.settings import load_settings
from artemis.proactive.routines import Routine, RoutineStep, RoutineStore
from artemis.tools.project_spec import ProjectSpec
from scripts.kabul_masaustu_checks.common import (
    DEMO_PROJECT_DIR,
    DEMO_SKILLS_DIR,
    GlobalTimeoutError,
    RecordingProvider,
    RowResult,
    assert_no_leaked_windows,
    check_deadline,
    close_window_cleanly,
    fake_provider_bridge,
    grab_neutralized,
    make_temp_config,
    open_real_window,
    select_settings_section,
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


def _mean_luminance(widget: QWidget) -> float:
    """Pencere görüntüsünün ortalama piksel parlaklığını (0..255) ölçer."""
    image = widget.grab().toImage()
    width, height = image.width(), image.height()
    if width <= 0 or height <= 0:
        raise AssertionError("Parlaklık ölçümü için boş görüntü alındı.")
    step_x = max(1, width // 120)
    step_y = max(1, height // 120)
    total = 0.0
    count = 0
    for y in range(0, height, step_y):
        for x in range(0, width, step_x):
            color = image.pixelColor(x, y)
            total += 0.2126 * color.red() + 0.7152 * color.green() + 0.0722 * color.blue()
            count += 1
    return total / max(1, count)


def _make_provider_tracker() -> tuple[list[RecordingProvider], Callable[[], RecordingProvider]]:
    """Yeniden kurulum dahil tüm sahte sağlayıcı örneklerini sırayla saklar."""
    created: list[RecordingProvider] = []

    def _factory() -> RecordingProvider:
        provider = RecordingProvider()
        created.append(provider)
        return provider

    return created, _factory


def _check_y59(
    app: QApplication,
    tmp_root: Path,
    out_dir: Path,
    *,
    deadline: float | None = None,
) -> RowResult:
    check_deadline(deadline, "Y:59")
    sub = tmp_root / "y59"
    window: DesktopWindow | None = None
    try:
        cfg, _ws, _state = make_temp_config(sub, overrides={"routines": {"enabled": False}})
        with fake_provider_bridge(RecordingProvider):
            window = open_real_window(app, cfg, deadline=deadline)
            select_settings_section(app, window, "Rutinler")
            panel = window.settings_panel
            sec = panel.routines
            assert not panel.routines_enabled.isChecked()
            assert sec.routine_list.count() == 0
            assert sec.routine_area.currentWidget() is sec.routine_area.empty
            assert sec.routine_area.empty.title_label.text() == "Rutinler kapalı"
            assert not sec.action_row.isVisible()
            for btn in (
                sec.add_button,
                sec.edit_button,
                sec.delete_button,
                sec.save_time_button,
                sec.clear_time_button,
                sec.fix_button,
            ):
                assert not btn.isEnabled()
            shot = grab_neutralized(app, window, out_dir, "Y59_routines_disabled.png")
            return RowResult(
                "Y:59",
                "PASS",
                "Rutinler kapalıyken boş durum 'Rutinler kapalı' ve tüm düğmeler devre dışı.",
                (shot.name,),
            )
    except GlobalTimeoutError:
        raise
    except _CHECK_ERRORS as error:
        return RowResult("Y:59", "FAIL", f"{type(error).__name__}: {error}")
    finally:
        if window is not None:
            close_window_cleanly(app, window)


def _check_y61(
    app: QApplication,
    tmp_root: Path,
    out_dir: Path,
    *,
    deadline: float | None = None,
) -> list[RowResult]:
    check_deadline(deadline, "Y:61")
    sub = tmp_root / "y61"
    window: DesktopWindow | None = None
    open_note = (
        '"Dosyayı aç" düğmesi güncel RoutinesSection içinde yok '
        "(ADR 0058 Ek karar dış editör açmak yerine uygulama içi düzenleme/düzeltme kullanır)."
    )
    try:
        cfg, _ws, state_dir = make_temp_config(
            sub,
            overrides={"routines": {"enabled": True}, "proactive": {"enabled": True}},
        )
        RoutineStore(state_dir / "routines.json").save(
            [
                Routine(
                    name="işe başla",
                    steps=[
                        RoutineStep(tool="open_application", arguments={"name": "vscode"}),
                        RoutineStep(tool="open_application", arguments={"name": "spotify"}),
                        RoutineStep(tool="window_list", arguments={}),
                    ],
                )
            ]
        )
        with fake_provider_bridge(RecordingProvider):
            window = open_real_window(app, cfg, deadline=deadline)
            select_settings_section(app, window, "Rutinler")
            sec = window.settings_panel.routines
            assert sec.routine_list.count() == 1
            item0 = sec.routine_list.item(0)
            assert item0 is not None and item0.text().startswith("işe başla · 3 adım:")
            sec.routine_list.setCurrentRow(0)
            app.processEvents()
            for btn in (
                sec.edit_button,
                sec.delete_button,
                sec.save_time_button,
                sec.clear_time_button,
            ):
                assert btn.isEnabled()
            shot = grab_neutralized(app, window, out_dir, "Y61_routines_list.png")
            return [
                RowResult(
                    "Y:61",
                    "PASS",
                    "Rutin listesi 'işe başla · 3 adım:' satırını gösterdi ve seçimle "
                    "düzenle/sil/zaman düğmeleri etkinleşti.",
                    (shot.name,),
                ),
                RowResult("Y:61-open", "SKIPPED", open_note),
            ]
    except GlobalTimeoutError:
        raise
    except _CHECK_ERRORS as error:
        return [
            RowResult("Y:61", "FAIL", f"{type(error).__name__}: {error}"),
            RowResult("Y:61-open", "SKIPPED", open_note),
        ]
    finally:
        if window is not None:
            close_window_cleanly(app, window)


def _check_y94_y95(
    app: QApplication,
    tmp_root: Path,
    out_dir: Path,
    *,
    deadline: float | None = None,
) -> list[RowResult]:
    check_deadline(deadline, "Y:94/Y:95")
    sub = tmp_root / "y94_y95"
    skills_folder = sub / "state" / "beceriler"
    window: DesktopWindow | None = None
    results: list[RowResult] = []
    try:
        cfg, _ws, state_dir = make_temp_config(
            sub,
            overrides={"skills": {"enabled": True, "folder": str(skills_folder)}},
        )
        skills_folder = state_dir / "beceriler"
        with fake_provider_bridge(RecordingProvider):
            window = open_real_window(app, cfg, deadline=deadline)
            select_settings_section(app, window, "Beceriler")
            skills = window.settings_panel.skills

            # Y:94 — Klasörü göster hedefi Gezgin başlatmadan yakalanır.
            try:
                assert skills.open_button.text() == "Klasörü göster"
                assert (skills_folder / "ornek-gunluk-ozet.md").is_file()
                captured_urls: list[QUrl] = []
                orig_open_url = skills_section_mod.QDesktopServices.openUrl

                def _capture_open_url(url: QUrl) -> bool:
                    captured_urls.append(url)
                    return True

                # Gerçek Explorer penceresi açıp bırakmamak için openUrl geçici yakalanır.
                skills_section_mod.QDesktopServices.openUrl = _capture_open_url  # type: ignore[method-assign]
                try:
                    QTest.mouseClick(skills.open_button, Qt.MouseButton.LeftButton)
                    app.processEvents()
                finally:
                    skills_section_mod.QDesktopServices.openUrl = orig_open_url  # type: ignore[method-assign]

                assert len(captured_urls) == 1
                assert Path(captured_urls[0].toLocalFile()).resolve() == skills_folder.resolve()
                shot94 = grab_neutralized(
                    app,
                    window,
                    out_dir,
                    "Y94_skills_folder.png",
                    real_roots={skills_folder: DEMO_SKILLS_DIR},
                )
                results.append(
                    RowResult(
                        "Y:94",
                        "PASS",
                        "'Klasörü göster' tıklaması Explorer açmadan beklenen yerel "
                        "klasör URL'sini QDesktopServices.openUrl ile hedefledi.",
                        (shot94.name,),
                    )
                )
            except GlobalTimeoutError:
                raise
            except _CHECK_ERRORS as error:
                results.append(RowResult("Y:94", "FAIL", f"{type(error).__name__}: {error}"))

            # Y:95 — Aynı pencere ve beceri klasöründe dosya ekleme + Yenile.
            check_deadline(deadline, "Y:95")
            try:
                target_file = skills_folder / "haftalik-rapor.md"
                target_file.unlink(missing_ok=True)
                QTest.mouseClick(skills.refresh_button, Qt.MouseButton.LeftButton)
                app.processEvents()
                before_items = [
                    skills.valid_list.item(i).text() for i in range(skills.valid_list.count())
                ]
                assert not any("haftalik-rapor" in text for text in before_items)

                target_file.write_text(
                    "---\nname: haftalik-rapor\n"
                    "description: Haftalık durum raporu.\n---\n"
                    "Haftalık işlerin kısa durum özetini çıkar.\n",
                    encoding="utf-8",
                )
                QTest.mouseClick(skills.refresh_button, Qt.MouseButton.LeftButton)
                app.processEvents()
                after_items = [
                    skills.valid_list.item(i).text() for i in range(skills.valid_list.count())
                ]
                assert "haftalik-rapor: Haftalık durum raporu." in after_items
                shot95 = grab_neutralized(
                    app,
                    window,
                    out_dir,
                    "Y95_skills_refresh.png",
                    real_roots={skills_folder: DEMO_SKILLS_DIR},
                )
                results.append(
                    RowResult(
                        "Y:95",
                        "PASS",
                        "UTF-8 haftalik-rapor.md eklendikten sonra Yenile ile listede "
                        "göründü (dosya elle Notepad yerine UTF-8 olarak yazıldı).",
                        (shot95.name,),
                    )
                )
            except GlobalTimeoutError:
                raise
            except _CHECK_ERRORS as error:
                results.append(RowResult("Y:95", "FAIL", f"{type(error).__name__}: {error}"))
            return results
    except GlobalTimeoutError:
        raise
    except _CHECK_ERRORS as error:
        if not results:
            results.append(RowResult("Y:94", "FAIL", f"{type(error).__name__}: {error}"))
        if len(results) < 2:
            results.append(RowResult("Y:95", "FAIL", f"{type(error).__name__}: {error}"))
        return results
    finally:
        if window is not None:
            close_window_cleanly(app, window)


def _check_y100(
    app: QApplication,
    tmp_root: Path,
    out_dir: Path,
    *,
    deadline: float | None = None,
) -> RowResult:
    check_deadline(deadline, "Y:100")
    sub = tmp_root / "y100"
    skills_folder = sub / "state" / "beceriler"
    window: DesktopWindow | None = None
    try:
        cfg, _ws, _state = make_temp_config(
            sub,
            overrides={"skills": {"enabled": True, "folder": str(skills_folder)}},
        )
        providers, factory = _make_provider_tracker()
        with fake_provider_bridge(factory):
            window = open_real_window(app, cfg, deadline=deadline)
            assert window.bridge._runtime is not None
            assert "skill_read" in window.bridge._runtime.registry

            select_settings_section(app, window, "Beceriler")
            panel = window.settings_panel
            assert panel.skills_enabled.isChecked()
            QTest.mouseClick(panel.skills_enabled, Qt.MouseButton.LeftButton)
            app.processEvents()
            assert not panel.skills_enabled.isChecked()
            assert panel.save_button.isVisible()

            old_rt = window.bridge._runtime
            QTest.mouseClick(panel.save_button, Qt.MouseButton.LeftButton)
            wait_until(
                app,
                lambda: (
                    window is not None
                    and window.bridge._runtime is not None
                    and window.bridge._runtime is not old_rt
                ),
                desc="Y:100 runtime restart",
                deadline=deadline,
            )
            wait_window_ready(app, window, deadline=deadline)

            assert load_settings(cfg).skills.enabled is False
            assert window.bridge._runtime is not None
            assert "skill_read" not in window.bridge._runtime.registry
            assert "skill_read" not in [
                d.name for d in window.bridge._runtime.registry.definitions()
            ]

            window.tabs.setCurrentWidget(window.chat_page)
            app.processEvents()
            window.prompt.setPlainText("haftalık rapor hazırla")
            QTest.mouseClick(window.send_button, Qt.MouseButton.LeftButton)
            wait_window_ready(app, window, deadline=deadline)
            assert providers and providers[-1].tool_names
            assert "skill_read" not in providers[-1].tool_names[-1]

            select_settings_section(app, window, "Beceriler")
            assert panel.skills.valid_area.empty.title_label.text() == "Beceriler kapalı"
            shot = grab_neutralized(
                app,
                window,
                out_dir,
                "Y100_skills_disabled.png",
                real_roots={skills_folder: DEMO_SKILLS_DIR},
            )
            return RowResult(
                "Y:100",
                "PASS",
                "Beceriler kapatılıp kaydedildikten sonra 'skill_read' hem runtime "
                "kaydından hem sohbet araç listesinden kalktı (sahte sağlayıcıyla doğrulandı; "
                "canlı LLM sohbeti atlandı).",
                (shot.name,),
            )
    except GlobalTimeoutError:
        raise
    except _CHECK_ERRORS as error:
        return RowResult("Y:100", "FAIL", f"{type(error).__name__}: {error}")
    finally:
        if window is not None:
            close_window_cleanly(app, window)


def _check_r26(
    app: QApplication,
    tmp_root: Path,
    out_dir: Path,
    *,
    deadline: float | None = None,
) -> RowResult:
    check_deadline(deadline, "R:26")
    sub = tmp_root / "r26"
    window: DesktopWindow | None = None
    wiz: ConnectWizard | None = None
    try:
        cfg, _ws, _state = make_temp_config(sub)
        with fake_provider_bridge(RecordingProvider):
            window = open_real_window(app, cfg, deadline=deadline)
            window._apply_theme("light")
            app.processEvents()

            wiz = ConnectWizard(
                cfg,
                parent=window,
                local_probe=lambda: [],
                open_url=lambda _u: None,
            )
            wiz.resize(760, 640)
            wiz.show()
            app.processEvents()
            assert themes.THEMES["light"]["bg"] in wiz.styleSheet()
            assert _mean_luminance(wiz) > 180
            shot_light = grab_neutralized(app, wiz, out_dir, "R26_wizard_light.png")
            wiz.hide()
            wiz.close()
            wiz.deleteLater()
            wiz = None
            app.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

            QTest.mouseClick(window.theme_toggle, Qt.MouseButton.LeftButton)
            app.processEvents()
            assert window._theme == "dark"

            wiz = ConnectWizard(
                cfg,
                parent=window,
                local_probe=lambda: [],
                open_url=lambda _u: None,
            )
            wiz.resize(760, 640)
            wiz.show()
            app.processEvents()
            assert themes.THEMES["dark"]["bg"] in wiz.styleSheet()
            assert _mean_luminance(wiz) < 80
            shot_dark = grab_neutralized(app, wiz, out_dir, "R26_wizard_dark.png")
            wiz.hide()
            wiz.close()
            wiz.deleteLater()
            wiz = None
            app.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

            return RowResult(
                "R:26",
                "PASS",
                "Bağlantı sihirbazı açık ve koyu temalarda beklenen QSS ve piksel "
                "parlaklığı (>180 / <80) ile açıldı.",
                (shot_light.name, shot_dark.name),
            )
    except GlobalTimeoutError:
        raise
    except _CHECK_ERRORS as error:
        return RowResult("R:26", "FAIL", f"{type(error).__name__}: {error}")
    finally:
        if wiz is not None:
            wiz.hide()
            wiz.close()
            wiz.deleteLater()
            app.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        if window is not None:
            close_window_cleanly(app, window)


def _check_r72(
    app: QApplication,
    tmp_root: Path,
    out_dir: Path,
    *,
    deadline: float | None = None,
) -> RowResult:
    check_deadline(deadline, "R:72")
    sub = tmp_root / "r72"
    window: DesktopWindow | None = None
    try:
        cfg, _ws, _state = make_temp_config(sub, overrides={"persist_memory": True})
        with fake_provider_bridge(RecordingProvider):
            window = open_real_window(app, cfg, deadline=deadline)
            window.prompt.setPlainText("merhaba deneme")
            QTest.mouseClick(window.send_button, Qt.MouseButton.LeftButton)
            wait_window_ready(app, window, deadline=deadline)
            assert "merhaba deneme" in window.chat.toPlainText()

            window.tabs.setCurrentWidget(window.data_page)
            app.processEvents()
            wait_until(
                app,
                lambda: (
                    window is not None
                    and window.data_panel._history_loaded
                    and window.data_panel.delete.isEnabled()
                ),
                desc="R:72 history loaded",
                deadline=deadline,
            )
            QTest.mouseClick(window.data_panel.delete, Qt.MouseButton.LeftButton)
            wait_until(
                app,
                lambda: window is not None and isinstance(window._approval_dialog, ApprovalDialog),
                desc="R:72 approval dialog",
                deadline=deadline,
            )
            dialog = window._approval_dialog
            assert isinstance(dialog, ApprovalDialog)
            assert dialog.item.tool_name == "conversation_history"
            assert dialog.item.arguments.get("action") == "delete"
            assert dialog.no_button.isDefault()
            shot = grab_neutralized(app, dialog, out_dir, "R72_history_delete_approval.png")

            # Onay diyalogları asla onaylanmaz; Esc ile reddedilip işlem iptal doğrulanır.
            QTest.keyClick(dialog, Qt.Key.Key_Escape)
            wait_until(
                app,
                lambda: window is not None and window._approval_dialog is None,
                desc="R:72 approval closed",
                deadline=deadline,
            )
            wait_window_ready(app, window, deadline=deadline)
            assert "merhaba deneme" in window.chat.toPlainText()
            return RowResult(
                "R:72",
                "SKIPPED",
                "Onay penceresi açıldı ve varsayılan Hayır/Esc ile kapatıldı; onay "
                "tıklanmadığı için gerçek geçmiş silme sonrası sohbet temizliği insan "
                "onayına bırakıldı.",
                (shot.name,),
            )
    except GlobalTimeoutError:
        raise
    except _CHECK_ERRORS as error:
        return RowResult("R:72", "FAIL", f"{type(error).__name__}: {error}")
    finally:
        if window is not None:
            close_window_cleanly(app, window)


def _check_r73(
    app: QApplication,
    tmp_root: Path,
    out_dir: Path,
    *,
    deadline: float | None = None,
) -> RowResult:
    check_deadline(deadline, "R:73")
    sub = tmp_root / "r73"
    window: DesktopWindow | None = None
    jobs_ref: object | None = None
    try:
        repo_dir = sub / "ornek_repo"
        repo_dir.mkdir(parents=True, exist_ok=True)
        cfg, _ws, state_dir = make_temp_config(
            sub,
            overrides={"code_agent": {"projects": {"ornek": str(repo_dir)}}},
        )
        draft_spec = {
            "spec_id": "00000000-0000-4000-8000-000000000011",
            "project": "ornek",
            "task": "Örnek görev",
            "scope": ["yalnız test"],
            "acceptance": ["test geçer"],
            "agent": "claude",
            "workdir": "proje",
            "mode": "background",
            "repo_path": str(repo_dir),
            "workdir_path": str(repo_dir),
        }
        job_spec = ProjectSpec(
            spec_id="00000000-0000-4000-8000-000000000010",
            project="ornek",
            task="Örnek iş görevi",
            scope=("yalnız test",),
            acceptance=("test geçer",),
            agent="claude",
            workdir="proje",
            mode="background",
            repo_path=str(repo_dir),
            workdir_path=str(repo_dir),
        ).model_dump(mode="json")
        job_record = {
            "job_id": "a1b2c3d4e5f6",
            "project": "ornek",
            "repo": str(repo_dir),
            "agent": "claude",
            "workdir": "proje",
            "workdir_path": str(repo_dir),
            "mode": "background",
            "base": "0" * 40,
            "spec": job_spec,
            "status": "running",
            "commits": [],
            "leftover_changes": None,
            "changes": "none",
            "verification": "unverified",
            "checks": [],
            "summary": "",
            "duration_seconds": 0.0,
            "cost_usd": None,
            "created_at": 1.0,
            "deadline": 2.0,
            "session_id": None,
            "question_id": None,
            "question": None,
            "cleanup_pending": False,
        }
        with closing(sqlite3.connect(state_dir / "code_agent.sqlite3")) as conn, conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS records "
                "(key TEXT PRIMARY KEY, kind TEXT NOT NULL, value TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT OR REPLACE INTO records VALUES (?, ?, ?)",
                (draft_spec["spec_id"], "draft", json.dumps(draft_spec, ensure_ascii=False)),
            )
            conn.execute(
                "INSERT OR REPLACE INTO records VALUES (?, ?, ?)",
                (job_record["job_id"], "job", json.dumps(job_record, ensure_ascii=False)),
            )

        with fake_provider_bridge(RecordingProvider):
            window = open_real_window(app, cfg, deadline=deadline)
            if window.bridge._runtime is not None:
                jobs_ref = window.bridge._runtime.context.services.get("code_agent_jobs")
            window.tabs.setCurrentWidget(window.projects_page)
            app.processEvents()
            window.projects_panel.refresh()
            wait_until(
                app,
                lambda: (
                    window is not None
                    and window.projects_panel.items.count() == 2
                    and not window._busy
                ),
                desc="R:73 projects loaded",
                deadline=deadline,
            )
            pp = window.projects_panel

            pp.items.setCurrentRow(0)
            app.processEvents()
            assert not pp.start_button.isEnabled()
            assert "Kilidi bırak" in pp.start_reason.text()
            assert pp.discard_button.isEnabled()
            assert not pp.release_button.isVisible()
            shot_draft = grab_neutralized(
                app,
                window,
                out_dir,
                "R73_locked_draft.png",
                real_roots={repo_dir: DEMO_PROJECT_DIR},
            )

            pp.items.setCurrentRow(1)
            app.processEvents()
            assert pp.release_button.isVisible()
            assert pp.release_button.isEnabled()
            assert not pp.cancel_button.isEnabled()
            assert not pp.clear_button.isEnabled()
            assert not pp.start_button.isEnabled()
            shot_job = grab_neutralized(
                app,
                window,
                out_dir,
                "R73_locked_job.png",
                real_roots={repo_dir: DEMO_PROJECT_DIR},
            )

            return RowResult(
                "R:73",
                "PASS",
                "Kesintili kilitli iş varken taslak başlatma kilitlendi, 'Kilidi bırak' "
                "düğmesi yalnız iş satırında etkin göründü (tıklanmadı).",
                (shot_draft.name, shot_job.name),
            )
    except GlobalTimeoutError:
        raise
    except _CHECK_ERRORS as error:
        return RowResult("R:73", "FAIL", f"{type(error).__name__}: {error}")
    finally:
        if window is not None:
            close_window_cleanly(app, window)
        ownership = getattr(jobs_ref, "_ownership", None)
        if ownership is not None:
            ownership.close()
            jobs_ref._ownership = None  # type: ignore[attr-defined]


def _check_w118(
    app: QApplication,
    tmp_root: Path,
    out_dir: Path,
    *,
    deadline: float | None = None,
) -> RowResult:
    check_deadline(deadline, "W:118")
    sub = tmp_root / "w118"
    window: DesktopWindow | None = None
    try:
        cfg, _ws, _state = make_temp_config(
            sub,
            overrides={"routines": {"enabled": False}, "voice": {"wake_word": "artemis"}},
        )
        with fake_provider_bridge(RecordingProvider):
            window = open_real_window(app, cfg, deadline=deadline)
            assert window.bridge._runtime is not None, "runtime kurulmadı"
            assert "routine_list" not in window.bridge._runtime.registry, (
                "routine_list başlangıçta kayıtlı olmamalıydı"
            )

            select_settings_section(app, window, "Ses")
            panel = window.settings_panel
            window.activateWindow()
            app.processEvents()
            panel.wake_name.setFocus()
            panel.wake_name.clear()
            QTest.keyClicks(panel.wake_name, "helena")
            app.processEvents()
            assert panel.wake_name.text() == "helena", f"wake_name={panel.wake_name.text()!r}"

            select_settings_section(app, window, "Rutinler")
            QTest.mouseClick(
                panel.routines_enabled,
                Qt.MouseButton.LeftButton,
                pos=QPoint(12, max(1, panel.routines_enabled.height() // 2)),
            )
            app.processEvents()
            assert panel.routines_enabled.isChecked(), "routines_enabled işaretlenmedi"
            assert panel.save_button.isVisible(), "Kaydet düğmesi görünür olmalıydı"
            assert panel.save_button.text() == "Kaydet", (
                f"save_button={panel.save_button.text()!r}"
            )

            old_rt = window.bridge._runtime
            QTest.mouseClick(panel.save_button, Qt.MouseButton.LeftButton)
            wait_until(
                app,
                lambda: (
                    window is not None
                    and window.bridge._runtime is not None
                    and window.bridge._runtime is not old_rt
                ),
                desc="W:118 runtime restart",
                deadline=deadline,
            )
            wait_window_ready(app, window, deadline=deadline)

            saved = load_settings(cfg)
            assert saved.routines.enabled is True, (
                f"saved.routines.enabled={saved.routines.enabled}"
            )
            assert saved.voice.wake_word == "helena", f"saved.wake_word={saved.voice.wake_word!r}"
            assert window.bridge._runtime is not None, "yeni runtime yok"
            assert window.bridge._runtime.settings.routines.enabled is True, (
                f"rt.routines={window.bridge._runtime.settings.routines.enabled}"
            )
            assert window.bridge._runtime.settings.voice.wake_word == "helena", (
                f"rt.wake_word={window.bridge._runtime.settings.voice.wake_word!r}"
            )
            assert "routine_list" in window.bridge._runtime.registry, (
                "yeniden kurulan runtime'da routine_list yok"
            )
            assert not panel.has_unsaved, "Kaydet sonrası has_unsaved False olmalıydı"

            shot = grab_neutralized(app, window, out_dir, "W118_settings_saved.png")
            return RowResult(
                "W:118",
                "PASS",
                "Ses ve Rutinler değişiklikleri Kaydet ile diske yazıldı ve çalışma "
                "zamanı sekmeden çıkmadan yeni ayarlarla yeniden kuruldu.",
                (shot.name,),
            )
    except GlobalTimeoutError:
        raise
    except _CHECK_ERRORS as error:
        return RowResult("W:118", "FAIL", f"{type(error).__name__}: {error}")
    finally:
        if window is not None:
            close_window_cleanly(app, window)


def run_panel_checks(
    app: QApplication,
    tmp_root: Path,
    out_dir: Path,
    *,
    deadline: float | None = None,
) -> list[RowResult]:
    """Masaüstü panel ve ayar kabul senaryolarını izole geçici klasörlerde çalıştırır."""
    results: list[RowResult] = []
    results.append(_check_y59(app, tmp_root, out_dir, deadline=deadline))
    assert_no_leaked_windows(app, "Y:59 sonrası", max_visible=2, max_desktop=0)

    results.extend(_check_y61(app, tmp_root, out_dir, deadline=deadline))
    assert_no_leaked_windows(app, "Y:61 sonrası", max_visible=2, max_desktop=0)

    results.extend(_check_y94_y95(app, tmp_root, out_dir, deadline=deadline))
    assert_no_leaked_windows(app, "Y:94/Y:95 sonrası", max_visible=2, max_desktop=0)

    for label, check_fn in (
        ("Y:100", _check_y100),
        ("R:26", _check_r26),
        ("R:72", _check_r72),
        ("R:73", _check_r73),
        ("W:118", _check_w118),
    ):
        results.append(check_fn(app, tmp_root, out_dir, deadline=deadline))
        assert_no_leaked_windows(app, f"{label} sonrası", max_visible=2, max_desktop=0)
    return results
