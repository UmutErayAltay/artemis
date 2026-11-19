"""ADR 0062 seçili metin eylemleri kabul doğrulaması: mantık ve isteğe bağlı masaüstü."""

from __future__ import annotations

import argparse
import asyncio
import io
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import artemis.channels.desktop.bridge as bridge_module  # noqa: E402
from artemis.brain.fake import ScriptedProvider  # noqa: E402
from artemis.brain.models import Completion, Message, ToolCall  # noqa: E402
from artemis.channels.desktop.bridge import DesktopBridge, State  # noqa: E402
from artemis.channels.desktop.dictation import (  # noqa: E402
    DictationBackend,
    windows_backend,
)
from artemis.channels.desktop.selection import (  # noqa: E402
    _NO_SELECTION,
    _PRIVACY,
    MAX_CHARS,
    SelectionActions,
    SelectionBackend,
    windows_selection_backend,
)
from artemis.channels.runtime import Runtime, create_runtime  # noqa: E402
from artemis.config.settings import (  # noqa: E402
    ModelConfig,
    SelectionActionsConfig,
    Settings,
)
from artemis.i18n import t  # noqa: E402
from artemis.safety.audit import AuditLog  # noqa: E402
from artemis.tools.base import ToolDefinition  # noqa: E402
from artemis.tools.registry import ToolRegistry  # noqa: E402

OWN_PID = 111
TARGET = 7
SECRET = "GIZLI-METIN-ORNEGI"  # sahte test metni, gerçek sır değil; anahtarlik: yoksay


class FakeHotkey:
    def start(self) -> bool:
        return True

    def stop(self) -> None:
        pass


class World:
    """Sahte Windows: pano, seçim, ön plan penceresi ve yazılan metin."""

    def __init__(self, selection: str | None = SECRET) -> None:
        self.clip = "ONCEKI PANO"
        self.selection = selection
        self.window = TARGET
        self.typed = ""
        self.keys_down = 0
        self.copy_error: Exception | None = None
        self.read_error: Exception | None = None
        self.nontext = False
        self.copy_calls = 0
        self.focus_works = True
        self.focus_calls: list[int] = []
        self.clip_history: list[str] = []

    def type_text(self, text: str, should_continue: Callable[[], bool]) -> int:
        sent = 0
        for char in text:
            if not should_continue():
                break
            self.typed += char
            sent += 1
        return sent

    def write(self, text: str) -> bool:
        self.clip = text
        self.clip_history.append(text)
        return True

    def read(self) -> str:
        if self.read_error is not None:
            raise self.read_error
        return self.clip

    def copy(self) -> None:
        self.copy_calls += 1
        if self.copy_error is not None:
            raise self.copy_error
        if self.selection is not None:
            self.clip = self.selection

    def modifiers_down(self) -> bool:
        if self.keys_down > 0:
            self.keys_down -= 1
            return True
        return False

    def focus(self, hwnd: int) -> bool:
        self.focus_calls.append(hwnd)
        return self.focus_works

    def backend(self) -> SelectionBackend:
        window = DictationBackend(
            lambda: self.window, lambda: 222, self.type_text, lambda press: FakeHotkey()
        )
        return SelectionBackend(
            window,
            self.read,
            self.write,
            self.copy,
            self.modifiers_down,
            self.focus,
            lambda: self.nontext,
        )


class Audit:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def emit(self, event: str, **fields: Any) -> None:
        self.events.append((event, fields))

    def named(self, name: str) -> list[dict[str, Any]]:
        return [fields for event, fields in self.events if event == name]


