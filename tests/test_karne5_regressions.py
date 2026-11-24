"""Karne 5 kalıcı güvenlik, gizlilik ve kararlılık regresyon testleri.

Tüm 12 düzeltmenin (R2-1, R2-4a, R2-4b, R2-5, F1–F8) artık güvenli tarafta kaldığını ve
bilinçli tasarımların (R2-2, R2-3, Adj-2) korunduğunu doğrular.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import threading
import time
import urllib.request
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from artemis.brain.fake import ScriptedProvider
from artemis.brain.models import Completion, ToolCall
from artemis.channels.desktop.selection import SelectionActions, SelectionBackend
from artemis.channels.desktop.selection_mouse import (
    MouseSnapshot,
    SelectionCandidate,
    SelectionMouseWatcher,
)
from artemis.channels.desktop.telegram_service import should_run
from artemis.channels.runtime import create_runtime
from artemis.channels.telegram.api import SentMessage
from artemis.channels.telegram.channel import TelegramChannel, serve_telegram
from artemis.channels.voice.model_health import (
    PIPER_VOICE_CATALOG,
    _require_https_request,
    download_voice_model,
)
from artemis.computer.session import ComputerSession
from artemis.computer.windows import UiElement
from artemis.config.settings import (
    ComputerUseConfig,
    EndpointConfig,
    MailConfig,
    ModelConfig,
    PerceptionConfig,
    RoutinesConfig,
    Settings,
    TelegramConfig,
)
from artemis.config.triggers import SuggestionChainConfig
from artemis.proactive.macro_recorder import MacroRecorder, ObserverSnapshot
from artemis.safety.audit import AuditLog
from artemis.safety.gate import SafetyGate, TurnState
from artemis.tools import calendar_mail as core_read
from artemis.tools import calendar_mail_write as core_write
from artemis.tools.base import ToolContext
from artemis.tools.plugins.calendar_mail_write import MailSend
from artemis.tools.plugins.computer import ComputerUse, planner_models, vision_models
from artemis.tools.plugins.filesystem import ReadFile
from artemis.tools.plugins.notify import NotifyTelegram
from artemis.tools.plugins.routines import RoutineSave
from artemis.tools.registry import ToolRegistry

ROOT = Path(__file__).resolve().parents[1]


class _FakeTelegramClient:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str, str]] = []

    def send_message(
        self, chat_id: int, text: str, kind: str = "notice", **_kw: Any
    ) -> SentMessage:
        self.messages.append((chat_id, text, kind))
        return SentMessage(message_id=len(self.messages))

    def close(self) -> None:
        return None


def _start_server(handler_cls: type[BaseHTTPRequestHandler]) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_r2_1_and_r2_2_caldav_blocks_cross_host_redirect_and_3xx_put() -> None:
    """R2-1/R2-2: _CalDavRedirectHandler çapraz-host yönlendirmede Authorization sızdırmaz."""
    sink_hits: list[tuple[str, str | None]] = []

    class SinkHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            sink_hits.append((self.path, self.headers.get("Authorization")))
            self.send_response(200)
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

    sink = _start_server(SinkHandler)

    class RedirectHandler(BaseHTTPRequestHandler):
        def _redir(self) -> None:
            self.send_response(302)
            self.send_header("Location", f"http://localhost:{sink.server_port}/stolen")
            self.end_headers()

        do_PUT = _redir
        do_GET = _redir
        do_OPTIONS = _redir

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

    class SameHostRedirectHandler(BaseHTTPRequestHandler):
        def _redir(self) -> None:
            self.send_response(302)
            self.send_header("Location", "/other")
            self.end_headers()

        do_PUT = _redir
        do_GET = _redir
        do_OPTIONS = _redir

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

    same_src = _start_server(SameHostRedirectHandler)
    src = _start_server(RedirectHandler)
    try:
        ics = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR\r\n"
        with pytest.raises(core_read.SourceError, match="HTTP 302"):
            core_write.put_caldav_event(
                f"http://127.0.0.1:{same_src.server_port}/cal/ev.ics", ics, "alice", "pw"
            )
        with pytest.raises(core_read.SourceError, match="farklı bir adrese"):
            core_write.put_caldav_event(
                f"http://127.0.0.1:{src.server_port}/cal/ev.ics", ics, "alice", "pw"
            )
        opener = urllib.request.build_opener(core_write._CalDavRedirectHandler)
        req = urllib.request.Request(
            f"http://127.0.0.1:{src.server_port}/cal/x",
            headers={"Authorization": "Basic YWxpY2U6cHc="},
            method="GET",
        )
        with pytest.raises(core_read.SourceError, match="farklı bir adrese"):
            opener.open(req, timeout=3.0)
        assert sink_hits == []
    finally:
        same_src.shutdown()
        same_src.server_close()
        src.shutdown()
        src.server_close()
        sink.shutdown()
        sink.server_close()


def test_r2_3_calendar_mail_write_requires_confirm_and_does_not_taint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R2-3 bilinçli tasarım: auto kipte bile onay sorar ve turu lekelemez."""
    monkeypatch.setenv(core_write.SMTP_PASSWORD_ENV, "pw")
    settings = Settings(
        workspace=tmp_path,
        state_dir=tmp_path / "state",
        audit_path=tmp_path / "audit.jsonl",
        persist_memory=False,
        mail=MailConfig(
            enabled=True, allow_send=True, smtp_host="smtp.example.com", username="u@example.com"
        ),
        computer_use=ComputerUseConfig(mode="auto"),
    )
    reg = ToolRegistry()
    reg.register(MailSend)
    gate = SafetyGate(reg)

    class FakeSMTP:
        def login(self, u: str, p: str) -> None:
            return None

        def send_message(self, msg: Any) -> dict[str, Any]:
            return {}

        def quit(self) -> None:
            return None

    ctx = ToolContext(tmp_path, settings, AuditLog(settings.audit_path), turn_id="r23")
    ctx.services["smtp_connect"] = lambda _h, _p: FakeSMTP()
    state = TurnState()
    asked: list[str] = []
    res = asyncio.run(
        gate.dispatch(
            ToolCall(
                id="1",
                name="mail_send",
                arguments={"to": "b@example.com", "subject": "Hi", "body": "Body"},
            ),
            ctx,
            state,
            lambda n, _a: (asked.append(n) or True),
        )
    )
    assert res.status == "ok" and asked == ["mail_send"] and state.tainted is False


