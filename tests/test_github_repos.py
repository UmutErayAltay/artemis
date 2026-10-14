"""GitHub repo eşitleme/keşif testleri: ağ ve gerçek gh yok; origin kontrolü gerçek git ile."""

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from jarvis.channels.runtime import create_runtime
from jarvis.config.settings import CodeAgentConfig, Settings, load_settings
from jarvis.tools import github_repos
from jarvis.tools.github_repos import (
    GitHubError,
    SyncReport,
    discover_projects,
    list_repos,
    sync_repos,
)


def make_repo(path: Path, origin: str | None) -> None:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    if origin:
        subprocess.run(["git", "-C", str(path), "remote", "add", "origin", origin], check=True)


class FakeRunner:
    def __init__(self, repos: Any, fail: set[str] | None = None) -> None:
        self.repos = repos
        self.fail = fail or set()
        self.calls: list[tuple[list[str], Path | None]] = []

    def __call__(
        self, argv: list[str], cwd: Path | None, timeout: float
    ) -> "subprocess.CompletedProcess[str]":
        self.calls.append((argv, cwd))
        if argv[:3] == ["gh", "repo", "list"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps(self.repos), "")
        name = Path(argv[-1]).name if argv[:3] == ["gh", "repo", "clone"] else (
            cwd.name if cwd else ""
        )
        if name in self.fail:
            return subprocess.CompletedProcess(argv, 1, "", "boom")
        if argv[:3] == ["gh", "repo", "clone"]:
            Path(argv[-1]).mkdir()
        return subprocess.CompletedProcess(argv, 0, "", "")


def repo(name: str, owner: str = "umut") -> dict[str, str]:
    return {"name": name, "nameWithOwner": f"{owner}/{name}"}


@pytest.mark.parametrize("bad", [
    "notalist", [1], [{"name": "a"}], [repo("..")], [repo(".")], [repo("a/b")],
    [{"name": "x y", "nameWithOwner": "umut/x y"}], [repo("ok", "other")],
    [{"name": "a" * 101, "nameWithOwner": "umut/" + "a" * 101}],
])
def test_list_rejects_bad_json(bad: Any) -> None:
    with pytest.raises(GitHubError):
        list_repos("umut", FakeRunner(bad))


def test_list_uses_safe_flags_and_returns_pairs() -> None:
    runner = FakeRunner([repo("Alpha"), repo("b.c_d-e")])
    assert list_repos("umut", runner) == [("Alpha", "umut/Alpha"), ("b.c_d-e", "umut/b.c_d-e")]
    argv = runner.calls[0][0]
    assert "--source" in argv and "--no-archived" in argv and "umut" in argv


def test_sync_clone_fetch_skip_and_failure_isolation(tmp_path: Path) -> None:
    repos_dir = tmp_path / "Projeler"
    repos_dir.mkdir()
    make_repo(repos_dir / "have", "https://github.com/Umut/have.git")
    make_repo(repos_dir / "foreign", "https://github.com/someone/foreign.git")
    (repos_dir / "plain").mkdir()
    make_repo(repos_dir / "badfetch", "git@github.com:umut/badfetch.git")
    runner = FakeRunner(
        [repo(n) for n in ("new", "have", "foreign", "plain", "badfetch", "badclone", "new2")],
        fail={"badfetch", "badclone"},
    )
    report = sync_repos("umut", repos_dir, runner)
    assert report.cloned == ["new", "new2"]
    assert report.fetched == ["have"]
    assert [n for n, _ in report.skipped] == ["foreign", "plain"]
    assert [n for n, _ in report.failed] == ["badfetch", "badclone"]
    assert all(isinstance(r, str) and r for _, r in report.skipped + report.failed)
    verbs = {tuple(a[:3]) for a, _ in runner.calls[1:]}
    assert verbs <= {("gh", "repo", "clone"), ("git", "fetch", "--prune")}  # pull/reset yok
    fetch = next(c for c in runner.calls if c[0][0] == "git")
    assert fetch[0] == ["git", "fetch", "--prune"]


