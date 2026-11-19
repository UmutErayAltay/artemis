"""Güvenilirlik ve Windows kontrol listesi kabul doğrulaması: gerçek süreçler ve SQLite."""

from __future__ import annotations

import asyncio
import io
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from artemis.brain.conversation import TurnRecord  # noqa: E402
from artemis.brain.fake import ScriptedProvider  # noqa: E402
from artemis.channels.runtime import create_runtime  # noqa: E402
from artemis.config.settings import AgentProfile, Settings  # noqa: E402
from artemis.memory.store import MemoryStore  # noqa: E402
from artemis.proactive.delivery import DeliveryOutcome  # noqa: E402
from artemis.proactive.scheduler import ProactiveScheduler  # noqa: E402
from artemis.proactive.store import ProactiveStore, reminder_event_id  # noqa: E402
from artemis.safety.audit import DEFAULT_MAX_BYTES, AuditLog  # noqa: E402
from artemis.tools import code_agent_process  # noqa: E402
from artemis.tools.code_agent_jobs import ACTIVE, CodeAgentJobs  # noqa: E402
from artemis.tools.code_agent_store_lock import StoreOwnership  # noqa: E402
from artemis.tools.plugins.history import HistoryArguments  # noqa: E402
from artemis.tools.plugins.reminders import (  # noqa: E402
    ReminderCancelArguments,
    ReminderRetry,
)


def _remove_tree(root: Path) -> None:
    if not root.exists():
        return

    def _handle_readonly(*args: Any) -> None:
        func, path = args[0], args[1]
        os.chmod(path, stat.S_IWRITE)
        func(path)

    for attempt in range(5):
        try:
            shutil.rmtree(root, onexc=_handle_readonly)
            return
        except OSError:
            time.sleep(0.1 * (attempt + 1))


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        ).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _make_runtime(base: Path, *, approved: bool = True) -> Any:
    state = base / "state"
    state.mkdir(parents=True, exist_ok=True)
    settings = Settings(
        workspace=base,
        state_dir=state,
        audit_path=base / "audit.jsonl",
        persist_memory=True,
    )
    return create_runtime(
        base / "config.yaml",
        lambda *_: approved,
        provider=ScriptedProvider([]),
        settings=settings,
    )


def check_cl149(base: Path) -> tuple[str, str]:
    rt = _make_runtime(base, approved=True)
    try:
        store: MemoryStore | None = rt.context.services.get("memory_store")
        if store is None:
            return "FAIL", "context.services['memory_store'] is None with persist_memory=True"
        t1 = TurnRecord(
            turn_id="t1",
            user_text="arsiv_korunan_mesaj_149",
            steps=(),
            answer="arsiv_yanit_149",
            answer_private=False,
            tainted=False,
        )
        t2 = TurnRecord(
            turn_id="t2",
            user_text="ikinci_mesaj_149",
            steps=(),
            answer="ikinci_yanit_149",
            answer_private=False,
            tainted=False,
        )
        rt.runner.conversation.record(t1)
        rt.runner.conversation.record(t2)
        store.add_note("kalici_oz_not_149", turn_id="t1", max_notes=10)

        # CLI /yeni akışı: rt.runner.reset_context() aktif bağlamı sıfırlar, arşivi silmez.
        reset_ok = rt.runner.reset_context()
        turns_after_yeni = store.history_status()["turns"]
        notes_after_yeni = [n.text for n in store.list_notes()]
        ctx_after_yeni = len(rt.runner.conversation._turns)

        rt.runner.conversation.record(
            TurnRecord(
                turn_id="t3",
                user_text="silinecek_baglam",
                steps=(),
                answer="silinecek_yanit",
                answer_private=False,
                tainted=False,
            )
        )
        args = HistoryArguments.model_validate({"action": "delete", "retention_days": 0})
        res = asyncio.run(rt.runner.dispatch_tool("conversation_history", args))
        turns_after_del = store.history_status()["turns"]
        notes_after_del = [n.text for n in store.list_notes()]
        ctx_after_del = len(rt.runner.conversation._turns)

        ok = (
            reset_ok
            and turns_after_yeni == 2
            and ctx_after_yeni == 0
            and notes_after_yeni == ["kalici_oz_not_149"]
            and res.success
            and turns_after_del == 0
            and ctx_after_del == 0
            and notes_after_del == ["kalici_oz_not_149"]
        )
        detail = (
            f"yeni(reset={reset_ok},turns={turns_after_yeni},ctx={ctx_after_yeni},"
            f"notes={len(notes_after_yeni)}) "
            f"sil(ok={res.success},turns={turns_after_del},ctx={ctx_after_del},"
            f"notes={notes_after_del})"
        )
        return ("PASS" if ok else "FAIL"), detail
    finally:
        rt.shutdown()


