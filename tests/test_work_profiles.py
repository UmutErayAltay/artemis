"""Bağlamsal çalışma profilleri (ADR 0083) ve canlı davranış kuralları testleri."""

from __future__ import annotations

import asyncio
import gc
import json
import os
import time
import weakref
from collections.abc import AsyncGenerator, AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
import yaml
from pydantic import ValidationError

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication, QWidget

from artemis.brain.agent_loop import AgentLoop
from artemis.brain.errors import ProviderError, RetryableProviderError
from artemis.brain.fake import ScriptedProvider
from artemis.brain.models import Completion, Message
from artemis.brain.router import ProviderRouter
from artemis.channels.desktop import bridge as bridge_module
from artemis.channels.desktop.app import build_window
from artemis.channels.desktop.dictation import DictationBackend
from artemis.channels.desktop.profile_bar import build_profile_button
from artemis.channels.desktop.retranslate import retranslate_widgets
from artemis.channels.desktop.selection import SelectionActions, SelectionBackend
from artemis.channels.desktop.settings_io import save_work_profile
from artemis.channels.desktop.settings_panel import SettingsPanel
from artemis.channels.desktop.window_events import notify_job
from artemis.channels.runner import CommandRunner
from artemis.channels.runtime import create_runtime
from artemis.channels.voice import factory as voice_factory
from artemis.channels.voice.adapters import FasterWhisperRecognizer, PCMChunk
from artemis.channels.voice.contracts import AudioFrame, AudioSegment
from artemis.channels.voice.core import VoiceController
from artemis.channels.voice.game_mode import GameModeRecognizer
from artemis.config.settings import (
    CalendarConfig,
    EndpointConfig,
    MailConfig,
    ModelConfig,
    PerceptionConfig,
    ProactiveConfig,
    Settings,
    TelegramConfig,
    VoiceConfig,
    load_settings,
)
from artemis.config.work_profile import (
    PROFILE_LABELS,
    WORK_PROFILES,
    effective_settings_for_profile,
    is_game_profile,
    is_local_only_profile,
    should_mute_tts,
    should_suppress_toast,
)
from artemis.i18n import set_language
from artemis.proactive.delivery import Deliverer
from artemis.safety.audit import AuditLog
from artemis.safety.gate import SafetyGate
from artemis.tools.base import ToolContext, ToolResult
from artemis.tools.registry import ToolRegistry
from artemis.tools.toast import ToastOutcome
from tests.test_desktop_gui import _close_cleanly, _wait
from tests.test_settings_panel import config


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])  # type: ignore[return-value]


def _make_settings(tmp_path: Path, *, work_profile: str = "work") -> Settings:
    return Settings(
        workspace=tmp_path / "desktop",
        state_dir=tmp_path / "state",
        audit_path=tmp_path / "audit.jsonl",
        work_profile=work_profile,  # type: ignore[arg-type]
        primary_model="cloud",
        fallback_model="local",
        hard_model="hard",
        allow_cloud_file_content=True,
        allow_cloud_clipboard=True,
        perception=PerceptionConfig(allow_cloud_screen=True),
        calendar=CalendarConfig(allow_cloud=True),
        mail=MailConfig(allow_cloud=True),
        proactive=ProactiveConfig(enabled=True, telegram=True),
        telegram=TelegramConfig(chat_id=12345),
        endpoints={"bulut": EndpointConfig(base_url="https://api.example.com/v1")},
        voice=VoiceConfig(
            stt_model=tmp_path / "whisper-small",
            stt_mode="hybrid",
            stt_cloud_endpoint="bulut",
            stt_cloud_model="whisper-large-v3",
            direct_audio_input=True,
            allow_cloud_tts_text=True,
        ),
        models={
            "cloud": ModelConfig(
                provider="openrouter",
                model="fixture-cloud",
                input_per_million=Decimal("0"),
                output_per_million=Decimal("0"),
            ),
            "hard": ModelConfig(
                provider="openrouter",
                model="fixture-hard",
                input_per_million=Decimal("0"),
                output_per_million=Decimal("0"),
            ),
            "local": ModelConfig(
                provider="ollama",
                model="fixture-local",
                input_per_million=Decimal("0"),
                output_per_million=Decimal("0"),
            ),
        },
    )