class Rig:
    def __init__(
        self,
        *,
        selection: str | None = SECRET,
        cloud_allowed: bool = True,
        local_alias: str | None = None,
        answer: Completion | None = None,
        backend: SelectionBackend | None = None,
    ) -> None:
        self.world = World(selection)
        self.audit = Audit()
        self.emitted: list[tuple[str, str]] = []
        self.calls: list[tuple[list[Message], str | None]] = []
        self.answer = answer or Completion(content="Çeviri sonucu", model="m")
        self.dictation_busy = False
        self.error: Exception | None = None
        self.actions = SelectionActions(
            audit=self.audit,
            backend=backend if backend is not None else self.world.backend(),
            complete=self._complete,
            emit=lambda kind, text: self.emitted.append((kind, text)),
            translate_to="de",
            cloud_allowed=cloud_allowed,
            local_alias=local_alias,
            other_busy=lambda: self.dictation_busy,
            own_pid=OWN_PID,
            timeout=2.0,
            copy_wait=0.1 if backend is None else 1.0,
            key_wait=0.1 if backend is None else 1.5,
            poll=0.001 if backend is None else 0.03,
        )

    async def _complete(
        self, messages: list[Message], model: str | None, timeout: float
    ) -> Completion:
        self.calls.append((messages, model))
        if self.error is not None:
            raise self.error
        return self.answer

    async def capture(self) -> None:
        self.actions.start_capture()
        await self.actions.wait()

    async def run(self, action: str = "translate") -> None:
        self.actions.start_run(action)
        await self.actions.wait()

    async def write_back(self) -> None:
        self.actions.start_write_back()
        await self.actions.wait()

    def kinds(self, kind: str) -> list[str]:
        return [text for name, text in self.emitted if name == kind]


class RecordingProvider(ScriptedProvider):
    def __init__(self, responses: list[Completion]) -> None:
        super().__init__(responses)
        self.tools: list[list[ToolDefinition]] = []

    async def complete(self, messages, tools, *, timeout, stream=None):  # type: ignore[no-untyped-def]
        self.tools.append(list(tools))
        return await super().complete(messages, tools, timeout=timeout, stream=stream)


def _bridge(tmp_path: Path, provider: RecordingProvider, *, enabled: bool = True) -> DesktopBridge:
    settings = Settings(
        workspace=tmp_path,
        state_dir=tmp_path / "state",
        audit_path=tmp_path / "audit.jsonl",
        primary_model="fake",
        allow_cloud_clipboard=True,
        models={
            "fake": ModelConfig(
                provider="ollama",
                model="fake",
                input_per_million=Decimal(0),
                output_per_million=Decimal(0),
            )
        },
        selection_actions=SelectionActionsConfig(enabled=enabled),
    )

    def factory(config: Path, approve: Any, on_record: Any) -> Runtime:
        return create_runtime(
            config,
            approve,
            settings=settings,
            provider=provider,
            registry=ToolRegistry(),
            audit=AuditLog(settings.audit_path, on_record),
        )

    bridge = DesktopBridge(tmp_path / "unused.yaml", factory)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if any(isinstance(e, State) and e.state == "ready" for e in bridge.poll_events()):
            return bridge
        time.sleep(0.01)
    raise AssertionError("köprü hazır olmadı")


def _drain(bridge: DesktopBridge, until: Callable[[list[Any]], bool]) -> list[Any]:
    events: list[Any] = []
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        events += bridge.poll_events()
        if until(events):
            return events
        time.sleep(0.01)
    raise AssertionError(f"beklenen olay gelmedi: {events}")


def _has(events: list[Any], state: str) -> bool:
    return any(isinstance(e, State) and e.state == state for e in events)


def _remove_tree(root: Path) -> None:
    if not root.exists():
        return
    last_error: OSError | None = None
    for attempt in range(5):
        try:
            shutil.rmtree(root)
            return
        except OSError as error:
            last_error = error
            time.sleep(0.1 * (attempt + 1))
    print(f"Temizleme uyarisi ({root}): {last_error}", file=sys.stderr)


def check_132(_base: Path) -> tuple[bool, str]:
    async def _run() -> tuple[bool, str]:
        full_len = MAX_CHARS + 5
        rig = Rig(selection="k" * full_len)
        await rig.capture()
        ready_msgs = rig.kinds("ready")
        note = t(" (ilk {max_chars} karakter alındı)").format(max_chars=MAX_CHARS)
        full_msg = t("{chars} karakter seçildi{note}.").format(chars=full_len, note="")
        msg = ready_msgs[0] if ready_msgs else ""
        has_note = note in msg and "ilk 6000 karakter alındı" in msg
        not_full = msg != full_msg and str(full_len) not in msg
        captured_len = len(rig.actions._text)
        ok = bool(ready_msgs) and has_note and not_full and captured_len == MAX_CHARS
        detail = (
            f"captured={captured_len}, has_note={has_note}, "
            f"not_full={not_full}, msg={msg!r} (mantık)"
        )
        return ok, detail

    return asyncio.run(_run())