def test_r2_4a_private_profile_blocks_cloud_voice_preview(tmp_path: Path) -> None:
    """R2-4a: private profilde hem validate_preview_request hem run_voice_preview reddeder."""
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from artemis.channels.desktop.settings_panel import SettingsPanel
    from artemis.channels.desktop.voice_preview import (
        VoicePreviewRequest,
        run_voice_preview,
        validate_preview_request,
    )

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "work_profile": "private",
                "voice": {"tts_backend": "edge", "allow_cloud_tts_text": True},
            }
        ),
        encoding="utf-8",
    )
    panel = SettingsPanel(cfg_path)
    try:
        panel.tts_combo.setCurrentIndex(panel.tts_combo.findData("edge"))
        panel.tts_cloud_consent.setChecked(True)
        req, _ep, _audit, err = validate_preview_request(panel)
        assert req is None and err is not None and "Gizli profil" in err
    finally:
        panel.finish_inventory()
        panel.deleteLater()
        app.processEvents()

    blocked_req = VoicePreviewRequest(
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
        sample_text="Test",
        work_profile="private",
    )
    with pytest.raises(ValueError, match="Gizli profil"):
        asyncio.run(run_voice_preview(blocked_req, None, tmp_path / "audit.jsonl"))


def test_r2_4b_private_profile_blocks_telegram_channel_and_notify_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R2-4b: private profil Telegram servisini, medya/turn yollarını ve notify_telegram'ı keser."""
    monkeypatch.setenv("ARTEMIS_TELEGRAM_BOT_TOKEN", "fake-tg-token")
    tg_cfg = TelegramConfig(run_in_desktop=True, chat_id=12345)
    assert should_run(tg_cfg, work_profile="work") is True
    assert should_run(tg_cfg, work_profile="private") is False

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "work_profile": "work",
                "primary_model": "local",
                "models": {
                    "local": {
                        "provider": "ollama",
                        "model": "qwen-fake",
                        "kind": "chat",
                        "input_per_million": 0,
                        "output_per_million": 0,
                    }
                },
                "telegram": {"run_in_desktop": True, "chat_id": 12345},
            }
        ),
        encoding="utf-8",
    )
    statuses: list[str] = []
    if importlib.util.find_spec("PySide6") is not None:
        from artemis.channels.desktop.profile_bar import switch_work_profile

        # switch_work_profile canlı durdurur; private profil ağ açılmadan durur.
        sync_calls: list[str] = []

        class FakeWin:
            _demo = False
            _config_path = cfg_path
            _work_profile = "work"
            _worker_ready = False
            profile_button = None

            def _set_status(self, *_a: Any) -> None:
                return None

            def _sync_telegram_service(self) -> None:
                sync_calls.append("synced")

        assert switch_work_profile(FakeWin(), "private") is True  # type: ignore[arg-type]
        assert sync_calls == ["synced"]
    else:
        raw_cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        raw_cfg["work_profile"] = "private"
        cfg_path.write_text(yaml.safe_dump(raw_cfg), encoding="utf-8")
    statuses.clear()
    code = asyncio.run(
        serve_telegram(cfg_path, asyncio.Event(), embedded=True, status=statuses.append)
    )
    assert code == 2 and any("Gizli profil" in s for s in statuses)

    audit = AuditLog(tmp_path / "audit.jsonl")
    fake_client = _FakeTelegramClient()
    channel = TelegramChannel(
        fake_client,  # type: ignore[arg-type]
        12345,
        audit=audit,
        approval_timeout=1.0,
        max_message_age=60,
        poll_timeout=1,
    )
    channel._config_path = cfg_path
    runtime = create_runtime(
        cfg_path,
        channel.approve,
        provider_builder=lambda _s, _a: ScriptedProvider([Completion(content="gizli yanit")]),
    )
    channel.attach(runtime)
    asyncio.run(channel._run_turn(runtime, "merhaba"))
    asyncio.run(channel._handle_voice(runtime, {"voice": {"file_id": "v1", "file_size": 10}}))
    asyncio.run(channel._handle_photo(runtime, {"photo": [{"file_id": "p1", "file_size": 10}]}))
    assert fake_client.messages == []

    # NotifyTelegram refusal in private profile
    settings = Settings(
        workspace=tmp_path,
        state_dir=tmp_path / "state",
        audit_path=audit.path,
        persist_memory=False,
        work_profile="private",
        telegram=TelegramConfig(chat_id=12345),
    )
    reg = ToolRegistry()
    reg.register(NotifyTelegram)
    gate = SafetyGate(reg)
    ctx = ToolContext(tmp_path, settings, audit, turn_id="nt1")
    ctx.services["telegram_client"] = fake_client
    res = asyncio.run(
        gate.dispatch(
            ToolCall(id="n1", name="notify_telegram", arguments={"text": "gizli"}),
            ctx,
            TurnState(),
            lambda _n, _a: True,
        )
    )
    assert res.status == "error" and "Gizli profil" in res.message
    assert fake_client.messages == []