def test_sync_does_not_create_parents(tmp_path: Path) -> None:
    with pytest.raises(GitHubError):
        sync_repos("umut", tmp_path / "a" / "b", FakeRunner([repo("x")]))
    sync_repos("umut", tmp_path / "ok", FakeRunner([repo("x")]))
    assert (tmp_path / "ok" / "x").is_dir()


def test_sync_stop_and_list_failure(tmp_path: Path) -> None:
    runner = FakeRunner([repo("a"), repo("b")])
    assert sync_repos("umut", tmp_path, runner, stop=lambda: True) == SyncReport()

    def failing(argv: list[str], cwd: Path | None, timeout: float) -> Any:
        return subprocess.CompletedProcess(argv, 1, "", "not logged in")

    with pytest.raises(GitHubError, match="not logged in"):
        sync_repos("umut", tmp_path, failing)


def test_discover_aliases_owner_forms_and_collisions(tmp_path: Path) -> None:
    make_repo(tmp_path / "Alpha", "https://github.com/UMUT/Alpha.git")
    make_repo(tmp_path / "beta", "git@github.com:umut/beta")
    make_repo(tmp_path / "Gamma.Tool", "ssh://git@github.com/umut/Gamma.Tool.git")
    make_repo(tmp_path / "other", "https://github.com/someone/other.git")
    make_repo(tmp_path / "gitlab", "https://gitlab.com/umut/gitlab.git")
    make_repo(tmp_path / "noremote", None)
    make_repo(tmp_path / "tricky", "https://github.com/umutx/tricky.git")
    make_repo(tmp_path / "d.p", "https://github.com/umut/d.p.git")
    make_repo(tmp_path / "d-p", "https://github.com/umut/d-p.git")
    make_repo(tmp_path / ("L" * 40), "https://github.com/umut/" + "L" * 40)
    (tmp_path / "file.txt").write_text("x")
    found = discover_projects("umut", tmp_path)
    assert found == {
        "alpha": tmp_path / "Alpha",
        "beta": tmp_path / "beta",
        "gamma-tool": tmp_path / "Gamma.Tool",
        "l" * 32: tmp_path / ("L" * 40),
    }
    assert discover_projects("umut", tmp_path / "missing") == {}


async def test_projects_discovered_per_call_and_explicit_wins(tmp_path: Path) -> None:
    from jarvis.tools.base import ToolContext
    from jarvis.tools.code_agent_jobs import CodeAgentJobs
    from jarvis.tools.plugins.code_agent import CodeAgentStatus, StatusArguments

    repos = tmp_path / "repos"
    make_repo(repos / "alpha", "https://github.com/umut/alpha.git")
    explicit = tmp_path / "mine"
    settings = Settings(
        workspace=tmp_path / "ws",
        persist_memory=False,
        code_agent=CodeAgentConfig(
            github_owner="umut", repos_dir=repos, projects={"alpha": explicit}
        ),
    )
    runtime = create_runtime(
        tmp_path / "c.yaml", lambda *_: False, settings=settings, provider=object()  # type: ignore[arg-type]
    )
    from jarvis.safety.audit import AuditLog
    from jarvis.tools.plugins.code_agent import available_projects

    # Çalışırken eşitlenen repo yeniden başlatmasız görünür (kullanıcı hatası: liste boştu).
    make_repo(repos / "beta", "https://github.com/umut/beta.git")
    agent = runtime.settings.code_agent
    assert available_projects(agent) == {"alpha": explicit, "beta": repos / "beta"}
    context = ToolContext(
        workspace=tmp_path, settings=runtime.settings,
        audit=AuditLog(tmp_path / "a.jsonl"),
        services={"code_agent_jobs": CodeAgentJobs(AuditLog(tmp_path / "a.jsonl"))},
    )  # fmt: skip
    listed = await CodeAgentStatus().execute(StatusArguments(), context)
    assert listed.data["projects"] == ["alpha", "beta"]
    off = CodeAgentConfig(repos_dir=repos)
    assert available_projects(off) == {}