def check_133(_base: Path) -> tuple[bool, str]:
    async def _run() -> tuple[bool, str]:
        rig = Rig(cloud_allowed=False, local_alias=None)
        await rig.capture()
        messages = rig.kinds("message")
        expected = t(_PRIVACY)
        got = messages[-1] if messages else ""
        msg_ok = got == expected
        copy_calls = rig.world.copy_calls
        clip_writes = len(rig.world.clip_history)
        model_calls = len(rig.calls)
        ok = msg_ok and copy_calls == 0 and clip_writes == 0 and model_calls == 0
        detail = (
            f"msg_ok={msg_ok}, copy_calls={copy_calls}, "
            f"clip_writes={clip_writes}, model_calls={model_calls} (mantık)"
        )
        return ok, detail

    return asyncio.run(_run())


def check_135(base: Path) -> tuple[bool, str]:
    world = World(SECRET)
    answer = Completion(
        content="Özet: bir istek var.",
        tool_calls=[ToolCall(id="1", name="delete_file", arguments={"path": "x"})],
    )
    provider = RecordingProvider([answer])
    with (
        patch.object(
            bridge_module, "windows_selection_backend", lambda window: world.backend()
        ),
        patch.object(bridge_module, "windows_backend", lambda: world.backend().window),
    ):
        bridge = _bridge(base, provider, enabled=True)
        try:
            runtime = bridge._runtime
            if runtime is None:
                return False, "runtime None (mantık)"
            hk_ok = bridge.selection_hotkey()
            _drain(bridge, lambda e: _has(e, "selection_ready"))
            run_ok = bridge.selection_run("summarize")
            _drain(bridge, lambda e: _has(e, "selection_result"))
            conv_msgs = len(runtime.runner.conversation.render().messages)
        finally:
            bridge.shutdown()

        audit_path = base / "audit.jsonl"
        audit_raw = audit_path.read_text("utf-8") if audit_path.exists() else ""
        has_audit_event = "selection_action" in audit_raw
        secret_in_audit = SECRET in audit_raw
        secret_bytes = SECRET.encode("utf-8")
        leaked_files: list[str] = []
        state_dir = base / "state"
        for file_path in state_dir.rglob("*") if state_dir.exists() else []:
            try:
                is_reg = file_path.is_file()
            except OSError:
                continue
            if is_reg and secret_bytes in file_path.read_bytes():
                leaked_files.append(file_path.name)
        if audit_path.is_file() and secret_bytes in audit_path.read_bytes():
            leaked_files.append(audit_path.name)
        tools_empty = provider.tools == [[]]
        ok = (
            hk_ok
            and run_ok
            and tools_empty
            and conv_msgs == 0
            and has_audit_event
            and not secret_in_audit
            and len(leaked_files) == 0
        )
        detail = (
            f"conv_msgs={conv_msgs}, audit_event={has_audit_event}, "
            f"secret_in_audit={secret_in_audit}, leaked_files={leaked_files}, "
            f"tools_empty={tools_empty} (mantık)"
        )
        return ok, detail


def check_136(base: Path) -> tuple[bool, str]:
    default_off = SelectionActionsConfig().enabled is False
    bridge = _bridge(base, RecordingProvider([]), enabled=False)
    try:
        hk_ret = bridge.selection_hotkey()
        time.sleep(0.2)
        selection_none = bridge._selection is None
        sel_events = [
            e.state
            for e in bridge.poll_events()
            if isinstance(e, State) and e.state.startswith("selection_")
        ]
        ok = default_off and hk_ret is True and selection_none and len(sel_events) == 0
        detail = (
            f"default_off={default_off}, hotkey_ret={hk_ret}, "
            f"selection_is_none={selection_none}, events={sel_events} (mantık)"
        )
        return ok, detail
    finally:
        bridge.shutdown()


