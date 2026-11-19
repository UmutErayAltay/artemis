"""ADR 0058, 0059, 0060 basliksiz kabul dogrulamasi (rutinler, takvim/posta, beceriler)."""

from __future__ import annotations

import asyncio
import imaplib
import io
import json
import os
import shutil
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, ConfigDict

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from artemis.brain.fake import ScriptedProvider  # noqa: E402
from artemis.brain.models import ToolCall  # noqa: E402
from artemis.channels.runtime import create_runtime  # noqa: E402
from artemis.config.settings import (  # noqa: E402
    CalendarConfig,
    MailConfig,
    MemoryConfig,
    RoutinesConfig,
    Settings,
    SkillsConfig,
)
from artemis.proactive.routines import (  # noqa: E402
    Routine,
    RoutinesError,
    RoutineStep,
    RoutineStore,
)
from artemis.safety.audit import AuditLog  # noqa: E402
from artemis.safety.gate import SafetyGate, TurnState  # noqa: E402
from artemis.skills import load_skills  # noqa: E402
from artemis.tools.base import BaseTool, Risk, ToolContext, ToolResult  # noqa: E402
from artemis.tools.calendar_mail import ICS_ENV, MAIL_ENV, SourceError, fetch_ics  # noqa: E402
from artemis.tools.plugins.calendar_mail import CalendarEvents, MailUnread  # noqa: E402
from artemis.tools.plugins.routines import (  # noqa: E402
    RoutineDelete,
    RoutineList,
    RoutineRun,
    RoutineSave,
)
from artemis.tools.registry import ToolRegistry, discover_tools  # noqa: E402


class _ProbeArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: int = 0


class _ProbeWrite(BaseTool):
    name = "probe_write"
    description = "Yan etkili sahte arac."
    args_model = _ProbeArgs
    risk: ClassVar[Risk] = Risk.SIDE_EFFECT

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        return ToolResult(success=True, status="ok", message="yapildi")


class _ProbeConfirm(_ProbeWrite):
    name = "probe_confirm"
    risk: ClassVar[Risk] = Risk.CONFIRM_REQUIRED


class _ProbeRead(_ProbeWrite):
    name = "probe_read"
    risk: ClassVar[Risk] = Risk.READ_ONLY


def _routine_registry() -> ToolRegistry:
    registry = ToolRegistry()
    for tool in (_ProbeWrite, _ProbeConfirm, _ProbeRead,
                 RoutineList, RoutineRun, RoutineSave, RoutineDelete):
        registry.register(tool)
    return registry


def _make_settings(base: Path, *, routines: bool = False) -> Settings:
    ws = base / "ws"
    state = base / "state"
    ws.mkdir(parents=True, exist_ok=True)
    state.mkdir(parents=True, exist_ok=True)
    return Settings(
        workspace=ws,
        state_dir=state,
        audit_path=base / "audit.jsonl",
        persist_memory=False,
        routines=RoutinesConfig(enabled=routines),
    )


def _skill_text(
    name: str, desc: str = "Kisa beceri aciklamasi.", body: str = "Adimlari izle."
) -> str:
    return f"---\nname: {name}\ndescription: {desc}\n---\n{body}\n"


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


async def check_0058_70a(base: Path) -> str:
    settings = _make_settings(base, routines=True)
    path = settings.state_dir / "routines.json"
    raw = b"{ bozuk"
    path.write_bytes(raw)
    gate = SafetyGate(_routine_registry())
    ctx = ToolContext(settings.workspace, settings, AuditLog(settings.audit_path))
    calls = [
        ("routine_list", {}),
        ("routine_run", {"name": "a"}),
        ("routine_save", {"name": "a", "steps": [{"tool": "probe_read", "arguments": {}}]}),
        ("routine_delete", {"name": "a"}),
    ]
    for name, args in calls:
        result = await gate.dispatch(
            ToolCall(id=name, name=name, arguments=args),
            ctx,
            TurnState(),
            lambda _n, _a: True,
        )
        assert not result.success and "bozuk" in result.message, (name, result.message)
    assert path.read_bytes() == raw
    raised = False
    try:
        RoutineStore(path).load()
    except RoutinesError:
        raised = True
    assert raised, "RoutineStore.load() RoutinesError firlatmadi"
    return "4 rutin araci reddetti, dosya degismedi, RoutinesError dogrulandi"