def test_work_profile_config_validation_and_round_trip(tmp_path: Path) -> None:
    path = config(tmp_path)
    loaded = load_settings(path)
    assert loaded.work_profile == "work"

    for profile in WORK_PROFILES:
        save_work_profile(path, profile)
        reloaded = load_settings(path)
        assert reloaded.work_profile == profile
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert raw["work_profile"] == profile

    with pytest.raises(ValueError):
        save_work_profile(path, "invalid")
    with pytest.raises(ValidationError):
        Settings(work_profile="unknown")  # type: ignore[arg-type]


def test_effective_settings_and_policy_helpers(tmp_path: Path) -> None:
    assert should_suppress_toast("game")
    assert should_suppress_toast("meeting")
    assert not should_suppress_toast("work")
    assert not should_suppress_toast("private")

    assert should_mute_tts("meeting")
    assert not should_mute_tts("work")
    assert is_local_only_profile("private")
    assert not is_local_only_profile("game")
    assert is_game_profile("game")
    assert not is_game_profile("meeting")

    base = _make_settings(tmp_path, work_profile="work")
    assert effective_settings_for_profile(base) is base

    private_cfg = base.model_copy(update={"work_profile": "private"})
    effective = effective_settings_for_profile(private_cfg)
    assert not effective.allow_cloud_file_content
    assert not effective.allow_cloud_clipboard
    assert not effective.perception.allow_cloud_screen
    assert not effective.calendar.allow_cloud
    assert not effective.mail.allow_cloud
    assert effective.voice.stt_mode == "local"
    assert not effective.voice.direct_audio_input
    assert not effective.voice.allow_cloud_tts_text
    assert not effective.proactive.telegram


async def test_provider_router_private_profile_blocks_cloud_and_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", uuid4().hex)
    settings = _make_settings(tmp_path, work_profile="work")
    live_profile = ["work"]
    calls: list[str] = []

    class FakeProv:
        def __init__(self, tag: str) -> None:
            self.tag = tag

        async def complete(
            self, messages: list[Message], tools: list[Any], *, timeout: float
        ) -> Completion:
            calls.append(self.tag)
            return Completion(content=f"yanıt:{self.tag}")

    router = ProviderRouter(
        settings,
        AuditLog(settings.audit_path),
        providers={
            "cloud": FakeProv("cloud"),
            "hard": FakeProv("hard"),
            "local": FakeProv("local"),
        },
        profile_getter=lambda: live_profile[0],
    )

    first = await router.complete([Message(role="user", content="selam")], [], timeout=10)
    assert first.content == "yanıt:cloud"

    live_profile[0] = "private"
    second = await router.complete([Message(role="user", content="gizli")], [], timeout=10)
    assert second.content == "yanıt:local"
    assert second.local_model

    # hard_model (bulut) istendiğinde bile yerel modele yönlendirir.
    third = await router.complete(
        [Message(role="user", content="derin")], [], timeout=10, model="hard"
    )
    assert third.content == "yanıt:local"

    # Doğrudan _call bile buluta çıkamaz.
    fourth = await router._call(
        "cloud", [Message(role="user", content="x")], [], timeout=10, local_fallback=False
    )
    assert fourth.content == "yanıt:local"
    assert calls == ["cloud", "local", "local", "local"]

    records = [
        json.loads(line)
        for line in settings.audit_path.read_text(encoding="utf-8").splitlines()
    ]
    blocked = [r for r in records if r["event"] == "cloud_blocked_by_profile"]
    assert len(blocked) == 3


