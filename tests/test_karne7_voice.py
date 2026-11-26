"""Karne 7 ses grubu: beklenmeyen hata türleri sessiz kalmaz, mikrofon kesintisi bildirilir."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from artemis.channels.desktop.bridge import DesktopBridge
from artemis.channels.desktop.bridge_events import State
from artemis.channels.voice import adapters
from artemis.channels.voice.adapters import SoundDeviceMicrophone
from artemis.channels.voice.model_health import _check_piper_pair
from artemis.config.settings import VoiceConfig
from tests.test_voice_speculative import NORMAL, SlowRecognizer, _controller

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


# --- G1: sabit özet yoksa Piper dosyaları okunup özetlenmez --------------------------


def test_piper_pair_without_pin_does_not_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    onnx = tmp_path / "v.onnx"
    cfg = tmp_path / "v.onnx.json"
    onnx.write_bytes(b"x" * 10)
    cfg.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        "artemis.channels.voice.model_health._sha256_file",
        lambda _p: (_ for _ in ()).throw(AssertionError("özet hesaplanmamalı")),
    )
    status, integrity, _msg, size = _check_piper_pair(
        onnx, cfg, expected_onnx_sha256=None, expected_json_sha256=None
    )
    assert (status, integrity) == ("ok", "ok")
    assert size == 12


# --- VS1: beklenmeyen tür de failed sinyaliyle gelir, durum etiketi takılmaz ----------


def test_model_op_thread_reports_unexpected_exception_type() -> None:
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from artemis.channels.desktop.voice_model_manager import _ModelOpThread

    app = QApplication.instance() or QApplication([])
    failed: list[tuple[int, str, str]] = []
    done: list[object] = []

    def work() -> tuple[list[object], str]:
        raise KeyError("beklenmeyen")

    thread = _ModelOpThread(3, "refresh", work)  # type: ignore[arg-type]
    thread.failed.connect(lambda g, op, msg: failed.append((g, op, msg)))
    thread.completed.connect(lambda *a: done.append(a))
    thread.start()
    assert thread.wait(3000)
    app.processEvents()
    assert len(failed) == 1 and failed[0][:2] == (3, "refresh")
    assert done == []


# --- VS2: ses oturumu beklenmeyen türle biterse kullanıcı hata görür ------------------


async def test_run_voice_reports_unexpected_exception(tmp_path: Path) -> None:
    bridge = DesktopBridge(tmp_path / "unused.yaml", lambda *_a, **_k: None)
    try:
        class Controller:
            async def run(self) -> None:
                raise KeyError("beklenmeyen")

        await bridge._run_voice(Controller())  # type: ignore[arg-type]
        states = [e.state for e in bridge.poll_events() if isinstance(e, State)]
        assert "voice_error" in states
        assert states.index("voice_error") < len(states) - 1 or states[-1] == "voice_off"
    finally:
        bridge.shutdown()


# --- M1/M2: mikrofon geri çağrısı durursa oturum hatayla biter, iptal takılmaz -------


def _mic_with_fake_stream(monkeypatch: pytest.MonkeyPatch) -> SoundDeviceMicrophone:
    class Stream:
        def __init__(self, **_kw: object) -> None:
            pass

        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

        def close(self) -> None:
            pass

    fake = SimpleNamespace(RawInputStream=Stream, PortAudioError=OSError)
    monkeypatch.setitem(sys.modules, "sounddevice", fake)
    monkeypatch.setattr(adapters, "_STALL_POLL_SECONDS", 0.02)
    monkeypatch.setattr(adapters, "_STALL_SECONDS", 0.1)
    return SoundDeviceMicrophone(VoiceConfig(queue_frames=2))


async def test_microphone_stall_raises_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
    mic = _mic_with_fake_stream(monkeypatch)
    frames = mic.frames()
    with pytest.raises(OSError, match="Mikrofon sinyali kesildi"):
        await asyncio.wait_for(anext(frames), 3)
    await mic.close()


async def test_microphone_paused_but_alive_does_not_stall(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mic = _mic_with_fake_stream(monkeypatch)
    mic.pause()

    async def feed() -> None:
        for _ in range(15):  # duraklatılmışken de geri çağrı sürer (kareler atılır)
            mic._callback(b"x", 1, None, None)
            await asyncio.sleep(0.02)
        mic.resume()
        mic._callback(b"ok", 1, None, None)

    feeder = asyncio.create_task(feed())
    frame = await asyncio.wait_for(anext(mic.frames()), 3)
    assert frame.pcm == b"ok"
    await feeder
    await mic.close()


async def test_microphone_cancel_does_not_hang(monkeypatch: pytest.MonkeyPatch) -> None:
    mic = _mic_with_fake_stream(monkeypatch)
    monkeypatch.setattr(adapters, "_STALL_SECONDS", 60.0)
    task = asyncio.create_task(anext(mic.frames()))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await mic.close()


# --- W1: uyandırma ön-kapısı beklenmeyen türle ölürse kapatılır -----------------------


async def test_wake_spotter_disabled_on_unexpected_exception() -> None:
    class Spotter:
        def feed(self, *_a: object, **_k: object) -> bool:
            raise KeyError("beklenmeyen")

        def close(self) -> None:
            pass

    controller, _runner, _t = _controller(NORMAL, SlowRecognizer(["artemis x"]))
    controller._wake_spotter = Spotter()  # type: ignore[assignment]
    states: list[str] = []
    controller.on_state = states.append
    task: asyncio.Task[bool] = asyncio.create_task(asyncio.to_thread(Spotter().feed, b""))
    await asyncio.wait([task])
    controller._wake_task = task
    controller._wake_finished(task, timestamp=0.0, epoch=controller._capture_epoch)
    assert controller._wake_spotter is None
    assert "wake:unavailable:runtime" in states


# --- G3: ayrıntı anahtarı PortAudio'yu yeniden sorgulamaz ---------------------------


def test_device_toggle_reuses_query_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from artemis.channels.desktop.settings_panel import SettingsPanel

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
    from artemis.channels.voice import stt_catalog

    monkeypatch.setattr(stt_catalog, "installed_path", lambda _n: None)
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({}), encoding="utf-8")
    panel = SettingsPanel(config)
    try:
        assert queries == ["d"]
        panel.advanced_devices.setChecked(True)
        panel.advanced_devices.setChecked(False)
        app.processEvents()
        assert queries == ["d"]
    finally:
        panel.finish_inventory()


# --- Karne 7 CI: panel kapanmadan silinirse çalışan model işi süreci çökertmez ----------


def test_deleting_widget_with_running_model_thread_does_not_abort(tmp_path: Path) -> None:
    pytest.importorskip("PySide6")
    import threading

    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QApplication

    from artemis.channels.desktop.voice_model_manager import VoiceModelManagerWidget

    app = QApplication.instance() or QApplication([])
    release = threading.Event()

    def slow_inventory(_path: Path) -> list[object]:
        release.wait(5)
        return []

    widget = VoiceModelManagerWidget(
        tmp_path / "config.yaml",
        inventory_fn=slow_inventory,  # type: ignore[arg-type]
        verify_fn=lambda *_a: None,  # type: ignore[arg-type]
        download_fn=lambda *_a: None,  # type: ignore[arg-type]
        clean_fn=lambda *_a: None,  # type: ignore[arg-type]
    )
    thread = None
    try:
        assert widget.trigger_refresh(quiet=True)
        thread = widget._thread
        assert thread is not None and thread.isRunning()
        # Panel close olmadan yıkılır (ebeveyn pencere silinmesi gibi); eskiden burada
        # "QThread: Destroyed while thread is still running" ile süreç düşüyordu.
        widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    finally:
        release.set()
    assert thread is not None and thread.wait(3000)
    app.processEvents()
