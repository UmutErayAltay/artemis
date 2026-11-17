"""UIA yetersizliğinde görsel OCR öğe çıkarıcı ve oturum entegrasyonu testleri."""

from __future__ import annotations

import json
import sys
import threading
from collections.abc import Callable
from time import monotonic
from types import SimpleNamespace
from typing import Any

import pytest

from artemis.brain.models import Completion, Usage
from artemis.computer import vision_elements as ve
from artemis.computer.session import ComputerSession
from artemis.computer.windows import UiElement
from artemis.config.settings import ComputerUseConfig


class _AuditSpy:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def emit(self, event: str, **fields: Any) -> None:
        self.events.append((event, fields))


def _approve_all(_kind: str, _args: dict[str, Any]) -> bool:
    return True


class _SteamLikeBackend:
    def __init__(self) -> None:
        self.clicks: list[tuple[int, int, str, bool]] = []

    def monitors(self) -> list[tuple[int, int, int, int]]:
        return [(0, 0, 1920, 1080)]

    def capture(self, left: int, top: int, width: int, height: int) -> tuple[bytes, int, int]:
        return b"\x00" * (width * height * 4), width, height

    def foreground_process(self) -> tuple[str, int]:
        return ("steamwebhelper.exe", 4242)

    def foreground_window(self) -> tuple[int, tuple[int, int, int, int]]:
        return (101, (0, 0, 1280, 800))

    def process_at(self, x: int, y: int) -> tuple[str, int]:
        return ("steamwebhelper.exe", 4242)

    def window_at(self, x: int, y: int) -> int:
        return 101

    def click(self, x: int, y: int, button: str = "left", double: bool = False) -> None:
        self.clicks.append((x, y, button, double))

    def scroll(self, x: int, y: int, notches: int) -> None:
        return None

    def type_text(self, text: str, should_continue: Callable[[], bool]) -> int:
        return len(text)

    def press_keys(self, keys: tuple[str, ...]) -> None:
        return None

    def focused_value_contains(self, expected: str, timeout: float) -> bool | None:
        return None

    def ui_elements(self, hwnd: int) -> list[UiElement]:
        return [
            UiElement(1, "Document", "Steam", (0, 0, 1280, 800), False, False),
            UiElement(2, "Button", "Kapat", (1240, 4, 32, 24), True, False),
        ]

    def vision_elements(self, hwnd: int, start_id: int) -> list[UiElement]:
        return [
            UiElement(start_id, "VisualText", "Deadlock", (120, 310, 90, 24), True, False),
            UiElement(start_id + 1, "VisualText", "Sil", (300, 310, 40, 24), True, False),
        ]


class _ScriptedProvider:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)

    async def complete(self, messages: Any, tools: Any, *, timeout: float) -> Completion:
        return Completion(content=self._responses.pop(0), tool_calls=[], usage=Usage())


def test_group_ocr_lines_splits_spaced_buttons_and_joins_phrase() -> None:
    lines = [
        [
            {"t": "MAĞAZA", "x": 20, "y": 10, "w": 70, "h": 20},
            {"t": "KÜTÜPHANE", "x": 140, "y": 10, "w": 110, "h": 20},
        ],
        [
            {"t": "Anadolu", "x": 30, "y": 60, "w": 75, "h": 20},
            {"t": "Rock", "x": 112, "y": 60, "w": 45, "h": 20},
        ],
    ]
    elements = ve.group_ocr_lines_to_elements(lines, offset_x=100, offset_y=200, start_id=3)
    assert [e.name for e in elements] == ["MAĞAZA", "KÜTÜPHANE", "Anadolu Rock"]
    assert [e.id for e in elements] == [3, 4, 5]
    assert elements[0].rect == (120, 210, 70, 20)
    assert elements[2].rect == (130, 260, 127, 20)
    assert all(e.kind == "VisualText" and e.interactive for e in elements)


