"""Araştırma işi (ADR 0063): onay, yalıtılmış bağlam, sayaç, iptal, süre, bütçe, rapor, restart.

Gerçek ağ/model yok: senaryolu sağlayıcı, sahte arama ve sahte toast.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest

from artemis.brain.agent_loop import AgentLoop
from artemis.brain.errors import BudgetExceeded
from artemis.brain.fake import ScriptedProvider
from artemis.brain.models import Completion, ToolCall
from artemis.config.settings import ResearchConfig, Settings
from artemis.memory.companion import companion_dir, ensure_skeleton
from artemis.safety.audit import AuditLog
from artemis.safety.gate import SafetyGate, TurnState
from artemis.tools import research_jobs
from artemis.tools.base import ToolContext
from artemis.tools.plugins.information import WebSearch
from artemis.tools.plugins.research import (
    ResearchCancel,
    ResearchResult,
    ResearchStart,
    ResearchStatus,
)
from artemis.tools.registry import ToolRegistry
from artemis.tools.research_jobs import SYSTEM_PROMPT, ResearchJobs

SECRET = "GIZLI-KIMLIK-NOTU"


def search_call(query: str, number: int = 1) -> Completion:
    call = ToolCall(id=f"s{number}", name="web_search", arguments={"query": query})
    return Completion(tool_calls=[call])


class Env:
    def __init__(
        self, tmp_path: Path, provider: Any, *, max_searches: int = 3, max_seconds: float = 30
    ) -> None:
        self.settings = Settings(
            workspace=tmp_path,
            state_dir=tmp_path / "state",
            audit_path=tmp_path / "audit.jsonl",
            persist_memory=False,
            research=ResearchConfig(
                enabled=True, max_searches=max_searches, max_seconds=max_seconds
            ),
        )
        self.audit = AuditLog(self.settings.audit_path)
        self.searches: list[str] = []
        self.toasts: list[tuple[str, str]] = []
        self.delay = 0.0

        async def fake_search(query: str) -> list[dict[str, str]]:
            self.searches.append(query)
            if self.delay:
                await asyncio.sleep(self.delay)
            return [{"title": "A", "url": f"https://example.org/{len(self.searches)}"}]

        def toast(title: str, body: str) -> Any:
            self.toasts.append((title, body))
            return type("Outcome", (), {"status": "ok"})()

        self.services: dict[str, Any] = {"web_search": fake_search, "toast": toast}
        registry = ToolRegistry()
        for tool in (WebSearch, ResearchStart, ResearchStatus, ResearchResult, ResearchCancel):
            registry.register(tool)
        self.registry = registry
        self.provider = provider
        self.jobs = ResearchJobs(self.settings, self.audit, provider, registry, self.services)
        self.context = ToolContext(
            tmp_path, self.settings, self.audit, services={"research_jobs": self.jobs}
        )
        self.gate = SafetyGate(registry)
        self.asked: list[tuple[str, dict[str, Any]]] = []
        self.answer = True

    def approve(self, name: str, arguments: dict[str, Any]) -> bool:
        self.asked.append((name, arguments))
        return self.answer

    async def call(self, tool: str, **arguments: Any):  # type: ignore[no-untyped-def]
        return await self.gate.dispatch(
            ToolCall(id="c", name=tool, arguments=arguments),
            self.context,
            TurnState(),
            self.approve,
        )

    def wait(self, timeout: float = 10) -> dict[str, Any]:
        thread = self.jobs._thread
        assert thread is not None
        thread.join(timeout)
        assert not thread.is_alive()
        job = self.jobs.get()
        assert job is not None
        return job

    def events(self, name: str) -> list[dict[str, Any]]:
        lines = self.settings.audit_path.read_text(encoding="utf-8").splitlines()
        return [r for r in map(json.loads, lines) if r["event"] == name]


def scripted(*answers: Completion) -> ScriptedProvider:
    return ScriptedProvider(list(answers))


async def test_start_always_asks_and_denial_starts_nothing(tmp_path: Path) -> None:
    env = Env(tmp_path, scripted(Completion(content="rapor")))
    env.answer = False
    result = await env.call("research_start", topic="kuantum bilgisayarlar")
    assert result.status == "denied"
    # Onayda konu ve en çok arama sayısı görünür.
    assert env.asked == [
        ("research_start", {"topic": "kuantum bilgisayarlar", "max_searches": 12})
    ]
    assert env.jobs.get() is None
    env.answer = True
    ok = await env.call("research_start", topic="kuantum bilgisayarlar")
    assert ok.success
    assert len(env.asked) == 2  # temiz turda bile yine sorar
    env.wait()


async def test_happy_path_writes_report_with_sources_and_result_is_external(
    tmp_path: Path,
) -> None:
    provider = scripted(
        search_call("a", 1), search_call("b", 2), Completion(content="Bulgular burada.")
    )
    env = Env(tmp_path, provider)
    started = await env.call("research_start", topic="  Konu / x:y*  ")
    job_id = started.data["job_id"]
    job = env.wait()
    assert job["status"] == "done" and job["searches"] == 2
    folder = env.settings.state_dir / "arastirma"
    report = (folder / f"{job_id}.md").read_text(encoding="utf-8")
    assert "Bulgular burada." in report
    assert "https://example.org/1" in report and "https://example.org/2" in report
    assert [p.name for p in folder.iterdir()] == [f"{job_id}.md"]  # konu yola girmedi, tmp kalmadı
    assert env.toasts[0][0] == "Araştırma bitti: Konu / x:y*"
    result = await env.call("research_result")
    assert result.success and result.external and result.content_kind == "web"
    assert "Bulgular burada." in result.data["report"]
    status = await env.call("research_status", job_id=job_id)
    assert status.data["status"] == "done"
    assert len(env.events("research_search")) == 2


async def test_job_context_has_no_private_sections(tmp_path: Path) -> None:
    env = Env(tmp_path, scripted(Completion(content="rapor")))
    # Yalıtım anlamlı olsun diye hafıza, beceri ve uygulama listesi gerçekten dolu.
    ensure_skeleton(env.settings.state_dir)
    (companion_dir(env.settings.state_dir) / "Kimlik.md").write_text(SECRET, encoding="utf-8")
    skills = env.settings.state_dir / "beceriler"
    skills.mkdir(parents=True)
    (skills / "x.md").write_text(
        f"---\nname: x\ndescription: {SECRET}\n---\ngövde", encoding="utf-8"
    )
    settings = env.settings.model_copy(
        update={
            "skills": env.settings.skills.model_copy(update={"enabled": True}),
            "applications": {SECRET: "c:/x.exe"},
        }
    )
    # Kontrol: aynı ayarla NORMAL döngü sırrı isteme alır; yani test bir şey kanıtlıyor.
    normal = scripted(Completion(content="tamam"))
    ctx = ToolContext(tmp_path, settings, env.audit)
    await AgentLoop(normal, SafetyGate(env.registry), ctx, lambda *_: True).run("merhaba")
    assert SECRET in json.dumps([m.content for m in normal.requests[0]])
    # İş döngüsü: tek sistem + tek kullanıcı iletisi, sır yok, gizli ileti yok.
    env.jobs.settings = settings
    await env.call("research_start", topic="konu")
    env.wait()
    messages = env.provider.requests[0]
    assert [m.role for m in messages] == ["system", "user"]
    assert messages[0].content == SYSTEM_PROMPT.format(n=3)
    assert not any(m.private for m in messages)
    assert SECRET not in json.dumps([m.content for m in messages])


async def test_only_web_search_exists_for_the_job_and_other_calls_do_not_run(
    tmp_path: Path,
) -> None:
    other = Completion(
        tool_calls=[ToolCall(id="o", name="research_start", arguments={"topic": "xyz"})]
    )
    env = Env(tmp_path, scripted(other, search_call("a"), Completion(content="bitti")))
    assert set(env.jobs.registry._tools) == {"web_search"}
    await env.call("research_start", topic="konu")
    job = env.wait()
    assert job["status"] == "done"
    assert env.searches == ["a"]
    # Yalnız kullanıcının kendi başlatma onayı var; iç çağrı hiçbir iş başlatmadı.
    assert [name for name, _ in env.asked] == ["research_start"]
    # Onay geri çağrısı başka aracı reddeder ve denetler (kapı tainted turda buna sorar).
    failed = [e for e in env.events("tool_result") if e.get("tool") == "research_start"]
    assert all(not item.get("success") for item in failed)


async def test_counter_exhaustion_denies_more_searches(tmp_path: Path) -> None:
    calls = [search_call(f"q{i}", i) for i in range(1, 5)]
    env = Env(tmp_path, scripted(*calls, Completion(content="yarım rapor")), max_searches=2)
    started = await env.call("research_start", topic="konu", max_searches=50)
    assert env.jobs.get(started.data["job_id"])["max_searches"] == 2  # config tavanı
    job = env.wait()
    assert env.searches == ["q1", "q2"]
    assert job["searches"] == 2
    assert len(env.events("research_search")) == 2
    # Sayaç bitince kapının sorduğu 3. arama onay geri çağrısında reddedildi ve denetlendi.
    assert len(env.events("research_denied")) >= 1


async def test_cancel_stops_job_and_writes_no_report(tmp_path: Path) -> None:
    env = Env(tmp_path, scripted(search_call("a"), Completion(content="x")))
    env.delay = 5
    started = await env.call("research_start", topic="konu")
    for _ in range(100):
        if env.searches:
            break
        time.sleep(0.05)
    result = await env.call("research_cancel")
    assert result.status == "sent" and not result.success
    job = env.wait()
    assert job["status"] == "cancelled"
    assert not (env.settings.state_dir / "arastirma" / f"{started.data['job_id']}.md").exists()
    assert (await env.call("research_cancel")).message == "Çalışan araştırma yok."


async def test_time_limit_ends_job_as_timeout(tmp_path: Path) -> None:
    env = Env(tmp_path, scripted(search_call("a"), Completion(content="x")), max_seconds=1)
    env.delay = 5
    await env.call("research_start", topic="konu")
    assert env.wait()["status"] == "timeout"


async def test_budget_exceeded_fails_honestly(tmp_path: Path) -> None:
    class Broke:
        async def complete(self, *args: Any, **kwargs: Any) -> Completion:
            raise BudgetExceeded("tavan")

    env = Env(tmp_path, Broke())
    await env.call("research_start", topic="konu")
    job = env.wait()
    assert job["status"] == "error" and "maliyet" in job["summary"]
    assert env.toasts[0][0].startswith("Araştırma bitmedi")


async def test_unexpected_error_in_thread_is_recorded_not_raised(tmp_path: Path) -> None:
    class Boom:
        async def complete(self, *args: Any, **kwargs: Any) -> Completion:
            raise KeyError("beklenmeyen")

    env = Env(tmp_path, Boom())
    await env.call("research_start", topic="konu")
    job = env.wait()
    assert job["status"] == "error" and "KeyError" in job["summary"]


async def test_one_running_job_at_a_time(tmp_path: Path) -> None:
    env = Env(tmp_path, scripted(search_call("a"), Completion(content="x")))
    env.delay = 5
    await env.call("research_start", topic="birinci")
    second = await env.call("research_start", topic="ikinci")
    assert not second.success and "Zaten" in second.message
    env.jobs.shutdown()
    assert env.wait()["status"] == "cancelled"


async def test_process_death_marks_running_job_uncertain(tmp_path: Path) -> None:
    env = Env(tmp_path, scripted())
    with env.jobs._connect() as db:
        db.execute(
            "INSERT INTO research_jobs (id, topic, status, max_searches, created) "
            "VALUES ('aaaaaaaaaaaa', 't', 'running', 3, 0)"
        )
    revived = ResearchJobs(env.settings, env.audit, env.provider, env.registry, env.services)
    job = revived.get("aaaaaaaaaaaa")
    assert job is not None and job["status"] == "uncertain"
    result = await env.call("research_result", job_id="aaaaaaaaaaaa")
    assert not result.success


async def test_disabled_flag_hides_tools_and_refuses(tmp_path: Path) -> None:
    from artemis.channels.runtime import create_runtime

    settings = Settings(
        workspace=tmp_path,
        state_dir=tmp_path / "state",
        audit_path=tmp_path / "a.jsonl",
        persist_memory=False,
    )
    registry = ToolRegistry()
    registry.register(ResearchStart)
    registry.register(WebSearch)
    runtime = create_runtime(
        tmp_path / "x.yaml",
        lambda *_: True,
        provider=scripted(),
        registry=registry,
        settings=settings,
    )
    assert "research_start" not in runtime.registry
    assert "web_search" in runtime.registry
    assert "research_jobs" not in runtime.context.services
    runtime.shutdown()
    # Araç yine de çağrılırsa (ör. eski kayıt) kapı onay sormadan reddeder.
    off = Env(tmp_path / "e", scripted())
    off.settings = off.settings.model_copy(update={"research": ResearchConfig()})
    off.context.settings = off.settings
    result = await off.call("research_start", topic="konu")
    assert not result.success and "kapalı" in result.message and off.asked == []


async def test_write_report_avoids_duplicate_heading_and_status_exposes_report_path(
    tmp_path: Path,
) -> None:
    provider = scripted(
        search_call("penguen", 1),
        Completion(content="# Araştırma Raporu: Penguenler\n\nMetin."),
    )
    env = Env(tmp_path, provider)
    started = await env.call("research_start", topic="Penguenler", max_searches=3)
    assert started.success
    job = env.wait()
    job_id = str(job["id"])
    written = env.jobs.report_path(job_id).read_text(encoding="utf-8")
    assert written.startswith("# Araştırma Raporu: Penguenler\n")
    assert "# Araştırma: Penguenler" not in written

    status = await env.call("research_status", job_id=job_id)
    assert status.success
    assert status.data["report_path"] == f"state/arastirma/{job_id}.md"


async def test_outer_timeout_sets_context_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = Env(tmp_path, scripted(Completion(content="rapor")))
    real_wait_for = asyncio.wait_for

    async def fake_wait_for(coro: Any, *args: Any, **kwargs: Any) -> Any:
        # Yalnız araştırma işinin dış wait_for sarmalayıcısını zaman aşımına uğrat.
        if "AgentLoop" in getattr(coro, "__qualname__", ""):
            coro.close()
            raise TimeoutError
        return await real_wait_for(coro, *args, **kwargs)

    monkeypatch.setattr(research_jobs.asyncio, "wait_for", fake_wait_for)
    await env.call("research_start", topic="konu")
    job = env.wait()
    assert job["status"] == "timeout"
    assert env.jobs._context is not None
    assert env.jobs._context.cancelled.is_set()