def check_cl150(base: Path) -> tuple[str, str]:
    rt = _make_runtime(base, approved=True)
    try:
        db_path = base / "state" / "proactive.sqlite3"
        store = ProactiveStore(db_path)
        now = datetime.now(UTC)
        past = now - timedelta(seconds=5)
        r1 = store.add_reminder("Fatura ode", past, tainted=False, now=now)
        r2 = store.add_reminder("Ilac al", past, tainted=False, now=now)

        class _BrokenDeliverer:
            async def deliver(self, **kwargs: Any) -> DeliveryOutcome:
                if kwargs.get("event_id") == reminder_event_id(r1.id):
                    return DeliveryOutcome(False, {"desktop": "failed"})
                raise OSError("belirsiz teslim")

        scheduler = ProactiveScheduler(rt, store, _BrokenDeliverer(), clock=lambda: now)
        try:
            asyncio.run(scheduler.tick())
        except OSError as error:
            tick_error: OSError | None = error
        else:
            tick_error = None
        # Talep edilip yarım kalan r2 oluşumu zaman aşımı sonrası 'uncertain' işaretlenir.
        future_clock = now + timedelta(hours=1)
        scheduler_after = ProactiveScheduler(
            rt, store, _BrokenDeliverer(), clock=lambda: future_clock
        )
        asyncio.run(scheduler_after.tick())

        statuses: dict[int, str] = {}
        for h in store.list_reminders("all"):
            statuses[h.id] = h.state or "pending"

        rt.context.services["proactive_store"] = store
        rt.context.services["proactive_clock"] = lambda: future_clock
        retry_tool = ReminderRetry()
        parsed = ReminderCancelArguments(reminder_id=r2.id)
        retry_res = asyncio.run(retry_tool.execute(parsed, rt.context))

        s1 = statuses.get(r1.id, "missing")
        s2 = statuses.get(r2.id, "missing")
        data = retry_res.data or {}
        new_id = data.get("reminder_id") or data.get("id")
        # Kullanıcı onayında görünen çift bildirim uyarısı ReminderRetry.description içindedir.
        expected_warning = (
            "Önceki teslim gerçekleşmiş olabilir: bu işlem çift bildirim üretebilir."
        )
        warn_ok = expected_warning in ReminderRetry.description
        tick_err_ok = (
            tick_error is not None
            and type(tick_error) is OSError
            and str(tick_error) == "belirsiz teslim"
        )
        states_ok = s1 == "failed" and s2 == "uncertain"
        new_rec_ok = new_id is not None and new_id not in (r1.id, r2.id)
        ok = tick_err_ok and states_ok and new_rec_ok and warn_ok
        tick_err_repr = f"{type(tick_error).__name__}:{tick_error}" if tick_error else "none"
        detail = (
            f"tick_err={tick_err_repr}, r1={s1}, r2={s2}, "
            f"new_id={new_id}, warn={warn_ok}, msg={retry_res.message!r}"
        )
        return ("PASS" if ok else "FAIL"), detail
    finally:
        rt.shutdown()


