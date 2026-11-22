"""Faz 1 hızlı kazanımlar testleri: ses önizleme, telemetri kartı ve üst çubuk bütçe sayacı."""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from artemis.brain.budget import BudgetLedger
from artemis.brain.fake import ScriptedProvider
from artemis.channels.desktop import bridge as bridge_module
from artemis.channels.desktop import budget_bar, voice_preview
from artemis.channels.desktop.app import build_window
from artemis.channels.desktop.bridge import State
from artemis.channels.desktop.settings_io import save_choices
from artemis.channels.desktop.settings_panel import SettingsPanel
from artemis.channels.desktop.voice_preview import (
    VoicePreviewRequest,
    VoicePreviewThread,
    discover_piper_voices,
    resolve_piper_paths,
    run_voice_preview,
)
from artemis.channels.desktop.voice_telemetry_card import (
    VoiceTelemetryCard,
    summarize_voice_telemetry,
)
from artemis.channels.desktop.window_events import handle_state
from artemis.channels.runtime import create_runtime
from artemis.channels.voice.adapters import PCMChunk
from artemis.channels.voice.metrics import MIN_TARGET_SAMPLES
from artemis.config.settings import load_settings
from tests.test_desktop_gui import _close_cleanly
from tests.test_settings_panel import config


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])  # type: ignore[return-value]


def test_voice_selectors_round_trip_and_consent_visibility(
    tmp_path: Path, qt_app: QApplication
) -> None:
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    onnx = models_dir / "tr_TR-custom.onnx"
    onnx.write_bytes(b"fake-onnx")
    onnx_json = Path(f"{onnx}.json")
    onnx_json.write_text("{}", encoding="utf-8")

    path = config(tmp_path, cloud=True)
    discovered = discover_piper_voices(path, onnx)
    assert discovered[0] == "tr_TR-custom"
    assert resolve_piper_paths(path, "tr_TR-custom", None, None) == (onnx, onnx_json)

    panel = SettingsPanel(path)
    try:
        panel.select_section("Yanıt (Çıkış)")
        panel.show()

        panel.tts_combo.setCurrentIndex(panel.tts_combo.findData("piper"))
        assert not panel.tts_cloud_consent.isVisible()
        assert panel.tts_piper_box.isVisible()
        assert not panel.tts_edge_box.isVisible()

        panel.tts_combo.setCurrentIndex(panel.tts_combo.findData("edge"))
        assert panel.tts_cloud_consent.isVisible()
        assert panel.tts_edge_box.isVisible()
        assert not panel.tts_cloud_box.isVisible()

        panel.tts_combo.setCurrentIndex(panel.tts_combo.findData("cloud"))
        assert panel.tts_cloud_consent.isVisible()
        assert panel.tts_cloud_box.isVisible()

        panel.tts_combo.setCurrentIndex(panel.tts_combo.findData("edge"))
        panel.edge_voice_combo.setEditText("tr-TR-AhmetNeural")
        panel.piper_voice_combo.setEditText("tr_TR-custom")
        save_choices(path, None, values=panel.values())
        loaded = load_settings(path)
        assert loaded.voice.edge_voice == "tr-TR-AhmetNeural"
        assert loaded.voice.piper_voice == "tr_TR-custom"
    finally:
        panel.deleteLater()
        qt_app.processEvents()


def test_voice_preview_refuses_without_consent_and_runs_with_mock_sink(
    tmp_path: Path, qt_app: QApplication
) -> None:
    path = config(tmp_path)
    req_no_consent = VoicePreviewRequest(
        backend="edge",
        edge_voice="tr-TR-EmelNeural",
        piper_voice="",
        piper_model=None,
        piper_config=None,
        tts_cloud_endpoint="",
        tts_cloud_protocol="openai",
        tts_cloud_model="",
        tts_cloud_voice="",
        tts_cloud_sample_rate=24000,
        tts_cloud_allow_paid=False,
        allow_cloud_tts_text=False,
        ffmpeg="ffmpeg",
        sample_text="Merhaba.",
    )
    with pytest.raises(ValueError, match="Metin gönderme izni"):
        asyncio.run(run_voice_preview(req_no_consent, None, tmp_path / "audit.jsonl"))

    # Mocked synthesizer + sink (ağ/ses aygıtı yok).
    played: list[bytes] = []

    class _FakeSynth:
        async def stream(self, text: str, cancelled: asyncio.Event):  # type: ignore[no-untyped-def]
            assert text == "Merhaba."
            yield PCMChunk(b"\x01\x02" * 4, 24000, 1, 2)

        async def close(self) -> None:
            pass

    class _FakeSink:
        async def play(
            self, chunks: Any, cancelled: asyncio.Event, on_first_pcm: Any = None
        ) -> None:
            async for chunk in chunks:
                played.append(chunk.pcm)
                if callable(on_first_pcm):
                    on_first_pcm()

        async def close(self) -> None:
            pass

    req_ok = VoicePreviewRequest(
        backend="edge",
        edge_voice="tr-TR-EmelNeural",
        piper_voice="",
        piper_model=None,
        piper_config=None,
        tts_cloud_endpoint="",
        tts_cloud_protocol="openai",
        tts_cloud_model="",
        tts_cloud_voice="",
        tts_cloud_sample_rate=24000,
        tts_cloud_allow_paid=False,
        allow_cloud_tts_text=True,
        ffmpeg="ffmpeg",
        sample_text="Merhaba.",
    )
    asyncio.run(
        run_voice_preview(
            req_ok,
            None,
            tmp_path / "audit.jsonl",
            synthesizer_factory=lambda _c, _e, _a: _FakeSynth(),
            sink_factory=_FakeSink,
        )
    )
    assert played == [b"\x01\x02" * 4]

    # Panel butonundan rızasız Edge önizlemesi thread başlatmadan reddedilir.
    panel = SettingsPanel(path)
    try:
        panel.tts_combo.setCurrentIndex(panel.tts_combo.findData("edge"))
        panel.edge_voice_combo.setEditText("tr-TR-EmelNeural")
        panel.tts_cloud_consent.setChecked(False)
        panel._toggle_voice_preview()
        assert panel.tts_preview_thread is None
        assert "Metin gönderme izni kapalı" in panel.tts_preview_status.text()
    finally:
        panel.deleteLater()
        qt_app.processEvents()


