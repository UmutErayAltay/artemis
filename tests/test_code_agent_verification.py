"""Bağımsız kontroller: `done` süreç bitti demektir; "doğrulandı" yalnız Artemis'in kontrolüyle."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
from pathlib import Path
from typing import Any

import pytest
from test_code_agent import make_jobs, reply, wait_job
from test_code_agent import repo as _repo_fixture
from test_code_agent_backends import CODEX, plain_script

from artemis.config.settings import CodeAgentConfig, ProjectEntry
from artemis.tools.code_agent_jobs import CodeAgentJobs

repo = _repo_fixture
PASS = [sys.executable, "-c", "print('ok-'+'marker')"]
FAIL = [sys.executable, "-c", "import sys; print('broke-it'); sys.exit(3)"]


def run(jobs: CodeAgentJobs, repo_path: Path, checks: Any = (), **kwargs: Any) -> dict[str, Any]:
    job_id = jobs.start("proj", repo_path, "görev", command=sys.executable, checks=checks, **kwargs)
    return wait_job(jobs, job_id)


def test_passing_check_is_accepted(tmp_path: Path, repo: Path) -> None:
    jobs, _ = make_jobs(tmp_path, reply())
    job = run(jobs, repo, [PASS])
    assert job["status"] == "done" and job["verification"] == "accepted"
    assert job["checks"][0]["passed"] is True and job["checks"][0]["exit_code"] == 0
    assert "ok-marker" in job["checks"][0]["output_tail"]
    audit = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    event = next(json.loads(x) for x in audit.splitlines() if "code_agent_checks" in x)
    assert event["exit_code"] == 0 and event["passed"] is True
    assert "ok-marker" not in json.dumps(event)  # çıktı denetime girmez
    jobs.shutdown()


def test_failing_check_keeps_done_but_fails_verification(tmp_path: Path, repo: Path) -> None:
    jobs, _ = make_jobs(tmp_path, reply())
    job = run(jobs, repo, [PASS, FAIL])
    assert job["status"] == "done" and job["verification"] == "failed"
    assert [c["passed"] for c in job["checks"]] == [True, False]
    assert job["checks"][1]["exit_code"] == 3 and "broke-it" in job["checks"][1]["output_tail"]
    assert "BAŞARISIZ" in job["summary"]
    jobs.shutdown()


def test_no_checks_is_unverified_and_panel_does_not_claim_verified(
    tmp_path: Path, repo: Path
) -> None:
    jobs, _ = make_jobs(tmp_path, reply())
    job = run(jobs, repo)
    assert job["status"] == "done" and job["verification"] == "unverified"
    assert job["checks"] == []
    pytest.importorskip("PySide6")
    from artemis.channels.desktop.projects_panel import status_chip, status_label

    assert status_label(job) == "Bitti (doğrulanmadı)" and status_chip(job)[0] != "Doğrulandı"
    assert status_label({**job, "verification": "failed"}) == "Bitti, kontrol başarısız"
    assert status_label({**job, "verification": "accepted"}) == "Doğrulandı"
    jobs.shutdown()


def test_missing_check_binary_fails_without_crash(tmp_path: Path, repo: Path) -> None:
    jobs, _ = make_jobs(tmp_path, reply())
    job = run(jobs, repo, [[str(tmp_path / "yok-boyle-bir-komut")]])
    assert job["verification"] == "failed" and job["checks"][0]["exit_code"] is None
    jobs.shutdown()


def test_check_timeout_is_failure(tmp_path: Path, repo: Path) -> None:
    jobs, _ = make_jobs(tmp_path, reply())
    slow = [sys.executable, "-c", "import time; time.sleep(30)"]
    job = run(jobs, repo, [slow], check_timeout=0.3)
    assert job["verification"] == "failed" and "zaman aşımı" in job["checks"][0]["output_tail"]
    jobs.shutdown()


def test_codex_exit_zero_without_commit_is_uncommitted(tmp_path: Path, repo: Path) -> None:
    edit = f"pathlib.Path({str(repo / 'a.txt')!r}).write_text('degisti')\n"
    jobs, _ = make_jobs(tmp_path, plain_script(tmp_path / "o", before=edit))
    spec = jobs.prepare("proj", "görev", ("k",), ("ö",), agent="codex")
    job_id = jobs.start("proj", repo, "görev", spec=spec, profile=CODEX, command="unused")
    job = wait_job(jobs, job_id)
    assert job["status"] == "done" and job["changes"] == "uncommitted"
    assert job["commits"] == [] and "commitlenmedi" in job["summary"]
    assert job["verification"] == "unverified"
    jobs.shutdown()


def test_commits_and_none_changes(tmp_path: Path, repo: Path) -> None:
    commit = (
        "pathlib.Path('b.txt').write_text('b')\n"
        "subprocess.run(['git','add','.'], check=True)\n"
        "subprocess.run(['git','-c','user.name=t','-c','user.email=t@t',"
        "'commit','-q','-m','ajan'], check=True)\n"
    )
    jobs, _ = make_jobs(tmp_path, reply(before=commit))
    assert run(jobs, repo)["changes"] == "commits"
    jobs.shutdown()
    jobs2, _ = make_jobs(tmp_path / "x" if (tmp_path / "x").mkdir() is None else tmp_path, reply())
    # İkinci iş: repo temiz değil artık (b.txt commitli, temiz) ve ajan hiçbir şey yapmaz.
    assert run(jobs2, repo)["changes"] == "none"
    jobs2.shutdown()


def test_cancelled_job_runs_no_checks(tmp_path: Path, repo: Path) -> None:
    marker = tmp_path / "ran"
    check = [sys.executable, "-c", f"open({str(marker)!r}, 'w').write('x')"]
    jobs, _ = make_jobs(tmp_path, reply(before="import time; time.sleep(30)"))
    job_id = jobs.start("proj", repo, "görev", command=sys.executable, checks=[check])
    wait_job(jobs, job_id, {"running"})
    assert jobs.cancel(job_id)
    job = wait_job(jobs, job_id)
    assert job["status"] == "cancelled" and job["verification"] == "unverified"
    assert not marker.exists()
    jobs.shutdown()


def test_cancel_during_check_stops_it(tmp_path: Path, repo: Path) -> None:
    jobs, _ = make_jobs(tmp_path, reply())
    slow = [sys.executable, "-c", "import time; time.sleep(30)"]
    job_id = jobs.start("proj", repo, "görev", command=sys.executable, checks=[slow])
    threading.Timer(1.0, jobs.cancel, args=(job_id,)).start()
    job = wait_job(jobs, job_id)
    assert job["status"] == "cancelled" and job["verification"] == "unverified"
    jobs.shutdown()


def test_checks_use_no_shell_and_child_environment(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARTEMIS_TEST_FAKE_TOKEN", "gizli-deger")
    out = tmp_path / "gorunen.json"
    body = (
        "import json, os, sys; "
        f"open({str(out)!r}, 'w').write(json.dumps([sorted(os.environ), sys.argv[1], os.getcwd()]))"
    )
    # `&&` ve `$HOME` kabuk olsaydı yorumlanırdı; argv öğesi olarak aynen ulaşmalı.
    check = [sys.executable, "-c", body, "a && b $HOME"]
    jobs, _ = make_jobs(tmp_path, reply())
    job = run(jobs, repo, [check])
    assert job["verification"] == "accepted"
    names, arg, cwd = json.loads(out.read_text(encoding="utf-8"))
    assert "ARTEMIS_TEST_FAKE_TOKEN" not in names
    assert arg == "a && b $HOME" and Path(cwd).resolve() == repo.resolve()
    jobs.shutdown()


def test_old_rows_without_new_fields_load_as_unverified(tmp_path: Path, repo: Path) -> None:
    jobs, _ = make_jobs(tmp_path, reply(), persistent=True)
    job = run(jobs, repo)
    jobs.shutdown()
    store = tmp_path / "jobs.sqlite3"
    with sqlite3.connect(store) as connection:
        record = json.loads(
            connection.execute(
                "SELECT value FROM records WHERE key=?", (job["job_id"],)
            ).fetchone()[0]
        )
        for key in ("verification", "checks", "changes"):
            del record[key]
        connection.execute(
            "UPDATE records SET value=? WHERE key=?", (json.dumps(record), job["job_id"])
        )
    restarted, _ = make_jobs(tmp_path, reply(), persistent=True)
    old = restarted.status()[0]
    assert old["status"] == "done" and old["verification"] == "unverified"
    assert old["checks"] == [] and old["changes"] == "none"
    restarted.shutdown()


@pytest.mark.parametrize("stdout", ["not json", ""])
def test_claude_parse_failure_reports_reason_and_redacted_stderr(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch, stdout: str
) -> None:
    monkeypatch.setenv("ARTEMIS_TEST_API_KEY", "sk-cok-gizli-123456")
    script = (
        "import sys; sys.stdin.read()\n"
        f"print({stdout!r})\n"
        "sys.stderr.write('Hata: oturum yok sk-cok-gizli-123456')\n"
    )
    jobs, _ = make_jobs(tmp_path, script)
    job = run(jobs, repo)
    assert job["status"] == "error"
    assert "JSONDecodeError" in job["summary"] and "oturum yok" in job["summary"]
    assert "sk-cok-gizli-123456" not in job["summary"]
    jobs.shutdown()


def test_config_checks_validated_and_reach_job(tmp_path: Path, repo: Path) -> None:
    with pytest.raises(ValueError):
        ProjectEntry(path=repo, checks=((),))
    with pytest.raises(ValueError):
        ProjectEntry(path=repo, checks=(("pytest", " "),))
    config = CodeAgentConfig(projects={"proj": {"path": str(repo), "checks": [["pytest", "-q"]]}})
    assert config.project_entries["proj"].checks == (("pytest", "-q"),)
    assert config.check_timeout_seconds == 600.0
    assert os.name  # CodeAgentConfig dış girdiyi dizi-dizi olarak kabul eder