def test_env_has_no_secrets(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GH_TOKEN", "x")
    monkeypatch.setenv("OPENROUTER_API_KEY", "x")
    seen: dict[str, Any] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> Any:
        seen.update(kwargs, argv=argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(github_repos.shutil, "which", lambda name: "/bin/" + name)
    monkeypatch.setattr(github_repos.subprocess, "run", fake_run)
    github_repos.run_command(["git", "fetch"], tmp_path, 5)
    assert "GH_TOKEN" not in seen["env"] and "OPENROUTER_API_KEY" not in seen["env"]
    assert seen["argv"][0] == "/bin/git" and "shell" not in seen and seen["timeout"] == 5
    monkeypatch.setattr(github_repos.shutil, "which", lambda name: None)
    with pytest.raises(GitHubError):
        github_repos.run_command(["gh"], None, 5)


def test_settings_validation_and_roundtrip(tmp_path: Path) -> None:
    for bad in ("-bad", "a" * 40, "x y", "a/b", ""):
        with pytest.raises(ValueError):
            CodeAgentConfig(github_owner=bad)
    from jarvis.channels.desktop.settings_panel import save_choices

    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({
        "primary_model": "first",
        "models": {"first": {"provider": "openrouter", "model": "m",
                             "input_per_million": "0", "output_per_million": "0"}},
        "code_agent": {"command": "claude"},
    }), encoding="utf-8")
    save_choices(path, "m", None, code_agent={
        "github_owner": "umut", "repos_dir": "Projeler", "auto_sync": False})
    agent = load_settings(path).code_agent
    assert agent.github_owner == "umut" and agent.auto_sync is False
    assert agent.repos_dir == (tmp_path / "Projeler").resolve() and agent.command == "claude"
    before = path.read_bytes()
    with pytest.raises(ValueError):
        save_choices(path, "m", None, code_agent={"github_owner": "-x"})
    assert path.read_bytes() == before
    save_choices(path, "m", None, code_agent={"github_owner": None, "repos_dir": None})
    assert load_settings(path).code_agent.github_owner is None


def test_panel_sync_runs_off_gui_thread(tmp_path: Path) -> None:
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from jarvis.channels.desktop.settings_panel import SettingsPanel

    app = QApplication.instance() or QApplication([])
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({
        "primary_model": "first",
        "models": {"first": {"provider": "openrouter", "model": "m",
                             "input_per_million": "0", "output_per_million": "0"}},
    }), encoding="utf-8")
    release = False

    def slow_sync(owner: str, folder: Path, runner: Any, stop: Any) -> SyncReport:
        while not release:
            time.sleep(0.01)
        return SyncReport(cloned=["x"], failed=[("y", "boom")])

    panel = SettingsPanel(config, github_sync=slow_sync)
    panel.inventory = None
    got: list[tuple[SyncReport, bool]] = []
    panel.github_synced.connect(lambda r, a: got.append((r, a)))
    panel.apply_requested.connect(lambda *_: got.append((SyncReport(), False)))
    panel.github_owner.setText("umut")
    panel.repos_dir.setText(str(tmp_path / "r"))
    panel._bind_github()
    # Bağlama anında kalıcı: tekrar açınca kullanıcı adı/klasör yeniden girilmez.
    saved = load_settings(config).code_agent
    assert saved.github_owner == "umut" and saved.repos_dir == tmp_path / "r"
    assert panel.github is not None and panel.github.isRunning()  # çağrı döndü, GUI serbest
    assert not panel.github_button.isEnabled()
    release = True
    deadline = time.monotonic() + 5
    while not got and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    assert got[0][0].cloned == ["x"] and got[0][1] is False
    app.processEvents()
    assert len(got) == 1  # eşitleme sonrası Uygula/yeniden başlatma tetiklenmez
    assert "1 klonlandı" in panel.github_status.text()
    assert panel.github_list.count() == 2
    panel.finish_inventory()
    assert panel.github_values()["github_owner"] == "umut"
    panel.github_owner.setText("bad owner")
    panel._bind_github()
    assert "Geçerli" in panel.github_status.text()
    panel.deleteLater()
