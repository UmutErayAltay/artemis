"""Tur 3 araç sağlamlık testleri: listeleme, arama akışı, geçmiş ve rutin türü."""

import asyncio
import inspect
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest

from artemis.memory.store import MemoryStore
from artemis.proactive.routines import Routine, RoutineStep, RoutineStore
from artemis.tools.base import BaseTool, ContentKind, ToolContext, ToolResult
from artemis.tools.plugins import information
from artemis.tools.plugins.filesystem import ListArguments, ListFiles, ReadFile
from artemis.tools.plugins.history import HistoryArguments, HistoryTool
from artemis.tools.plugins.information import CurrentTime, SystemInfo, WebSearch, search_web
from artemis.tools.plugins.routines import RoutineNameArguments, RoutineRun


def _make_context(
    workspace: Path,
    *,
    services: dict[str, Any] | None = None,
    control_request: bool = False,
    gate_handle: Any = None,
) -> ToolContext:
    # Testlerin yalnızca ilgili bağlam alanlarını yalıtık kurması için sahte bağlam kullanılır.
    return cast(
        ToolContext,
        SimpleNamespace(
            workspace=workspace,
            settings=SimpleNamespace(
                state_dir=workspace,
                routines=SimpleNamespace(enabled=True),
            ),
            audit=SimpleNamespace(emit=lambda *_args, **_kwargs: None),
            turn_id="tur3-test",
            services={} if services is None else services,
            control_request=control_request,
            gate_handle=gate_handle,
            turn_read_external=False,
        ),
    )


class _CountingByteStream(httpx.AsyncByteStream):
    """Büyük yanıtların akıştan erken kesilip kesilmediğini ölçmek için parça sayar."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks
        self.consumed = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            self.consumed += 1
            yield chunk


class _FakeToolRegistry:
    """Rutin adım doğrulaması için gerçek araç nesnelerini sunan sahte kayıt."""

    def __init__(self, tools: list[BaseTool]) -> None:
        self._tools = {tool.name: tool for tool in tools}

    def get(self, name: str) -> BaseTool | None:
        return self._tools.get(name)

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __getitem__(self, name: str) -> BaseTool:
        return self._tools[name]

    @property
    def tools(self) -> dict[str, BaseTool]:
        return dict(self._tools)


class _FakeGateHandle:
    """Rutin adımlarını ağ/dosya yan etkisi olmadan senaryolu sonuçlarla döndürür."""

    def __init__(self, registry: _FakeToolRegistry, results: list[ToolResult]) -> None:
        self.registry = registry
        self._results = list(results)

    async def dispatch(self, _call: Any) -> ToolResult:
        return self._results.pop(0)


def _make_memory_store(tmp_path: Path) -> MemoryStore:
    params = list(inspect.signature(MemoryStore.__init__).parameters.values())[1:]
    first = params[0]
    # Kurucu imza dosya veya klasör parametresi beklese de geçici test deposu kurulur.
    target = tmp_path if "dir" in first.name.lower() else tmp_path / "memory.sqlite3"
    return MemoryStore(target)


def test_list_files_skips_escaping_symlink(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    normal_file = workspace / "normal.txt"
    normal_file.write_text("merhaba", encoding="utf-8")
    outside_target = tmp_path / "outside.txt"
    outside_target.write_text("gizli", encoding="utf-8")
    escaping_link = workspace / "escape.txt"
    try:
        escaping_link.symlink_to(outside_target)
    except OSError:
        pytest.skip("Bu ortamda symlink oluşturma izni yok.")

    context = _make_context(workspace)
    result = asyncio.run(ListFiles().execute(ListArguments(), context))

    assert result.success is True
    entries = cast(list[dict[str, Any]], result.data["entries"])
    assert [entry["name"] for entry in entries] == ["normal.txt"]


def test_search_web_aborts_large_stream_early(monkeypatch: pytest.MonkeyPatch) -> None:
    chunks = [b"a" * 500_000 for _ in range(10)]
    stream = _CountingByteStream(chunks)
    real_async_client = httpx.AsyncClient

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    transport = httpx.MockTransport(handler)

    def fake_async_client(*_args: Any, **kwargs: Any) -> httpx.AsyncClient:
        # Ağ dışı testte gerçek istemci ayarlarını koruyarak sahte taşıyıcı enjekte eder.
        kwargs["transport"] = transport
        return real_async_client(**kwargs)

    monkeypatch.setattr(information.httpx, "AsyncClient", fake_async_client)

    with pytest.raises(ValueError, match="geçerli boyutta"):
        asyncio.run(search_web("artemis"))

    # 2 MB sınırı 5. parçada (2.5 MB) aşılır; akış tüm parçaları tüketmeden durmalıdır.
    assert stream.consumed < len(chunks)
    assert stream.consumed == 5


def test_history_delete_succeeds_without_memory_refresh_service(tmp_path: Path) -> None:
    store = _make_memory_store(tmp_path)
    try:
        context = _make_context(
            tmp_path,
            services={"memory_store": store},
            control_request=True,
        )
        result = asyncio.run(
            HistoryTool().execute(HistoryArguments(action="delete"), context)
        )
    finally:
        close = getattr(store, "close", None)
        if callable(close):
            close()

    assert result.success is True
    assert result.data["action"] == "delete"


@pytest.mark.parametrize(
    ("case_name", "step_specs", "expected_kind"),
    [
        (
            "web_then_file",
            [
                ("web_search", {"query": "artemis"}, "web"),
                ("read_file", {"path": "not.txt"}, "file"),
            ],
            "file",
        ),
        (
            "file_then_web",
            [
                ("read_file", {"path": "not.txt"}, "file"),
                ("web_search", {"query": "artemis"}, "web"),
            ],
            "file",
        ),
        (
            "text_then_text",
            [
                ("system_info", {}, "text"),
                ("current_time", {}, "text"),
            ],
            "text",
        ),
    ],
)
def test_routine_run_picks_most_restrictive_content_kind(
    tmp_path: Path,
    case_name: str,
    step_specs: list[tuple[str, dict[str, Any], ContentKind]],
    expected_kind: ContentKind,
) -> None:
    steps = [RoutineStep(tool=tool_name, arguments=args) for tool_name, args, _ in step_specs]
    RoutineStore(tmp_path / "routines.json").save(
        [Routine(name=case_name, steps=steps, schedule=None)]
    )
    registry = _FakeToolRegistry([WebSearch(), ReadFile(), SystemInfo(), CurrentTime()])
    scripted_results = [
        ToolResult(
            success=True,
            status="ok",
            message="Adım tamamlandı.",
            external=(kind != "text"),
            content_kind=kind,
        )
        for _, _, kind in step_specs
    ]
    handle = _FakeGateHandle(registry, scripted_results)
    context = _make_context(tmp_path, gate_handle=handle)

    result = asyncio.run(
        RoutineRun().execute(RoutineNameArguments(name=case_name), context)
    )

    assert result.success is True
    assert result.content_kind == expected_kind