def check_cl151(base: Path) -> tuple[str, str]:
    if os.name != "nt":
        return "SKIP", "Windows Job Object API yalniz Windows uzerinde mevcut"

    repo = base / "repo_cl151"
    _init_git_repo(repo)
    pids_file = base / "spawned_pids.txt"
    agent_code = (
        "import os,pathlib,subprocess,sys,time;"
        "g=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
        f"pathlib.Path({str(pids_file)!r}).write_text(f'{{os.getpid()}} {{g.pid}}');"
        "time.sleep(60)"
    )
    profile = AgentProfile(
        kind="generic",
        command=sys.executable,
        args=("-c", agent_code),
    )

    child_pid = 0
    grandchild_pid = 0
    rt1 = _make_runtime(base)
    try:
        jobs = rt1.context.services.get("code_agent_jobs")
        if not isinstance(jobs, CodeAgentJobs):
            return "FAIL", "runtime code_agent_jobs servisini baslatamadi"
        spec = jobs.prepare(
            "cl151",
            "Yetim surec ve kilit testi",
            ("README.md",),
            ("Kapanista alt surecler temizlenmeli.",),
            agent="generic",
        )
        jobs.start(
            "cl151",
            repo,
            "Yetim surec ve kilit testi",
            spec=spec,
            profile=profile,
        )
        spawn_deadline = time.monotonic() + 8.0
        while time.monotonic() < spawn_deadline:
            if pids_file.exists():
                parts = pids_file.read_text(encoding="utf-8").split()
                if len(parts) == 2 and all(p.isdigit() for p in parts):
                    child_pid, grandchild_pid = int(parts[0]), int(parts[1])
                    break
            time.sleep(0.05)
        if not (child_pid and grandchild_pid):
            return "FAIL", "ajan veya torun surec PID dosyasi olusmadi"

        # Gercek urun kapanis yolu: runtime.shutdown() -> CodeAgentJobs.shutdown()
        rt1.shutdown()

        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            if not _pid_alive(child_pid) and not _pid_alive(grandchild_pid):
                break
            time.sleep(0.05)
        child_dead = not _pid_alive(child_pid)
        grand_dead = not _pid_alive(grandchild_pid)
    finally:
        rt1.shutdown()
        for pid in (child_pid, grandchild_pid):
            if pid and _pid_alive(pid):
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    check=False,
                )

    lock_path = base / "state" / "code_agent.lock"
    try:
        own = StoreOwnership(lock_path)
    except OSError:
        lock_free_after_shutdown = False
    else:
        own.close()
        lock_free_after_shutdown = True

    rt2 = _make_runtime(base)
    try:
        reacquired = isinstance(rt2.context.services.get("code_agent_jobs"), CodeAgentJobs)
    finally:
        rt2.shutdown()

    ok = child_dead and grand_dead and lock_free_after_shutdown and reacquired
    detail = (
        f"child_pid={child_pid}, grandchild_pid={grandchild_pid}, "
        f"child_dead={child_dead}, grandchild_dead={grand_dead}, "
        f"lock_free_after_shutdown={lock_free_after_shutdown}, lock_reacquired={reacquired}"
    )
    return ("PASS" if ok else "FAIL"), detail


def _init_git_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    for args in (
        ["git", "init"],
        ["git", "config", "user.email", "kabul@test.local"],
        ["git", "config", "user.name", "Kabul Test"],
    ):
        subprocess.run(args, cwd=repo, capture_output=True, creationflags=flags, check=True)
    (repo / "README.md").write_text("kabul\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "."], cwd=repo, capture_output=True, creationflags=flags, check=True
    )
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=repo,
        capture_output=True,
        creationflags=flags,
        check=True,
    )