async def check_0058_71(base: Path) -> str:
    settings = _make_settings(base, routines=True)
    store = RoutineStore(settings.state_dir / "routines.json")
    steps = [
        RoutineStep(tool="probe_write"),
        RoutineStep(tool="probe_confirm"),
        RoutineStep(tool="probe_read"),
    ]
    store.save([Routine(name="sabah", steps=steps)])
    gate = SafetyGate(_routine_registry())
    ctx = ToolContext(settings.workspace, settings, AuditLog(settings.audit_path))
    asked: list[str] = []
    result = await gate.dispatch(
        ToolCall(id="run1", name="routine_run", arguments={"name": "sabah"}),
        ctx,
        TurnState(),
        lambda name, _args: asked.append(name) or True,
    )
    assert result.success, result.message
    assert asked == ["probe_confirm"]
    lines = settings.audit_path.read_text(encoding="utf-8").splitlines()
    events = [json.loads(line) for line in lines if line.strip()]
    starts = [e for e in events if e["event"] == "routine_run_start"]
    ends = [e for e in events if e["event"] == "routine_run_end"]
    dispatches = [e["tool"] for e in events if e["event"] == "tool_dispatch"]
    assert len(starts) == 1 and starts[0]["steps"] == 3
    assert len(ends) == 1 and ends[0]["steps_completed"] == 3 and ends[0]["completed"] is True
    assert dispatches == ["routine_run", "probe_write", "probe_confirm", "probe_read"]
    return f"dispatches={dispatches}, steps_completed={ends[0]['steps_completed']}"


class _QtHeadlessUnavailableError(Exception):
    """Qt veya platform eklentisi baslatilamadi; yalniz bu durumda SKIP."""


def _drive_calendar_section(base: Path, typed_url: str) -> str:
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    try:
        from PySide6.QtWidgets import QApplication, QVBoxLayout

        from artemis.channels.desktop.calendar_mail_section import CalendarMailSection

        app = QApplication.instance() or QApplication([])
    except (ImportError, RuntimeError) as error:
        raise _QtHeadlessUnavailableError(type(error).__name__) from error
    cfg_path = base / "config.yaml"
    if not cfg_path.exists():
        cfg_path.write_text("proactive:\n  timezone: Europe/Istanbul\n", encoding="utf-8")
    cal_layout = QVBoxLayout()
    mail_layout = QVBoxLayout()
    section = CalendarMailSection(cfg_path, calendar=cal_layout, mail=mail_layout)
    section.calendar_url.setText(typed_url)
    section._save_calendar()
    if section._job is not None:
        section._job.wait(5000)
    app.processEvents()
    return section.message.text()


def check_0059_78(base: Path) -> str:
    url = "http://calendar.example/private/kabulSAHTE_ics_7741/basic.ics"
    message = ""
    try:
        fetch_ics(url)
    except SourceError as error:
        message = str(error)
    assert message, "SourceError firlatilmadi"
    assert url not in message
    assert "kabulSAHTE_ics_7741" not in message
    assert "calendar.example" not in message
    assert "https" in message.casefold()
    try:
        http_status = _drive_calendar_section(base, url)
        malformed_status = _drive_calendar_section(base, "not a url")
    except _QtHeadlessUnavailableError as error:
        return f"SKIP:SourceError={message!r}, UI headless surulemedi ({error})"
    for shown in (http_status, malformed_status):
        assert "Kaydedilmedi." in shown, shown
        assert "https" in shown.casefold(), shown
        assert url not in shown and "kabulSAHTE_ics_7741" not in shown, shown
        assert "not a url" not in shown, shown
    return (
        f"SourceError={message!r}, http_status={http_status!r}, "
        f"malformed_status={malformed_status!r}"
    )


async def check_0059_86(base: Path) -> str:
    settings = _make_settings(base).model_copy(
        update={
            "calendar": CalendarConfig(enabled=False),
            "mail": MailConfig(enabled=False),
        }
    )
    asked: list[str] = []
    runtime = create_runtime(
        base / "config.yaml",
        lambda tool_name, _args: asked.append(tool_name) or False,
        provider=ScriptedProvider([]),
        settings=settings,
    )
    try:
        prefixes = ("calendar_", "mail_")
        def_leaks = [
            d.name for d in runtime.registry.definitions()
            if d.name.startswith(prefixes)
        ]
        assert def_leaks == [], f"definitions icinde takvim/posta araci kaldi: {def_leaks}"
        known_names = [
            d.name for d in discover_tools().definitions()
            if d.name.startswith(prefixes)
        ]
        assert known_names, "discover_tools() takvim/posta araci dondurmedi"
        for name in known_names:
            assert name not in runtime.registry, f"registry icinde {name} kaldi"
        for name in ("calendar_events", "mail_unread"):
            result = await runtime.gate.dispatch(
                ToolCall(id=name, name=name, arguments={}),
                runtime.context,
                TurnState(),
                lambda tool_name, _args: asked.append(tool_name) or False,
            )
            assert not result.success and "kapalı" in result.message, (name, result.message)
        assert asked == []
        return (
            f"create_runtime filtreledi (defs=[], checked={known_names}), "
            "kapali reddi ok, asked=[]"
        )
    finally:
        runtime.shutdown()


