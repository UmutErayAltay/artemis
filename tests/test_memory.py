"""Vault hafızası gerçek dosyalarla sınanır: arama, okuma, ekleme, onay ve gizlilik.

Ağ ve model indirme yoktur. Anlamsal arama sahte bir gömücüyle (embedder) yapılır;
numpy gerektiren testler, numpy yoksa atlanır ve bu sayı rapor edilir.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from jarvis.brain.agent_loop import AgentLoop
from jarvis.brain.fake import ScriptedProvider
from jarvis.brain.models import Completion, ToolCall
from jarvis.config.settings import MemoryConfig, Settings
from jarvis.memory.embedding import EmbeddingIndex, EmbeddingUnavailable
from jarvis.memory.vault import is_excluded, iter_notes, turkish_fold
from jarvis.safety.audit import AuditLog
from jarvis.safety.gate import TAINT_NOTICE, SafetyGate, TurnState
from jarvis.tools.base import Risk, ToolContext, ToolResult
from jarvis.tools.registry import discover_tools

EXCLUDE = MemoryConfig().exclude
UNAVAILABLE_NOTICE = "Embedding kullanılamadı; sözcük araması yapıldı."

VAULT_FILES = {
    "Projeler/jarvis.md": (
        "---\ntitle: Jarvis planı\n---\n\nSes komutları ve onay akışı burada planlanır.\n"
    ),
    "Kişisel/tercihler.md": "Sabah kahve: filtre, şekersiz.\n",
    "daily/2026-10-13.md": "Bugün kahve içtim.\n",
    ".obsidian/workspace.md": "kahve gizli ayar\n",
    "850-Companion/Core.md": "kahve çekirdek kimlik\n",
    "notlar.txt": "kahve düz metin\n",
    "İstanbul.md": "İSTANBUL ışık\n",
}


def fold(text: str) -> str:
    """Testin kendi Türkçe katlaması; ürün kodundan bağımsız beklenti üretir."""
    return text.replace("İ", "i").replace("I", "ı").casefold()


def write_note(root: Path, relative: str, text: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def vault_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    for relative, text in VAULT_FILES.items():
        write_note(root, relative, text)
    return root


def make_context(base: Path, memory: MemoryConfig, **settings_overrides: Any) -> ToolContext:
    """Her test kendi geçici kökünde çalışır; masaüstü ve durum dizini izole kalır."""
    base.mkdir(parents=True, exist_ok=True)
    workspace = base / "desktop"
    workspace.mkdir(exist_ok=True)
    settings = Settings(
        workspace=workspace,
        state_dir=base / "state",
        audit_path=base / "audit.jsonl",
        memory=memory,
        **settings_overrides,
    )
    return ToolContext(workspace=workspace, settings=settings, audit=AuditLog(settings.audit_path))


async def invoke(context: ToolContext, tool_name: str, **arguments: Any) -> ToolResult:
    tool = discover_tools().get(tool_name)
    assert tool is not None
    return await tool.execute(tool.args_model.model_validate(arguments), context)


def no_approval(name: str, arguments: dict[str, Any]) -> bool:
    pytest.fail(f"Onay istenmemeliydi: {name}")


class FakeEmbedder:
    """Kavram boyutlu sahte gömücü: 'espresso' sözcüğü 'kahve' ile aynı boyuta düşer.

    Bu sayede sözcük aramasının bulamadığı bir sorgu anlamsal aramada bulunur. Sabit bir
    bias boyutu sıfır vektörünü engeller. Çağrılar kaydedilir: (metinler, is_query).
    """

    CONCEPTS = {
        "kahve": 0,
        "espresso": 0,
        "sabah": 1,
        "ses": 2,
        "onay": 3,
        "istanbul": 4,
        "ışık": 4,
        "zeytinyağı": 5,
    }
    DIMENSIONS = 7

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], bool]] = []

    def __call__(self, texts: Sequence[str], is_query: bool) -> list[list[float]]:
        self.calls.append((list(texts), is_query))
        return [self._vector(text) for text in texts]

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self.DIMENSIONS
        for token in re.findall(r"\w+", fold(text)):
            index = self.CONCEPTS.get(token)
            if index is not None:
                vector[index] += 1.0
        vector[6] = 0.05
        return vector


# 1. Kayıt ve sözleşme meta verisi


async def test_three_memory_tools_are_registered_with_contract_metadata() -> None:
    registry = discover_tools()
    search = registry.get("vault_search")
    read = registry.get("vault_read_note")
    append = registry.get("vault_append")
    assert search is not None and read is not None and append is not None

    assert search.risk is Risk.READ_ONLY
    assert search.external is True and search.content_kind == "file"
    assert (search.label, search.done_label) == ("Notlarda arama", "Notlarda arandı")
    assert read.risk is Risk.READ_ONLY
    assert read.external is True and read.content_kind == "file"
    assert (read.label, read.done_label) == ("Not okuma", "Not okundu")
    assert append.risk is Risk.CONFIRM_REQUIRED
    assert append.external is False
    assert (append.label, append.done_label) == ("Nota ekleme", "Nota eklendi")
    for tool in (search, read, append):
        assert tool.outbound is False


async def test_search_arguments_enforce_documented_bounds() -> None:
    search = discover_tools().get("vault_search")
    assert search is not None
    assert {"query", "limit"} <= set(search.args_model.model_json_schema()["properties"])
    for bad in ({"query": ""}, {"query": "x" * 501}, {"query": "a", "limit": 0},
                {"query": "a", "limit": 21}, {"query": "a", "extra": 1}):
        with pytest.raises(ValidationError):
            search.args_model.model_validate(bad)


# 2. Kapalı hafıza


async def test_memory_tools_are_disabled_without_existing_vault(tmp_path: Path) -> None:
    cases = {
        "none": MemoryConfig(),
        "missing": MemoryConfig(vault_path=tmp_path / "yok"),
    }
    for label, memory in cases.items():
        context = make_context(tmp_path / label, memory)
        results = [
            await invoke(context, "vault_search", query="kahve"),
            await invoke(context, "vault_read_note", path="Projeler/jarvis.md"),
            await invoke(context, "vault_append", path="Projeler/jarvis.md", text="Not"),
        ]
        for result in results:
            assert result.success is False and result.status == "error", label
            assert "Hafıza kapalı" in result.message, label
        assert not (tmp_path / label / "state").exists(), label
    assert not (tmp_path / "yok").exists()


# 3. Sözcük araması


async def test_lexical_search_returns_only_visible_markdown_notes(
    tmp_path: Path, vault: Path
) -> None:
    context = make_context(tmp_path, MemoryConfig(vault_path=vault))
    result = await invoke(context, "vault_search", query="kahve")
    assert result.success and result.status == "ok"
    assert result.data["method"] == "lexical"
    # daily/, .obsidian/, 850-Companion/ ve notlar.txt de "kahve" içerir; hiçbiri çıkmamalı.
    assert [item["path"] for item in result.data["results"]] == ["Kişisel/tercihler.md"]
    assert "notice" not in result.data


@pytest.mark.parametrize(
    ("query", "expected_paths"),
    [
        ("İstanbul", ["İstanbul.md"]),
        ("istanbul", ["İstanbul.md"]),
        ("ışık", ["İstanbul.md"]),
        # Türkçe katlama I→ı yapar: "ISTANBUL" → "ıstanbul", bu "istanbul"dan farklı bir
        # sözcüktür. Not "İSTANBUL" olarak yazılı olduğu için bu sorgu bilinçli olarak
        # eşleşmez; sonuç dürüstçe boş kalır.
        ("ISTANBUL", []),
    ],
)
async def test_turkish_casefold_matches_dotted_i_and_documents_dotless_i(
    tmp_path: Path, vault: Path, query: str, expected_paths: list[str]
) -> None:
    context = make_context(tmp_path, MemoryConfig(vault_path=vault))
    result = await invoke(context, "vault_search", query=query)
    assert result.success
    assert [item["path"] for item in result.data["results"]] == expected_paths


def test_turkish_fold_maps_dotted_and_dotless_i_before_casefold() -> None:
    assert turkish_fold("İSTANBUL") == "istanbul"
    assert turkish_fold("ISTANBUL") == "ıstanbul"
    assert turkish_fold("Sabah") == "sabah"


async def test_results_are_sorted_by_score_and_title_is_extracted(
    tmp_path: Path, vault: Path
) -> None:
    context = make_context(tmp_path, MemoryConfig(vault_path=vault))
    result = await invoke(context, "vault_search", query="ses onay")
    results = result.data["results"]
    scores = [item["score"] for item in results]
    assert results[0]["path"] == "Projeler/jarvis.md"
    assert results[0]["title"] == "Jarvis planı"
    assert scores == sorted(scores, reverse=True)
    assert all(len(item["excerpt"]) <= 300 and "\n" not in item["excerpt"] for item in results)


async def test_limit_defaults_to_config_and_argument_caps_results(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    for index in range(7):
        write_note(vault_root, f"Toplantılar/not{index}.md", f"Toplantı notu {index}, toplantı.\n")
    context = make_context(tmp_path, MemoryConfig(vault_path=vault_root, max_results=5))
    default = await invoke(context, "vault_search", query="toplantı")
    assert len(default.data["results"]) == 5
    limited = await invoke(context, "vault_search", query="toplantı", limit=2)
    assert len(limited.data["results"]) == 2


@pytest.mark.parametrize("query", ["  ", "?!.", "a"])
async def test_empty_punctuation_or_one_letter_query_is_successful_with_no_hits(
    tmp_path: Path, vault: Path, query: str
) -> None:
    context = make_context(tmp_path, MemoryConfig(vault_path=vault))
    result = await invoke(context, "vault_search", query=query)
    assert result.success is True and result.status == "ok"
    assert result.data["results"] == []


# 4. Dış içerik lekesi ve gerçek kapı


@pytest.mark.parametrize(
    ("call_name", "arguments"),
    [
        ("vault_search", {"query": "kahve"}),
        ("vault_read_note", {"path": "Kişisel/tercihler.md"}),
    ],
)
async def test_vault_read_taints_turn_through_real_gate(
    tmp_path: Path, vault: Path, call_name: str, arguments: dict[str, Any]
) -> None:
    context = make_context(tmp_path, MemoryConfig(vault_path=vault))
    gate = SafetyGate(discover_tools())
    state = TurnState()
    result = await gate.dispatch(
        ToolCall(id="r1", name=call_name, arguments=arguments), context, state, no_approval
    )
    assert result.success and result.external is True
    assert result.content_kind == "file"
    assert state.tainted is True

    # Lekeli turda yazma, onay penceresini dış içerik uyarısıyla açar ve reddedilirse yazmaz.
    note = vault / "Projeler/jarvis.md"
    before = note.read_bytes()
    asked: list[tuple[str, dict[str, Any]]] = []

    def decline(name: str, args: dict[str, Any]) -> bool:
        asked.append((name, args))
        return False

    appended = await gate.dispatch(
        ToolCall(
            id="w1",
            name="vault_append",
            arguments={"path": "Projeler/jarvis.md", "text": "Yeni satır."},
        ),
        context,
        state,
        decline,
    )
    assert appended.status == "denied" and appended.success is False
    assert asked == [("vault_append", {"path": "Projeler/jarvis.md", "text": "Yeni satır."})]
    assert gate.pending_notice.startswith(TAINT_NOTICE)
    assert note.read_bytes() == before


async def test_invalid_search_arguments_are_rejected_by_gate_without_taint(
    tmp_path: Path, vault: Path
) -> None:
    context = make_context(tmp_path, MemoryConfig(vault_path=vault))
    gate = SafetyGate(discover_tools())
    state = TurnState()
    result = await gate.dispatch(
        ToolCall(id="s1", name="vault_search", arguments={"query": ""}),
        context,
        state,
        no_approval,
    )
    assert result.success is False and result.status == "error"
    assert state.tainted is False


# 5. Not okuma


async def test_read_returns_note_body_as_external_file(tmp_path: Path, vault: Path) -> None:
    context = make_context(tmp_path, MemoryConfig(vault_path=vault))
    result = await invoke(context, "vault_read_note", path="Projeler/jarvis.md")
    assert result.success and result.status == "ok"
    assert result.external is True and result.content_kind == "file"
    assert "Ses komutları ve onay akışı" in result.data["content"]
    assert result.data["truncated"] is False


@pytest.mark.parametrize(
    "path",
    [
        "daily/2026-10-13.md",
        ".obsidian/workspace.md",
        "850-Companion/Core.md",
        "notlar.txt",
        "Projeler/eksik.md",
    ],
)
async def test_read_rejects_excluded_hidden_non_markdown_and_missing(
    tmp_path: Path, vault: Path, path: str
) -> None:
    context = make_context(tmp_path, MemoryConfig(vault_path=vault))
    result = await invoke(context, "vault_read_note", path=path)
    assert result.success is False and result.status == "error"
    assert str(vault) not in result.message


@pytest.mark.parametrize("path", ["../x.md", "../../etc/passwd.md"])
async def test_read_rejects_traversal(tmp_path: Path, vault: Path, path: str) -> None:
    context = make_context(tmp_path, MemoryConfig(vault_path=vault))
    result = await invoke(context, "vault_read_note", path=path)
    assert result.success is False and result.status == "error"
    assert str(tmp_path) not in result.message


async def test_read_rejects_absolute_path_inside_vault(tmp_path: Path, vault: Path) -> None:
    context = make_context(tmp_path, MemoryConfig(vault_path=vault))
    result = await invoke(
        context, "vault_read_note", path=str(vault / "Projeler" / "jarvis.md")
    )
    assert result.success is False and result.status == "error"
    assert str(vault) not in result.message


async def test_read_rejects_symlink_pointing_outside_vault(tmp_path: Path, vault: Path) -> None:
    outside = tmp_path / "gizli.md"
    outside.write_text("SIZINTI", encoding="utf-8")
    link = vault / "Dışarı.md"
    try:
        link.symlink_to(outside)
    except OSError as error:
        pytest.skip(f"Bu platformda symlink yetkisi yok: {error}")
    context = make_context(tmp_path, MemoryConfig(vault_path=vault))
    result = await invoke(context, "vault_read_note", path="Dışarı.md")
    assert result.success is False
    assert "SIZINTI" not in result.model_dump_json()


async def test_read_is_capped_at_max_note_chars(tmp_path: Path, vault: Path) -> None:
    write_note(vault, "Uzun.md", "satır\n" * 5000)
    context = make_context(tmp_path, MemoryConfig(vault_path=vault, max_note_chars=1000))
    result = await invoke(context, "vault_read_note", path="Uzun.md")
    assert result.success and result.data["truncated"] is True
    assert len(result.data["content"]) == 1000


# 6. Ekleme: mevcut notun baytları korunur


@pytest.mark.parametrize("original", ["Son satır sonsuz değil", "Satır var\n"])
async def test_append_keeps_original_bytes_and_separates_with_one_blank_line(
    tmp_path: Path, vault: Path, original: str
) -> None:
    note = write_note(vault, "Projeler/notlar.md", original)
    context = make_context(tmp_path, MemoryConfig(vault_path=vault))
    text = "Yeni karar: kahve sabah."
    result = await invoke(context, "vault_append", path="Projeler/notlar.md", text=text)
    assert result.success and result.status == "ok"
    separator = b"" if original.endswith("\n") else b"\n"
    expected = original.encode("utf-8") + separator + b"\n" + text.encode("utf-8") + b"\n"
    assert note.read_bytes() == expected
    assert result.data == {
        "path": "Projeler/notlar.md",
        "created": False,
        "appended_chars": len(text),
    }


async def test_append_never_truncates_a_large_note(tmp_path: Path, vault: Path) -> None:
    original = "".join(f"satır {index:05d} kahve\n" for index in range(6000)).encode("utf-8")
    assert len(original) > 100_000
    note = vault / "Projeler/buyuk.md"
    note.write_bytes(original)
    context = make_context(tmp_path, MemoryConfig(vault_path=vault))
    result = await invoke(context, "vault_append", path="Projeler/buyuk.md", text="Sonuna eklendi.")
    assert result.success
    updated = note.read_bytes()
    assert updated[: len(original)] == original
    assert updated[len(original) :] == b"\n" + b"Sonuna eklendi." + b"\n"


# 7. Ekleme: yeni not, eksik klasör, dışlanan hedefler, sembolik bağlantı


async def test_append_creates_missing_note_with_exclusive_semantics(
    tmp_path: Path, vault: Path
) -> None:
    context = make_context(tmp_path, MemoryConfig(vault_path=vault))
    result = await invoke(context, "vault_append", path="Projeler/yeni.md", text="İlk satır.")
    assert result.success and result.data["created"] is True
    assert (vault / "Projeler/yeni.md").read_bytes() == "İlk satır.\n".encode()


async def test_append_does_not_create_missing_parent_directories(
    tmp_path: Path, vault: Path
) -> None:
    context = make_context(tmp_path, MemoryConfig(vault_path=vault))
    result = await invoke(context, "vault_append", path="Yeni/Klasör/not.md", text="x")
    assert result.success is False and result.status == "error"
    assert not (vault / "Yeni").exists()


@pytest.mark.parametrize(
    "path",
    [
        "daily/2026-10-13.md",
        "daily/yeni.md",
        ".obsidian/workspace.md",
        ".obsidian/yeni.md",
        "notlar.txt",
        "850-Companion/Core.md",
    ],
)
async def test_append_rejects_excluded_hidden_and_non_markdown_targets(
    tmp_path: Path, vault: Path, path: str
) -> None:
    before = vault_bytes(vault)
    context = make_context(tmp_path, MemoryConfig(vault_path=vault))
    result = await invoke(context, "vault_append", path=path, text="Ekleme.")
    assert result.success is False and result.status == "error"
    assert vault_bytes(vault) == before


async def test_append_rejects_symlinked_note_even_when_target_is_inside_vault(
    tmp_path: Path, vault: Path
) -> None:
    target = vault / "Projeler/jarvis.md"
    link = vault / "Projeler/baglanti.md"
    try:
        link.symlink_to(target)
    except OSError as error:
        pytest.skip(f"Bu platformda symlink yetkisi yok: {error}")
    before = target.read_bytes()
    context = make_context(tmp_path, MemoryConfig(vault_path=vault))
    result = await invoke(context, "vault_append", path="Projeler/baglanti.md", text="Ekleme.")
    assert result.success is False
    assert target.read_bytes() == before
    assert link.is_symlink()


# 8. Ekleme onayı


@pytest.mark.parametrize("approved", [False, True])
async def test_append_is_confirm_required_and_declined_write_leaves_note_untouched(
    tmp_path: Path, vault: Path, approved: bool
) -> None:
    note = vault / "Kişisel/tercihler.md"
    before = note.read_bytes()
    context = make_context(tmp_path, MemoryConfig(vault_path=vault))
    gate = SafetyGate(discover_tools())
    asked: list[tuple[str, dict[str, Any]]] = []

    def answer(name: str, args: dict[str, Any]) -> bool:
        asked.append((name, args))
        return approved

    result = await gate.dispatch(
        ToolCall(
            id="w1",
            name="vault_append",
            arguments={"path": "Kişisel/tercihler.md", "text": "Yarın 10:00 diş randevusu."},
        ),
        context,
        TurnState(),
        answer,
    )
    assert asked == [
        ("vault_append", {"path": "Kişisel/tercihler.md", "text": "Yarın 10:00 diş randevusu."})
    ]
    assert gate.pending_notice.startswith("Bu işlem onay gerektirir.")
    if approved:
        assert result.success and result.status == "ok"
        assert note.read_bytes() == before + "\nYarın 10:00 diş randevusu.\n".encode()
    else:
        assert result.status == "denied" and result.success is False
        assert note.read_bytes() == before


def test_append_plugin_source_never_rewrites_notes() -> None:
    """Yazma yalnız ekleme ("a") ve yeni dosya ("x") kipinde olmalı; "w" ve write_text yasak."""
    source = Path(__file__).resolve().parents[1] / "jarvis" / "tools" / "plugins" / "memory.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    offenders: list[str] = []
    modes: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in {"write_text", "write_bytes"}:
            offenders.append(f"satır {node.lineno}: {func.attr}")
        is_open = (isinstance(func, ast.Name) and func.id == "open") or (
            isinstance(func, ast.Attribute) and func.attr == "open"
        )
        if not is_open:
            continue
        constants = [
            arg.value
            for arg in [*node.args, *(keyword.value for keyword in node.keywords)]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
        ]
        for value in constants:
            if re.fullmatch(r"[rwaxbt+]+", value):
                modes.add(value)
                if "w" in value:
                    offenders.append(f"satır {node.lineno}: open(..., {value!r})")
    assert offenders == []
    assert {"a", "x"} <= modes


# 9. Anlamsal arama (numpy gerektirir)


def _vault_passages(calls: Sequence[tuple[list[str], bool]], since: int) -> list[str]:
    return [text for texts, is_query in calls[since:] if not is_query for text in texts]


async def test_embedding_search_ranks_semantic_match_and_caches_outside_vault(
    tmp_path: Path, vault: Path
) -> None:
    pytest.importorskip("numpy")
    embedder = FakeEmbedder()
    context = make_context(
        tmp_path, MemoryConfig(vault_path=vault, embedding_model="fake-model")
    )
    context.services["embedder"] = embedder
    before = vault_bytes(vault)

    first = await invoke(context, "vault_search", query="espresso")
    assert first.success and first.data["method"] == "embedding"
    assert first.data["results"][0]["path"] == "Kişisel/tercihler.md"
    cache_files = list((tmp_path / "state" / "embedding").rglob("*.npz"))
    assert cache_files, "Vektör önbelleği state_dir/embedding altında oluşmalı"
    assert vault_bytes(vault) == before, "Vault içinde yeni dosya oluşmamalı"

    # İkinci aramada hiçbir not yeniden gömülmemeli; yalnız sorgu gömülür.
    since = len(embedder.calls)
    second = await invoke(context, "vault_search", query="espresso")
    assert second.success and second.data["method"] == "embedding"
    assert _vault_passages(embedder.calls, since) == []

    # Tek bir notun değişmesi yalnız o notu yeniden gömer.
    note = vault / "Kişisel/tercihler.md"
    note.write_bytes((note.read_text(encoding="utf-8") + "Yeni: zeytinyağı tercihi.\n").encode())
    since = len(embedder.calls)
    third = await invoke(context, "vault_search", query="zeytinyağı")
    assert third.success and third.data["results"][0]["path"] == "Kişisel/tercihler.md"
    passages = _vault_passages(embedder.calls, since)
    assert any("zeytinyağı" in fold(text) for text in passages)
    assert not any(
        word in fold(text) for text in passages for word in ("onay", "istanbul", "planı")
    ), "Değişmeyen notlar yeniden gömülmemeli"


async def test_embedding_index_rejects_cache_directory_inside_vault(
    tmp_path: Path, vault: Path
) -> None:
    with pytest.raises(ValueError):
        EmbeddingIndex(
            vault, "fake-model", vault / ".cache", EXCLUDE, embedder=FakeEmbedder()
        )


async def test_embedding_unavailable_falls_back_to_lexical_with_notice(
    tmp_path: Path, vault: Path
) -> None:
    def unavailable(texts: Sequence[str], is_query: bool) -> list[list[float]]:
        raise EmbeddingUnavailable("Model yok")

    context = make_context(
        tmp_path, MemoryConfig(vault_path=vault, embedding_model="fake-model")
    )
    context.services["embedder"] = unavailable
    result = await invoke(context, "vault_search", query="kahve")
    assert result.success is True and result.status == "ok"
    assert result.data["method"] == "lexical"
    assert result.data["notice"] == UNAVAILABLE_NOTICE
    assert [item["path"] for item in result.data["results"]] == ["Kişisel/tercihler.md"]


async def test_lexical_search_cannot_find_synonym_that_embedding_finds(
    tmp_path: Path, vault: Path
) -> None:
    # Anlamsal aramanın değerini kanıtlar: sözcük araması "espresso" için boş döner.
    context = make_context(tmp_path, MemoryConfig(vault_path=vault))
    result = await invoke(context, "vault_search", query="espresso")
    assert result.success and result.data["results"] == []


# 10. Gizlilik: dosya içeriği bulut ayarına bağlıdır


@pytest.mark.parametrize(("allowed", "expected_private"), [(False, True), (True, False)])
async def test_vault_search_tool_message_privacy_follows_cloud_file_setting(
    tmp_path: Path, vault: Path, allowed: bool, expected_private: bool
) -> None:
    provider = ScriptedProvider(
        [
            Completion(
                tool_calls=[ToolCall(id="s1", name="vault_search", arguments={"query": "kahve"})]
            ),
            Completion(content="Tercihin filtre kahve."),
        ]
    )
    context = make_context(
        tmp_path,
        MemoryConfig(vault_path=vault),
        allow_cloud_file_content=allowed,
    )
    gate = SafetyGate(discover_tools())
    loop = AgentLoop(provider, gate, context, no_approval)
    result = await loop.run("kahve tercihim ne?")
    assert result.stop_reason == "completed"
    tool_messages = [message for message in provider.requests[1] if message.role == "tool"]
    assert tool_messages, "Araç sonucu sağlayıcıya gitmeli"
    assert all(message.private is expected_private for message in tool_messages)


# 11. Dışlama kuralları ve not listesi (sözleşme birimleri)


@pytest.mark.parametrize(
    ("relative", "excluded"),
    [
        ("Projeler/jarvis.md", False),
        ("Projeler\\jarvis.md", False),
        ("daily/2026-10-13.md", True),
        ("x/daily/not.md", True),
        ("850-Companion/Core.md", True),
        ("a/850-Companion/b.md", True),
        ("knowledge/index.md", True),
        # Tam yol kalıpları yalnız tam eşleşir; başka klasördeki aynı adı dışlamaz.
        ("x/knowledge/index.md", False),
        (".obsidian/workspace.md", True),
        (".git/config.md", True),
        ("Projeler/.gizli.md", True),
        ("notlar.txt", True),
        ("Projeler/resim.png", True),
    ],
)
def test_is_excluded_rules(relative: str, excluded: bool) -> None:
    assert is_excluded(relative, EXCLUDE) is excluded


async def test_iter_notes_skips_excluded_hidden_and_escaping_links(
    tmp_path: Path, vault: Path
) -> None:
    outside_dir = tmp_path / "dis"
    outside_dir.mkdir()
    (outside_dir / "x.md").write_text("DIS", encoding="utf-8")
    try:
        (vault / "Bağlantı").symlink_to(outside_dir, target_is_directory=True)
        (vault / "Dışarı.md").symlink_to(outside_dir / "x.md")
    except OSError as error:
        pytest.skip(f"Bu platformda symlink yetkisi yok: {error}")
    root = vault.resolve()
    found = [
        Path(path).resolve().relative_to(root).as_posix()
        for path in iter_notes(vault, EXCLUDE)
    ]
    assert found == sorted(found)
    assert set(found) == {"Projeler/jarvis.md", "Kişisel/tercihler.md", "İstanbul.md"}
