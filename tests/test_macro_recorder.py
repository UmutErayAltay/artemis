"""Öğretilebilir makro kaydedici motoru testleri (ADR 0087).

Gerçek OS etkileşimi yoktur: enjekte edilen sahte `MacroObserverBackend` ve sahte araçlar
kullanılır. Gizlilik (engelli süreçler, IsPassword/hassas alanlar, sır maskeleme), sınırlar
(<= 12 adım, routine_* yasağı) ve SafetyGate üzerinden kayıt/oynatma doğrulanır.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, ClassVar

import pytest
from pydantic import BaseModel, ConfigDict, Field

from artemis.brain.models import ToolCall
from artemis.computer.windows import UiElement
from artemis.config.settings import ComputerUseConfig, RoutinesConfig, Settings
from artemis.i18n import set_language
from artemis.proactive.macro_recorder import (
    FocusedControlInfo,
    MacroRecorder,
    ObservedEvent,
    ObserverSnapshot,
    WindowsMacroObserver,
    compile_macro_steps,
    focused_control_info,
    sanitize_macro_text,
)
from artemis.proactive.routines import MAX_STEPS, RoutineStore
from artemis.safety.audit import AuditLog
from artemis.safety.gate import SafetyGate, TurnState
from artemis.tools.base import BaseTool, Risk, ToolContext, ToolResult
from artemis.tools.plugins.routines import RoutineRun, RoutineSave
from artemis.tools.registry import ToolRegistry


class FakeBackend:
    """Testler için deterministik `MacroObserverBackend`."""

    def __init__(self, snapshots: list[ObserverSnapshot]) -> None:
        self._snapshots = list(snapshots)
        self._index = 0

    def push(self, snap: ObserverSnapshot) -> None:
        self._snapshots.append(snap)

    def snapshot(self) -> ObserverSnapshot:
        if self._index >= len(self._snapshots):
            return ObserverSnapshot()
        item = self._snapshots[self._index]
        self._index += 1
        return item


class OpenAppArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1)
    file_path: str | None = None


class OpenUrlArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=1)


class ComputerGoalArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal: str = Field(min_length=1, max_length=500)


class WindowArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    window_id: int = Field(gt=0)
    action: str = Field(min_length=1)


class FakeOpenApp(BaseTool):
    name = "open_application"
    description = "Sahte uygulama açma."
    args_model = OpenAppArgs
    risk: ClassVar[Risk] = Risk.SIDE_EFFECT
    ran: ClassVar[list[tuple[str, dict[str, Any]]]] = []

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        self.ran.append((self.name, arguments.model_dump()))
        return ToolResult(success=True, status="ok", message="açıldı")


class FakeOpenUrl(FakeOpenApp):
    name = "open_url"
    description = "Sahte adres açma."
    args_model = OpenUrlArgs
    risk: ClassVar[Risk] = Risk.READ_ONLY


class FakeComputerUse(FakeOpenApp):
    name = "computer_use"
    description = "Sahte bilgisayar kullanımı."
    args_model = ComputerGoalArgs
    risk: ClassVar[Risk] = Risk.CONFIRM_REQUIRED


class FakeWindowControl(FakeOpenApp):
    name = "window_control"
    description = "Sahte pencere düzeni."
    args_model = WindowArgs
    risk: ClassVar[Risk] = Risk.SIDE_EFFECT


def _registry() -> ToolRegistry:
    reg = ToolRegistry()
    for tool_cls in (
        FakeOpenApp,
        FakeOpenUrl,
        FakeComputerUse,
        FakeWindowControl,
        RoutineSave,
        RoutineRun,
    ):
        reg.register(tool_cls)
    return reg


def _settings(tmp_path: Path, *, apps: dict[str, str] | None = None) -> Settings:
    return Settings(
        workspace=tmp_path,
        state_dir=tmp_path / "state",
        audit_path=tmp_path / "audit.jsonl",
        persist_memory=False,
        routines=RoutinesConfig(enabled=True),
        computer_use=ComputerUseConfig(mode="accept"),
        applications=apps or {"Not Defteri": "notepad.exe", "Hesap Makinesi": "calc.exe"},
    )


@pytest.fixture(autouse=True)
def _reset_language_and_runs() -> None:
    set_language("tr")
    FakeOpenApp.ran = []


def test_uia_focus_and_click_observation_plus_nl_compile_deterministically(tmp_path: Path) -> None:
    backend = FakeBackend(
        [
            ObserverSnapshot(
                exe_path=r"C:\Windows\System32\notepad.exe",
                pid=4010,
                hwnd=100,
                cursor_pos=(50, 50),
                left_down=False,
                focused=FocusedControlInfo(name="Metin Düzenleyici", kind="Edit"),
            ),
            ObserverSnapshot(
                exe_path=r"C:\Windows\System32\notepad.exe",
                pid=4010,
                hwnd=100,
                cursor_pos=(120, 45),
                left_down=True,
                focused=FocusedControlInfo(name="Kaydet", kind="Button"),
                elements=(
                    UiElement(
                        id=1,
                        kind="Button",
                        name="Kaydet",
                        rect=(100, 30, 60, 30),
                        interactive=True,
                        focused=True,
                    ),
                ),
            ),
        ]
    )
    rec = MacroRecorder(backend=backend, own_pid=9999)
    rec.start()
    first = rec.poll_once()
    second = rec.poll_once()
    text_ev = rec.record_text(
        "Merhaba ekip",
        app="notepad.exe",
        target_info=FocusedControlInfo(name="Belge alanı", kind="Edit"),
    )
    rec.stop()

    assert first is not None and first.kind == "app_focus" and first.app == "notepad.exe"
    assert second is not None and second.kind == "click" and second.label == "Kaydet"
    assert text_ev is not None and text_ev.kind == "text"

    draft = rec.compile_draft(
        "sabah notu",
        _registry(),
        _settings(tmp_path),
        nl_text="1. https://example.com/rapor aç\n2. Hesap Makinesi aç",
    )
    assert draft.error == ""
    assert draft.masked_count == 0
    assert draft.skipped_blocked_count == 0
    expected_text_goal = "notepad.exe içinde «Belge alanı» alanına şu metni yaz: Merhaba ekip"
    assert [(s.tool, s.arguments) for s in draft.steps] == [
        ("open_application", {"name": "Not Defteri", "file_path": None}),
        ("computer_use", {"goal": "notepad.exe uygulamasında «Kaydet» (Button) öğesine tıkla"}),
        ("computer_use", {"goal": expected_text_goal}),
        ("open_url", {"url": "https://example.com/rapor"}),
        ("open_application", {"name": "Hesap Makinesi", "file_path": None}),
    ]


def test_skips_blocked_processes_and_own_pid(tmp_path: Path) -> None:
    backend = FakeBackend(
        [
            ObserverSnapshot(exe_path=r"C:\Windows\System32\cmd.exe", pid=111, left_down=True),
            ObserverSnapshot(exe_path=r"C:\Apps\1password.exe", pid=222, left_down=True),
            ObserverSnapshot(exe_path=r"C:\Apps\Code.exe", pid=333, left_down=True),
            ObserverSnapshot(exe_path=r"C:\Apps\artemis.exe", pid=9999, left_down=True),
            ObserverSnapshot(exe_path="", pid=444, left_down=True),
            ObserverSnapshot(
                exe_path=r"C:\Windows\System32\calc.exe",
                pid=555,
                cursor_pos=(10, 10),
                left_down=False,
            ),
        ]
    )
    rec = MacroRecorder(backend=backend, own_pid=9999)
    rec.start()
    results = [rec.poll_once() for _ in range(6)]
    rec.stop()

    assert results[:5] == [None, None, None, None, None]
    assert results[5] is not None and results[5].app == "calc.exe"
    assert rec.skipped_blocked_count == 5
    assert [e.app for e in rec.events] == ["calc.exe"]

    # record_text da engelli süreci reddeder.
    blocked_text = rec.record_text("gizli komut", exe_path=r"C:\Windows\powershell.exe", pid=12)
    assert blocked_text is None
    assert rec.skipped_blocked_count == 6


def test_masks_and_skips_is_password_and_sensitive_fields_with_zero_leakage(
    tmp_path: Path,
) -> None:
    secret_pw = "CokGizliParola!987"
    secret_otp = "849201"
    secret_card = "4532 1234 5678 9012"
    secret_key = "-".join(("sk", "proj", "secret1234567890abcdef"))

    backend = FakeBackend(
        [
            # IsPassword=True odaklanmış öğe tıklaması -> atlanır, sır/ad saklanmaz.
            ObserverSnapshot(
                exe_path=r"C:\Apps\browser.exe",
                pid=700,
                hwnd=20,
                cursor_pos=(30, 40),
                left_down=True,
                focused=FocusedControlInfo(
                    name="",
                    kind="Edit",
                    automation_id="pwd_input",
                    is_password=True,
                ),
                elements=(
                    UiElement(
                        id=1,
                        kind="Edit",
                        name="<parola>",
                        rect=(10, 10, 100, 50),
                        interactive=True,
                        focused=True,
                    ),
                ),
            ),
            # Hassas kontrol adına tıklama ("OTP Kodu" / "CVV")
            ObserverSnapshot(
                exe_path=r"C:\Apps\browser.exe",
                pid=700,
                hwnd=20,
                cursor_pos=(40, 40),
                left_down=False,
            ),
            ObserverSnapshot(
                exe_path=r"C:\Apps\browser.exe",
                pid=700,
                hwnd=20,
                cursor_pos=(40, 40),
                left_down=True,
                focused=FocusedControlInfo(name="OTP Doğrulama Kodu", kind="Edit"),
            ),
        ]
    )
    rec = MacroRecorder(backend=backend, own_pid=9999)
    rec.start()
    ev1 = rec.poll_once()  # app_focus emitted, password click masked/skipped
    ev2 = rec.poll_once()  # mouse release
    ev3 = rec.poll_once()  # OTP click masked/skipped
    # record_text on password & sensitive fields
    assert (
        rec.record_text(
            secret_pw,
            app="browser.exe",
            target_info=FocusedControlInfo(kind="Edit", is_password=True),
        )
        is None
    )
    assert (
        rec.record_text(
            secret_otp,
            app="browser.exe",
            target_info=FocusedControlInfo(name="OTP kodu", kind="Edit"),
        )
        is None
    )
    rec.stop()

    assert ev1 is not None and ev1.kind == "app_focus"
    assert ev2 is None
    assert ev3 is None

    draft = rec.compile_draft(
        "giris akisi",
        _registry(),
        _settings(tmp_path),
        nl_text=(
            f"parola: {secret_pw}\n"
            f"Kart no: {secret_card}\n"
            f"Anahtar {secret_key} kaydet\n"
            "Devam düğmesine tıkla"
        ),
    )
    assert draft.masked_count >= 7
    dumped = draft.model_dump_json()
    for secret in (secret_pw, secret_otp, secret_card, secret_key):
        assert secret not in dumped
    assert "[GİZLENDİ]" in dumped
    assert any("Devam düğmesine tıkla" in str(step.arguments) for step in draft.steps)


def test_sanitize_macro_text_redacts_secrets_and_url_userinfo() -> None:
    text, masked = sanitize_macro_text("https://ali:gizli123@example.com/path token=abcdef123456")
    assert masked is True
    assert "gizli123" not in text
    assert "abcdef123456" not in text


def test_bounds_steps_to_max_12_and_forbids_recursive_routine_tools(tmp_path: Path) -> None:
    nl_lines = [f"Adım {i} çalıştır" for i in range(1, 18)]
    nl_lines.insert(2, "routine_run çalıştır")
    nl_lines.insert(5, "routine_save ile yaz")
    events = [ObservedEvent(kind="text", app="notepad.exe", value="routine_delete")]
    steps, warnings, truncated, _ = compile_macro_steps(
        events, nl_lines, _registry(), settings=_settings(tmp_path)
    )
    assert len(steps) == MAX_STEPS
    assert truncated is True
    assert all(not step.tool.startswith("routine_") for step in steps)
    assert any("rutin aracı" in w for w in warnings)
    assert any("12" in w for w in warnings)


def test_windows_macro_observer_and_focused_control_info_with_mocked_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")

    def fake_runner(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        payload = {"k": "Edit", "n": "Gizli Ad", "a": "pwd", "p": True, "r": [1, 2, 30, 20]}
        return subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(payload),
            stderr="",
        )

    info = focused_control_info(runner=fake_runner)
    assert info is not None
    assert info.is_password is True
    assert info.name == ""  # IsPassword=True iken ad dahi saklanmaz
    assert info.kind == "Edit"

    # WindowsMacroObserver sahte ön plan + imleç ile çalışır, engelli süreçte UIA okumaz.
    import artemis.proactive.macro_recorder as mr

    uia_calls: list[int] = []
    monkeypatch.setattr(mr, "foreground_process", lambda: (r"C:\Windows\cmd.exe", 500))
    monkeypatch.setattr(mr, "_cursor_and_lbutton", lambda: ((15, 25), True))
    monkeypatch.setattr(mr, "ui_elements", lambda hwnd, **kw: uia_calls.append(hwnd) or [])

    obs = WindowsMacroObserver(own_pid=9999, runner=fake_runner)
    blocked_snap = obs.snapshot()
    assert blocked_snap.exe_path.endswith("cmd.exe")
    assert blocked_snap.focused is None
    assert uia_calls == []


async def test_macro_save_and_playback_dispatch_every_step_through_safety_gate(
    tmp_path: Path,
) -> None:
    backend = FakeBackend(
        [
            ObserverSnapshot(
                exe_path=r"C:\Windows\System32\notepad.exe",
                pid=101,
                hwnd=50,
                cursor_pos=(15, 15),
                left_down=True,
                focused=FocusedControlInfo(name="Dosya", kind="MenuItem"),
            )
        ]
    )
    rec = MacroRecorder(backend=backend, own_pid=9999)
    rec.start()
    rec.poll_once()
    rec.stop()

    reg = _registry()
    settings = _settings(tmp_path)
    draft = rec.compile_draft(
        "belge ac",
        reg,
        settings,
        nl_text="https://example.com aç",
    )
    assert draft.error == ""
    assert len(draft.steps) == 3

    gate = SafetyGate(reg)
    context = ToolContext(tmp_path, settings, AuditLog(settings.audit_path))
    approvals: list[str] = []

    def deny_all(name: str, _args: dict[str, Any]) -> bool:
        approvals.append(f"deny:{name}")
        return False

    def approve_all(name: str, _args: dict[str, Any]) -> bool:
        approvals.append(f"allow:{name}")
        return True

    step_dicts = [step.model_dump() for step in draft.steps]
    denied = await gate.dispatch(
        ToolCall(id="c1", name="routine_save", arguments={"name": draft.name, "steps": step_dicts}),
        context,
        TurnState(),
        deny_all,
    )
    assert denied.status == "denied"
    assert RoutineStore(settings.state_dir / "routines.json").load() == []

    saved = await gate.dispatch(
        ToolCall(id="c2", name="routine_save", arguments={"name": draft.name, "steps": step_dicts}),
        context,
        TurnState(),
        approve_all,
    )
    assert saved.success, saved.message

    approvals.clear()
    ran = await gate.dispatch(
        ToolCall(id="c3", name="routine_run", arguments={"name": draft.name}),
        context,
        TurnState(),
        approve_all,
    )
    assert ran.success, ran.message
    # Her adım SafetyGate.dispatch üzerinden yürür:
    # routine_run (SIDE_EFFECT, temiz tur) -> open_application (SIDE_EFFECT),
    # computer_use (CONFIRM_REQUIRED -> sorar), open_url (READ_ONLY, temiz tur).
    assert approvals == ["allow:computer_use"]
    assert [name for name, _args in FakeOpenApp.ran] == [
        "open_application",
        "computer_use",
        "open_url",
    ]
    audit_lines = [
        json.loads(line)
        for line in settings.audit_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    dispatched_tools = [
        row["tool"] for row in audit_lines if row["event"] == "tool_dispatch"
    ]
    assert dispatched_tools == [
        "routine_save",
        "routine_run",
        "open_application",
        "computer_use",
        "open_url",
    ]