async def check_0059_87(base: Path) -> str:
    ics_secret = "kabulSAHTE_ics_7741"
    mail_secret = "kabulSAHTE_parola_9920"
    ics_val = f"https://calendar.example/private/{ics_secret}/basic.ics"
    mail_val = mail_secret
    prev_ics, prev_mail = os.environ.get(ICS_ENV), os.environ.get(MAIL_ENV)
    os.environ[ICS_ENV], os.environ[MAIL_ENV] = ics_val, mail_val
    try:
        settings = _make_settings(base).model_copy(
            update={
                "calendar": CalendarConfig(enabled=True, allow_cloud=False),
                "mail": MailConfig(
                    enabled=True, host="imap.example",
                    username="kabul@example.invalid", allow_cloud=False,
                ),
            }
        )
        registry = ToolRegistry()
        registry.register(CalendarEvents)
        registry.register(MailUnread)
        gate = SafetyGate(registry)
        ctx = ToolContext(settings.workspace, settings, AuditLog(settings.audit_path))

        def _fail_fetch(url: str) -> bytes:
            # Gercek urun yolu: fetch_ics icinde urllib hatasi adresi tasiyarak firlar.
            raise OSError(f"URLError while fetching {url}")

        class _FailImap:
            def login(self, user: str, secret: str) -> tuple[str, list[bytes]]:
                raise imaplib.IMAP4.error(f"AUTHENTICATIONFAILED user={user} pass={secret}")

            def logout(self) -> tuple[str, list[bytes]]:
                return "BYE", []

        ctx.services["calendar_fetch"] = _fail_fetch
        ctx.services["mail_connect"] = lambda _host, _port: _FailImap()
        for call_id, tool_name in (("cal", "calendar_events"), ("mail", "mail_unread")):
            await gate.dispatch(
                ToolCall(id=call_id, name=tool_name, arguments={}),
                ctx, TurnState(), lambda _n, _a: False,
            )
        audit_bytes = settings.audit_path.read_bytes()
        assert len(audit_bytes) > 0
        logged_tools = {
            json.loads(line).get("tool")
            for line in audit_bytes.decode("utf-8").splitlines()
            if line.strip()
        }
        assert {"calendar_events", "mail_unread"} <= logged_tools
        ctx_repr = repr(ctx)
        settings_json = json.dumps(settings.model_dump(mode="json"))
        leaked: list[str] = []
        for kind, needle in (
            ("ics", ics_secret),
            ("ics_url", "calendar.example/private"),
            ("parola", mail_secret),
        ):
            raw_bytes = needle.encode("utf-8")
            escaped_bytes = json.dumps(needle)[1:-1].encode("utf-8")
            if raw_bytes in audit_bytes or escaped_bytes in audit_bytes:
                leaked.append(kind)
            assert needle not in ctx_repr, f"ctx repr sizintisi: {kind}"
            assert needle not in settings_json, f"settings sizintisi: {kind}"
        assert not leaked, f"audit.jsonl icine sir sizdi: {','.join(leaked)}"
        return f"audit_bytes={len(audit_bytes)}, sir yok (audit/repr/settings)"
    finally:
        if prev_ics is None:
            os.environ.pop(ICS_ENV, None)
        else:
            os.environ[ICS_ENV] = prev_ics
        if prev_mail is None:
            os.environ.pop(MAIL_ENV, None)
        else:
            os.environ[MAIL_ENV] = prev_mail


def check_0060_93(base: Path) -> str:
    ws = base / "ws"
    vault = base / "vault"
    inside_dir = vault / ".artemis" / "beceriler"
    ws.mkdir(parents=True, exist_ok=True)
    inside_dir.mkdir(parents=True, exist_ok=True)
    (inside_dir / "rapor.md").write_text(_skill_text("rapor"), encoding="utf-8")
    outside_dir = Path(tempfile.mkdtemp(prefix="artemis_kabul_skills_ext_"))
    try:
        (outside_dir / "rapor.md").write_text(_skill_text("rapor"), encoding="utf-8")
        common = dict(
            workspace=ws,
            state_dir=vault / ".artemis",
            persist_memory=False,
            memory=MemoryConfig(vault_path=vault),
        )
        cat_in = load_skills(
            Settings(
                **common,
                audit_path=base / "a1.jsonl",
                skills=SkillsConfig(enabled=True, folder=inside_dir),
            )
        )
        assert cat_in.refused and cat_in.skills == []
        cat_out = load_skills(
            Settings(
                **common,
                audit_path=base / "a2.jsonl",
                skills=SkillsConfig(enabled=True, folder=outside_dir),
            )
        )
        assert cat_out.refused is None and [s.name for s in cat_out.skills] == ["rapor"]
        return "vault ici reddedildi, dis temp klasor yuklendi"
    finally:
        _remove_tree(outside_dir)


