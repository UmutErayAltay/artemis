"""Gerçek dosyalar geçici kökte; OS ve ağ etkileri yalnız enjekte adaptörlerde kalır."""

import os
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from jarvis.brain.models import ToolCall
from jarvis.config.settings import Settings
from jarvis.safety.audit import AuditLog
from jarvis.safety.gate import TAINT_NOTICE, SafetyGate, TurnState
from jarvis.tools.base import ToolContext, ToolResult
from jarvis.tools.platform import WindowsDesktop, child_environment
from jarvis.tools.plugins.information import SearchParser
from jarvis.tools.registry import discover_tools


@pytest.fixture
def tool_context(tmp_path: Path) -> ToolContext:
    workspace = tmp_path / "Masaüstü"
    workspace.mkdir()
    settings = Settings(
        workspace=workspace,
        state_dir=tmp_path / "state",
        audit_path=tmp_path / "audit.jsonl",
        applications={"notepad": r"C:\Windows\notepad.exe"},
    )
    return ToolContext(workspace=workspace, settings=settings, audit=AuditLog(settings.audit_path))


async def invoke(context: ToolContext, tool_name: str, **arguments: Any) -> ToolResult:
    tool = discover_tools().get(tool_name)
    assert tool is not None
    return await tool.execute(tool.args_model.model_validate(arguments), context)


async def test_filesystem_lifecycle_uses_real_files(tool_context: ToolContext) -> None:
    root = tool_context.workspace
    assert (await invoke(tool_context, "create_directory", path="arşiv")).success
    text = "İlk satır\r\nİkinci satır\r\n"
    assert (await invoke(tool_context, "create_file", path="not.txt", content=text)).success
    assert (root / "not.txt").read_bytes() == text.encode("utf-8")
    listing = await invoke(tool_context, "list_files")
    assert listing.success and listing.external
    entries = {entry["name"]: entry for entry in listing.data["entries"]}
    assert entries["not.txt"]["size"] == len(text.encode("utf-8"))
    assert entries["arşiv"]["is_dir"]
    read = await invoke(tool_context, "read_file", path="not.txt")
    assert read.success and read.external and read.content_kind == "file"
    assert read.data["content"] == text.replace("\r\n", "\n")
    assert (await invoke(tool_context, "rename_file", path="not.txt", new_name="yeni.txt")).success
    assert not (root / "not.txt").exists()
    moved = await invoke(tool_context, "move_file", source="yeni.txt", destination="arşiv/yeni.txt")
    assert moved.success and not (root / "yeni.txt").exists()
    nested = await invoke(tool_context, "list_files", path="arşiv")
    assert nested.data["entries"][0]["path"] == "arşiv/yeni.txt"
    trash = root.parent / "trash"
    trash.mkdir()
    tool_context.services["trash"] = lambda path: path.rename(trash / path.name)
    deleted = await invoke(tool_context, "delete_file", path="arşiv/yeni.txt")
    assert deleted.success
    assert not (root / "arşiv/yeni.txt").exists()
    assert (trash / "yeni.txt").read_bytes() == text.encode("utf-8")


@pytest.mark.parametrize("bad_path", [".", "..", "../outside", "/tmp/outside", r"C:\outside"])
@pytest.mark.parametrize(
    ("tool", "field", "other"),
    [
        ("list_files", "path", {}),
        ("read_file", "path", {}),
        ("create_file", "path", {"content": "x"}),
        ("create_directory", "path", {}),
        ("delete_file", "path", {}),
        ("rename_file", "path", {"new_name": "safe.txt"}),
        ("rename_file", "new_name", {"path": "safe.txt"}),
        ("move_file", "source", {"destination": "target.txt"}),
        ("move_file", "destination", {"source": "safe.txt"}),
    ],
)
async def test_every_path_argument_is_guarded(
    tool_context: ToolContext, tool: str, field: str, other: dict[str, Any], bad_path: str
) -> None:
    (tool_context.workspace / "safe.txt").write_text("koru", encoding="utf-8")
    result = await invoke(tool_context, tool, **{**other, field: bad_path})
    assert not result.success and result.status == "error"
    assert (tool_context.workspace / "safe.txt").read_text(encoding="utf-8") == "koru"