def test_voice_preview_finish_inventory_detaches_running_thread(
    tmp_path: Path, qt_app: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered = threading.Event()
    release = threading.Event()

    class _SlowSynth:
        async def stream(self, text: str, cancelled: asyncio.Event):  # type: ignore[no-untyped-def]
            entered.set()
            await asyncio.to_thread(release.wait, 5)
            yield PCMChunk(b"\x00\x00", 24000, 1, 2)

        async def close(self) -> None:
            pass

    class _SlowSink:
        async def play(
            self, chunks: Any, cancelled: asyncio.Event, on_first_pcm: Any = None
        ) -> None:
            async for _chunk in chunks:
                pass

        async def close(self) -> None:
            pass

    monkeypatch.setattr(voice_preview, "_SHUTDOWN_WAIT_MS", 50)
    panel = SettingsPanel(config(tmp_path))
    req = VoicePreviewRequest(
        backend="edge",
        edge_voice="tr-TR-EmelNeural",
        piper_voice="",
        piper_model=None,
        piper_config=None,
        tts_cloud_endpoint="",
        tts_cloud_protocol="openai",
        tts_cloud_model="",
        tts_cloud_voice="",
        tts_cloud_sample_rate=24000,
        tts_cloud_allow_paid=False,
        allow_cloud_tts_text=True,
        ffmpeg="ffmpeg",
        sample_text="Test.",
    )
    thread = VoicePreviewThread(
        req,
        None,
        tmp_path / "audit.jsonl",
        panel,
        synthesizer_factory=lambda _c, _e, _a: _SlowSynth(),
        sink_factory=_SlowSink,
    )
    panel.tts_preview_thread = thread
    thread.done.connect(panel._voice_preview_done)
    thread.failed.connect(panel._voice_preview_failed)
    thread.finished.connect(panel._voice_preview_finished)
    thread.start()
    assert entered.wait(3)

    panel.finish_inventory()
    assert panel.tts_preview_thread is None
    assert thread.parent() is None
    assert thread in voice_preview._DETACHED

    release.set()
    assert thread.wait(3000)
    qt_app.processEvents()
    assert thread not in voice_preview._DETACHED
    panel.deleteLater()
    qt_app.processEvents()


def test_voice_telemetry_summary_and_card_states(tmp_path: Path, qt_app: QApplication) -> None:
    cfg = config(tmp_path)
    missing_dir = tmp_path / "yok"
    assert summarize_voice_telemetry(missing_dir).status == "missing"

    empty_dir = tmp_path / "state" / "voice-telemetry"
    empty_dir.mkdir(parents=True)
    assert summarize_voice_telemetry(empty_dir).status == "empty"

    card = VoiceTelemetryCard(cfg)
    assert "rapor dosyası yok" in card.overview_label.text()

    # Bozuk ve yanlış şemalı dosyalar güvenli atlanır.
    (empty_dir / "bad.json").write_text("{", encoding="utf-8")
    (empty_dir / "v1.json").write_text(json.dumps({"schema_version": 1, "outcomes": {}}), "utf-8")
    corrupt_summary = summarize_voice_telemetry(empty_dir)
    assert corrupt_summary.status == "corrupt" and corrupt_summary.skipped_files == 2
    card.refresh()
    assert "2 dosya atlandı" in card.overview_label.text()

    # İki v2 dosyası: P50/P90 ortalanmaz, ham örneklerden hesaplanır.
    def _utt(uid: int, ms: float) -> dict[str, Any]:
        return {
            "utterance_id": uid,
            "response_kind": "task",
            "outcome": "completed",
            "invalid_timing": False,
            "timings": {"speech_end": 10.0, "first_pcm": 10.0 + ms / 1000.0},
        }

    r1 = {
        "schema_version": 2,
        "total_utterances": 2,
        "samples": 2,
        "invalid_timing": 0,
        "aborted": 0,
        "no_audio": 0,
        "outcomes": {"completed": 2, "rejected": 0},
        "utterances": [_utt(1, 500.0), _utt(2, 700.0)],
    }
    r2_samples = [_utt(i, 1000.0) for i in range(MIN_TARGET_SAMPLES)]
    r2 = {
        "schema_version": 2,
        "total_utterances": MIN_TARGET_SAMPLES,
        "samples": MIN_TARGET_SAMPLES,
        "invalid_timing": 0,
        "aborted": 1,
        "no_audio": 1,
        "outcomes": {"completed": MIN_TARGET_SAMPLES, "rejected": 2},
        "utterances": r2_samples,
    }
    p1 = empty_dir / "voice-1.json"
    p2 = empty_dir / "voice-2.json"
    p1.write_text(json.dumps(r1), encoding="utf-8")
    time.sleep(0.02)
    p2.write_text(json.dumps(r2), encoding="utf-8")

    summary = summarize_voice_telemetry(empty_dir)
    assert summary.status == "ok"
    assert summary.file_count == 2
    assert summary.skipped_files == 2
    assert summary.total_utterances == 2 + MIN_TARGET_SAMPLES
    assert summary.latency_samples == 2 + MIN_TARGET_SAMPLES
    assert summary.p50_ms == pytest.approx(1000.0)
    assert summary.targets_met is True

    card.refresh_button.click()
    assert "Oturumlar: 2" in card.overview_label.text()
    assert "Kalite hedefleri karşılandı" in card.quality_label.text()
    card.deleteLater()
    qt_app.processEvents()


def test_budget_bar_read_only_warning_and_responsive_visibility(
    tmp_path: Path, qt_app: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config(tmp_path)
    raw = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    raw["daily_budget_usd"] = "5.00"
    cfg.write_text(yaml.safe_dump(raw), encoding="utf-8")

    db_path = tmp_path / "state" / "budget.sqlite3"
    snap0 = budget_bar.read_budget_snapshot(db_path, Decimal("5.00"))
    assert snap0.available and snap0.spent == Decimal(0)
    assert not db_path.exists()  # dosya yaratma yan etkisi yok

    ledger = BudgetLedger(db_path, Decimal("5.00"))
    tok = ledger.reserve(Decimal("4.25"))  # %85 -> chipWarning
    snap1 = budget_bar.read_budget_snapshot(db_path, Decimal("5.00"))
    assert budget_bar.format_budget_text(snap1) == "$4.25 / $5.00"

    def factory(config_path: Path, approve: Any, **kwargs: Any) -> Any:
        return create_runtime(
            config_path,
            approve,
            settings=load_settings(config_path),
            provider=ScriptedProvider([]),
            **kwargs,
        )

    monkeypatch.setattr(bridge_module, "create_runtime", factory)
    monkeypatch.setattr(SettingsPanel, "_refresh_devices", lambda *_: None)
    window = build_window(cfg, app=qt_app)
    window.show()
    qt_app.processEvents()
    try:
        assert window.budget_chip.text() == "$4.25 / $5.00"
        assert window.budget_chip.objectName() == "chipWarning"

        ledger.settle(tok, Decimal("1.00"))
        window._active_request = "req-1"
        handle_state(window, State("ready", "Hazır", "req-1"))
        assert window.budget_chip.text() == "$1.00 / $5.00"
        assert window.budget_chip.objectName() == "chip"

        # request_id=None ile worker hazır olayı da bütçeyi yeniler.
        ledger.reserve(Decimal("3.50"))
        handle_state(window, State("ready", "Hazır", None))
        assert window.budget_chip.text() == "$4.50 / $5.00"
        assert window.budget_chip.objectName() == "chipWarning"

        for w, expected in ((1280, True), (1000, True), (800, True), (760, False)):
            window.resize(w, 720)
            qt_app.processEvents()
            assert window.budget_chip.isVisibleTo(window) is expected
    finally:
        _close_cleanly(qt_app, window)

    # Bozuk SQLite dosyası güvenli biçimde Bütçe: — gösterir.
    bad_db = tmp_path / "corrupt.sqlite3"
    bad_db.write_bytes(b"not-a-sqlite-db")
    snap_bad = budget_bar.read_budget_snapshot(bad_db, Decimal("5.00"))
    assert not snap_bad.available
    assert budget_bar.format_budget_text(snap_bad) == "Bütçe: —"