def test_vision_elements_parses_runner_output_and_handles_bad_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    payload = {
        "left": 10,
        "top": 20,
        "lines": [[{"t": "Deadlock", "x": 50, "y": 80, "w": 90, "h": 22}]],
    }

    def ok_runner(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload))

    els = ve.vision_elements(999, runner=ok_runner)
    assert len(els) == 1
    assert els[0].name == "Deadlock"
    assert els[0].rect == (60, 100, 90, 22)

    def bad_runner(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(returncode=0, stdout="not-json")

    assert ve.vision_elements(999, runner=bad_runner) == []


@pytest.mark.asyncio
async def test_session_uses_vision_fallback_and_keeps_destructive_gate() -> None:
    backend = _SteamLikeBackend()
    audit = _AuditSpy()
    done_json = (
        '{"type": "done", "text": "Deadlock seçildi.", '
        '"reason": "Tamamlandı", "sensitive": false}'
    )
    provider = _ScriptedProvider(
        [
            '{"type": "click_element", "element": 4, "reason": "Sil tıkla", "sensitive": false}',
            '{"type": "click_element", "element": 3, "reason": "Deadlock seç", "sensitive": false}',
            done_json,
            '{"verified": true, "evidence": "Deadlock seçili görünüyor."}',
        ]
    )
    session = ComputerSession(
        provider=provider,
        backend=backend,
        config=ComputerUseConfig(observation="uia", max_steps=6),
        own_pid=9999,
        approve=_approve_all,
        stop=threading.Event(),
        on_step=lambda _msg: None,
        audit=audit,
        provider_timeout=5.0,
    )
    result = await session.run("Steam'de Deadlock'a tıkla", "turn-1", deadline=monotonic() + 30)
    assert result.reason == "done"
    assert result.verified is True
    assert backend.clicks == [(165, 322, "left", False)]
    assert any(ev[0] == "computer_vision_fallback" for ev in audit.events)
    assert any(ev[0] == "computer_destructive_blocked" for ev in audit.events)


class _DualMonitorScaledBackend(_SteamLikeBackend):
    def monitors(self) -> list[tuple[int, int, int, int]]:
        return [(0, 0, 1920, 1080), (1920, 0, 1920, 1080)]

    def foreground_window(self) -> tuple[int, tuple[int, int, int, int]]:
        return (101, (0, 0, 3840, 1080))


@pytest.mark.asyncio
async def test_dual_monitor_scales_primary_screenshot_coords_and_maps_secondary() -> None:
    backend = _DualMonitorScaledBackend()
    audit = _AuditSpy()
    provider = _ScriptedProvider(
        [
            '{"type": "click", "x": 400, "y": 300, "reason": "Ekran 1 tıkla", "sensitive": false}',
            '{"type": "click", "x": 2200, "y": 400, "reason": "Ekran 2 tıkla", "sensitive": false}',
            '{"type": "done", "text": "Tıklandı.", "reason": "Tamamlandı", "sensitive": false}',
            '{"verified": true, "evidence": "Tamam."}',
        ]
    )
    session = ComputerSession(
        provider=provider,
        backend=backend,
        config=ComputerUseConfig(observation="screen", screenshot_width=1280, max_steps=6),
        own_pid=9999,
        approve=_approve_all,
        stop=threading.Event(),
        on_step=lambda _msg: None,
        audit=audit,
        provider_timeout=5.0,
    )
    result = await session.run("İki ekrana tıkla", "turn-2", deadline=monotonic() + 30)
    assert result.reason == "done"
    assert backend.clicks == [(600, 450, "left", False), (2200, 400, "left", False)]


def test_vision_elements_survives_null_coordinates() -> None:
    import json as _json
    import sys as _sys
    from types import SimpleNamespace

    from artemis.computer import vision_elements as ve

    if _sys.platform != "win32":
        return
    word = {"t": "Oyna", "x": None, "y": 1, "w": 5, "h": 5}
    bad = _json.dumps({"left": 0, "top": 0, "lines": [[word]]})
    out = ve.vision_elements(
        5, runner=lambda *_a, **_k: SimpleNamespace(returncode=0, stdout=bad)
    )
    assert out == []
