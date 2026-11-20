"""Tetikleyici ve yerel dosya araması masaüstü kabul denetimleri (Y:106, Y:107, Y:175)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QCoreApplication, QEvent, Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox, QPushButton

from artemis.channels.desktop.settings_io import save_triggers
from artemis.channels.desktop.trigger_dialog import TriggerDialog, validate_trigger
from artemis.channels.desktop.triggers_section import TriggersSection
from artemis.config.settings import load_settings
from scripts.kabul_masaustu_checks.common import (
    DEMO_DOCS_DIR,
    DEMO_DOWNLOADS_DIR,
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

if TYPE_CHECKING:
    from artemis.channels.desktop.app import DesktopWindow

_TRIGGER_ROW_ERRORS = (AssertionError, OSError, RuntimeError, TimeoutError, ValueError)


def _drive_modal(
    app: QApplication,
    trigger_click: Callable[[], None],
    handler: Callable[[QDialog | QMessageBox], None],
) -> None:
    """Modal diyalogu QTimer ile sürer; hata olursa diyalogu kapatıp kilitlenmeyi önler."""
    modal_errors: list[BaseException] = []

    def _on_modal() -> None:
        modal = QApplication.activeModalWidget()
        try:
            if not isinstance(modal, (QDialog, QMessageBox)):
                raise AssertionError(f"Beklenen modal pencere açılmadı: {modal!r}")
            handler(modal)
        except _TRIGGER_ROW_ERRORS as error:
            modal_errors.append(error)
        finally:
            app.processEvents()
            # Doğrulama başarısızlığında veya hata durumunda modal açık kalıp exec()'i kilitlemesin.
            if isinstance(modal, (QDialog, QMessageBox)) and modal.isVisible():
                modal.reject()

    QTimer.singleShot(80, _on_modal)
    trigger_click()
    app.processEvents()
    if modal_errors:
        raise modal_errors[0]


def _first_row_button(section: TriggersSection, caption: str) -> QPushButton:
    """Tetikleyici listesinin ilk satırından istenen düğmeyi döndürür."""
    item = section.trigger_list.item(0)
    if item is None:
        raise AssertionError("Tetikleyici listesi ilk satırı bulunamadı.")
    row_widget = section.trigger_list.itemWidget(item)
    if row_widget is None:
        raise AssertionError("Tetikleyici ilk satır widget'ı bulunamadı.")
    for button in row_widget.findChildren(QPushButton):
        if button.text() == caption:
            return button
    raise AssertionError(f"Satırda {caption!r} düğmesi bulunamadı.")


def _run_y106(
    app: QApplication,
    window: DesktopWindow,
    cfg_path: Path,
    temp_downloads: Path,
    out_dir: Path,
    *,
    deadline: float | None = None,
) -> RowResult:
    """Y:106 — Tetikleyici ekleme, otomatik açılma ve Kaydet öncesi yazmama denetimi."""
    check_deadline(deadline, "Y:106")
    select_settings_section(app, window, "Tetikleyiciler")
    panel = window.settings_panel
    assert not panel.triggers_enabled.isChecked()
    assert not panel.proactive_enabled.isChecked()
    assert panel.triggers.trigger_area.empty.title_label.text() == "Tetikleyiciler kapalı"

    empty_action = panel.triggers.trigger_area.empty.action_button
    assert empty_action is not None

    def _fill_add_dialog(modal: QDialog | QMessageBox) -> None:
        assert isinstance(modal, TriggerDialog)
        # Gerçek İndirilenler klasörüne dokunmamak için hızlı düğme geçici klasöre yönlendirilir.
        modal.quick_buttons[0].setProperty("folder", str(temp_downloads))
        modal.quick_buttons[0].setEnabled(True)
        QTest.mouseClick(modal.quick_buttons[0], Qt.MouseButton.LeftButton)
        app.processEvents()
        assert modal.name.text() == "Indirilenler"
        modal.kind.setCurrentIndex(1)
        app.processEvents()
        grab_neutralized(
            app,
            modal,
            out_dir,
            "Y106_trigger_dialog.png",
            real_roots={temp_downloads: DEMO_DOWNLOADS_DIR},
        )
        QTest.mouseClick(modal.ok_button, Qt.MouseButton.LeftButton)

    _drive_modal(
        app,
        lambda: QTest.mouseClick(empty_action, Qt.MouseButton.LeftButton),
        _fill_add_dialog,
    )

    assert panel.triggers.trigger_list.count() == 1
    assert panel.triggers_enabled.isChecked() is True
    assert "açıldı" in panel.triggers.notice.text()
    assert panel.triggers.warning_row.isVisible()
    assert panel.triggers.proactive_button.isVisible()

    before_bytes = cfg_path.read_bytes()
    QTest.mouseClick(panel.triggers.proactive_button, Qt.MouseButton.LeftButton)
    app.processEvents()
    assert panel.proactive_enabled.isChecked() is True
    assert panel.has_unsaved is True
    # "Zamanlı işleri aç" yalnızca kutuyu işaretler; Kaydet'e basılmadan diske yazmaz.
    assert cfg_path.read_bytes() == before_bytes

    grab_neutralized(
        app,
        window,
        out_dir,
        "Y106_triggers_added.png",
        real_roots={temp_downloads: DEMO_DOWNLOADS_DIR},
    )
    QTest.mouseClick(panel.save_button, Qt.MouseButton.LeftButton)
    wait_window_ready(app, window, deadline=deadline)
    assert load_settings(cfg_path).proactive.enabled is True

    return RowResult(
        row_id="Y:106",
        status="PASS",
        observation=(
            "Tetikleyici formu İndirilenler+PDF ile eklendi, triggers.enabled otomatik açıldı, "
            "Zamanlı işleri aç Kaydet öncesi diske yazmadı ve Kaydet sonrası kalıcı oldu."
        ),
        screenshots=("Y106_trigger_dialog.png", "Y106_triggers_added.png"),
    )


def _run_y107(
    app: QApplication,
    window: DesktopWindow,
    cfg_path: Path,
    sub: Path,
    temp_downloads: Path,
    out_dir: Path,
    *,
    deadline: float | None = None,
) -> tuple[RowResult, RowResult]:
    """Y:107 ve Y:107-sil — Düzenleme, silme onayı (Vazgeç), doğrulama ve 10 üst sınırı."""
    check_deadline(deadline, "Y:107")
    panel = window.settings_panel

    # 1. Düzenleme
    edit_btn = _first_row_button(panel.triggers, "Düzenle")

    def _edit_dialog(modal: QDialog | QMessageBox) -> None:
        assert isinstance(modal, TriggerDialog)
        modal.message.setText("Gelen PDF: {file}")
        QTest.mouseClick(modal.ok_button, Qt.MouseButton.LeftButton)

    _drive_modal(
        app,
        lambda: QTest.mouseClick(edit_btn, Qt.MouseButton.LeftButton),
        _edit_dialog,
    )
    assert load_settings(cfg_path).triggers.items[0].message == "Gelen PDF: {file}"

    # 2. Silme onayı (ASLA onaylama: varsayılan Vazgeç ile kapat)
    delete_btn = _first_row_button(panel.triggers, "Sil")

    def _cancel_delete_box(modal: QDialog | QMessageBox) -> None:
        assert isinstance(modal, QMessageBox)
        default_btn = modal.defaultButton()
        escape_btn = modal.escapeButton()
        assert default_btn is not None and default_btn.text() == "Vazgeç"
        assert escape_btn is not None and escape_btn.text() == "Vazgeç"
        grab_neutralized(app, modal, out_dir, "Y107_trigger_delete_confirm.png")
        QTest.mouseClick(default_btn, Qt.MouseButton.LeftButton)

    _drive_modal(
        app,
        lambda: QTest.mouseClick(delete_btn, Qt.MouseButton.LeftButton),
        _cancel_delete_box,
    )
    assert "Indirilenler" in [item.name for item in load_settings(cfg_path).triggers.items]

    # 3. Geçersiz klasör + yinelenen ad doğrulama hataları
    validation_dialog = TriggerDialog(
        None,
        list(load_settings(cfg_path).triggers.items),
        [],
        "",
        parent=window,
    )
    try:
        validation_dialog.show()
        app.processEvents()
        validation_dialog.name.setText("Indirilenler")
        validation_dialog.folder.setText(str(sub / "olmayan_klasor"))
        QTest.mouseClick(validation_dialog.ok_button, Qt.MouseButton.LeftButton)
        app.processEvents()

        assert validation_dialog.result_trigger is None
        folder_err = validation_dialog._errors["folder"]
        name_err = validation_dialog._errors["name"]
        assert not folder_err.isHidden()
        assert folder_err.text() == "Klasör bulunamadı."
        assert not name_err.isHidden()
        assert name_err.text() == "Bu ad zaten kullanılıyor."
        assert "Traceback" not in folder_err.text()
        assert "validation" not in folder_err.text().casefold()
        assert "Traceback" not in name_err.text()
        assert "validation" not in name_err.text().casefold()

        grab_neutralized(
            app,
            validation_dialog,
            out_dir,
            "Y107_trigger_validation.png",
            real_roots={sub: DEMO_DOWNLOADS_DIR},
        )
    finally:
        validation_dialog.hide()
        validation_dialog.close()
        validation_dialog.deleteLater()
        app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    # 4. 10 tetikleyici üst sınırı
    ten_items = [{"name": f"t{idx}", "folder": str(temp_downloads)} for idx in range(10)]
    save_triggers(cfg_path, ten_items, enable=True)
    panel.triggers.refresh()
    app.processEvents()

    assert panel.triggers.trigger_list.count() == 10
    assert not panel.triggers.add_button.isEnabled()
    panel.triggers._add()
    assert "En çok 10 tetikleyici olabilir." in panel.triggers.notice.text()

    cap_item, cap_errors = validate_trigger(
        name="t10",
        folder=str(temp_downloads),
        pattern="*",
        message="Yeni: {file}",
        suggest=None,
        routine=None,
        taken=[f"t{idx}" for idx in range(10)],
        count=10,
    )
    assert cap_item is None
    assert cap_errors.get("name") == "En çok 10 tetikleyici olabilir."

    grab_neutralized(
        app,
        window,
        out_dir,
        "Y107_trigger_max10.png",
        real_roots={temp_downloads: DEMO_DOWNLOADS_DIR},
    )

    row_107 = RowResult(
        row_id="Y:107",
        status="PASS",
        observation=(
            "Tetikleyici düzenlendi, olmayan klasör ve yinelenen ad Türkçe hata verdi, "
            "10 tetikleyici üst sınırı hem arayüzde hem validate_trigger ile doğrulandı."
        ),
        screenshots=("Y107_trigger_validation.png", "Y107_trigger_max10.png"),
    )
    row_107_del = RowResult(
        row_id="Y:107-sil",
        status="SKIPPED",
        observation=(
            "Tetikleyici Sil onay penceresi açıldı ve varsayılan Vazgeç ile kapatıldı; "
            "silme onayı insan adımına bırakıldı"
        ),
        screenshots=("Y107_trigger_delete_confirm.png",),
    )
    return row_107, row_107_del


def _run_y175(
    app: QApplication,
    tmp_root: Path,
    out_dir: Path,
    *,
    deadline: float | None = None,
) -> RowResult:
    """Y:175 — Dosya araması yeniden dizinlemesi sırasında arayüzün yanıt vermesi."""
    check_deadline(deadline, "Y:175")
    sub = tmp_root / "y175"
    docs_dir = sub / "belgeler"
    docs_dir.mkdir(parents=True, exist_ok=True)
    # Dizinlemenin "dizinleniyor" durumunu yakalayacak kadar sürmesi için ~250 x ~4 KB dosya.
    body_block = "Artemis yerel dosya araması deneme metni. " * 95
    for idx in range(250):
        (docs_dir / f"belge_{idx:03d}.txt").write_text(
            f"Belge {idx:03d}\n{body_block}\n",
            encoding="utf-8",
        )

    cfg_path, _, _ = make_temp_config(
        sub,
        overrides={"file_search": {"enabled": True, "roots": [str(docs_dir)]}},
    )
    provider = RecordingProvider()
    with fake_provider_bridge(lambda: provider):
        window = open_real_window(app, cfg_path, deadline=deadline)
        try:
            select_settings_section(app, window, "Dosya araması")
            panel = window.settings_panel
            index = panel.file_search._index
            assert index is not None
            wait_until(
                app,
                lambda: not index.running,
                timeout=30.0,
                desc="ilk dizinleme bitişi",
                deadline=deadline,
            )

            QTest.mouseClick(panel.file_search.clear_button, Qt.MouseButton.LeftButton)
            wait_until(
                app,
                lambda: (
                    panel.file_search._clearing is None
                    and "0 dosya" in panel.file_search.status_label.text()
                ),
                timeout=15.0,
                desc="indeks silme",
                deadline=deadline,
            )

            QTest.mouseClick(panel.file_search.reindex_button, Qt.MouseButton.LeftButton)
            assert "dizinleniyor" in panel.file_search.status_label.text()
            grab_neutralized(
                app,
                window,
                out_dir,
                "Y175_file_search_indexing.png",
                real_roots={docs_dir: DEMO_DOCS_DIR},
            )

            # Arka plan dizinlemesi sürerken ana pencere ve sohbet köprüsü yanıt vermeyi sürdürmeli.
            assert window._submit_text("kısa yoklama")
            wait_until(
                app,
                lambda: not window._busy,
                timeout=15.0,
                desc="sohbet yanıtı",
                deadline=deadline,
            )
            assert len(provider.requests) >= 1

            def _indexing_done() -> bool:
                status = panel.file_search.status_label.text()
                return (
                    "dizinleniyor" not in status
                    and "henüz yok" not in status
                    and "250 dosya" in status
                )

            wait_until(
                app,
                _indexing_done,
                timeout=30.0,
                desc="dosya araması dizinleme bitişi",
                deadline=deadline,
            )
            grab_neutralized(
                app,
                window,
                out_dir,
                "Y175_file_search_done.png",
                real_roots={docs_dir: DEMO_DOCS_DIR},
            )
        finally:
            close_window_cleanly(app, window)

    return RowResult(
        row_id="Y:175",
        status="PASS",
        observation=(
            "250 dosyalık dizinleme sırasında 'dizinleniyor' durumu görüldü, Artemis sohbette "
            "yanıt vermeyi sürdürdü ve sonunda '250 dosya' durumu doğrulandı."
        ),
        screenshots=("Y175_file_search_indexing.png", "Y175_file_search_done.png"),
    )


def run_triggers_and_files_checks(
    app: QApplication,
    tmp_root: Path,
    out_dir: Path,
    *,
    deadline: float | None = None,
) -> list[RowResult]:
    """Y:106, Y:107, Y:107-sil ve Y:175 kabul denetimlerini çalıştırıp sonuçları döndürür."""
    results: list[RowResult] = []

    check_deadline(deadline, "Y:106/Y:107")
    sub_106 = tmp_root / "y106_107"
    temp_downloads = sub_106 / "Indirilenler"
    temp_downloads.mkdir(parents=True, exist_ok=True)
    cfg_106, _, _ = make_temp_config(
        sub_106,
        overrides={"proactive": {"enabled": False}, "triggers": {"enabled": False}},
    )

    y106_result: RowResult | None = None
    y107_done = False
    try:
        with fake_provider_bridge(RecordingProvider):
            window = open_real_window(app, cfg_106, deadline=deadline)
            try:
                y106_result = _run_y106(
                    app, window, cfg_106, temp_downloads, out_dir, deadline=deadline
                )
                results.append(y106_result)
                row_107, row_107_del = _run_y107(
                    app,
                    window,
                    cfg_106,
                    sub_106,
                    temp_downloads,
                    out_dir,
                    deadline=deadline,
                )
                results.extend((row_107, row_107_del))
                y107_done = True
            finally:
                close_window_cleanly(app, window)
    except GlobalTimeoutError:
        raise
    except _TRIGGER_ROW_ERRORS as error:
        if y106_result is None:
            results.append(RowResult("Y:106", "FAIL", f"{type(error).__name__}: {error}"))
        if not y107_done:
            results.append(RowResult("Y:107", "FAIL", f"{type(error).__name__}: {error}"))
            results.append(RowResult("Y:107-sil", "FAIL", f"{type(error).__name__}: {error}"))
    assert_no_leaked_windows(app, "Y:106/Y:107 sonrası", max_visible=2, max_desktop=0)

    try:
        results.append(_run_y175(app, tmp_root, out_dir, deadline=deadline))
    except GlobalTimeoutError:
        raise
    except _TRIGGER_ROW_ERRORS as error:
        results.append(RowResult("Y:175", "FAIL", f"{type(error).__name__}: {error}"))
    assert_no_leaked_windows(app, "Y:175 sonrası", max_visible=2, max_desktop=0)

    return results