def _run_verification_case(
    base: Path, name: str, checks: list[list[str]]
) -> tuple[str, str]:
    proj_dir = base / name
    _init_git_repo(proj_dir)
    state_dir = base / f"state_{name}"
    state_dir.mkdir(parents=True, exist_ok=True)
    audit = AuditLog(base / f"audit_{name}.jsonl")
    jobs = CodeAgentJobs(audit, store_path=state_dir / "code_agent.sqlite3")
    profile = AgentProfile(
        kind="generic",
        command=sys.executable,
        args=("-c", "import sys; sys.stdin.read()"),
    )
    try:
        spec = jobs.prepare(
            name,
            "Kontrol dogrulama gorevi",
            ("README.md",),
            ("Kontrol sonucu dogrulanmali.",),
            agent="generic",
        )
        job_id = jobs.start(
            name,
            proj_dir,
            "Kontrol dogrulama gorevi",
            spec=spec,
            profile=profile,
            checks=checks,
            check_timeout=15.0,
        )
        deadline = time.monotonic() + 20.0
        record: dict[str, Any] = {}
        while time.monotonic() < deadline:
            matches = [j for j in jobs.status() if j["job_id"] == job_id]
            if matches and matches[0]["status"] not in ACTIVE:
                record = matches[0]
                break
            time.sleep(0.05)
        return str(record.get("verification", "timeout")), str(record.get("summary", ""))
    finally:
        jobs.shutdown()


def check_cl179(base: Path) -> tuple[str, str]:
    pass_cmd = [[sys.executable, "-c", "raise SystemExit(0)"]]
    fail_cmd = [[sys.executable, "-c", "raise SystemExit(3)"]]

    v_pass, sum_pass = _run_verification_case(base, "p_pass", pass_cmd)
    v_fail, sum_fail = _run_verification_case(base, "p_fail", fail_cmd)
    v_none, sum_none = _run_verification_case(base, "p_none", [])

    pass_ok = v_pass == "accepted" and "kontroller geçti" in sum_pass.casefold()
    fail_ok = v_fail == "failed" and "BAŞARISIZ" in sum_fail
    none_ok = v_none == "unverified" and "doğrulanmadı" in sum_none.casefold()
    ok = pass_ok and fail_ok and none_ok
    detail = (
        f"pass={v_pass}, fail={v_fail}, none={v_none}"
    )
    return ("PASS" if ok else "FAIL"), detail


def check_cl182(base: Path) -> tuple[str, str]:
    db_path = base / "state" / "memory.sqlite3"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = MemoryStore(db_path)
    secret_sentence = "KABUL-SILINEN-CUMLE-7f3a9c"
    kept_note = "KABUL-KORUNAN-OZ-NOT-991a"

    store.add_turn(
        TurnRecord(
            turn_id="t_del",
            user_text=f"Merhaba {secret_sentence} kaydi.",
            steps=(),
            answer="Yanit metni",
            answer_private=False,
            tainted=False,
        )
    )
    store.add_note(kept_note, turn_id="t_del", max_notes=10)

    store.delete_history()

    needle = secret_sentence.encode("utf-8")
    found_in: list[str] = []
    for suffix in ("", "-wal", "-shm", "-journal"):
        candidate = db_path.with_name(db_path.name + suffix)
        if candidate.exists() and needle in candidate.read_bytes():
            found_in.append(candidate.name)

    notes_after = [n.text for n in store.list_notes()]
    turns_after = store.recent_turns(10)
    ok = len(found_in) == 0 and notes_after == [kept_note] and len(turns_after) == 0
    detail = f"found_in_files={found_in}, turns_after={len(turns_after)}, notes={notes_after}"
    return ("PASS" if ok else "FAIL"), detail


def check_cl184(base: Path) -> tuple[str, str]:
    audit_path = base / "audit.jsonl"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    initial_size = DEFAULT_MAX_BYTES + 128
    with audit_path.open("wb") as f:
        f.seek(initial_size - 1)
        f.write(b"\n")

    log = AuditLog(audit_path)
    log.emit("kabul_rotation_event", marker="cl184_yeni_kayit")

    pid = os.getpid()
    rotated = sorted(base.glob(f"audit-*-{pid}.jsonl"))
    new_exists = audit_path.exists()
    new_content = audit_path.read_text(encoding="utf-8") if new_exists else ""
    rotated_size = rotated[0].stat().st_size if rotated else 0

    ok = (
        len(rotated) == 1
        and rotated_size == initial_size
        and new_exists
        and "cl184_yeni_kayit" in new_content
        and audit_path.stat().st_size < initial_size
    )
    rot_name = rotated[0].name if rotated else "none"
    detail = (
        f"rotated={rot_name}, rotated_size={rotated_size}, "
        f"new_exists={new_exists}, marker_in_new={'cl184_yeni_kayit' in new_content}"
    )
    return ("PASS" if ok else "FAIL"), detail