def _wait_notepad_foreground(
    win_backend: DictationBackend, proc: subprocess.Popen[bytes], timeout: float = 5.0
) -> int | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if win_backend.foreground_pid() == proc.pid:
                return win_backend.foreground_window()
        except OSError:
            pass
        time.sleep(0.05)
    return None


def _masaustu_checks(base: Path) -> list[tuple[str, str, str]]:
    from artemis.computer import windows as win_comp

    results: list[tuple[str, str, str]] = []
    win_back = windows_backend()
    sel_back = windows_selection_backend(win_back)
    try:
        original_clip = sel_back.read_clipboard()
    except OSError:
        original_clip = ""

    try:
        # 5: sec-127-pano-geri-yukleme
        proc1 = subprocess.Popen(["notepad.exe"])
        try:
            hwnd = _wait_notepad_foreground(win_back, proc1)
            if hwnd is None:
                results.append(("FAIL", "sec-127-pano-geri-yukleme", "Notepad odaklanamadı"))
            else:
                win_comp.type_text("DENEME", lambda: True)
                win_comp.press_keys(("ctrl", "a"))
                time.sleep(0.1)
                sel_back.write_clipboard("ONCEKI")
                rig = Rig(backend=sel_back)
                asyncio.run(rig.capture())
                restored = sel_back.read_clipboard()
                ready = rig.kinds("ready")
                ok = restored == "ONCEKI" and len(ready) == 1
                results.append(
                    (
                        "PASS" if ok else "FAIL",
                        "sec-127-pano-geri-yukleme",
                        f"clip={restored!r}, ready={ready}",
                    )
                )
        finally:
            proc1.terminate()
            proc1.wait(timeout=5)

        # 6: sec-128-secim-yok
        proc2 = subprocess.Popen(["notepad.exe"])
        try:
            hwnd = _wait_notepad_foreground(win_back, proc2)
            if hwnd is None:
                results.append(("FAIL", "sec-128-secim-yok", "Notepad odaklanamadı"))
            else:
                rig = Rig(backend=sel_back)
                asyncio.run(rig.capture())
                msgs = rig.kinds("message")
                expected_no_sel = t(_NO_SELECTION)
                got_msg = msgs[-1] if msgs else ""
                ok = got_msg == expected_no_sel and len(rig.calls) == 0
                results.append(
                    (
                        "PASS" if ok else "FAIL",
                        "sec-128-secim-yok",
                        f"msg={got_msg!r}, calls={len(rig.calls)}",
                    )
                )
        finally:
            proc2.terminate()
            proc2.wait(timeout=5)

        # 7: sec-130-hedef-degisti
        proc3a = subprocess.Popen(["notepad.exe"])
        proc3b: subprocess.Popen[bytes] | None = None
        try:
            hwnd_a = _wait_notepad_foreground(win_back, proc3a)
            if hwnd_a is None:
                results.append(("FAIL", "sec-130-hedef-degisti", "İlk Notepad odaklanamadı"))
            else:
                win_comp.type_text("HEDEF", lambda: True)
                win_comp.press_keys(("ctrl", "a"))
                time.sleep(0.1)
                no_focus_back = SelectionBackend(
                    sel_back.window,
                    sel_back.read_clipboard,
                    sel_back.write_clipboard,
                    sel_back.send_copy,
                    sel_back.modifiers_down,
                    lambda _h: False,
                    sel_back.has_nontext_clipboard,
                )
                rig = Rig(backend=no_focus_back)
                asyncio.run(rig.capture())
                asyncio.run(rig.run("translate"))
                proc3b = subprocess.Popen(["notepad.exe"])
                _wait_notepad_foreground(win_back, proc3b)
                asyncio.run(rig.write_back())
                msgs = rig.kinds("message")
                events = rig.audit.named("selection_write")
                outcome = events[-1]["outcome"] if events else ""
                typed = events[-1]["typed"] if events else -1
                ok = outcome == "no_target" and typed == 0 and any("değişti" in m for m in msgs)
                results.append(
                    (
                        "PASS" if ok else "FAIL",
                        "sec-130-hedef-degisti",
                        f"outcome={outcome}, typed={typed}, msgs={msgs}",
                    )
                )
        finally:
            if proc3b is not None:
                proc3b.terminate()
                proc3b.wait(timeout=5)
            proc3a.terminate()
            proc3a.wait(timeout=5)

        # 8: sec-131-esc
        proc4 = subprocess.Popen(["notepad.exe"])
        try:
            hwnd = _wait_notepad_foreground(win_back, proc4)
            if hwnd is None:
                results.append(("FAIL", "sec-131-esc", "Notepad odaklanamadı"))
            else:
                win_comp.type_text("KAYNAK", lambda: True)
                win_comp.press_keys(("ctrl", "a"))
                time.sleep(0.1)
                long_answer = Completion(content="u" * 400, model="m")
                rig = Rig(backend=sel_back, answer=long_answer)
                asyncio.run(rig.capture())
                asyncio.run(rig.run("translate"))

                def _send_esc() -> None:
                    time.sleep(0.08)
                    win_comp.press_keys(("escape",))

                esc_thread = threading.Thread(target=_send_esc, daemon=True)
                esc_thread.start()
                asyncio.run(rig.write_back())
                esc_thread.join(timeout=2.0)
                msgs = rig.kinds("message")
                last_msg = msgs[-1] if msgs else ""
                ok = "yarım kaldı" in last_msg.lower()
                results.append(
                    (
                        "PASS" if ok else "FAIL",
                        "sec-131-esc",
                        f"msg={last_msg!r}",
                    )
                )
        finally:
            proc4.terminate()
            proc4.wait(timeout=5)
    finally:
        sel_back.write_clipboard(original_clip)

    return results


