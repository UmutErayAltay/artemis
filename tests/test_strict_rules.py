"""Sıkı istem kuralları ve yıkıcı öğe kapısı testleri (ADR 0078)."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from artemis.brain.models import Completion, Message
from artemis.brain.strict_rules import destructive_unrequested, strict_rules_on
from artemis.computer.session import ComputerSession, SessionResult
from artemis.config.settings import ComputerUseConfig, ModelConfig

OWN_PID = 4242


@dataclass(frozen=True)
class _UiElement:
    id: int
    kind: str
    name: str
    rect: tuple[int, int, int, int]
    interactive: bool = True
    focused: bool = False


class _UiaBackend:
    def __init__(
        self,
        elements: list[_UiElement],
        visual_elements: list[_UiElement] | None = None,
    ) -> None:
        self.elements = elements
        self.visual_elements: list[_UiElement] = list(visual_elements or ())
        self.calls: list[tuple[Any, ...]] = []
        self.foreground = (r"C:\Program Files\App\app.exe", 100)
        self.window = (7, (0, 0, 800, 600))
        self.monitor_rects = [(0, 0, 800, 600)]

    def monitors(self) -> list[tuple[int, int, int, int]]:
        return list(self.monitor_rects)

    def capture(self, left: int, top: int, width: int, height: int) -> tuple[bytes, int, int]:
        return bytes(width * height * 4), width, height

    def foreground_process(self) -> tuple[str, int]:
        return self.foreground

    def foreground_window(self) -> tuple[int, tuple[int, int, int, int]]:
        return self.window

    def process_at(self, x: int, y: int) -> tuple[str, int]:
        return self.foreground

    def window_at(self, x: int, y: int) -> int:
        return self.window[0]

    def click(self, x: int, y: int, button: str = "left", double: bool = False) -> None:
        self.calls.append(("click", x, y, button, double))

    def scroll(self, x: int, y: int, notches: int) -> None:
        self.calls.append(("scroll", x, y, notches))

    def type_text(self, text: str, should_continue: Callable[[], bool]) -> int:
        self.calls.append(("type", text))
        return len(text)

    def press_keys(self, keys: tuple[str, ...]) -> None:
        self.calls.append(("keys", keys))

    def focused_value_contains(self, expected: str, timeout: float) -> bool | None:
        return None

    def ui_elements(self, hwnd: int) -> list[_UiElement]:
        return list(self.elements)

    def vision_elements(self, hwnd: int, start_id: int) -> list[_UiElement]:
        return list(self.visual_elements)


class _ScriptedVision:
    def __init__(self, *contents: str) -> None:
        self.contents = list(contents)
        self.seen: list[list[Message]] = []

    async def complete(
        self, messages: list[Message], tools: list[Any], *, timeout: float
    ) -> Completion:
        self.seen.append(list(messages))
        content = self.contents.pop(0) if len(self.contents) > 1 else self.contents[0]
        return Completion(content=content)


class _Audit:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def emit(self, event: str, **fields: Any) -> None:
        self.events.append((event, fields))

    def named(self, event: str) -> list[dict[str, Any]]:
        return [fields for name, fields in self.events if name == event]


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


def _act(**fields: Any) -> str:
    fields.setdefault("reason", "deneme")
    return json.dumps(fields)


_DONE = _act(type="done", text="Bitti.")
_YES = json.dumps({"verified": True, "evidence": "Tamam."})


async def _run_uia_session(
    goal: str,
    *contents: str,
    elements: list[_UiElement],
    visual_elements: list[_UiElement] | None = None,
    strict: bool = False,
) -> tuple[SessionResult, _UiaBackend, _ScriptedVision, _Audit]:
    backend = _UiaBackend(elements, visual_elements=visual_elements)
    provider = _ScriptedVision(*contents)
    audit = _Audit()
    clock = _Clock()
    config = ComputerUseConfig(
        enabled=True,
        model="m",
        mode="auto",
        observation="uia",
        step_delay_seconds=0,
    )
    session = ComputerSession(
        provider=provider,
        backend=backend,
        approve=lambda _n, _a: True,
        audit=audit,
        config=config,
        own_pid=OWN_PID,
        stop=threading.Event(),
        on_step=lambda _s: None,
        provider_timeout=15.0,
        clock=clock,
        sleep=clock.sleep,
        strict=strict,
    )
    result = await session.run(goal, "t1", None)
    return result, backend, provider, audit


def test_destructive_unrequested_cases() -> None:
    reason = destructive_unrequested("Hesabı sil", "duyuruyu oku")
    assert reason is not None and "yıkıcı" in reason

    assert destructive_unrequested("Hesabı sil", "hesabımı sil") is None

    logout_reason = destructive_unrequested("Çıkış", "dosyayı kaydet")
    assert logout_reason is not None

    assert destructive_unrequested("Kaydet", "duyuruyu oku") is None

    upper_reason = destructive_unrequested("SİL", "duyuruyu oku")
    assert upper_reason is not None


def test_destructive_unrequested_family_permission_comes_from_user_text() -> None:
    # (a) goal'da sözcük var, kullanıcı söylememiş: engellenir.
    assert (
        destructive_unrequested("Hesabı sil", "hesabımı sil", user_text="duyuruyu oku")
        is not None
    )
    # (b) ikisi de söylüyor: açılır.
    assert destructive_unrequested("Hesabı sil", "hesabımı sil", user_text="hesabımı sil") is None
    # (c) user_text None: eski davranış.
    assert destructive_unrequested("Hesabı sil", "hesabımı sil") is None
    assert destructive_unrequested("Hesabı sil", "duyuruyu oku") is not None
    # (d) boş user_text kapalı başarısız olur.
    assert destructive_unrequested("Hesabı sil", "hesabımı sil", user_text="") is not None
    # Olumsuz biçim izin değildir.
    assert (
        destructive_unrequested(
            "Hesabı sil", "hesabımı sil", user_text="hesabımı silmeden kapat"
        )
        is not None
    )


def _model(**fields: object) -> ModelConfig:
    return ModelConfig.model_validate(
        {"input_per_million": 0, "output_per_million": 0, **fields}
    )


def test_strict_rules_on_defaults_and_overrides() -> None:
    assert strict_rules_on(None) is False
    assert (
        strict_rules_on(_model(provider="anthropic", model="claude-sonnet-4-20250514"))
        is False
    )
    assert strict_rules_on(_model(provider="claude_cli", model="sonnet")) is False
    assert (
        strict_rules_on(_model(provider="openrouter", model="anthropic/claude-3.5-sonnet"))
        is False
    )
    assert strict_rules_on(_model(provider="openrouter", model="openai/gpt-4o")) is True
    assert (
        strict_rules_on(
            _model(provider="openrouter", model="openai/gpt-4o", strict_rules=False)
        )
        is False
    )


async def test_session_destructive_gate_blocks_unrequested_in_auto_mode() -> None:
    elements = [_UiElement(id=1, kind="Button", name="Hesabı sil", rect=(10, 10, 80, 30))]
    click_del = _act(type="click_element", element=1, reason="Düğmeye tıkla")

    result, backend, provider, audit = await _run_uia_session(
        "Duyuruyu oku", click_del, _DONE, _YES, elements=elements
    )

    assert result.reason == "done"
    assert backend.calls == []
    second_user_prompt = str(provider.seen[1][1].content)
    assert "ENGELLENDİ (click_element)" in second_user_prompt
    assert "yıkıcı bir öğe" in second_user_prompt
    destructive_events = audit.named("computer_destructive_blocked")
    assert destructive_events == [{"turn_id": "t1", "element_kind": "Button"}]
    assert "Hesabı sil" not in repr(destructive_events)


async def test_session_destructive_gate_allows_when_goal_requests_it() -> None:
    elements = [_UiElement(id=1, kind="Button", name="Hesabı sil", rect=(10, 10, 80, 30))]
    click_del = _act(type="click_element", element=1, reason="Hesabı sil düğmesine tıkla")

    result, backend, _provider, audit = await _run_uia_session(
        "Hesabımı sil", click_del, _DONE, _YES, elements=elements
    )

    assert result.reason == "done"
    assert backend.calls == [("click", 50, 25, "left", False)]
    assert audit.named("computer_destructive_blocked") == []


async def test_session_strict_prompt_toggle() -> None:
    elements = [_UiElement(id=1, kind="Button", name="Tamam", rect=(10, 10, 80, 30))]

    _, _, strict_provider, _ = await _run_uia_session(
        "Tamam'a bas", _DONE, _YES, elements=elements, strict=True
    )
    assert "STRICT RULES" in str(strict_provider.seen[0][0].content)

    _, _, normal_provider, _ = await _run_uia_session(
        "Tamam'a bas", _DONE, _YES, elements=elements, strict=False
    )
    assert "STRICT RULES" not in str(normal_provider.seen[0][0].content)


def test_gate_negated_goal_does_not_open_and_new_families() -> None:
    assert destructive_unrequested("Sil", "hiçbir şeyi silme, sadece listele") is not None
    assert destructive_unrequested("Sil", "dosyaları silmeden önce göster") is not None
    assert destructive_unrequested("Sil", "dosyayı silmek istiyorum") is None
    assert destructive_unrequested("Çöp kutusunu boşalt", "notu aç") is not None
    assert destructive_unrequested("Discard changes", "save the file") is not None
    assert destructive_unrequested("Don’t Save", "Not Defteri'ni kapat") is not None
    assert destructive_unrequested("Kaydetme", "kaydetmeden kapat") is None
    # Her içerik sözcüğü hedefte geçmeli: "veriyi sil" hesabı silmeye izin vermez.
    assert destructive_unrequested("Hesabı ve veriyi sil", "veriyi sil") is not None
    assert destructive_unrequested("Hesabı ve veriyi sil", "hesabımı ve verimi sil") is None


def test_space_and_delete_cannot_repeat() -> None:
    import pytest
    from pydantic import ValidationError

    from artemis.computer.actions import Action

    for keys in ("space", "delete"):
        with pytest.raises(ValidationError):
            Action.model_validate({"type": "key", "keys": keys, "repeat": 3, "reason": "x"})


async def test_session_gate_blocks_ctrl_d_without_delete_goal() -> None:
    elements = [_UiElement(id=1, kind="ListItem", name="rapor.docx", rect=(10, 10, 80, 30))]
    press = _act(type="key", keys="ctrl+d", reason="Kısayol", sensitive=False)
    result, backend, _provider, audit = await _run_uia_session(
        "Raporu aç", press, _DONE, _YES, elements=elements
    )
    assert result.reason == "done"
    assert backend.calls == []
    assert audit.named("computer_destructive_blocked") == [
        {"turn_id": "t1", "element_kind": "Key"}
    ]


def test_gate_needs_matching_target_and_strong_stem() -> None:
    # Aynı aile yetmez: "duyuruyu sil" hedefi "Hesabı sil" düğmesini açmaz.
    assert destructive_unrequested("Hesabı sil", "duyuruyu sil") is not None
    assert destructive_unrequested("Sil", "duyuruyu sil") is None
    # Zayıf kök ("oturum") çıkış ailesini açmaz.
    assert destructive_unrequested("Çıkış", "oturumu kaydet") is not None
    assert destructive_unrequested("Çıkış", "çıkış yap") is None


async def test_session_gate_blocks_unrequested_delete_key() -> None:
    elements = [_UiElement(id=1, kind="ListItem", name="rapor.docx", rect=(10, 10, 80, 30))]
    for keys in ("delete", "shift+delete"):
        press = _act(type="key", keys=keys, reason="Temizle", sensitive=False)
        result, backend, _provider, audit = await _run_uia_session(
            "Raporu aç", press, _DONE, _YES, elements=elements
        )
        assert result.reason == "done"
        assert backend.calls == []
        assert audit.named("computer_destructive_blocked") == [
            {"turn_id": "t1", "element_kind": "Key"}
        ]
    press = _act(type="key", keys="delete", reason="Sil", sensitive=True)
    _result, backend, _provider, _audit = await _run_uia_session(
        "Dosyayı sil", press, _DONE, _YES, elements=elements
    )
    assert backend.calls != []


async def test_session_gate_blocks_enter_on_focused_destructive_element() -> None:
    elements = [
        _UiElement(id=1, kind="Button", name="Hesabı sil", rect=(10, 10, 80, 30), focused=True)
    ]
    enter = _act(type="key", keys="enter", reason="Onayla", sensitive=True)
    result, backend, _provider, audit = await _run_uia_session(
        "Duyuruyu oku", enter, _DONE, _YES, elements=elements
    )
    assert result.reason == "done"
    assert backend.calls == []
    assert audit.named("computer_destructive_blocked") == [
        {"turn_id": "t1", "element_kind": "Button"}
    ]


def test_gate_english_and_passive_negation_does_not_open() -> None:
    for goal in ("don't delete anything, just list", "do not remove the files",
                 "close it without deleting", "never erase my notes", "don’t delete it",
                 "dosyalar asla silinmesin", "eklenti kaldırılmasın", "sakın silme"):
        assert destructive_unrequested("Delete", goal) is not None, goal
    assert destructive_unrequested("Delete", "delete the old file") is None
    assert destructive_unrequested("Sil", "dosyayı sil, klasörü değil") is None
    assert destructive_unrequested("Don't save", "close without saving, choose don't save") is None


def test_destructive_caps_english_labels_blocked() -> None:
    for name in ("UNINSTALL", "DISCARD", "SIGN OUT", "WIPE", "SIL"):
        assert destructive_unrequested(name, "open settings") is not None, name


def test_uninstall_allowed_when_goal_requests_it() -> None:
    assert destructive_unrequested("UNINSTALL", "uninstall spotify") is None
    assert destructive_unrequested("Uninstall Spotify", "uninstall spotify") is None


def test_caps_english_negated_goal_still_blocked() -> None:
    assert destructive_unrequested("UNINSTALL", "don't uninstall spotify") is not None


def _make_direct_uia_session(
    elements: list[Any], *, used_vision: bool = False
) -> tuple[ComputerSession, _UiaBackend, _Audit]:
    backend = _UiaBackend(elements)
    audit = _Audit()
    clock = _Clock()
    config = ComputerUseConfig(
        enabled=True, model="m", mode="auto", observation="uia", step_delay_seconds=0
    )
    session = ComputerSession(
        provider=_ScriptedVision(_DONE, _YES),
        backend=backend,
        approve=lambda _n, _a: True,
        audit=audit,
        config=config,
        own_pid=OWN_PID,
        stop=threading.Event(),
        on_step=lambda _s: None,
        provider_timeout=15.0,
        clock=clock,
        sleep=clock.sleep,
    )
    session._goal = "Duyuruyu oku"
    session._elements = {item.id: item for item in elements}
    session._last_used_vision = used_vision
    return session, backend, audit


def _construct_type_action(text: str, reason: str = "Onayla") -> Any:
    from artemis.computer.actions import Action

    return Action.model_construct(
        type="type",
        text=text,
        reason=reason,
        screen=1,
        x=None,
        y=None,
        element=None,
        keys=None,
        button="left",
        double=False,
        dy=None,
        seconds=None,
        ratio=None,
        sensitive=False,
        observed=None,
        progress=None,
    )


async def test_session_gate_blocks_type_newline_on_focused_destructive_element() -> None:
    from artemis.computer.windows import UiElement

    elements = [
        UiElement(
            id=1,
            kind="Button",
            name="Hesabı sil",
            rect=(10, 10, 80, 30),
            interactive=True,
            focused=True,
        )
    ]
    for newline_text in ("\n", "\r\n", "merhaba\n", "tamam\r"):
        # parse_action satır sonunu zaten şema katmanında reddedebilir; kapı doğrudan doğrulanır.
        session, backend, audit = _make_direct_uia_session(elements)
        reason = await session._act(_construct_type_action(newline_text), "t1", [], None, 100.0)
        assert isinstance(reason, str) and "yıkıcı bir öğe" in reason
        assert backend.calls == []
        assert audit.named("computer_destructive_blocked") == [
            {"turn_id": "t1", "element_kind": "Button"}
        ]


async def test_session_gate_blocks_type_space_on_focused_destructive_element() -> None:
    elements = [
        _UiElement(id=1, kind="Button", name="Hesabı sil", rect=(10, 10, 80, 30), focused=True)
    ]
    type_space = _act(type="type", text=" ", reason="Boşluk ile etkinleştir")
    result, backend, _provider, audit = await _run_uia_session(
        "Duyuruyu oku", type_space, _DONE, _YES, elements=elements
    )
    assert result.reason == "done"
    assert backend.calls == []
    assert audit.named("computer_destructive_blocked") == [
        {"turn_id": "t1", "element_kind": "Button"}
    ]


async def test_session_gate_allows_ordinary_type_into_focused_edit() -> None:
    elements = [
        _UiElement(id=1, kind="Edit", name="Arama", rect=(10, 10, 120, 30), focused=True)
    ]
    type_text = _act(type="type", text="merhaba dünya", reason="Arama alanına yaz")
    result, backend, _provider, audit = await _run_uia_session(
        "Duyuruyu oku", type_text, _DONE, _YES, elements=elements
    )
    assert result.reason == "done"
    assert backend.calls == [("type", "merhaba dünya")]
    assert audit.named("computer_destructive_blocked") == []


async def test_session_gate_blocks_enter_on_vision_list_without_focus() -> None:
    visual = [
        _UiElement(
            id=1, kind="VisualText", name="Hesabı sil", rect=(10, 10, 80, 30), focused=False
        )
    ]
    enter = _act(type="key", keys="enter", reason="Onayla")
    result, backend, provider, audit = await _run_uia_session(
        "Duyuruyu oku", enter, _DONE, _YES, elements=[], visual_elements=visual
    )
    assert result.reason == "done"
    assert backend.calls == []
    assert audit.named("computer_destructive_blocked") == []
    blocked = audit.named("computer_blocked")
    assert len(blocked) == 1
    assert "görsel öğe listesi" in blocked[0]["reason"]
    assert "click_element" in blocked[0]["reason"]
    assert "Hesabı sil" not in blocked[0]["reason"]
    assert "click_element" in str(provider.seen[1][1].content)


async def test_session_gate_blocks_type_space_on_vision_list_without_focus() -> None:
    visual = [
        _UiElement(
            id=1, kind="VisualText", name="Hesabı sil", rect=(10, 10, 80, 30), focused=False
        )
    ]
    type_space = _act(type="type", text=" ", reason="Onayla")
    result, backend, _provider, audit = await _run_uia_session(
        "Duyuruyu oku", type_space, _DONE, _YES, elements=[], visual_elements=visual
    )
    assert result.reason == "done"
    assert backend.calls == []
    assert audit.named("computer_destructive_blocked") == []
    blocked = audit.named("computer_blocked")
    assert len(blocked) == 1 and "görsel öğe listesi" in blocked[0]["reason"]


async def test_session_gate_blocks_type_newline_on_vision_list_without_focus() -> None:
    from artemis.computer.windows import UiElement

    visual = [
        UiElement(
            id=1,
            kind="VisualText",
            name="Hesabı sil",
            rect=(10, 10, 80, 30),
            interactive=True,
            focused=False,
        )
    ]
    session, backend, audit = _make_direct_uia_session(visual, used_vision=True)
    reason = await session._act(_construct_type_action("\n"), "t1", [], None, 100.0)
    assert isinstance(reason, str) and "görsel öğe listesi" in reason
    assert "click_element" in reason and "Hesabı sil" not in reason
    assert backend.calls == []
    assert audit.named("computer_destructive_blocked") == []


async def test_session_gate_allows_enter_on_uia_list_without_focus() -> None:
    elements = [
        _UiElement(id=1, kind="Button", name="Hesabı sil", rect=(10, 10, 80, 30), focused=False)
    ]
    enter = _act(type="key", keys="enter", reason="Onayla")
    result, backend, _provider, audit = await _run_uia_session(
        "Duyuruyu oku", enter, _DONE, _YES, elements=elements
    )
    assert result.reason == "done"
    assert backend.calls == [("keys", ("enter",))]
    assert audit.named("computer_destructive_blocked") == []
    assert audit.named("computer_blocked") == []


def test_strict_chat_rules_direct_explicit_requests_to_tool_call() -> None:
    from artemis.brain.strict_rules import STRICT_CHAT_RULES

    normalized = " ".join(STRICT_CHAT_RULES.split())
    assert "metinle ek onay sorma" in normalized
    assert "aracı doğrudan çağır" in normalized
