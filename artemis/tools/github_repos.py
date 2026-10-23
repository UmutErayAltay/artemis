"""Kendi GitHub repolarını listeleme, klonlama/fetch ve yerel keşif (ADR 0023). Qt'siz.

Kimlik doğrulamayı `gh` yönetir (keyring); Artemis token okumaz, saklamaz, yalnız `gh`/`git`
çalıştırır. Mevcut depolara yalnız `git fetch --prune` yapılır: çalışma ağacına, dala veya
commit'lere asla dokunulmaz.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from artemis.tools.platform import child_environment

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
REPO_NAME_RE = re.compile(r"[A-Za-z0-9._-]{1,100}")
ALIAS_RE = re.compile(r"[a-z0-9_-]{1,32}")
LIST_TIMEOUT = 60.0
FETCH_TIMEOUT = 300.0
CLONE_TIMEOUT = 900.0

# (argv, cwd, timeout) -> CompletedProcess; argv[0] "gh" veya "git" adıdır, yol çözümü
# varsayılan çalıştırıcıdadır. Testler sahte çalıştırıcı enjekte eder.
Runner = Callable[[list[str], Path | None, float], "subprocess.CompletedProcess[str]"]


class GitHubError(Exception):
    """Mesaj kullanıcıya olduğu gibi gösterilir; sır içermez."""


def run_command(
    argv: list[str], cwd: Path | None, timeout: float
) -> subprocess.CompletedProcess[str]:
    exe = shutil.which(argv[0])
    if exe is None:
        raise GitHubError(f"'{argv[0]}' komutu bulunamadı (PATH).")
    try:
        return subprocess.run(
            [exe, *argv[1:]],
            cwd=cwd,
            env=child_environment(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=NO_WINDOW,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise GitHubError(f"'{argv[0]}' zaman aşımına uğradı.") from error
    except OSError as error:
        raise GitHubError(f"'{argv[0]}' çalıştırılamadı: {type(error).__name__}.") from error


@dataclass
class SyncReport:
    cloned: list[str] = field(default_factory=list)
    fetched: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)


def list_repos(owner: str, runner: Runner = run_command) -> list[tuple[str, str]]:
    """(ad, sahip/ad) çiftleri. Fork'lar bilerek dışarıda: başkasının kodu."""
    result = runner(
        ["gh", "repo", "list", owner, "--source", "--no-archived", "--limit", "500",
         "--json", "name,nameWithOwner"],
        None, LIST_TIMEOUT,
    )  # fmt: skip
    if result.returncode != 0:
        detail = result.stderr.strip()[:300]
        raise GitHubError(f"gh repo list başarısız (çıkış kodu {result.returncode}). {detail}")
    try:
        data = json.loads(result.stdout)
    except ValueError as error:
        raise GitHubError("gh çıktısı JSON olarak okunamadı.") from error
    if not isinstance(data, list):
        raise GitHubError("gh çıktısı beklenen listeyi içermiyor.")
    repos: list[tuple[str, str]] = []
    for item in data:
        name = item.get("name") if isinstance(item, dict) else None
        full = item.get("nameWithOwner") if isinstance(item, dict) else None
        if (
            not isinstance(name, str)
            or not isinstance(full, str)
            or not REPO_NAME_RE.fullmatch(name)
            or name in {".", ".."}
            or full.casefold() != f"{owner}/{name}".casefold()
        ):
            # Ad klasör adı olacağından şüpheli tek kayıt bile bütün listeyi geçersiz kılar.
            raise GitHubError("gh çıktısında geçersiz bir repo kaydı var; eşitleme durduruldu.")
        repos.append((name, full))
    return repos


def _origin_url(path: Path) -> str | None:
    # ponytail: .git/config'i regex'le okur (alt süreç yok, 500 repo hızlı); worktree/.git
    # dosyası ve include'lar desteklenmez, bu depolar "git deposu değil" sayılır.
    try:
        text = (path / ".git" / "config").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    section = re.search(r'^\[remote "origin"\][ \t]*\r?\n((?:(?!\[).*\r?\n?)*)', text, re.M)
    if section is None:
        return None
    url = re.search(r"^\s*url\s*=\s*(\S.*?)\s*$", section.group(1), re.M)
    return url.group(1) if url else None


def _origin_repo(path: Path, owner: str) -> str | None:
    """Origin github.com/<owner>/<ad>(.git) ise <ad>, aksi halde None (https ve ssh)."""
    url = _origin_url(path)
    if url is None:
        return None
    match = re.fullmatch(
        r"(?:https://(?:[^@/:\s]+@)?github\.com/|git@github\.com:|ssh://git@github\.com/)"
        r"([^/\s]+)/([^/\s]+?)(?:\.git)?/?",
        url,
        re.I,
    )
    if match is None or match.group(1).casefold() != owner.casefold():
        return None
    return match.group(2)


def sync_repos(
    owner: str,
    repos_dir: Path,
    runner: Runner = run_command,
    stop: Callable[[], bool] = lambda: False,
) -> SyncReport:
    """Eksik repoyu klonlar; mevcut olana yalnız `git fetch --prune`. Hatalar tek tek tutulur."""
    repos = list_repos(owner, runner)
    try:
        repos_dir.mkdir(exist_ok=True)  # üst klasörler sessizce oluşturulmaz
    except OSError as error:
        raise GitHubError(f"Repo klasörü hazırlanamadı: {type(error).__name__}.") from error
    report = SyncReport()
    for name, full in repos:
        if stop():
            break
        target = repos_dir / name
        try:
            if not target.exists():
                result = runner(["gh", "repo", "clone", full, str(target)], None, CLONE_TIMEOUT)
                bucket, done = report.cloned, "klonlama"
            elif (origin := _origin_repo(target, owner)) is not None and (
                origin.casefold() == name.casefold()
            ):
                result = runner(["git", "fetch", "--prune"], target, FETCH_TIMEOUT)
                bucket, done = report.fetched, "fetch"
            else:
                report.skipped.append((name, "klasör var ama bu GitHub reposu değil"))
                continue
        except GitHubError as error:
            report.failed.append((name, str(error)))
            continue
        if result.returncode == 0:
            bucket.append(name)
        else:
            report.failed.append((name, f"{done} başarısız: {result.stderr.strip()[:200]}"))
    return report


def discover_projects(owner: str, repos_dir: Path) -> dict[str, Path]:
    """Yalnız yerel: origin'i github.com/<owner>/ olan doğrudan alt klasörler -> takma ad."""
    found: dict[str, list[Path]] = {}
    try:
        children = sorted(repos_dir.iterdir())
    except OSError:
        return {}
    for child in children:
        if not child.is_dir() or _origin_repo(child, owner) is None:
            continue
        alias = re.sub(r"[^a-z0-9_-]", "-", child.name.lower())[:32]
        if ALIAS_RE.fullmatch(alias):
            found.setdefault(alias, []).append(child)
    # Çakışan takma adlar belirsizdir: ikisi de atlanır.
    return {alias: paths[0] for alias, paths in found.items() if len(paths) == 1}
