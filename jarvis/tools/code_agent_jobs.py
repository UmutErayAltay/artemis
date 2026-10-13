"""Kod ajanı işleri (ADR 0023): turdan bağımsız thread'de headless `claude -p`.

asyncio değil thread: iş tur süresini aşar ve CLI'da olay döngüsü turla birlikte kapanır.
Sonuç ajanın beyanına değil git'e (çıkış kodu, taban..HEAD commit'leri, kalan değişiklik)
dayanır.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from jarvis.safety.audit import AuditLog
from jarvis.tools.platform import child_environment

SUMMARY_LIMIT = 2000
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Süreç fabrikası: varsayılan subprocess.Popen; testler communicate/kill/returncode
# sunan sahte bir nesne enjekte eder.
ProcessFactory = Callable[..., Any]

RULES = (
    "Kurallar / Rules: Work only inside this repository. Commit locally with clear messages. "
    "NEVER push, never add or change remotes, never rewrite history (no reset --hard, "
    "rebase, amend of earlier commits, force). Yalnız bu depoda çalış, değişiklikleri net "
    "mesajlarla yerel olarak commit et, asla push etme. Bitirirken kısa bir Türkçe özet yaz."
)


class CodeAgentError(Exception):
    """Başlatma ön koşulu sağlanmadı; mesaj kullanıcıya olduğu gibi gösterilir."""


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
            timeout=30,
            creationflags=NO_WINDOW,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CodeAgentError(f"git çalıştırılamadı: {type(error).__name__}.") from error


class CodeAgentJobs:
    def __init__(
        self,
        audit: AuditLog,
        *,
        process_factory: ProcessFactory = subprocess.Popen,
    ) -> None:
        self._audit = audit
        self._factory = process_factory
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._procs: dict[str, Any] = {}
        self._cancelled: set[str] = set()

    def start(
        self,
        alias: str,
        path: Path,
        task: str,
        *,
        command: str = "claude",
        max_minutes: float = 20.0,
        model: str | None = None,
    ) -> str:
        exe = shutil.which(command)
        if exe is None:
            raise CodeAgentError(f"'{command}' komutu bulunamadı (PATH).")
        if _git(path, "rev-parse", "--is-inside-work-tree").returncode != 0:
            raise CodeAgentError(f"'{alias}' bir git deposu değil.")
        head = _git(path, "rev-parse", "HEAD")
        if head.returncode != 0:
            raise CodeAgentError(f"'{alias}' deposunda henüz commit yok.")
        if _git(path, "status", "--porcelain").stdout.strip():
            raise CodeAgentError(
                f"'{alias}' çalışma ağacı kirli; önce değişiklikleri commit'leyin veya "
                "kaldırın (ajanın commit'leri kayıtlı tabanın üstünde kalmalı)."
            )
        base = head.stdout.strip()
        job_id = uuid.uuid4().hex[:8]
        with self._lock:
            if any(j["project"] == alias and j["status"] == "running" for j in self._jobs.values()):
                raise CodeAgentError(f"'{alias}' için zaten çalışan bir kod ajanı işi var.")
            self._jobs[job_id] = {
                "job_id": job_id,
                "project": alias,
                "base": base,
                "status": "running",
                "commits": [],
                "leftover_changes": False,
                "summary": "",
                "duration_seconds": 0.0,
                "cost_usd": None,
            }
        argv = [
            exe, "-p", "--output-format", "json", "--permission-mode", "acceptEdits",
            "--allowedTools", "Read", "Edit", "Write", "Glob", "Grep", "Bash",
            "--disallowedTools", "Bash(git push:*)", "WebFetch",
            "WebSearch", "--append-system-prompt", RULES,
        ]  # fmt: skip
        if model:
            argv += ["--model", model]
        self._audit.emit("code_agent_started", job_id=job_id, project=alias, base=base)
        threading.Thread(
            target=self._run,
            args=(job_id, path, task, argv, max_minutes * 60),
            name=f"code-agent-{job_id}",
            daemon=True,
        ).start()
        return job_id

    def _run(self, job_id: str, path: Path, task: str, argv: list[str], timeout: float) -> None:
        began = time.monotonic()
        status = "error"
        summary = ""
        cost: float | None = None
        try:
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
                creationflags=NO_WINDOW,
            )
        except OSError as error:
            summary = f"Kod ajanı başlatılamadı: {type(error).__name__}."
            proc = None
        if proc is not None:
            with self._lock:
                self._procs[job_id] = proc
                cancelled = job_id in self._cancelled
            if cancelled:
                proc.kill()
            timed_out = False
            try:
                stdout, stderr = proc.communicate(task, timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                proc.kill()
                stdout, stderr = proc.communicate()
            with self._lock:
                cancelled = job_id in self._cancelled
            status, summary, cost = self._judge(proc.returncode, stdout, stderr)
            if cancelled:
                status = "cancelled"
            elif timed_out:
                status = "timeout"
        commits, leftover = self._git_state(job_id, path)
        with self._lock:
            job = self._jobs[job_id]
            job.update(
                status=status,
                commits=commits,
                leftover_changes=leftover,
                summary=summary[:SUMMARY_LIMIT],
                duration_seconds=round(time.monotonic() - began, 1),
                cost_usd=cost,
            )
            self._procs.pop(job_id, None)
            record = dict(job)
        record.pop("base")
        self._audit.emit("code_agent_done", **record)

    @staticmethod
    def _judge(code: int | None, stdout: str, stderr: str) -> tuple[str, str, float | None]:
        try:
            parsed = json.loads(stdout)
        except (ValueError, TypeError):
            parsed = None
        if not isinstance(parsed, dict):
            detail = f"Ajan çıktısı okunamadı (çıkış kodu {code}). {stderr.strip()}"
            return "error", detail[:500], None
        result = parsed.get("result")
        cost = parsed.get("total_cost_usd")
        text = result if isinstance(result, str) else ""
        ok = code == 0 and parsed.get("is_error") is False
        return (
            "done" if ok else "error",
            text or (stderr.strip() if not ok else ""),
            float(cost) if isinstance(cost, int | float) else None,
        )

    def _git_state(self, job_id: str, path: Path) -> tuple[list[str], bool]:
        with self._lock:
            base = self._jobs[job_id]["base"]
        try:
            log = _git(path, "log", "--format=%h %s", f"{base}..HEAD")
            dirty = _git(path, "status", "--porcelain")
        except CodeAgentError:
            return [], False
        commits = log.stdout.splitlines() if log.returncode == 0 else []
        return commits, bool(dirty.stdout.strip())

    def status(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(job) for job in self._jobs.values()]

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job["status"] != "running":
                return False
            self._cancelled.add(job_id)
            proc = self._procs.get(job_id)
        if proc is not None:
            proc.kill()
        return True

    def shutdown(self) -> None:
        for job in self.status():
            if job["status"] == "running":
                self.cancel(job["job_id"])