@pytest.mark.parametrize("tool", ["create_file", "move_file", "rename_file"])
async def test_existing_target_is_preserved(tool_context: ToolContext, tool: str) -> None:
    root = tool_context.workspace
    (root / "keep.txt").write_text("eski", encoding="utf-8")
    (root / "source.txt").write_text("yeni", encoding="utf-8")
    arguments = {
        "create_file": {"path": "keep.txt", "content": "yeni"},
        "move_file": {"source": "source.txt", "destination": "keep.txt"},
        "rename_file": {"path": "source.txt", "new_name": "keep.txt"},
    }
    result = await invoke(tool_context, tool, **arguments[tool])
    assert not result.success
    assert (root / "keep.txt").read_text(encoding="utf-8") == "eski"
    assert (root / "source.txt").read_text(encoding="utf-8") == "yeni"


async def test_noop_trash_is_not_reported_as_success(tool_context: ToolContext) -> None:
    path = tool_context.workspace / "keep.txt"
    path.write_text("kalmalı", encoding="utf-8")
    tool_context.services["trash"] = lambda path: None
    result = await invoke(tool_context, "delete_file", path="keep.txt")
    assert result.success is False
    assert result.status == "error"
    assert path.exists()


async def test_trash_error_does_not_destroy_file(tool_context: ToolContext) -> None:
    path = tool_context.workspace / "keep.txt"
    path.write_text("kalmalı", encoding="utf-8")

    def unavailable_trash(path: Path) -> None:
        raise OSError("Geri dönüşüm kutusu yok")

    tool_context.services["trash"] = unavailable_trash
    result = await invoke(tool_context, "delete_file", path="keep.txt")
    assert not result.success and result.status == "error"
    assert "Geri dönüşüm kutusu yok" not in result.message
    assert path.exists()


async def test_missing_files_and_invalid_utf8_are_errors(tool_context: ToolContext) -> None:
    for name in ("read_file", "delete_file"):
        result = await invoke(tool_context, name, path="missing.txt")
        assert not result.success
    (tool_context.workspace / "binary.dat").write_bytes(b"\xff\xfe")
    assert not (await invoke(tool_context, "read_file", path="binary.dat")).success


async def test_read_rejects_fifo_without_opening_it(tool_context: ToolContext) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO testi POSIX platformu gerektirir.")
    fifo = tool_context.workspace / "stream"
    os.mkfifo(fifo)
    result = await invoke(tool_context, "read_file", path="stream")
    assert not result.success and result.status == "error"
    assert fifo.exists()


async def test_filesystem_errors_do_not_echo_sensitive_path(tool_context: ToolContext) -> None:
    secret_like_name = "OPENROUTER_API_KEY_synthetic_secret.txt"
    result = await invoke(tool_context, "read_file", path=secret_like_name)
    assert not result.success
    assert secret_like_name not in result.message


async def test_read_limit_is_explicit(tool_context: ToolContext) -> None:
    (tool_context.workspace / "large.txt").write_text("x" * 300_000, encoding="utf-8")
    result = await invoke(tool_context, "read_file", path="large.txt")
    assert result.success and result.data["truncated"]
    assert len(result.data["content"]) == 262_144


async def test_mutations_reject_internal_symlinks_and_preserve_targets(
    tool_context: ToolContext,
) -> None:
    root = tool_context.workspace
    real = root / "real"
    real.mkdir()
    target = real / "keep.txt"
    target.write_text("koru", encoding="utf-8")
    alias = root / "alias"
    try:
        alias.symlink_to(real, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"Bu platformda symlink yetkisi yok: {error}")

    read = await invoke(tool_context, "read_file", path="alias/keep.txt")
    assert read.success and read.data["content"] == "koru"

    results = [
        await invoke(tool_context, "create_file", path="alias/new.txt", content="yeni"),
        await invoke(tool_context, "create_directory", path="alias/new-dir"),
        await invoke(
            tool_context,
            "move_file",
            source="real/keep.txt",
            destination="alias/moved.txt",
        ),
        await invoke(tool_context, "rename_file", path="alias/keep.txt", new_name="renamed.txt"),
        await invoke(tool_context, "delete_file", path="alias/keep.txt"),
    ]

    assert all(not result.success and result.status == "error" for result in results)
    assert target.read_text(encoding="utf-8") == "koru"
    assert not (real / "new.txt").exists()
    assert not (real / "new-dir").exists()
    assert not (real / "moved.txt").exists()
    assert alias.is_symlink()