def test_r2_5_send_mail_reports_accepted_and_refused_recipients() -> None:
    """R2-5: kısmi SMTP reddinde teslim edilen ve reddedilen adresler açıkça bildirilir."""

    class PartialSMTP:
        def login(self, u: str, p: str) -> None:
            return None

        def send_message(self, msg: Any) -> dict[str, tuple[int, bytes]]:
            return {"bad@example.com": (550, b"No such user")}

        def quit(self) -> None:
            return None

    with pytest.raises(core_read.SourceError) as exc:
        core_write.send_mail(
            lambda _h, _p: PartialSMTP(),
            host="smtp.example.com",
            port=465,
            username="u@example.com",
            password="pw",
            to="a@example.com, bad@example.com",
            subject="Test",
            body="Body",
        )
    text = str(exc.value)
    assert "a@example.com" in text and "bad@example.com" in text


def test_f1_to_f4_automation_and_selection_guards(tmp_path: Path) -> None:
    """F1 (XOR chain), F2 (private capture block), F3 (fail-closed selection), F4 (macro LMB)."""
    # F1
    if importlib.util.find_spec("PySide6") is not None:
        from artemis.channels.desktop.chain_dialog import validate_chain

        chain, errors = validate_chain(
            name="dual",
            after_routine="morning",
            after_trigger="opened",
            suggest="Özet?",
        )
        assert chain is None and "after_routine" in errors
    with pytest.raises(ValueError, match="Yalnız bir|yalnız bir"):
        SuggestionChainConfig(
            name="dual", after_routine="morning", after_trigger="opened", suggest="x"
        )

    # F2 / Adj-3
    vis_settings = Settings(
        workspace=tmp_path,
        state_dir=tmp_path / "state",
        audit_path=tmp_path / "audit.jsonl",
        persist_memory=False,
        work_profile="private",
        primary_model="cloud_vis",
        models={
            "cloud_vis": ModelConfig(
                provider="openai_compatible",
                model="gpt-vis",
                kind="chat",
                endpoint="ep",
                input_per_million=0,
                output_per_million=0,
            )
        },
        endpoints={"ep": EndpointConfig(base_url="https://api.example.com/v1", api_key=True)},
        computer_use=ComputerUseConfig(enabled=True, model="cloud_vis", observation="screen"),
        perception=PerceptionConfig(allow_cloud_screen=True),
    )
    captures = [0]

    class SpyHost:
        @contextmanager
        def session(self):  # type: ignore[no-untyped-def]
            yield threading.Event(), (lambda _s: None), (lambda _n, _a: True)

    class SpyBackend:
        def capture(self, *_a: Any) -> tuple[bytes, int, int]:
            captures[0] += 1
            return b"", 1, 1

    ctx = ToolContext(tmp_path, vis_settings, AuditLog(vis_settings.audit_path), turn_id="f2")
    ctx.services["computer_host"] = SpyHost()
    ctx.services["computer_backend"] = SpyBackend()
    res = asyncio.run(ComputerUse().execute(ComputerUse.args_model(goal="look"), ctx))
    assert captures[0] == 0 and "Gizli profil" in res.message

    # F3: empty exe_path fails closed on both mouse watcher and accept_selection
    cands: list[SelectionCandidate] = []
    watcher = SelectionMouseWatcher(
        on_candidate=cands.append,
        on_dismiss=lambda: None,
        own_pid=100,
        settle_delay=0.0,
        probe_selection=lambda: ("secret", False, True),
    )
    watcher.step(MouseSnapshot(True, 10, 10, hwnd=555, pid=999, exe_path=""), 1.0, settle=False)
    watcher.step(MouseSnapshot(False, 50, 10, hwnd=555, pid=999, exe_path=""), 1.2, settle=False)
    assert cands == []

    from artemis.channels.desktop.dictation import DictationBackend

    sel_events: list[tuple[str, str]] = []
    def _make_hotkey(_cb: Any) -> Any:
        return type("H", (), {"start": lambda _s: True, "stop": lambda _s: None})()

    actions = SelectionActions(
        audit=AuditLog(tmp_path / "sel_audit.jsonl"),
        backend=SelectionBackend(
            window=DictationBackend(
                foreground_window=lambda: 555,
                foreground_pid=lambda: 999,
                type_text=lambda _t, _c: 0,
                escape_hotkey=_make_hotkey,
            ),
            read_clipboard=lambda: "",
            write_clipboard=lambda _t: True,
            send_copy=lambda: None,
            modifiers_down=lambda: False,
            focus_window=lambda _h: True,
            foreground_process=lambda: ("", 999),
        ),
        complete=lambda _m, _a, _t: asyncio.sleep(0, result=Completion(content="ok")),
        emit=lambda k, p: sel_events.append((k, p)),
        translate_to="en",
        cloud_allowed=True,
        local_alias=None,
        own_pid=100,
    )
    actions.accept_selection(555, "secret")
    assert sel_events == [("message", "Korumalı veya engelli pencerede seçili metin alınmaz.")]

    # F4
    cmd_exe = r"C:\Windows\System32\cmd.exe"
    snaps = iter(
        [
            ObserverSnapshot(exe_path=cmd_exe, pid=200, hwnd=1, left_down=True),
            ObserverSnapshot(exe_path=cmd_exe, pid=200, hwnd=1, left_down=True),
            ObserverSnapshot(
                exe_path=r"C:\Windows\System32\notepad.exe",
                pid=300,
                hwnd=2,
                cursor_pos=(40, 40),
                left_down=True,
            ),
        ]
    )
    snap_backend = type("B", (), {"snapshot": staticmethod(lambda: next(snaps))})()
    rec = MacroRecorder(snap_backend, own_pid=100)
    rec.start()
    for _ in range(3):
        rec.poll_once()
    assert rec.skipped_blocked_count == 1
    assert [e.kind for e in rec.events] == ["app_focus"]


