"""Karne 8 kod bulguları: iş parçacığı ömrü, sınır yakalamaları, kapanış bütçesi."""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def test_start_unparented_survives_parent_deletion_and_releases() -> None:
    pytest.importorskip("PySide6")
    from PySide6.QtCore import QCoreApplication, QEvent, QObject, QThread
    from PySide6.QtWidgets import QApplication

    from artemis.channels.desktop import thread_shutdown

    app = QApplication.instance() or QApplication([])
    release = threading.Event()

    class Blocking(QThread):
        def run(self) -> None:
            release.wait(5)

    parent = QObject()
    thread = Blocking(parent)
    try:
        thread_shutdown.start_unparented(thread)
        assert thread.parent() is None and thread in thread_shutdown._UNPARENTED
        # Eski ebeveyn çalışırken silinir; ebeveynsiz iş parçacığı süreci düşürmez.
        parent.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        assert thread.isRunning()
    finally:
        release.set()
    assert thread.wait(3000)
    for _ in range(50):
        app.processEvents()
        if thread not in thread_shutdown._UNPARENTED:
            break
    assert thread not in thread_shutdown._UNPARENTED


def test_deleting_calendar_section_with_running_check_does_not_abort(tmp_path: Path) -> None:
    pytest.importorskip("PySide6")
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QApplication, QVBoxLayout

    from artemis.channels.desktop.calendar_mail_section import CalendarMailSection

    app = QApplication.instance() or QApplication([])
    release = threading.Event()

    def slow_check() -> str | None:
        release.wait(5)
        return None

    section = CalendarMailSection(
        tmp_path / "config.yaml", calendar=QVBoxLayout(), mail=QVBoxLayout()
    )
    thread = None
    try:
        section._start(slow_check, "gizli", "hedef", "Takvim")
        thread = section._job
        assert thread is not None and thread.isRunning()
        # Panel iş sürerken yıkılır; eskiden "Destroyed while thread is still running" ile düşerdi.
        section.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    finally:
        release.set()
    assert thread is not None and thread.wait(3000)
    app.processEvents()


# --- K3: GpuProbe beklenmeyen hatada bile sinyalleri yayar ---------------------------------


def test_gpu_probe_unexpected_errors_still_emit_scanned_devices_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("PySide6")
    from artemis.channels.desktop import voice_settings_section as vss
    from artemis.channels.voice import stt_catalog

    def boom(*_a: object) -> None:
        raise KeyError("beklenmeyen")

    monkeypatch.setattr(stt_catalog, "installed_path", boom)
    monkeypatch.setattr(stt_catalog, "gpu_memory_mb", boom)
    monkeypatch.setattr(vss, "query_device_snapshot", boom)
    probe = vss.GpuProbe()
    got: dict[str, list[object]] = {"scanned": [], "devices": [], "done": []}
    probe.scanned.connect(got["scanned"].append)
    probe.devices.connect(got["devices"].append)
    probe.done.connect(got["done"].append)
    probe.run()
    assert got["scanned"] == [{c.name: None for c in stt_catalog.CATALOG}]
    assert got["devices"] == [None] and got["done"] == [None]


# --- K4: ses başlatma sınırı beklenmeyen türü de kullanıcıya bildirir ----------------------


def test_voice_start_unexpected_error_reports_voice_error_and_off(tmp_path: Path) -> None:
    from test_desktop_bridge import _collect_until, _fixture, _has_state_message

    from artemis.channels.desktop.bridge import State

    async def broken(_runtime: object, _run: object, _on_state: object) -> None:
        raise KeyError("synthetic-secret-detail")

    bridge = _fixture(tmp_path, [], voice_factory=broken)
    try:
        assert bridge.start_voice()
        events = _collect_until(bridge, lambda items: _has_state_message(items, "voice", "off"))
        errors = [e for e in events if isinstance(e, State) and e.state == "voice_error"]
        assert len(errors) == 1
        assert "synthetic-secret-detail" not in errors[0].message
    finally:
        bridge.shutdown()
        assert bridge._stopped.wait(2)


def test_voice_start_future_exception_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    from concurrent.futures import Future

    from artemis.channels.desktop.bridge import _log_voice_start_failure

    future: Future[None] = Future()
    future.set_exception(KeyError("x"))
    with caplog.at_level("WARNING"):
        _log_voice_start_failure(future)
    assert "KeyError" in caplog.text
    cancelled: Future[None] = Future()
    cancelled.cancel()
    _log_voice_start_failure(cancelled)  # iptal hata değildir