async def test_provider_router_private_profile_raises_without_local_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", uuid4().hex)
    settings = Settings(
        workspace=tmp_path / "desktop",
        state_dir=tmp_path / "state",
        audit_path=tmp_path / "audit.jsonl",
        work_profile="private",
        primary_model="cloud",
        models={
            "cloud": ModelConfig(
                provider="openrouter",
                model="fixture-cloud",
                input_per_million=Decimal("0"),
                output_per_million=Decimal("0"),
            )
        },
    )

    class ForbiddenProv:
        async def complete(
            self, messages: list[Message], tools: list[Any], *, timeout: float
        ) -> Completion:
            raise AssertionError("Bulut sağlayıcı çağrılmamalı")

    router = ProviderRouter(
        settings,
        AuditLog(settings.audit_path),
        providers={"cloud": ForbiddenProv()},
    )
    with pytest.raises(ProviderError):
        await router.complete([Message(role="user", content="gizli")], [], timeout=10)

    records = [
        json.loads(line)
        for line in settings.audit_path.read_text(encoding="utf-8").splitlines()
    ]
    assert any(r["event"] == "cloud_blocked_by_profile" for r in records)


async def test_provider_router_private_never_falls_back_from_local_to_cloud(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", uuid4().hex)
    settings = Settings(
        workspace=tmp_path / "desktop",
        state_dir=tmp_path / "state",
        audit_path=tmp_path / "audit.jsonl",
        work_profile="private",
        primary_model="local",
        fallback_model="cloud",
        models={
            "local": ModelConfig(
                provider="ollama",
                model="fixture-local",
                input_per_million=Decimal("0"),
                output_per_million=Decimal("0"),
            ),
            "cloud": ModelConfig(
                provider="openrouter",
                model="fixture-cloud",
                input_per_million=Decimal("0"),
                output_per_million=Decimal("0"),
            ),
        },
    )

    class FailingLocal:
        async def complete(
            self, messages: list[Message], tools: list[Any], *, timeout: float
        ) -> Completion:
            raise RetryableProviderError("yerel kapalı")

    class ForbiddenCloud:
        async def complete(
            self, messages: list[Message], tools: list[Any], *, timeout: float
        ) -> Completion:
            raise AssertionError("Gizli profilde bulut yedeğe düşülmemeli")

    router = ProviderRouter(
        settings,
        AuditLog(settings.audit_path),
        providers={"local": FailingLocal(), "cloud": ForbiddenCloud()},
    )
    with pytest.raises(RetryableProviderError):
        await router.complete([Message(role="user", content="x")], [], timeout=10)


async def test_agent_loop_and_selection_enforce_private_profile(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path, work_profile="private")
    audit = AuditLog(settings.audit_path)
    context = ToolContext(workspace=settings.workspace, settings=settings, audit=audit)
    agent = AgentLoop(
        ScriptedProvider([Completion(content="yerel yanıt")]),
        SafetyGate(ToolRegistry()),
        context,
        lambda *_: False,
    )
    assert agent._result_private(
        ToolResult(success=True, status="ok", message="ok", content_kind="file")
    )
    res = await agent.run("merhaba")
    assert res.record is not None and res.record.answer_private is True

    # SelectionActions canlı profille bulutu kapatır.
    live_profile = ["private"]
    seen_models: list[str | None] = []

    async def fake_complete(
        messages: list[Message], model: str | None, timeout: float
    ) -> Completion:
        seen_models.append(model)
        assert messages[1].private is True
        return Completion(content="düzeltildi", local_model=True)

    win_backend = DictationBackend(
        foreground_window=lambda: 100,
        foreground_pid=lambda: 123,
        type_text=lambda text, _cont: len(text),
        escape_hotkey=lambda _cb: SimpleNamespace(start=lambda: True, stop=lambda: None),
    )
    backend = SelectionBackend(
        window=win_backend,
        read_clipboard=lambda: "ilk",
        write_clipboard=lambda _t: True,
        send_copy=lambda: None,
        modifiers_down=lambda: False,
        focus_window=lambda _h: True,
        has_nontext_clipboard=lambda: False,
    )
    events: list[tuple[str, str]] = []
    actions = SelectionActions(
        audit=audit,
        backend=backend,
        complete=fake_complete,
        emit=lambda kind, msg: events.append((kind, msg)),
        translate_to="en",
        cloud_allowed=True,
        local_alias="local",
        profile_getter=lambda: live_profile[0],
        own_pid=999999,
    )
    actions._phase = "ready"
    actions._text = "merhaba dünya"
    actions.start_run("fix")
    await actions.wait()
    assert seen_models == ["local"]
    assert ("result", "düzeltildi") in events

    # Yerel model yoksa gizli profilde reddedilir.
    no_local = SelectionActions(
        audit=audit,
        backend=backend,
        complete=fake_complete,
        emit=lambda kind, msg: events.append((kind, msg)),
        translate_to="en",
        cloud_allowed=True,
        local_alias=None,
        profile_getter=lambda: "private",
    )
    no_local.start_capture()
    assert events[-1][0] == "message" and "bulut" in events[-1][1].lower()


async def test_voice_controller_meeting_mute_and_private_cloud_block(tmp_path: Path) -> None:
    class FakeMic:
        async def frames(self) -> AsyncGenerator[AudioFrame]:
            if False:
                yield AudioFrame(b"", 0.0)

        def pause(self) -> None:
            pass

        def resume(self) -> None:
            pass

        def discard_pending(self) -> None:
            pass

        async def close(self) -> None:
            pass

    class FakeVad:
        def is_speech(self, pcm: bytes) -> bool:
            return False

    class FakeRec:
        async def transcribe(self, pcm: bytes) -> str:
            return "artemis merhaba"

    class ForbiddenCloudRec:
        async def transcribe(self, pcm: bytes) -> str:
            raise AssertionError("Gizli profilde bulut STT çağrılmamalı")

    class FakeRemoteSynth:
        async def stream(self, text: str, cancelled: asyncio.Event) -> AsyncGenerator[PCMChunk]:
            raise AssertionError("Gizli profilde uzak TTS çağrılmamalı")
            yield PCMChunk(b"", 16000, 1, 2)

        async def close(self) -> None:
            pass

    class FakeLocalSynth:
        def __init__(self) -> None:
            self.spoken: list[str] = []

        async def stream(self, text: str, cancelled: asyncio.Event) -> AsyncGenerator[PCMChunk]:
            self.spoken.append(text)
            yield PCMChunk(b"\x01\x00" * 160, 16000, 1, 2)

        async def close(self) -> None:
            pass

    class SpySink:
        def __init__(self) -> None:
            self.plays = 0

        async def play(
            self,
            chunks: AsyncIterator[PCMChunk],
            cancelled: asyncio.Event,
            on_first_pcm: Any,
        ) -> None:
            self.plays += 1
            async for _ in chunks:
                pass
            on_first_pcm()

        async def close(self) -> None:
            pass

    states: list[str] = []
    sink = SpySink()
    local_tts = FakeLocalSynth()
    profile = ["meeting"]
    received_audio: list[bytes | None] = []

    async def run_cmd(cmd: str, **kwargs: Any) -> Any:
        received_audio.append(kwargs.get("audio_wav"))
        return SimpleNamespace(text="Merhaba!", phases=[], turn_id="t1", tool_calls=[], record=None)

    runner = CommandRunner.__new__(CommandRunner)
    cfg = VoiceConfig(
        stt_mode="hybrid",
        direct_audio_input=True,
        tts_backend="edge",
    )
    controller = VoiceController(
        runner,
        cfg,
        FakeMic(),
        FakeVad(),
        FakeRec(),
        FakeRemoteSynth(),  # type: ignore[arg-type]
        sink,
        run_command=run_cmd,
        stream_responses=True,
        on_state=states.append,
        cloud_recognizer=ForbiddenCloudRec(),  # type: ignore[arg-type]
        local_synthesizer=local_tts,
        audit=AuditLog(tmp_path / "voice-audit.jsonl"),
        muted=True,
        profile_getter=lambda: profile[0],
    )

    # Meeting profilinde hem wake ack hem yanıt sesi susturulur, "speaking" yayımlanmaz.
    await controller._acknowledge_wake(uid=1, gid=1)
    await controller._run_and_speak("merhaba", uid=2, gid=2)
    assert sink.plays == 0
    assert "speaking" not in states

    # Private profilinde unmuted olsa bile bulut STT, direct_audio_input ve uzak TTS engellenir.
    profile[0] = "private"
    controller.set_muted(False)
    seg = AudioSegment(pcm=b"\x01\x00" * 320, speech_start=0.0, speech_end=0.2)
    await controller._handle_utterance(seg)
    if controller._active is not None:
        await controller._active
    assert received_audio == [None, None]
    assert local_tts.spoken == ["Merhaba!"]
    assert sink.plays == 1


async def test_factory_respects_private_and_game_profiles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _make_settings(tmp_path, work_profile="private")
    runtime = SimpleNamespace(
        settings=settings,
        context=SimpleNamespace(settings=settings),
        audit=AuditLog(settings.audit_path),
    )
    monkeypatch.setattr(voice_factory, "_prepare", lambda _res: asyncio.sleep(0))
    recognizer = await voice_factory.build_dictation_recognizer(runtime)  # type: ignore[arg-type]
    assert isinstance(recognizer, FasterWhisperRecognizer)

    # Game profilinde hafif model varsa GameModeRecognizer kurulur.
    light_dir = tmp_path / "small"
    light_dir.mkdir()
    game_settings = settings.model_copy(
        update={
            "work_profile": "game",
            "voice": settings.voice.model_copy(
                update={"game_mode": False, "game_mode_stt_model": light_dir}
            ),
        }
    )
    game_runtime = SimpleNamespace(
        settings=game_settings,
        context=SimpleNamespace(settings=game_settings),
        audit=AuditLog(settings.audit_path),
    )
    monkeypatch.setattr(GameModeRecognizer, "start", lambda self: None)
    from contextlib import AsyncExitStack

    async with AsyncExitStack() as cleanup:
        primary = FasterWhisperRecognizer(game_settings.voice)
        rec, _cloud, _use_cloud = voice_factory._setup_game_mode(
            game_runtime,  # type: ignore[arg-type]
            primary,
            game_settings.voice,
            None,
            lambda _s: None,
            cleanup,
        )
    assert isinstance(rec, GameModeRecognizer)


async def test_deliverer_and_notify_job_profile_suppression(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path, work_profile="work")
    audit = AuditLog(settings.audit_path)
    toasts: list[tuple[str, str]] = []
    telegrams: list[str] = []

    class FakeTelegram:
        def send_message(self, _chat_id: int, text: str, **_kwargs: Any) -> None:
            telegrams.append(text)

    profile = ["game"]
    deliverer = Deliverer(
        settings,
        audit,
        panel=lambda _t, _b: True,
        toast=lambda title, body: (toasts.append((title, body)), ToastOutcome("ok", ""))[1],
        telegram=FakeTelegram(),  # type: ignore[arg-type]
        profile_getter=lambda: profile[0],
    )

    now = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
    out_game = await deliverer.deliver(
        event_id="e1", scheduled_for=now, kind="reminder", title="T", body="B", private=False
    )
    assert out_game.targets["toast"] == "suppressed"
    assert not toasts

    profile[0] = "meeting"
    out_meeting = await deliverer.deliver(
        event_id="e2", scheduled_for=now, kind="reminder", title="T", body="B", private=False
    )
    assert out_meeting.targets["toast"] == "suppressed"

    profile[0] = "private"
    before_private = len(telegrams)
    out_private = await deliverer.deliver(
        event_id="e3", scheduled_for=now, kind="reminder", title="T", body="B", private=False
    )
    assert out_private.targets["toast"] == "ok"
    assert out_private.targets["telegram"] == "withheld"
    assert len(telegrams) == before_private

    # notify_job toast suppression in game/meeting vs delivery in work/private
    job_toasts: list[tuple[str, str]] = []
    chats: list[tuple[str, str]] = []
    win = SimpleNamespace(
        _notified_jobs=set(),
        _work_profile="game",
        _append_chat=lambda who, msg: chats.append((who, msg)),
        _toast=lambda title, msg: (job_toasts.append((title, msg)), ToastOutcome("ok", ""))[1],
    )
    notify_job(win, {"job_id": "j1", "status": "done", "project": "p"})  # type: ignore[arg-type]
    assert len(chats) == 1
    assert not job_toasts

    win._work_profile = "work"
    notify_job(win, {"job_id": "j2", "status": "done", "project": "p"})  # type: ignore[arg-type]
    deadline = time.monotonic() + 2.0
    while not job_toasts and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(job_toasts) == 1


def test_topbar_profile_switcher_persistence_retranslation_and_fit(
    tmp_path: Path, qt_app: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config(tmp_path)

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

    font_path = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "Fonts" / "segoeui.ttf"
    font_id = QFontDatabase.addApplicationFont(str(font_path)) if font_path.is_file() else -1
    prev_font = qt_app.font()
    if font_id >= 0:
        qt_app.setFont(QFont("Segoe UI", 10))

    window = build_window(cfg, demo=False, app=qt_app)
    try:
        window.show()
        _wait(qt_app, lambda: window._worker_ready)
        assert window._work_profile == "work"
        assert window.profile_button.objectName() == "chip"
        assert window.profile_button.text() == "Çalışma"

        # Menü eylemleriyle tüm profillere geçiş
        for profile in ("game", "private", "meeting", "work"):
            window._profile_actions[profile].trigger()
            qt_app.processEvents()
            assert window._work_profile == profile
            assert window.profile_button.text() == PROFILE_LABELS[profile]
            expected_obj = "chip" if profile == "work" else "chipWarning"
            assert window.profile_button.objectName() == expected_obj
            assert load_settings(cfg).work_profile == profile
            deadline = time.monotonic() + 2.0
            while (
                window.bridge._runtime is not None
                and window.bridge._runtime.context.settings.work_profile != profile
            ):
                if time.monotonic() >= deadline:
                    raise AssertionError("Bridge work_profile zamanında güncellenmedi")
                time.sleep(0.01)

        # TR <-> EN canlı çeviri testi
        window._profile_actions["private"].trigger()
        qt_app.processEvents()
        set_language("en")
        retranslate_widgets([window], "en")
        qt_app.processEvents()
        assert window.profile_button.text() == "Private"
        assert [window._profile_actions[p].text() for p in WORK_PROFILES] == [
            "Work", "Game", "Private", "Meeting"
        ]

        set_language("tr")
        retranslate_widgets([window], "tr")
        qt_app.processEvents()
        assert window.profile_button.text() == "Gizli"
        assert [window._profile_actions[p].text() for p in WORK_PROFILES] == [
            "Çalışma", "Oyun", "Gizli", "Toplantı"
        ]

        # 720..1280px Segoe UI 10 üst çubuk taşma / kırpılma taraması
        window.budget_chip.setText("$12.34 / $25.00")
        window._show_model("claude-sonnet-4-5-20250929-very-long-name")
        window._set_status(
            "Bilgisayarı kullanıyorum · düğmeye tıkla · durdurmak için Esc",
            "running",
        )
        for width in (720, 760, 800, 900, 1000, 1180, 1280):
            window.resize(width, 720)
            qt_app.processEvents()
            assert window.status_pill.width() >= window.status_pill.sizeHint().width(), width
            header = window._topbar
            layout = header.layout()
            assert layout is not None
            visible = [
                child
                for i in range(layout.count())
                if (item := layout.itemAt(i)) is not None
                and (child := item.widget()) is not None
                and child.isVisibleTo(header)
            ]
            for child in visible:
                assert header.rect().contains(child.geometry()), (width, child.objectName())
            for idx, first in enumerate(visible):
                for second in visible[idx + 1 :]:
                    assert not first.geometry().intersects(second.geometry()), (
                        width, first.objectName(), second.objectName()
                    )
        assert window.profile_button.isVisibleTo(window)
    finally:
        set_language("tr")
        _close_cleanly(qt_app, window)
        qt_app.setFont(prev_font)
        if font_id >= 0:
            QFontDatabase.removeApplicationFont(font_id)


def test_profile_button_has_no_signal_cycles(qt_app: QApplication) -> None:
    class Host(QWidget):
        def __init__(self) -> None:
            super().__init__()
            self._work_profile = "work"
            self.profile_button: Any = None
            self._profile_actions: dict[str, Any] = {}
            self.calls: list[str] = []

        def _on_profile_action(self, action: Any) -> None:
            self.calls.append(str(action.data()))

    host = Host()
    btn = build_profile_button(host)  # type: ignore[arg-type]
    host._profile_actions["private"].trigger()
    assert host.calls == ["private"]
    holder = [host]
    ref = weakref.ref(host)
    del host, btn
    widget = holder.pop()
    widget.close()
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qt_app.processEvents()
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        del widget
        assert ref() is None
    finally:
        if was_enabled:
            gc.enable()


async def test_voice_controller_set_muted_interrupts_active_playback(tmp_path: Path) -> None:
    class FakeMic:
        async def frames(self) -> AsyncGenerator[AudioFrame]:
            if False:
                yield AudioFrame(b"", 0.0)

        def pause(self) -> None:
            pass

        def resume(self) -> None:
            pass

        def discard_pending(self) -> None:
            pass

        async def close(self) -> None:
            pass

    class FakeVad:
        def is_speech(self, pcm: bytes) -> bool:
            return False

    class FakeRec:
        async def transcribe(self, pcm: bytes) -> str:
            return ""

    class FakeLocalSynth:
        async def stream(self, text: str, cancelled: asyncio.Event) -> AsyncGenerator[PCMChunk]:
            while not cancelled.is_set():
                yield PCMChunk(b"\x01\x00" * 160, 16000, 1, 2)
                await asyncio.sleep(0.01)

        async def close(self) -> None:
            pass

    started = asyncio.Event()
    interrupted = asyncio.Event()

    class BlockingSink:
        async def play(
            self,
            chunks: AsyncIterator[PCMChunk],
            cancelled: asyncio.Event,
            on_first_pcm: Any,
        ) -> None:
            on_first_pcm()
            started.set()
            try:
                async for _ in chunks:
                    if cancelled.is_set():
                        break
            finally:
                if cancelled.is_set():
                    interrupted.set()

        async def close(self) -> None:
            pass

    async def run_cmd(_cmd: str, **_kwargs: Any) -> Any:
        return SimpleNamespace(
            text="Uzun yanıt", phases=[], turn_id="t1", tool_calls=[], record=None
        )

    runner = CommandRunner.__new__(CommandRunner)
    controller = VoiceController(
        runner,
        VoiceConfig(),
        FakeMic(),
        FakeVad(),
        FakeRec(),
        FakeLocalSynth(),
        BlockingSink(),
        run_command=run_cmd,
        stream_responses=False,
        audit=AuditLog(tmp_path / "voice-mute.jsonl"),
    )
    task = asyncio.create_task(controller._run_and_speak("merhaba", uid=1, gid=1))
    await asyncio.wait_for(started.wait(), timeout=2.0)
    controller.set_muted(True)
    await asyncio.wait_for(interrupted.wait(), timeout=2.0)
    await asyncio.wait_for(task, timeout=2.0)
    assert controller._is_muted()