def test_f5_hybrid_recapture_resyncs_elements_when_window_changes(tmp_path: Path) -> None:
    """F5: hybrid yeniden yakalamada pencere değişince eski pencere öğesi tıklanmaz."""

    class HybridSyncBackend:
        def __init__(self) -> None:
            self.fg_calls = 0
            self.clicks: list[tuple[int, int]] = []

        def monitors(self) -> list[tuple[int, int, int, int]]:
            return [(0, 0, 800, 600)]

        def foreground_window(self) -> tuple[int, tuple[int, int, int, int]]:
            self.fg_calls += 1
            return (10 if self.fg_calls == 1 else 20, (0, 0, 800, 600))

        def window_rect(self, hwnd: int) -> tuple[int, int, int, int]:
            return (0, 0, 800, 600)

        def foreground_process(self) -> tuple[str, int]:
            return (r"C:\Program Files\App\app.exe", 500)

        def process_at(self, x: int, y: int) -> tuple[str, int]:
            return (r"C:\Program Files\App\app.exe", 500)

        def window_at(self, x: int, y: int) -> int:
            return 20

        def ui_elements(self, hwnd: int) -> list[UiElement]:
            if hwnd == 10:
                return []
            return [
                UiElement(
                    id=1,
                    name="Win2Btn",
                    kind="Button",
                    rect=(300, 300, 40, 30),
                    interactive=True,
                    focused=False,
                )
            ]

        def vision_elements(self, hwnd: int, start_id: int) -> list[UiElement]:
            if hwnd == 10:
                return [
                    UiElement(
                        id=start_id,
                        name="Win1Btn",
                        kind="ocr",
                        rect=(100, 100, 40, 30),
                        interactive=True,
                        focused=False,
                    )
                ]
            return [
                UiElement(
                    id=start_id,
                    name="Win2Btn",
                    kind="ocr",
                    rect=(300, 300, 40, 30),
                    interactive=True,
                    focused=False,
                )
            ]

        def capture(self, left: int, top: int, w: int, h: int) -> tuple[bytes, int, int]:
            return bytes(w * h * 4), w, h

        def click(self, x: int, y: int, button: str = "left", double: bool = False) -> None:
            self.clicks.append((x, y))

    class Exec:
        def __init__(self) -> None:
            self.step = 0

        async def complete(
            self, messages: list[Any], tools: list[Any], *, timeout: float = 30.0
        ) -> Completion:
            self.step += 1
            if self.step == 1:
                return Completion(
                    content='{"type":"click_element","element":1,"reason":"Win2Btn"}'
                )
            if self.step == 2:
                return Completion(content='{"type":"done","text":"clicked","reason":"done"}')
            return Completion(content='{"verified":true,"evidence":"clicked Win2Btn"}')

    backend = HybridSyncBackend()
    session = ComputerSession(
        provider=Exec(),  # type: ignore[arg-type]
        backend=backend,  # type: ignore[arg-type]
        approve=lambda _n, _a: True,
        audit=AuditLog(tmp_path / "audit.jsonl"),
        config=ComputerUseConfig(
            enabled=True, observation="hybrid", mode="auto", step_delay_seconds=0.0
        ),
        own_pid=999,
        stop=threading.Event(),
        on_step=lambda _s: None,
        provider_timeout=5.0,
    )
    res = asyncio.run(session.run("click button", "turn-h2", deadline=time.monotonic() + 10.0))
    assert res.reason == "done" and res.verified is True
    assert backend.clicks == [(320, 315)]


