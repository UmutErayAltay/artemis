"""Oyun modu: gerçek model, GPU veya Windows çağrısı olmadan sahte tanıyıcılarla sınanır."""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from _wait import wait_until

from artemis.channels.voice import stt_catalog
from artemis.channels.voice.adapters import FasterWhisperRecognizer
from artemis.channels.voice.game_mode import (
    GameModeRecognizer,
    fullscreen_app_active,
    resolve_light_model,
)
from artemis.config.settings import VoiceConfig


class FakeRecognizer:
    def __init__(self, name: str, log: list[str]) -> None:
        self.name = name
        self.log = log
        self.failing = False
        self.prepare_calls = 0
        self.unload_calls = 0

    async def transcribe(self, pcm: bytes) -> str:
        return self.name

    async def transcribe_partial(self, pcm: bytes) -> str:
        return self.name

    def prepare(self) -> None:
        self.prepare_calls += 1
        self.log.append(f"{self.name}.prepare")
        if self.failing:
            raise RuntimeError("model yüklenemedi")

    def unload(self) -> None:
        self.unload_calls += 1
        self.log.append(f"{self.name}.unload")


class ScriptedDetector:
    """Listedeki okumaları sırayla verir, sonra sonuncuyu tekrarlar."""

    def __init__(self, readings: list[bool]) -> None:
        self.readings = list(readings)
        self.calls = 0
        self._lock = threading.Lock()

    def __call__(self) -> bool:
        with self._lock:
            value = self.readings[min(self.calls, len(self.readings) - 1)]
            self.calls += 1
            return value


def wait_for(condition: Callable[[], bool], timeout: float = 2.0) -> bool:
    return wait_until(condition, timeout)


def make_game(
    detector: Callable[[], bool],
) -> tuple[GameModeRecognizer, FakeRecognizer, FakeRecognizer, list[str], list[str]]:
    log: list[str] = []
    states: list[str] = []
    primary = FakeRecognizer("primary", log)
    light = FakeRecognizer("light", log)
    game = GameModeRecognizer(
        primary, light, detector, states.append, poll_seconds=0.01, confirm_polls=2
    )
    return game, primary, light, log, states


def test_starts_on_primary() -> None:
    game, primary, light, _log, _states = make_game(ScriptedDetector([False]))
    assert game.active is primary
    assert asyncio.run(game.transcribe(b"\x00\x00")) == "primary"
    assert asyncio.run(game.transcribe_partial(b"\x00\x00")) == "primary"
    game.prepare()
    assert primary.prepare_calls == 1
    assert light.prepare_calls == 0


def test_enters_game_after_confirmed_polls() -> None:
    game, primary, light, log, states = make_game(ScriptedDetector([True]))
    game.start()
    try:
        assert wait_for(lambda: "game:on" in states)
        assert log.index("light.prepare") < log.index("primary.unload")
        assert game.active is light
        assert game.in_game
        assert asyncio.run(game.transcribe(b"")) == "light"
        assert primary.unload_calls == 1
    finally:
        game.close()


def test_single_blip_does_not_switch() -> None:
    detector = ScriptedDetector([False, True, False])
    game, primary, light, log, states = make_game(detector)
    game.start()
    try:
        assert wait_for(lambda: detector.calls >= 10)
        assert light.prepare_calls == 0
        assert primary.unload_calls == 0
        assert states == []
        assert log == []
        assert game.active is primary
    finally:
        game.close()


def test_leaves_game_after_confirmed_polls() -> None:
    game, primary, light, log, states = make_game(
        ScriptedDetector([True, True, True, False])
    )
    game.start()
    try:
        assert wait_for(lambda: "game:off" in states)
        assert states == ["game:on", "game:off"]
        assert log == ["light.prepare", "primary.unload", "primary.prepare", "light.unload"]
        assert log.index("primary.prepare") < log.index("light.unload")
        assert game.active is primary
        assert not game.in_game
        assert asyncio.run(game.transcribe(b"")) == "primary"
    finally:
        game.close()


def test_prepare_failure_keeps_primary_and_recovers() -> None:
    game, primary, light, _log, states = make_game(ScriptedDetector([True]))
    light.failing = True
    game.start()
    try:
        assert wait_for(lambda: "game:error" in states)
        assert game.active is primary
        assert primary.unload_calls == 0
        assert "game:on" not in states
        assert game.running
        light.failing = False
        assert wait_for(lambda: "game:on" in states)
        assert game.active is light
        assert primary.unload_calls == 1
        assert game.running
    finally:
        game.close()