def check_0060_98(base: Path) -> str:
    folder = base / "beceriler"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "iyi.md").write_text(_skill_text("iyi"), encoding="utf-8")
    (folder / "baslik.md").write_text("başlıksız metin", encoding="utf-8")
    (folder / "utf16.md").write_bytes(_skill_text("utf16").encode("utf-16"))
    settings = _make_settings(base).model_copy(
        update={"skills": SkillsConfig(enabled=True, folder=folder)}
    )
    catalog = load_skills(settings)
    assert [s.name for s in catalog.skills] == ["iyi"]
    reasons = dict(catalog.invalid)
    assert "baslik.md" in reasons and bool(reasons["baslik.md"].strip())
    assert "utf16.md" in reasons and bool(reasons["utf16.md"].strip())
    return f"loaded=['iyi'], invalid={sorted(reasons)}"


def check_0060_99(base: Path) -> str:
    folder = base / "beceriler"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "bom.md").write_bytes(b"\xef\xbb\xbf" + _skill_text("bom").encode("utf-8"))
    (folder / "ansi.md").write_bytes(_skill_text("ansi", body="ş").encode("cp1254"))
    settings = _make_settings(base).model_copy(
        update={"skills": SkillsConfig(enabled=True, folder=folder)}
    )
    catalog = load_skills(settings)
    assert [s.name for s in catalog.skills] == ["bom"]
    reasons = dict(catalog.invalid)
    assert "ansi.md" in reasons and bool(reasons["ansi.md"].strip())
    return "bom.md yuklendi, ansi.md reddedildi"


def main() -> int:
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if isinstance(sys.stderr, io.TextIOWrapper):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    temp_root = Path(tempfile.mkdtemp(prefix="artemis_kabul_0058_0060_"))
    passed = failed = skipped = 0

    def emit(status: str, item_id: str, text: str) -> None:
        nonlocal passed, failed, skipped
        clean = " ".join(text.splitlines()).strip()
        print(f"{status} {item_id} {clean}")
        if status == "PASS":
            passed += 1
        elif status == "FAIL":
            failed += 1
        else:
            skipped += 1

    steps: list[tuple[str, Callable[[Path], str | Awaitable[str]] | None, str | None]] = [
        ("0058-70a", check_0058_70a, None),
        ("0058-70b", None, "UI/Windows manual"),
        ("0058-71", check_0058_71, None),
        ("0059-78", check_0059_78, None),
        ("0059-78-ui", None, "UI panel flow (Kaydet ve dene)"),
        ("0059-86", check_0059_86, None),
        ("0059-86-model", None, "needs live model"),
        ("0059-87", check_0059_87, None),
        ("0059-87-ui", None, "UI/Windows keyring (Kaydi sil)"),
        ("0060-93", check_0060_93, None),
        ("0060-93-ui", None, "UI panel message and Yenile flow"),
        ("0060-98", check_0060_98, None),
        ("0060-98-ui", None, "UI panel Yenile listing"),
        ("0060-99", check_0060_99, None),
        ("0060-99-ui", None, "UI/Windows Notepad save flow"),
    ]
    try:
        for item_id, fn, skip_reason in steps:
            if fn is None:
                emit("SKIP", item_id, skip_reason or "UI")
                continue
            item_dir = temp_root / item_id
            item_dir.mkdir(parents=True, exist_ok=True)
            try:
                outcome = fn(item_dir)
                detail = asyncio.run(outcome) if asyncio.iscoroutine(outcome) else str(outcome)
                if detail.startswith("SKIP:"):
                    emit("SKIP", item_id, detail.removeprefix("SKIP:"))
                else:
                    emit("PASS", item_id, detail)
            except (AssertionError, RoutinesError, SourceError,
                    ValueError, OSError, imaplib.IMAP4.error) as error:
                emit("FAIL", item_id, f"{type(error).__name__}: {error}")
    finally:
        _remove_tree(temp_root)
    print(f"OZET pass={passed} fail={failed} skip={skipped}")
    return 1 if failed > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