class FakeDesktop:
    def __init__(self) -> None:
        self.clipboard = "ilk"
        self.opened: list[str] = []
        self.closed: list[str] = []
        self.close_ok = True
        self.write_ok = True

    def open_application(self, executable: str) -> None:
        self.opened.append(executable)

    def close_application(self, executable: str) -> bool:
        self.closed.append(executable)
        return self.close_ok

    def read_clipboard(self) -> str:
        return self.clipboard

    def write_clipboard(self, text: str) -> bool:
        if self.write_ok:
            self.clipboard = text
        return self.write_ok


async def test_desktop_success_sent_and_failure(tool_context: ToolContext) -> None:
    desktop = FakeDesktop()
    tool_context.services["desktop"] = desktop
    opened = await invoke(tool_context, "open_application", name="notepad")
    assert opened.status == "sent" and not opened.success
    assert desktop.opened == [r"C:\Windows\notepad.exe"]
    assert (await invoke(tool_context, "close_application", name="notepad")).success
    desktop.close_ok = False
    assert not (await invoke(tool_context, "close_application", name="notepad")).success
    read = await invoke(tool_context, "read_clipboard")
    assert read.success and read.external and read.content_kind == "clipboard"
    assert read.data["text"] == "ilk"
    assert (await invoke(tool_context, "write_clipboard", text="Türkçe")).success
    assert desktop.clipboard == "Türkçe"
    desktop.write_ok = False
    assert not (await invoke(tool_context, "write_clipboard", text="hata")).success


async def test_unconfigured_apps_never_reach_adapter(tool_context: ToolContext) -> None:
    desktop = FakeDesktop()
    tool_context.services["desktop"] = desktop
    for tool in ("open_application", "close_application"):
        result = await invoke(tool_context, tool, name="arbitrary.exe")
        assert not result.success and "izin" in result.message
    assert desktop.opened == desktop.closed == []


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("open_application", {"name": "notepad"}),
        ("close_application", {"name": "notepad"}),
        ("read_clipboard", {}),
        ("write_clipboard", {"text": "x"}),
    ],
)
async def test_linux_returns_honest_unsupported(
    tool_context: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
    tool: str,
    arguments: dict[str, str],
) -> None:
    monkeypatch.setattr("jarvis.tools.platform.platform.system", lambda: "Linux")
    # Varsayılan yol işçi sürecinde çalışır; testte gerçek GUI etkisini önlemek için
    # platform kontrolünü aynı süreçte uygulayan adaptörü kullan.
    tool_context.services["desktop"] = WindowsDesktop()
    result = await invoke(tool_context, tool, **arguments)
    assert not result.success and "yalnız Windows" in result.message


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "javascript:alert(1)",
        "https://",
        "ftp://example.org",
        "https://user:password@example.org",
        "https://example.org/\nx",
    ],
)
async def test_unsafe_urls_never_reach_browser(tool_context: ToolContext, url: str) -> None:
    calls: list[str] = []
    tool_context.services["url_opener"] = lambda value: calls.append(value) or True
    result = await invoke(tool_context, "open_url", url=url)
    assert not result.success and result.status == "error"
    assert not calls


async def test_browser_result_is_never_claimed_as_opened(tool_context: ToolContext) -> None:
    tool_context.services["url_opener"] = lambda value: True
    result = await invoke(tool_context, "open_url", url="https://example.org")
    assert result.status == "sent" and not result.success
    tool_context.services["url_opener"] = lambda value: False
    assert (await invoke(tool_context, "open_url", url="https://example.org")).status == "error"