def test_f6_to_f8_and_adj2_preserved(tmp_path: Path) -> None:
    """F6 (claude_cli filtered), F7 (Piper pins + HTTPS redirect), F8 (ISCC order), Adj-2."""
    cli_cfg = ModelConfig(
        provider="claude_cli",
        model="sonnet",
        kind="chat",
        input_per_million=0,
        output_per_million=0,
    )
    s_hybrid = Settings(
        workspace=tmp_path,
        primary_model="cli",
        models={"cli": cli_cfg},
        perception=PerceptionConfig(allow_cloud_screen=True),
        computer_use=ComputerUseConfig(observation="hybrid"),
    )
    assert vision_models(s_hybrid) == () and planner_models(s_hybrid) == ()
    s_uia = s_hybrid.model_copy(
        update={"computer_use": s_hybrid.computer_use.model_copy(update={"observation": "uia"})}
    )
    assert vision_models(s_uia) == ("cli",) and planner_models(s_uia) == ()

    assert all(s.onnx_sha256 and s.json_sha256 for s in PIPER_VOICE_CATALOG.values())
    with pytest.raises(ValueError, match="HTTPS"):
        _require_https_request(httpx.Request("GET", "http://example.com/voice.onnx"))
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="SHA-256"):
        download_voice_model(
            "piper:tr_TR-dfki-medium",
            cfg_path,
            piper_models_dir=tmp_path / "models",
            url_fetcher=lambda u: b'{"sample_rate": 22050}' if ".json" in u else b"TAMPERED",
        )

    wf = (ROOT / ".github/workflows/release-windows.yml").read_text(encoding="utf-8")
    assert wf.index("Resolve signing mode") < wf.index("Build Inno Setup installer")
    assert "/DSignToolCmd=signtool" in wf

    # Adj-2 preserved (Umut 2026-11-21 decision)
    r_settings = Settings(
        workspace=tmp_path,
        state_dir=tmp_path / "r_state",
        audit_path=tmp_path / "r_audit.jsonl",
        persist_memory=False,
        routines=RoutinesConfig(enabled=True),
        computer_use=ComputerUseConfig(mode="auto"),
    )
    reg = ToolRegistry()
    reg.register(RoutineSave)
    reg.register(ReadFile)
    res = asyncio.run(
        SafetyGate(reg).dispatch(
            ToolCall(
                id="r1",
                name="routine_save",
                arguments={
                    "name": "tainted_auto",
                    "steps": [{"tool": "read_file", "arguments": {"path": "a.txt"}}],
                },
            ),
            ToolContext(tmp_path, r_settings, AuditLog(r_settings.audit_path), turn_id="adj2"),
            TurnState(tainted=True),
            lambda _n, _a: False,
        )
    )
    assert res.status == "ok"