# --- K5 + K6: panel açılışı PortAudio/HF aramasını GUI'de yapmaz ---------------------------


def test_panel_open_defers_device_query_to_probe_and_fills_on_arrival(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("PySide6")
    import sys
    from types import SimpleNamespace

    import yaml
    from PySide6.QtWidgets import QApplication

    from artemis.channels.desktop import voice_settings_section as vss
    from artemis.channels.desktop.settings_panel import SettingsPanel
    from artemis.channels.voice import stt_catalog

    app = QApplication.instance() or QApplication([])
    queries: list[str] = []
    fake = SimpleNamespace(
        PortAudioError=OSError,
        default=SimpleNamespace(device=(0, 0)),
        query_devices=lambda: queries.append("d") or [
            {"name": "Mic", "max_input_channels": 1, "hostapi": 0}
        ],
        query_hostapis=lambda: [{"name": "MME"}],
    )
    monkeypatch.setitem(sys.modules, "sounddevice", fake)
    monkeypatch.setattr(stt_catalog, "installed_path", lambda _n: None)
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({}), encoding="utf-8")
    # Conftest gerçek işçiyi kapatır; "işçi bekleniyor" durumunu işaretçiyle taklit ederiz.
    monkeypatch.setattr(
        vss, "start_gpu_probe", lambda panel: setattr(panel, "gpu_probe", vss.GpuProbe())
    )
    panel = SettingsPanel(config)
    try:
        assert queries == []  # GUI iş parçacığı PortAudio'ya dokunmadı
        assert not panel.device_combo.isEnabled()
        snapshot = vss.query_device_snapshot()  # işçinin yapacağı iş (sahte sounddevice)
        assert queries == ["d"]
        vss.device_scan_done(panel, snapshot)
        assert queries == ["d"]  # sonuç gelince yeniden sorgulanmaz
        assert panel.device_combo.isEnabled()
        items = [panel.device_combo.itemText(i) for i in range(panel.device_combo.count())]
        assert any("Mic" in item for item in items)
        assert not panel._dirty
        vss.device_scan_done(panel, None)  # işçi başarısız: liste boş ve kapalı
        assert panel.device_combo.itemText(0) == "Mikrofon bulunamadı"
    finally:
        panel.gpu_probe = None
        panel.finish_inventory()
        app.processEvents()


def test_stt_paths_never_scan_on_gui_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("PySide6")
    import yaml
    from PySide6.QtWidgets import QApplication

    from artemis.channels.desktop import voice_settings_section as vss
    from artemis.channels.desktop.settings_panel import SettingsPanel
    from artemis.channels.voice import stt_catalog

    QApplication.instance() or QApplication([])
    calls: list[str] = []
    monkeypatch.setattr(stt_catalog, "installed_path", lambda n: calls.append(n))
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({}), encoding="utf-8")
    panel = SettingsPanel(config)
    try:
        panel._populate_stt_choices("small")
        assert panel.stt_model_path() is None  # tarama bitmedi: Kaydet yolu değiştirmez
        panel._download_stt()  # tarama bitmedi: indirme başlamaz
        assert panel.stt_download_thread is None
        assert calls == []
        vss.stt_scan_done(panel, {c.name: tmp_path for c in stt_catalog.CATALOG})
        assert panel.stt_model_path() == tmp_path
        assert calls == []
    finally:
        panel.finish_inventory()


# --- K7: kapanışta toplam bekleme sınırlıdır ----------------------------------------------


def test_shared_budget_bounds_total_wait_for_stuck_threads() -> None:
    import time

    from artemis.channels.desktop import thread_shutdown as ts

    class Stuck:
        def __init__(self) -> None:
            self.interrupted = False
            self.finished = type("S", (), {"connect": lambda *_a: None})()

        def isRunning(self) -> bool:  # noqa: N802 - Qt API adı
            return True

        def isFinished(self) -> bool:  # noqa: N802
            return False

        def requestInterruption(self) -> None:  # noqa: N802
            self.interrupted = True

        def wait(self, ms: int) -> bool:
            time.sleep(ms / 1000)
            return False

        def setParent(self, _p: object) -> None:  # noqa: N802
            pass

    threads = [Stuck() for _ in range(5)]
    before = len(ts.DETACHED)
    try:
        ts.interrupt_threads(*threads)  # type: ignore[arg-type]
        assert all(th.interrupted for th in threads)
        started = time.monotonic()
        with ts.shared_budget(200):
            results = [ts.shutdown_thread(th) for th in threads]  # type: ignore[arg-type]
        elapsed = time.monotonic() - started
        assert results == [True] * 5  # hepsi ayrıldı
        assert elapsed < 1.5  # 5 x 6 sn yerine ~0.2 sn
    finally:
        del ts.DETACHED[before:]


