"""Tur 2 düzeltmeleri: boş araç argümanları ve boş kasa durumunda önbelleğin silinmesi."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from artemis.brain.errors import ProviderError
from artemis.brain.models import ToolCall
from artemis.brain.providers import _arguments, _chat_calls, _ChatStreamAccumulator
from artemis.memory.embedding import EmbeddingIndex

# --- Madde 5: providers.py boş/eksik araç argümanları ---


def test_arguments_treats_empty_or_whitespace_as_empty_dict() -> None:
    assert _arguments("") == {}
    assert _arguments("   ") == {}
    assert _arguments("\n\t ") == {}
    assert _arguments('{"path": "not.md"}') == {"path": "not.md"}


@pytest.mark.parametrize("bad", ["{broken", "[1, 2]", "null", "123", None])
def test_arguments_keeps_rejecting_malformed_or_non_object_values(bad: object) -> None:
    with pytest.raises(ProviderError):
        _arguments(bad)


def test_chat_calls_accepts_missing_or_empty_arguments() -> None:
    calls = _chat_calls(
        [
            {"id": "c1", "function": {"name": "ping"}},
            {"id": "c2", "function": {"name": "pong", "arguments": ""}},
            {"id": "c3", "function": {"name": "trim", "arguments": "  "}},
        ]
    )
    assert calls == [
        ToolCall(id="c1", name="ping", arguments={}),
        ToolCall(id="c2", name="pong", arguments={}),
        ToolCall(id="c3", name="trim", arguments={}),
    ]

    with pytest.raises(ProviderError):
        _chat_calls([{"id": "c4", "function": {"name": "bad", "arguments": "{broken"}}])


def test_stream_accumulator_with_no_argument_deltas_yields_empty_dict() -> None:
    class _Sink:
        def text(self, _delta: str) -> None: ...
        def tool_call(self) -> None: ...

    acc = _ChatStreamAccumulator(_Sink())  # type: ignore[arg-type]
    delta_tool = {"index": 0, "id": "c1", "function": {"name": "ping", "arguments": ""}}
    acc("data: " + json.dumps({"choices": [{"delta": {"tool_calls": [delta_tool]}}]}))
    acc("data: " + json.dumps({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}))
    acc("data: [DONE]")
    calls = _chat_calls(acc.response()["choices"][0]["message"]["tool_calls"])
    assert calls == [ToolCall(id="c1", name="ping", arguments={})]


# --- Madde 6: embedding.py kasa boşaldığında eski önbelleği siler ---


def test_embedding_sync_deletes_cache_when_vault_becomes_empty(tmp_path: Path) -> None:
    pytest.importorskip("numpy")
    vault = tmp_path / "vault"
    vault.mkdir()
    cache_dir = tmp_path / "cache"
    note = vault / "gizli.md"
    note.write_text("Çok gizli kahve notu.\n", encoding="utf-8")

    def fake_embedder(texts: Sequence[str], is_query: bool) -> list[list[float]]:
        return [[1.0, 0.5] for _ in texts]

    index = EmbeddingIndex(vault, "fake-model", cache_dir, (), embedder=fake_embedder)
    cache_file = cache_dir / "fake-model.npz"

    hits = index.search("kahve", 5)
    assert len(hits) == 1
    assert cache_file.is_file()

    # Tüm notlar silindikten sonra gizlilik için diskteki önbellek dosyası da silinmelidir.
    note.unlink()
    assert index.search("kahve", 5) == []
    assert not cache_file.exists()

    # Önbellek yokken boş kasada tekrar arama hata vermemelidir.
    assert index.search("kahve", 5) == []
    assert not cache_file.exists()
