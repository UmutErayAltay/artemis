"""UIA yetersizliğinde görsel OCR öğe çıkarıcı ve oturum entegrasyonu testleri."""

from __future__ import annotations

import json
import queue
import sys
import threading
from collections.abc import Callable
from time import monotonic
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


class _FakeStdin:
    def __init__(self, on_write: Callable[[str], None]) -> None:
        self._on_write = on_write
        self.written: list[str] = []

    def write(self, data: str) -> int:
        self.written.append(data)
        self._on_write(data)
        return len(data)

    def flush(self) -> None:
        return None


class _FakeStdout:
    def __init__(self, out_q: queue.Queue[str | None]) -> None:
        self._out_q = out_q

    def readline(self) -> str:
        item = self._out_q.get()
        return "" if item is None else item


class _FakeWorkerProc:
    """Sahte kalıcı PowerShell OCR süreci: her yazılan satır için yanıt kuyruğuna üretim yapar."""

    def __init__(self, responder: Callable[[str, int], str | None]) -> None:
        self._responder = responder
        self._out_q: queue.Queue[str | None] = queue.Queue()
        self.stdin = _FakeStdin(self._handle_write)
        self.stdout = _FakeStdout(self._out_q)
        self.killed = False
        self.call_count = 0

    def _handle_write(self, data: str) -> None:
        self.call_count += 1
        reply = self._responder(data, self.call_count)
        if reply != "__HANG__":
            self._out_q.put(reply)

    def poll(self) -> int | None:
        return -9 if self.killed else None

    def kill(self) -> None:
        self.killed = True
        self._out_q.put(None)

    def wait(self, timeout: float | None = None) -> int:
        return -9 if self.killed else 0


@pytest.fixture(autouse=True)
def _reset_vision_worker() -> Any:
    ve._shutdown_worker()
    yield
    ve._shutdown_worker()


def test_vision_elements_parses_runner_output_and_handles_bad_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    payload = {
        "left": 10,
        "top": 20,
        "lines": [[{"t": "Deadlock", "x": 50, "y": 80, "w": 90, "h": 22}]],
    }

    ok_proc = _FakeWorkerProc(lambda _req, _n: json.dumps(payload) + "\n")
    els = ve.vision_elements(999, runner=lambda: ok_proc)
    assert len(els) == 1
    assert els[0].name == "Deadlock"
    assert els[0].rect == (60, 100, 90, 22)

    bad_proc = _FakeWorkerProc(lambda _req, _n: "not-json\n")
    assert ve.vision_elements(999, runner=lambda: bad_proc) == []
    assert bad_proc.killed is True


def test_vision_worker_reused_across_multiple_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    starts: list[_FakeWorkerProc] = []

    def respond(req_line: str, call_idx: int) -> str:
        req = json.loads(req_line)
        word = f"Item{call_idx}_{req['hwnd']}"
        return (
            json.dumps(
                {
                    "left": 0,
                    "top": 0,
                    "lines": [[{"t": word, "x": 10, "y": 10, "w": 40, "h": 18}]],
                }
            )
            + "\n"
        )

    def factory() -> _FakeWorkerProc:
        proc = _FakeWorkerProc(respond)
        starts.append(proc)
        return proc

    first = ve.vision_elements(101, runner=factory)
    second = ve.vision_elements(202, runner=factory)
    assert len(starts) == 1
    assert len(starts[0].stdin.written) == 2
    assert [e.name for e in first] == ["Item1_101"]
    assert [e.name for e in second] == ["Item2_202"]
    assert starts[0].killed is False


def test_vision_worker_restarts_after_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    starts: list[_FakeWorkerProc] = []
    good_line = (
        json.dumps(
            {
                "left": 5,
                "top": 5,
                "lines": [[{"t": "Kurtuldu", "x": 10, "y": 10, "w": 60, "h": 20}]],
            }
        )
        + "\n"
    )

    def factory() -> _FakeWorkerProc:
        idx = len(starts)
        if idx == 0:
            proc = _FakeWorkerProc(lambda _req, _n: "__HANG__")
        else:
            proc = _FakeWorkerProc(lambda _req, _n: good_line)
        starts.append(proc)
        return proc

    assert ve.vision_elements(101, timeout=0.05, runner=factory) == []
    assert len(starts) == 1
    assert starts[0].killed is True

    recovered = ve.vision_elements(101, timeout=0.5, runner=factory)
    assert len(starts) == 2
    assert [e.name for e in recovered] == ["Kurtuldu"]


def test_vision_worker_restarts_after_eof_and_maintains_health_on_error_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    starts: list[_FakeWorkerProc] = []
    good_line = (
        json.dumps(
            {
                "left": 0,
                "top": 0,
                "lines": [[{"t": "Tamam", "x": 10, "y": 10, "w": 50, "h": 20}]],
            }
        )
        + "\n"
    )

    def factory() -> _FakeWorkerProc:
        if not starts:
            # İlk işçi EOF döner (çökmüş süreç).
            proc = _FakeWorkerProc(lambda _req, _n: None)
        else:
            # İkinci işçi ilk istekte {"error": ...}, ikincide geçerli yanıt döner.
            proc = _FakeWorkerProc(
                lambda _req, n: '{"error":"gecici"}\n' if n == 1 else good_line
            )
        starts.append(proc)
        return proc

    assert ve.vision_elements(101, runner=factory) == []
    assert len(starts) == 1
    assert starts[0].killed is True

    # {"error": ...} işçiyi öldürmemeli
    assert ve.vision_elements(101, runner=factory) == []
    assert len(starts) == 2
    assert starts[1].killed is False

    els = ve.vision_elements(101, runner=factory)
    assert len(starts) == 2
    assert [e.name for e in els] == ["Tamam"]


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


def test_vision_elements_survives_null_coordinates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    word = {"t": "Oyna", "x": None, "y": 1, "w": 5, "h": 5}
    bad = json.dumps({"left": 0, "top": 0, "lines": [[word]]}) + "\n"
    proc = _FakeWorkerProc(lambda _req, _n: bad)
    out = ve.vision_elements(5, runner=lambda: proc)
    assert out == []


@pytest.mark.skipif(sys.platform != "win32", reason="ctypes windll yalnız Windows'ta")
def test_vision_elements_handles_large_hwnd_without_overflow() -> None:
    # 0x80000001 boyutundaki tanıtıcı IsWindow çağrısında taşma fırlatmadan [] dönmeli.
    assert ve.vision_elements(0x80000001) == []


def test_vision_script_uses_per_monitor_v2_dpi_with_fallback_before_window_calls() -> None:
    script = ve._VISION_OCR_SCRIPT
    assert "SetProcessDpiAwarenessContext" in script
    assert "-4" in script
    assert "SetProcessDPIAware" in script
    call_idx = script.index("SetProcessDpiAwarenessContext([IntPtr](-4))")
    fallback_idx = script.index("SetProcessDPIAware()", call_idx)
    assert call_idx < fallback_idx < script.index("[ArtemisVisionNative]::DwmGetWindowAttribute")
    assert call_idx < script.index("[ArtemisVisionNative]::GetWindowRect")
    assert call_idx < script.index("OcrEngine")