def test_close_stops_thread_and_is_idempotent() -> None:
    game, _primary, _light, _log, _states = make_game(ScriptedDetector([False]))
    game.close()  # başlamadan kapatmak hata vermez
    game2, _p, _l, _lg, _st = make_game(ScriptedDetector([False]))
    game2.start()
    assert game2.running
    game2.close()
    assert not game2.running
    game2.close()
    game2.start()  # kapanmış nesne yeniden başlamaz
    assert not game2.running


def test_fullscreen_false_off_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    assert fullscreen_app_active() is False


def test_resolve_light_model(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[str] = []

    def fake_installed(name: str) -> Path | None:
        calls.append(name)
        return tmp_path

    monkeypatch.setattr(stt_catalog, "installed_path", fake_installed)
    configured = tmp_path / "light-model"
    assert resolve_light_model(VoiceConfig(game_mode_stt_model=configured)) == configured
    assert calls == []
    assert resolve_light_model(VoiceConfig()) == tmp_path
    assert calls == ["small"]


def test_faster_whisper_unload_releases_model(tmp_path: Path) -> None:
    recognizer = FasterWhisperRecognizer(VoiceConfig(stt_model=tmp_path))
    recognizer._model = object()
    recognizer.unload()
    assert recognizer._model is None


def test_old_model_is_not_unloaded_while_a_call_still_uses_it() -> None:
    """Geçişten önce büyük modeli almış çağrı bitmeden model bırakılmaz (geç çağrı onu oyunda
    geri yüklemesin)."""
    release = threading.Event()
    log: list[str] = []
    states: list[str] = []

    class Slow(FakeRecognizer):
        async def transcribe(self, pcm: bytes) -> str:
            await asyncio.to_thread(release.wait, 2.0)
            return self.name

    primary = Slow("primary", log)
    light = FakeRecognizer("light", log)
    game = GameModeRecognizer(
        primary, light, ScriptedDetector([True]), states.append, poll_seconds=0.01
    )
    result: list[str] = []
    caller = threading.Thread(target=lambda: result.append(asyncio.run(game.transcribe(b""))))
    caller.start()
    assert wait_for(lambda: game._inflight.get(id(primary), 0) == 1)
    game.start()
    try:
        assert wait_for(lambda: game.active is light)
        time.sleep(0.05)
        assert primary.unload_calls == 0  # süren çağrı varken bırakılmadı
        release.set()
        caller.join(2.0)
        assert result == ["primary"]
        assert wait_for(lambda: primary.unload_calls == 1 and "game:on" in states)
    finally:
        release.set()
        game.close()


def test_stop_during_prepare_does_not_swap_and_unloads_target() -> None:
    log: list[str] = []
    states: list[str] = []
    primary = FakeRecognizer("primary", log)

    class StoppingLight(FakeRecognizer):
        def prepare(self) -> None:
            super().prepare()
            game.close()

    light = StoppingLight("light", log)
    game = GameModeRecognizer(
        primary, light, ScriptedDetector([True]), states.append, poll_seconds=0.01
    )
    game._switch(to_game=True)

    assert game.active is primary
    assert not game.in_game
    assert primary.unload_calls == 0
    assert light.prepare_calls == 1
    assert light.unload_calls == 1
    assert states == []


def test_stop_during_inflight_wait_does_not_unload_previous() -> None:
    log: list[str] = []
    states: list[str] = []
    primary = FakeRecognizer("primary", log)
    light = FakeRecognizer("light", log)
    game = GameModeRecognizer(
        primary, light, ScriptedDetector([True]), states.append, poll_seconds=0.01
    )
    rec = game._acquire()
    assert rec is primary
    threading.Timer(0.05, game.close).start()
    game._switch(to_game=True)
    assert primary.unload_calls == 0
    assert "game:on" not in states
    game._release(rec)


def test_game_mode_emits_model_labels_on_switch() -> None:
    log: list[str] = []
    states: list[str] = []
    primary = FakeRecognizer("primary", log)
    light = FakeRecognizer("light", log)
    game = GameModeRecognizer(
        primary,
        light,
        ScriptedDetector([True, False]),
        states.append,
        poll_seconds=0.01,
        primary_label="large-v3",
        light_label="small (CPU)",
    )
    game._switch(to_game=True)
    game._switch(to_game=False)
    assert states == ["game:on:small (CPU)", "game:off:large-v3"]


def test_resolve_model_label() -> None:
    from pathlib import Path

    from artemis.channels.voice.game_mode import resolve_model_label

    assert resolve_model_label(None, fallback="varsayilan") == "varsayilan"
    assert resolve_model_label(Path("C:/models/small")) == "small"
    hf = Path("C:/hub/models--Systran--faster-whisper-small/snapshots/536b0662")
    assert resolve_model_label(hf) == "small"