def main() -> int:
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if isinstance(sys.stderr, io.TextIOWrapper):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="ADR 0062 seçili metin eylemleri kabul testi")
    parser.add_argument("--masaustu", action="store_true", default=False)
    args = parser.parse_args()

    temp_root = Path(tempfile.mkdtemp(prefix="artemis_kabul_0062_"))
    pass_count = 0
    fail_count = 0
    skip_count = 0

    logic_checks: list[tuple[str, Callable[[Path], tuple[bool, str]]]] = [
        ("sec-132-6000-karakter", check_132),
        ("sec-133-bulut-kapali-ret", check_133),
        ("sec-135-gecmis-bellek-temiz", check_135),
        ("sec-136-kisayol-kapali", check_136),
    ]
    masaustu_ids = [
        "sec-127-pano-geri-yukleme",
        "sec-128-secim-yok",
        "sec-130-hedef-degisti",
        "sec-131-esc",
    ]

    try:
        for item_id, fn in logic_checks:
            item_dir = temp_root / item_id
            item_dir.mkdir(parents=True, exist_ok=True)
            try:
                ok, detail = fn(item_dir)
            except Exception as error:  # noqa: BLE001 - madde siniri
                ok = False
                detail = f"{type(error).__name__}: {error} (mantık)"
            clean_detail = " ".join(str(detail).splitlines()).strip()
            if ok:
                pass_count += 1
                print(f"PASS {item_id} {clean_detail}")
            else:
                fail_count += 1
                print(f"FAIL {item_id} {clean_detail}")

        if args.masaustu:
            desk_dir = temp_root / "masaustu"
            desk_dir.mkdir(parents=True, exist_ok=True)
            for status, item_id, detail in _masaustu_checks(desk_dir):
                clean_detail = " ".join(str(detail).splitlines()).strip()
                if status == "PASS":
                    pass_count += 1
                elif status == "SKIP":
                    skip_count += 1
                else:
                    fail_count += 1
                print(f"{status} {item_id} {clean_detail}")
        else:
            for item_id in masaustu_ids:
                skip_count += 1
                print(f"SKIP {item_id} --masaustu gerekli")

        print(f"SUMMARY pass={pass_count} fail={fail_count} skip={skip_count}")
    finally:
        _remove_tree(temp_root)

    return 1 if fail_count > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