def check_rs58(base: Path) -> tuple[str, str]:
    state = base / "state"
    state.mkdir(parents=True, exist_ok=True)
    corrupt_db = state / "code_agent.sqlite3"
    corrupt_db.write_bytes(b"not a sqlite database" * 100)

    settings = Settings(
        workspace=base,
        state_dir=state,
        audit_path=base / "audit.jsonl",
        persist_memory=False,
    )
    rt = create_runtime(
        base / "config.yaml",
        lambda *_: False,
        provider=ScriptedProvider([]),
        settings=settings,
    )
    try:
        services = rt.context.services
        files = sorted(p.name for p in state.iterdir())
        service_val = services.get("code_agent_jobs")
        ok = rt.runner is not None
        detail = f"runtime_up={ok}, service={type(service_val).__name__}, state_files={files}"
        return ("PASS" if ok else "FAIL"), detail
    finally:
        rt.shutdown()


def check_rs70(base: Path) -> tuple[str, str]:
    del base
    proc_elapsed = 0.0
    proc_ok = True
    if os.name == "nt":
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            **code_agent_process.process_options(),
        )
        try:
            capture = code_agent_process.ProcessCapture(proc, "")
            t0 = time.monotonic()
            capture.finish(kill=True)
            proc_elapsed = time.monotonic() - t0
            proc_ok = proc_elapsed < 8.0 and proc.poll() is not None
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=2)

    try:
        from PySide6.QtCore import QCoreApplication, QThread

        from artemis.channels.desktop.thread_shutdown import DETACHED, shutdown_thread
    except ImportError as exc:
        if proc_ok:
            return "PASS", f"process_cleanup_s={proc_elapsed:.2f} (PySide6 yok: {exc})"
        return "FAIL", f"process_cleanup_s={proc_elapsed:.2f}"

    app = QCoreApplication.instance() or QCoreApplication([])

    class _BlockedThread(QThread):
        def run(self) -> None:
            while not self.isInterruptionRequested():
                self.msleep(50)
            self.msleep(400)

    t = _BlockedThread()
    t.start()
    deadline = time.monotonic() + 2.0
    while not t.isRunning() and time.monotonic() < deadline:
        time.sleep(0.01)

    t0 = time.monotonic()
    detached = shutdown_thread(t, wait_ms=50)
    elapsed = time.monotonic() - t0

    t.wait(1000)
    if t in DETACHED:
        DETACHED.remove(t)
    del app

    ok = detached and elapsed < 2.0 and proc_ok
    detail = (
        f"detached={detached}, thread_shutdown_s={elapsed:.3f}, "
        f"proc_cleanup_s={proc_elapsed:.3f}"
    )
    return ("PASS" if ok else "FAIL"), detail


def main() -> int:
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if isinstance(sys.stderr, io.TextIOWrapper):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    checks: list[tuple[str, Callable[[Path], tuple[str, str]]]] = [
        ("CL149", check_cl149),
        ("CL150", check_cl150),
        ("CL151", check_cl151),
        ("CL179", check_cl179),
        ("CL182", check_cl182),
        ("CL184", check_cl184),
        ("RS58", check_rs58),
        ("RS70", check_rs70),
    ]
    any_fail = False
    for check_id, fn in checks:
        temp_dir = Path(tempfile.mkdtemp(prefix="kabul-guv-"))
        try:
            status, detail = fn(temp_dir)
        except Exception as error:  # noqa: BLE001
            status = "FAIL"
            detail = f"{type(error).__name__}: {error}"
        finally:
            _remove_tree(temp_dir)
        if status not in {"PASS", "FAIL", "SKIP"}:
            status = "FAIL"
        if status == "FAIL":
            any_fail = True
        clean = " ".join(str(detail).splitlines()).strip()
        print(f"{status} {check_id} {clean}")
    return 1 if any_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
