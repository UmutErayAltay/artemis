"""F5: kalıcı taslak, toplam son tarih ve doğrulanmış headless Claude segmentleri.

SQLite özel yerel metaveridir; restart yeni süreç veya eski PID öldürme üretmez.
Jarvis kapısı yalnız başlatma/cevap/iptali kapsar, Claude Bash'i OS sandbox değildir.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import suppress
from copy import deepcopy
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from jarvis.safety.audit import AuditLog, redact
from jarvis.tools.code_agent_process import (
    CLEANUP_SECONDS,
    NO_WINDOW,
    ProcessCapture,
    kill_tree,
    process_options,
)
from jarvis.tools.code_agent_store_lock import StoreOwnership
from jarvis.tools.platform import child_environment
from jarvis.tools.project_spec import AUTHORITY_BOUNDARY, ProjectSpec, SegmentOutput

SUMMARY_LIMIT = 2000
ProcessFactory = Callable[..., Any]
ACTIVE = {"queued", "running", "waiting", "cancelling"}
RULES = (
    "Work only inside this trusted repository. Commit locally with clear messages. "
    "NEVER push, change remotes, rewrite history or reset user changes. "
    "Delegate implementation to coder and mechanical checks to tester when useful. "
    "Do not call native AskUserQuestion in headless mode: if clarification is necessary, "
    "finish this segment with structured_output kind=question, question=your full question "
    "and summary=progress. Otherwise return kind=completed, question=null, summary=a brief "
    "Turkish result. A question is not completion. User answers arrive in a resumed session. "
    + AUTHORITY_BOUNDARY
)


class CodeAgentError(Exception):
    """Beklenen iş hatası; ham CLI çıktısı/sır yerine güvenli açıklama taşır."""


def _git(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=path,
            env=child_environment(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            creationflags=NO_WINDOW,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CodeAgentError(f"git çalıştırılamadı: {type(error).__name__}.") from error


def _checked_git(path: Path, *args: str) -> str:
    result = _git(path, *args)
    if result.returncode != 0:
        raise CodeAgentError("Git doğrulaması başarısız; depo durumu temiz kabul edilmedi.")
    return result.stdout.strip()


def role_profile(coder_model: str | None, tester_model: str | None) -> dict[str, Any]:
    profile: dict[str, Any] = {}
    for name, description, prompt, tools, model in (
        (
            "coder",
            "Implementation agent",
            "Implement the approved scope; report changes honestly.",
            ["Read", "Edit", "Write", "Glob", "Grep", "Bash"],
            coder_model,
        ),
        (
            "tester",
            "Mechanical verification agent",
            "Run focused checks and report actual failures.",
            ["Read", "Glob", "Grep", "Bash"],
            tester_model,
        ),
        (
            "coordinator",
            "Project manager",
            RULES,
            ["Read", "Glob", "Grep", "Bash", "Agent(coder)", "Agent(tester)"],
            None,
        ),
    ):
        entry: dict[str, Any] = {"description": description, "prompt": prompt, "tools": tools}
        if model:
            entry["model"] = model
        profile[name] = entry
    return profile


class CodeAgentJobs:
    def __init__(
        self,
        audit: AuditLog,
        *,
        process_factory: ProcessFactory = subprocess.Popen,
        store_path: Path | None = None,
    ) -> None:
        self._audit = audit
        self._factory = process_factory
        self._lock = threading.RLock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._drafts: dict[str, ProjectSpec] = {}
        self._procs: dict[str, Any] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._all_threads: set[threading.Thread] = set()
        self._cancelled: set[str] = set()
        self._settings: dict[str, tuple[Path, list[str]]] = {}
        self._shutdown = False
        self._deadlines: dict[str, float] = {}
        self._began: dict[str, float] = {}
        self._ownership: StoreOwnership | None = None
        self._store_path = store_path
        if store_path is not None:
            store_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                self._ownership = StoreOwnership(store_path.with_suffix(".lock"))
            except OSError as error:
                raise CodeAgentError(
                    "Proje iş servisi başka bir runtime tarafından kullanılıyor; "
                    "bu kanalda proje araçları kapalı."
                ) from error
            descriptor = os.open(store_path, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(descriptor)
            with sqlite3.connect(store_path) as connection:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS records (key TEXT PRIMARY KEY, "
                    "kind TEXT NOT NULL, value TEXT NOT NULL)"
                )
                for key, kind, value in connection.execute("SELECT key, kind, value FROM records"):
                    record = json.loads(value)
                    if kind == "draft":
                        self._drafts[key] = ProjectSpec.model_validate(record)
                    else:
                        if record["status"] in ACTIVE or record["status"] == "cleanup_pending":
                            record.update(
                                status="interrupted",
                                cleanup_pending=True,
                                summary="Önceki süreç kesildi; otomatik yeniden başlatılmadı. "
                                "Depo kilidi belirsiz süreç nedeniyle korunuyor.",
                            )
                        self._jobs[key] = record
            for record in self._jobs.values():
                self._save(record["job_id"], "job", record)

    def _save(self, key: str, kind: str, record: dict[str, Any]) -> None:
        if self._store_path is not None:
            try:
                with sqlite3.connect(self._store_path, timeout=2) as connection:
                    connection.execute(
                        "INSERT OR REPLACE INTO records VALUES (?, ?, ?)",
                        (key, kind, json.dumps(record, ensure_ascii=False)),
                    )
            except sqlite3.Error as error:
                raise CodeAgentError("İş metaverisi yazılamadı; işlem başlatılmadı.") from error

    def prepare(
        self, project: str, task: str, scope: tuple[str, ...], acceptance: tuple[str, ...]
    ) -> ProjectSpec:
        spec = ProjectSpec(
            spec_id=str(uuid.uuid4()),
            project=project,
            task=task,
            scope=scope,
            acceptance=acceptance,
        )
        with self._lock:
            self._save(spec.spec_id, "draft", spec.model_dump(mode="json"))
            self._drafts[spec.spec_id] = spec
        self._audit.emit("code_agent_draft", spec=spec.model_dump(mode="json"))
        return spec

    def drafts(self) -> list[dict[str, Any]]:
        with self._lock:
            used = {job["spec"]["spec_id"] for job in self._jobs.values()}
            return [
                spec.model_dump(mode="json")
                for spec in self._drafts.values()
                if spec.spec_id not in used
            ]

    def start(
        self,
        alias: str,
        path: Path,
        task: str,
        *,
        command: str = "claude",
        max_minutes: float = 20.0,
        model: str | None = None,
        manager_model: str | None = None,
        coder_model: str | None = None,
        tester_model: str | None = None,
        spec: ProjectSpec | None = None,
    ) -> str:
        exe = shutil.which(command)
        if exe is None:
            raise CodeAgentError(f"'{command}' komutu bulunamadı (PATH).")
        if not _git(path, "rev-parse", "--is-inside-work-tree").returncode == 0:
            raise CodeAgentError(f"'{alias}' bir git deposu değil.")
        physical = str(Path(_checked_git(path, "rev-parse", "--show-toplevel")).resolve())
        path = Path(physical)
        with self._lock:
            if self._shutdown:
                raise CodeAgentError("İş servisi kapanıyor.")
            if any(
                job["repo"] == physical and (job["status"] in ACTIVE or job.get("cleanup_pending"))
                for job in self._jobs.values()
            ):
                raise CodeAgentError(f"'{alias}' için zaten çalışan veya belirsiz bir iş var.")
            if spec is None:
                # Eski project/task çağrısı da tam görev ve yetki sınırıyla gate onayı alır.
                spec = self.prepare(alias, task, (task,), ("Görevin sonucu doğrulanmalı.",))
            if spec.project != alias or spec.task != task or self._drafts.get(spec.spec_id) != spec:
                raise CodeAgentError("Taslak bulunamadı veya onaylanan tam kopya değişmiş.")
            if any(job["spec"]["spec_id"] == spec.spec_id for job in self._jobs.values()):
                raise CodeAgentError("Bu taslak daha önce başlatılmış.")
            base = _checked_git(path, "rev-parse", "HEAD")
            if _checked_git(path, "status", "--porcelain"):
                raise CodeAgentError(
                    f"'{alias}' çalışma ağacı kirli; önce değişiklikleri kaydedin."
                )
            argv = [
                exe,
                "-p",
                "--output-format",
                "json",
                "--json-schema",
                json.dumps(SegmentOutput.model_json_schema()),
                "--permission-mode",
                "acceptEdits",
                "--allowedTools",
                "Read",
                "Edit",
                "Write",
                "Glob",
                "Grep",
                "Bash",
                "Agent(coder)",
                "Agent(tester)",
                "--disallowedTools",
                "Bash(git push:*)",
                "WebFetch",
                "WebSearch",
                "AskUserQuestion",
                "--append-system-prompt",
                RULES,
                "--agents",
                json.dumps(role_profile(coder_model, tester_model)),
                "--agent",
                "coordinator",
            ]
            if selected := manager_model or model:
                argv += ["--model", selected]
            job_id = uuid.uuid4().hex[:12]
            record: dict[str, Any] = {
                "job_id": job_id,
                "project": alias,
                "repo": physical,
                "base": base,
                "spec": spec.model_dump(mode="json"),
                "status": "queued",
                "commits": [],
                "leftover_changes": None,
                "summary": "",
                "duration_seconds": 0.0,
                "cost_usd": None,
                "created_at": time.time(),
                "deadline": time.time() + max_minutes * 60,
                "session_id": None,
                "question_id": None,
                "question": None,
                "cleanup_pending": False,
            }
            self._save(job_id, "job", record)
            self._jobs[job_id] = record
            self._settings[job_id] = path, argv
            self._began[job_id] = time.monotonic()
            self._deadlines[job_id] = self._began[job_id] + max_minutes * 60
            self._audit.emit("code_agent_started", job_id=job_id, project=alias, base=base)
            self._launch(job_id, "Approved project specification:\n" + spec.model_dump_json())
            return job_id

    def _launch(self, job_id: str, prompt: str) -> None:
        thread = threading.Thread(
            target=self._run, args=(job_id, prompt), name=f"code-agent-{job_id}", daemon=True
        )
        self._threads[job_id] = thread
        self._all_threads.add(thread)
        thread.start()

    def _update(self, job_id: str, **values: Any) -> bool:
        with self._lock:
            record = self._jobs[job_id]
            updated = {**record, **values}
            try:
                self._save(job_id, "job", updated)
            except CodeAgentError:
                record.update(
                    status="cleanup_pending",
                    cleanup_pending=True,
                    summary="Kalıcı iş durumu yazılamadı; depo kilidi korunuyor.",
                )
                # Audit de yazılamasa görünür hata ve depo kilidi korunur.
                with suppress(OSError):
                    self._audit.emit("code_agent_store_error", job_id=job_id)
                return False
            record.update(values)
            return True

    def _can_run(self, job_id: str) -> bool:
        with self._lock:
            return (
                not self._shutdown
                and job_id not in self._cancelled
                and time.monotonic() < self._deadlines[job_id]
            )

    def _run(self, job_id: str, prompt: str) -> None:
        proc = None
        capture = None
        status, summary, cost = "error", "Kod ajanı sonucu doğrulanamadı.", None
        clean = True
        segment: SegmentOutput | None = None
        session: str | None = None
        try:
            with self._lock:
                job = self._jobs[job_id]
                path, configured = self._settings[job_id]
                argv = list(configured)
                if job["session_id"]:
                    argv += ["--resume", job["session_id"]]
                if job_id in self._cancelled or self._shutdown:
                    status = "cancelled"
                elif time.monotonic() >= self._deadlines[job_id]:
                    status = "timeout"
                else:
                    if not self._update(job_id, status="running"):
                        raise CodeAgentError("İş durumu kaydedilemedi.")
            if status not in {"cancelled", "timeout"}:
                # İptal fabrikayı beklemez; spawn sonrası aynı işaret yeniden okunur.
                proc = self._factory(
                    argv,
                    cwd=path,
                    env=child_environment(),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    **process_options(),
                )
                with self._lock:
                    self._procs[job_id] = proc
            if proc is not None:
                capture = ProcessCapture(proc, prompt, resume_allowed=lambda: self._can_run(job_id))
                while not capture.exited():
                    with self._lock:
                        cancelled = job_id in self._cancelled
                        deadline = self._deadlines[job_id]
                    if cancelled:
                        status = "cancelled"
                        break
                    if time.monotonic() >= deadline:
                        status = "timeout"
                        break
                    if capture.overflow.is_set():
                        summary = "Ajan çıktısı sınırı aştı; sonuç doğrulanamadı."
                        break
                    time.sleep(0.02)
                clean = capture.finish(kill=not capture.exited())
                if clean and status not in {"cancelled", "timeout"}:
                    status, summary, cost, segment, session = self._judge(
                        proc.returncode,
                        capture.output[0],
                        capture.output[1],
                        self._jobs[job_id]["session_id"],
                    )
                    if capture.overflow.is_set() or capture.io_failed.is_set():
                        status, summary = "error", "Ajan çıktı aktarımı doğrulanamadı."
        except (OSError, ValueError, CodeAgentError) as error:
            summary = f"Kod ajanı yürütülemedi: {type(error).__name__}."
            if capture is not None:
                clean = capture.finish(kill=True)
            elif proc is not None:
                clean = kill_tree(proc)
                try:
                    proc.wait(timeout=CLEANUP_SECONDS)
                except (OSError, subprocess.TimeoutExpired):
                    clean = False
        if not clean:
            status, summary = (
                "cleanup_pending",
                "Süreç ağacı temizliği doğrulanamadı; depo kilitli.",
            )
        try:
            commits = _checked_git(
                path, "log", "--format=%h %s", f"{self._jobs[job_id]['base']}..HEAD"
            )
            dirty = bool(_checked_git(path, "status", "--porcelain"))
            commit_list = commits.splitlines()
        except CodeAgentError:
            commit_list, dirty = [], None
            if status in {"done", "waiting"}:
                status, summary = "error", "Git sonucu doğrulanamadı; iş başarılı sayılmadı."
        with self._lock:
            job = self._jobs[job_id]
            if job_id in self._cancelled and clean:
                status = "cancelled"
            elif time.monotonic() >= self._deadlines[job_id] and status in {"done", "waiting"}:
                status = "timeout"
            question_id = str(uuid.uuid4()) if status == "waiting" else None
            self._update(
                job_id,
                status=status,
                summary=redact(summary[:SUMMARY_LIMIT]),
                cost_usd=cost,
                commits=redact(commit_list),
                leftover_changes=dirty,
                cleanup_pending=not clean,
                duration_seconds=round(time.monotonic() - self._began[job_id], 1),
                session_id=session or job["session_id"],
                question_id=question_id,
                question=redact(segment.question) if segment and status == "waiting" else None,
            )
            if clean:
                self._procs.pop(job_id, None)
            record = deepcopy(job)
            self._audit.emit(
                "code_agent_waiting" if status == "waiting" else "code_agent_done",
                **{k: v for k, v in record.items() if k != "base"},
            )
            if status == "waiting":
                watcher = threading.Thread(
                    target=self._expire_waiting,
                    args=(job_id, question_id),
                    daemon=True,
                    name=f"code-agent-deadline-{job_id}",
                )
                self._threads[job_id] = watcher
                self._all_threads.add(watcher)
                watcher.start()

    def _expire_waiting(self, job_id: str, question_id: str | None) -> None:
        while True:
            with self._lock:
                job = self._jobs[job_id]
                if job["status"] != "waiting" or job["question_id"] != question_id:
                    return
                if time.monotonic() >= self._deadlines[job_id]:
                    self._update(
                        job_id,
                        status="timeout",
                        question_id=None,
                        question=None,
                        summary="Toplam iş süresi soru beklerken doldu.",
                    )
                    self._audit.emit("code_agent_done", **deepcopy(job))
                    return
            time.sleep(0.05)

    @staticmethod
    def _judge(
        code: int | None, stdout: str, stderr: str, expected_session: str | None = None
    ) -> tuple[str, str, float | None, SegmentOutput | None, str | None]:
        try:
            parsed = json.loads(stdout)
            if not isinstance(parsed, dict) or code != 0 or parsed.get("is_error") is not False:
                raise ValueError("Başarısız Claude sonucu.")
            session = parsed.get("session_id")
            if not isinstance(session, str) or str(uuid.UUID(session)) != session:
                raise ValueError("Geçerli session UUID gerekli.")
            if expected_session is not None and session != expected_session:
                raise ValueError("Resume farklı oturuma geçti.")
            segment = SegmentOutput.model_validate(parsed.get("structured_output"))
            reported = parsed.get("total_cost_usd")
            cost = (
                float(reported)
                if isinstance(reported, int | float)
                and not isinstance(reported, bool)
                and 0 <= reported < float("inf")
                else None
            )
            return (
                "waiting" if segment.kind == "question" else "done",
                segment.summary,
                cost,
                segment,
                session,
            )
        except (ValueError, TypeError, ValidationError):
            return "error", "CLI çıkışı veya structured_output doğrulanamadı.", None, None, None

    def status(self) -> list[dict[str, Any]]:
        with self._lock:
            return deepcopy(list(self._jobs.values()))

    def answer(self, job_id: str, question_id: str, answer: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if (
                job is None
                or self._shutdown
                or job["status"] != "waiting"
                or job["question_id"] != question_id
                or job_id in self._cancelled
                or time.monotonic() >= self._deadlines[job_id]
                or not job["session_id"]
            ):
                return False
            if not self._update(job_id, status="queued", question_id=None, question=None):
                return False
            self._audit.emit("code_agent_answer", job_id=job_id, question_id=question_id)
            self._launch(job_id, answer)
            return True

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job["status"] not in ACTIVE:
                return False
            self._cancelled.add(job_id)
            if job["status"] == "waiting":
                self._update(
                    job_id,
                    status="cancelled",
                    question_id=None,
                    question=None,
                    summary="İptal edildi; yarım değişiklikler diskte kalır.",
                )
                self._audit.emit("code_agent_done", **deepcopy(job))
            else:
                self._update(job_id, status="cancelling")
            return True

    def shutdown(self) -> None:
        with self._lock:
            self._shutdown = True
            for job in self._jobs.values():
                if job["status"] in ACTIVE:
                    self.cancel(job["job_id"])
            threads = list(self._all_threads)
        deadline = time.monotonic() + CLEANUP_SECONDS + 0.5
        for thread in threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        with self._lock:
            for job in self._jobs.values():
                if job["status"] in ACTIVE:
                    self._update(
                        job["job_id"],
                        status="cleanup_pending",
                        cleanup_pending=True,
                        summary="Kapanış temizliği doğrulanamadı; depo kilitli.",
                    )

        if (
            self._ownership is not None
            and not any(thread.is_alive() for thread in threads)
            and not any(job.get("cleanup_pending") for job in self.status())
        ):
            self._ownership.close()
            self._ownership = None