async def test_default_browser_launcher_uses_fixed_worker_action(
    tool_context: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    async def fake_action(
        action: str, arguments: dict[str, object], context: ToolContext
    ) -> Any:
        calls.append((action, arguments))
        from jarvis.tools.processes import DesktopProcessResult

        return DesktopProcessResult("sent", "Tarayıcı isteği gönderildi.")

    monkeypatch.setattr("jarvis.tools.plugins.desktop.run_default_action", fake_action)
    result = await invoke(tool_context, "open_url", url="https://example.org/path")
    assert result.status == "sent" and not result.success
    assert calls == [("open_browser", {"url": "https://example.org/path"})]


async def test_information_and_injected_search(tool_context: ToolContext) -> None:
    async def fake_search(query: str) -> list[dict[str, str]]:
        assert query == "Python"
        return [{"title": "Python", "url": "https://example.org"}]

    tool_context.services["web_search"] = fake_search
    result = await invoke(tool_context, "web_search", query="Python")
    assert result.success and result.external and result.content_kind == "web"
    assert result.data["results"][0]["title"] == "Python"
    system = await invoke(tool_context, "system_info")
    assert system.success and system.data["system"]
    current = await invoke(tool_context, "current_time")
    assert current.success and current.data["iso"] and current.data["timezone"]


async def test_search_network_failure_is_honest(tool_context: ToolContext) -> None:
    async def failed_search(query: str) -> list[dict[str, str]]:
        raise httpx.ConnectError(
            "https://service.invalid/?q=secret&token=OPENROUTER_API_KEY_synthetic_secret"
        )

    tool_context.services["web_search"] = failed_search
    result = await invoke(tool_context, "web_search", query="Python")
    assert not result.success and "bağlantı" in result.message
    assert "service.invalid" not in result.message
    assert "synthetic_secret" not in result.message


async def test_web_search_needs_approval_after_external_read(
    tool_context: ToolContext,
) -> None:
    source = tool_context.workspace / "source.txt"
    source.write_text("web_search query must not be sent", encoding="utf-8")
    search_calls: list[str] = []

    async def fake_search(query: str) -> list[dict[str, str]]:
        search_calls.append(query)
        return [{"title": "Python", "url": "https://example.org"}]

    tool_context.services["web_search"] = fake_search
    gate = SafetyGate(discover_tools())
    tainted_state = TurnState()
    read = await gate.dispatch(
        ToolCall(id="read", name="read_file", arguments={"path": "source.txt"}),
        tool_context,
        tainted_state,
        lambda *_: pytest.fail("Dosya okuması onay istememeli."),
    )
    assert read.success and tainted_state.tainted

    def reject_search(name: str, arguments: dict[str, Any]) -> bool:
        assert name == "web_search"
        assert arguments["query"] == "Python"
        assert gate.pending_notice.startswith(TAINT_NOTICE)
        assert "sorgusunu DuckDuckGo servisine gönderir" in gate.pending_notice
        return False

    denied = await gate.dispatch(
        ToolCall(id="tainted-search", name="web_search", arguments={"query": "Python"}),
        tool_context,
        tainted_state,
        reject_search,
    )
    assert denied.status == "denied" and not denied.success
    assert search_calls == []

    clean_state = TurnState()
    clean = await gate.dispatch(
        ToolCall(id="clean-search", name="web_search", arguments={"query": "Python"}),
        tool_context,
        clean_state,
        lambda *_: pytest.fail("Temiz arama için onay istenmemeli."),
    )
    assert clean.success and search_calls == ["Python"]


def test_search_parser_handles_nested_title_and_redirect() -> None:
    parser = SearchParser()
    parser.feed(
        '<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org">'
        "Türkçe <b>sonuç</b></a>"
    )
    assert parser.results == [{"title": "Türkçe sonuç", "url": "https://example.org"}]


def test_child_environment_excludes_provider_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UNRELATED_TEST_VALUE", "should-not-propagate")
    monkeypatch.setenv("JARVIS_TEST_TOKEN", "synthetic-sentinel")
    monkeypatch.setenv("DISPLAY", ":77")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/test")
    monkeypatch.setenv("USERPROFILE", r"C:\Users\test")
    monkeypatch.setenv("SYSTEMDRIVE", "C:")
    environment = child_environment()
    # Eksik SystemDrive Brave profilini bozuyordu; GUI uygulamaları bunu almalı.
    assert environment["SYSTEMDRIVE"] == "C:"
    assert "UNRELATED_TEST_VALUE" not in environment
    assert "JARVIS_TEST_TOKEN" not in environment
    assert environment["DISPLAY"] == ":77"
    assert environment["XDG_RUNTIME_DIR"] == "/run/user/test"
    assert environment["USERPROFILE"] == r"C:\Users\test"
    assert set(key.upper() for key in environment) <= {
        "SYSTEMROOT",
        "WINDIR",
        "TEMP",
        "TMP",
        "PATH",
        "PATHEXT",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "HOMEDRIVE",
        "HOMEPATH",
        "HOME",
        "SYSTEMDRIVE",
        "PROGRAMDATA",
        "ALLUSERSPROFILE",
        "PUBLIC",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "PROGRAMW6432",
        "COMMONPROGRAMFILES",
        "COMMONPROGRAMFILES(X86)",
        "COMMONPROGRAMW6432",
        "COMSPEC",
        "COMPUTERNAME",
        "USERNAME",
        "USERDOMAIN",
        "OS",
        "PROCESSOR_ARCHITECTURE",
        "NUMBER_OF_PROCESSORS",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "DISPLAY",
        "WAYLAND_DISPLAY",
        "XAUTHORITY",
        "XDG_RUNTIME_DIR",
        "DBUS_SESSION_BUS_ADDRESS",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "XDG_CURRENT_DESKTOP",
        "XDG_SESSION_TYPE",
        "DESKTOP_SESSION",
        "SESSION_MANAGER",
    }


@pytest.mark.parametrize(
    ("exit_code", "remaining", "expected"),
    [(1, False, True), (0, True, False), (0, False, True)],
)
def test_close_checks_exit_status_and_process_absence(
    monkeypatch: pytest.MonkeyPatch, exit_code: int, remaining: bool, expected: bool
) -> None:
    monkeypatch.setattr("jarvis.tools.platform.platform.system", lambda: "Windows")
    monkeypatch.setattr("jarvis.tools.platform.time.sleep", lambda _: None)
    output = b'"notepad.exe","1234"\r\n' if remaining else b""
    replies: Iterator[subprocess.CompletedProcess[bytes]] = iter(
        [
            subprocess.CompletedProcess([], exit_code, b"", b""),
            subprocess.CompletedProcess([], 0, output, b""),
        ]
    )

    calls: list[list[str]] = []

    def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        assert kwargs["shell"] is False
        assert "OPENROUTER_API_KEY" not in kwargs["env"]
        calls.append(args[0])
        return next(replies, subprocess.CompletedProcess([], 0, output, b""))

    monkeypatch.setattr("jarvis.tools.platform.subprocess.run", fake_run)
    assert WindowsDesktop().close_application(r"C:\Windows\notepad.exe") is expected
    assert calls[0][-2:] == ["/IM", "notepad.exe"]
    assert "/F" not in calls[0]
    assert calls[1][1:4] == ["/FI", "IMAGENAME eq notepad.exe", "/FO"]


def test_close_description_warns_that_all_matching_processes_are_closed() -> None:
    definition = discover_tools().get("close_application")
    assert definition is not None
    assert "tüm süreçlerini" in definition.description
    assert "kaybolabilir" in definition.description


@pytest.mark.parametrize(("windows_open", "status"), [(False, "ok"), (True, "error")])
def test_worker_reports_verified_window_closure_separately(
    monkeypatch: pytest.MonkeyPatch, windows_open: bool, status: str
) -> None:
    from jarvis.tools.desktop_worker import _dispatch

    monkeypatch.setattr(WindowsDesktop, "close_application", lambda *_: False)
    monkeypatch.setattr(WindowsDesktop, "application_windows_open", lambda *_: windows_open)
    result = _dispatch("close_application", {"executable": "brave.exe"})
    assert result["status"] == status
    if status == "ok":
        assert result["data"] == {"windows_closed": True, "processes_closed": False}
        assert "arka plan" in result["message"]


def test_close_rechecks_until_processes_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("jarvis.tools.platform.platform.system", lambda: "Windows")
    monkeypatch.setattr("jarvis.tools.platform.time.sleep", lambda _: None)
    remaining: Iterator[set[int]] = iter([{42}, set()])
    monkeypatch.setattr(WindowsDesktop, "_application_pids", lambda *_: next(remaining))
    monkeypatch.setattr(
        "jarvis.tools.platform.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess([], 0, b"", b""),
    )
    assert WindowsDesktop().close_application("brave.exe")


@pytest.mark.parametrize(("visible", "expected"), [(True, "ok"), (False, "sent")])
def test_open_worker_verifies_window_before_claiming_success(
    monkeypatch: pytest.MonkeyPatch, visible: bool, expected: str
) -> None:
    from jarvis.tools.desktop_worker import _dispatch

    monkeypatch.setattr(WindowsDesktop, "open_application", lambda *_: None)
    monkeypatch.setattr(WindowsDesktop, "wait_for_application_window", lambda *_: visible)
    result = _dispatch("open_application", {"executable": "notepad.exe"})
    assert result["status"] == expected
    assert result["data"].get("window_verified", False) is visible


async def test_open_notepad_file_rejects_workspace_escape(tool_context: ToolContext) -> None:
    desktop = FakeDesktop()
    tool_context.services["desktop"] = desktop
    result = await invoke(
        tool_context, "open_application", name="notepad", file_path="../secret.txt"
    )
    assert result.status == "error"
    assert desktop.opened == []


def test_worker_document_arguments_are_explicit_and_extra_fields_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jarvis.tools.desktop_worker import _dispatch

    opened: list[tuple[str, str]] = []
    monkeypatch.setattr(
        WindowsDesktop, "open_document", lambda self, exe, doc: opened.append((exe, doc))
    )
    monkeypatch.setattr(WindowsDesktop, "wait_for_application_window", lambda *_: True)
    arguments = {"executable": "notepad.exe", "file_path": "C:/test/note.txt"}
    assert _dispatch("open_application", arguments)["status"] == "ok"
    assert opened == [("notepad.exe", "C:/test/note.txt")]
    assert _dispatch("open_application", {**arguments, "shell": True})["status"] == "error"
    assert len(opened) == 1


@pytest.mark.parametrize("query_ok", [True, False])
def test_packaged_window_child_is_matched_and_unknown_pid_is_not_success(
    monkeypatch: pytest.MonkeyPatch, query_ok: bool
) -> None:
    import ctypes
    from ctypes import wintypes
    from types import SimpleNamespace

    class Function:
        def __init__(self, callback: Any) -> None:
            self.callback = callback

        def __call__(self, *args: Any) -> Any:
            return self.callback(*args)

    def get_pid(hwnd: Any, pointer: Any) -> int:
        ctypes.cast(pointer, ctypes.POINTER(wintypes.DWORD)).contents.value = 88 if hwnd == 2 else 9
        return int(query_ok)

    api = SimpleNamespace(
        EnumWindows=Function(lambda callback, data: callback(1, data)),
        EnumChildWindows=Function(lambda hwnd, callback, data: callback(2, data)),
        IsWindowVisible=Function(lambda hwnd: True), GetWindowThreadProcessId=Function(get_pid),
        GetClassNameW=Function(lambda *args: 0),
    )
    monkeypatch.setattr("jarvis.tools.platform.platform.system", lambda: "Windows")
    monkeypatch.setattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE, raising=False)
    monkeypatch.setattr(WindowsDesktop, "_application_pids", lambda *_: {88})
    monkeypatch.setattr(WindowsDesktop, "_win_dll", lambda *_: api)
    if query_ok:
        assert WindowsDesktop().application_windows_open("calc.exe")
    else:
        with pytest.raises(OSError):
            WindowsDesktop().application_windows_open("calc.exe")


def test_open_uses_no_shell_and_an_environment_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("jarvis.tools.platform.platform.system", lambda: "Windows")
    calls = []

    def fake_popen(*args: Any, **kwargs: Any) -> None:
        calls.append((args, kwargs))

    monkeypatch.setattr("jarvis.tools.platform.subprocess.Popen", fake_popen)
    WindowsDesktop().open_application(r"C:\Windows\notepad.exe")
    args, kwargs = calls[0]
    assert args == ([r"C:\Windows\notepad.exe"],)
    assert kwargs["shell"] is False
    assert "OPENROUTER_API_KEY" not in kwargs["env"]


def test_only_network_tools_declare_outbound() -> None:
    # Dışarı giden tek araçlar ADR 0013 kuralını tetikler; yeni araç bilerek listeye eklenmeli.
    registry = discover_tools()
    outbound: set[str] = set()
    for definition in registry.definitions():
        tool = registry.get(definition.name)
        assert tool is not None
        if tool.outbound:
            outbound.add(definition.name)
    # notify_telegram metni makine dışına taşır; alıcı sabit olsa da ADR 0013 geçerli (ADR 0016).
    assert outbound == {"open_url", "web_search", "notify_telegram"}