# --- K8: eski iş parçacığının geç finished'ı yeni işi silmez --------------------------------


def test_stale_task_finished_does_not_clear_new_thread(tmp_path: Path) -> None:
    pytest.importorskip("PySide6")
    from PySide6.QtCore import QThread
    from PySide6.QtWidgets import QApplication

    from artemis.channels.desktop.voice_model_manager import VoiceModelManagerWidget

    QApplication.instance() or QApplication([])
    widget = VoiceModelManagerWidget(
        tmp_path / "config.yaml",
        defer_load=True,
        inventory_fn=lambda _p: [],
        verify_fn=lambda *_a: None,  # type: ignore[arg-type]
        download_fn=lambda *_a: None,  # type: ignore[arg-type]
        clean_fn=lambda *_a: None,  # type: ignore[arg-type]
    )
    old, new = QThread(), QThread()
    widget._thread = new  # type: ignore[assignment]
    widget.sender = lambda: old  # type: ignore[method-assign]
    widget._on_task_finished()
    assert widget._thread is new
    widget.sender = lambda: new  # type: ignore[method-assign]
    widget._on_task_finished()
    assert widget._thread is None


# --- K9: BaseException yolunda da failed yayılır -------------------------------------------


def test_model_op_thread_reports_failed_on_base_exception() -> None:
    pytest.importorskip("PySide6")
    from artemis.channels.desktop.voice_model_manager import _ModelOpThread

    def work() -> tuple[list[object], str]:
        raise KeyboardInterrupt

    thread = _ModelOpThread(3, "load", work)  # type: ignore[arg-type]
    failed: list[tuple[int, str, str]] = []
    thread.failed.connect(lambda g, o, m: failed.append((g, o, m)))
    with pytest.raises(KeyboardInterrupt):
        thread.run()
    assert failed == [(3, "load", "KeyboardInterrupt")]


# --- K10: kapatılan ön-kapı/canlı yazı close() ile geçici dosyayı bırakır ------------------


async def test_failed_wake_spotter_and_live_are_closed() -> None:
    import asyncio

    from tests.test_voice_speculative import NORMAL, SlowRecognizer, _controller

    class Closable:
        closed = False

        def close(self) -> None:
            self.closed = True

    async def fail() -> bool:
        raise KeyError("x")

    controller, _runner, _t = _controller(NORMAL, SlowRecognizer(["artemis x"]))
    spotter, live = Closable(), Closable()
    controller._wake_spotter = spotter  # type: ignore[assignment]
    controller._live = live  # type: ignore[assignment]
    wake: asyncio.Task[bool] = asyncio.create_task(fail())
    live_task: asyncio.Task[bool] = asyncio.create_task(fail())
    await asyncio.wait([wake, live_task])
    controller._wake_finished(wake, timestamp=0.0, epoch=controller._capture_epoch)
    controller._live_finished(live_task)  # type: ignore[arg-type]
    assert spotter.closed and live.closed
    assert controller._wake_spotter is None and controller._live is None


# --- K12: yalnız bilinen temiz yer tutucular lekesizdir ------------------------------------


@pytest.mark.parametrize(
    ("template", "external"),
    [
        ("{title} notları", True),
        ("{app} açıldı", True),
        ("Özetle: {file}", True),
        ("{url} aç", True),  # gelecekte eklenecek bilinmeyen yer tutucu
        ("{name}", False),
        ("Rutin bitti: {source}", False),
        ("{name} / {source} / {minutes} dk", False),
        ("{name} ve {path}", True),
        ("düz metin", False),
        ("", False),
        (None, False),
    ],
)
def test_suggest_uses_external_inverted_allowlist(template: str | None, external: bool) -> None:
    from artemis.proactive.triggers import suggest_uses_external

    assert suggest_uses_external(template) is external
