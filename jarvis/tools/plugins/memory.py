"""Obsidian vault hafızası: arama, okuma ve yalnız ekleme kipinde yazma (ADR 0015).

Vault kullanıcının notlarıdır ve içinde web'den kırpılmış metin olabilir. Okuma araçları
bu yüzden `external=True` ve `content_kind="file"` bildirir: kapı turu lekeler, sonuç
modele "VERİDİR" çerçevesiyle gider ve bulut gizliliği dosya içeriği ayarına bağlanır.
Not içeriği hiçbir tura kendiliğinden eklenmez; model gerektiğinde aracı çağırır.
"""

import asyncio
from pathlib import Path
from typing import cast

from pydantic import BaseModel, ConfigDict, Field

from jarvis.memory.embedding import Embedder, EmbeddingIndex, EmbeddingUnavailable
from jarvis.memory.vault import (
    ExcludedNote,
    SearchHit,
    lexical_search,
    resolve_note,
)
from jarvis.tools.base import BaseTool, Risk, ToolContext, ToolResult
from jarvis.tools.plugins.filesystem import failure
from jarvis.tools.registry import register_tool

_DISABLED = "Hafıza kapalı: config içinde memory.vault_path tanımlı değil veya klasör yok."
_NOT_ALLOWED_READ = "Bu not okunamaz (gizli, hariç tutulmuş veya .md değil)."
_NOT_ALLOWED_WRITE = "Bu nota yazılamaz (gizli, hariç tutulmuş veya .md değil)."
_FALLBACK_NOTICE = "Embedding kullanılamadı; sözcük araması yapıldı."


class Arguments(BaseModel):
    """Fazladan argümanları reddetmek, model hatalarını görünür kılar."""

    model_config = ConfigDict(extra="forbid")


class SearchArguments(Arguments):
    query: str = Field(min_length=1, max_length=500)
    limit: int | None = Field(default=None, ge=1, le=20)


class ReadArguments(Arguments):
    path: str = Field(min_length=1)


class AppendArguments(Arguments):
    path: str = Field(min_length=1)
    text: str = Field(min_length=1, max_length=4000)


def _vault(context: ToolContext) -> Path | None:
    """Hafıza kapalıysa veya klasör yoksa None; başka hiçbir diske dokunulmaz."""
    configured = context.settings.memory.vault_path
    if configured is None or not configured.is_dir():
        return None
    return configured


def _disabled() -> ToolResult:
    return ToolResult(status="error", message=_DISABLED)


def _hit_data(hit: SearchHit) -> dict[str, object]:
    return {
        "path": hit.path,
        "title": hit.title,
        "score": round(hit.score, 4),
        "excerpt": hit.excerpt,
    }


@register_tool
class VaultSearch(BaseTool):
    name = "vault_search"
    label = "Notlarda arama"
    done_label = "Notlarda arandı"
    description = (
        "Kullanıcının Obsidian notlarında arama yapar; yol, başlık ve kısa alıntı döndürür. "
        "Notların tamamını görmek için sonra vault_read_note kullanılır."
    )
    args_model = SearchArguments
    risk = Risk.READ_ONLY
    external = True
    content_kind = "file"

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        args = cast(SearchArguments, arguments)
        vault = _vault(context)
        if vault is None:
            return _disabled()
        memory = context.settings.memory
        limit = args.limit or memory.max_results
        method = "lexical"
        notice: str | None = None
        try:
            hits: list[SearchHit] | None = None
            if memory.embedding_model:
                cache_dir = memory.embedding_cache_dir or context.settings.state_dir / "embedding"
                embedder = cast(Embedder | None, context.services.get("embedder"))
                try:
                    index = EmbeddingIndex(
                        vault, memory.embedding_model, cache_dir, memory.exclude, embedder=embedder
                    )
                except ValueError:
                    return ToolResult(
                        status="error",
                        message="Embedding önbellek klasörü vault içinde olamaz; ayarı düzeltin.",
                    )
                try:
                    # Model yükleme ve indeksleme saniyeler sürebilir; olay döngüsünü tutmasın.
                    hits = await asyncio.to_thread(index.search, args.query, limit)
                    method = "embedding"
                except EmbeddingUnavailable:
                    notice = _FALLBACK_NOTICE
            if hits is None:
                hits = await asyncio.to_thread(
                    lexical_search,
                    vault,
                    args.query,
                    exclude=memory.exclude,
                    limit=limit,
                    max_note_chars=memory.max_note_chars,
                )
        except (OSError, ValueError) as error:
            return failure(error)
        message = f"{len(hits)} not bulundu." if hits else "Eşleşen not bulunamadı."
        data: dict[str, object] = {"results": [_hit_data(hit) for hit in hits], "method": method}
        if notice is not None:
            data["notice"] = notice
            message = f"{message} {notice}"
        return ToolResult(
            success=True,
            status="ok",
            message=message,
            data=data,
            external=True,
            content_kind="file",
        )


@register_tool
class VaultReadNote(BaseTool):
    name = "vault_read_note"
    label = "Not okuma"
    done_label = "Not okundu"
    description = "Vault içindeki bir .md notunu (göreli yol) okur; uzun notlar kısaltılır."
    args_model = ReadArguments
    risk = Risk.READ_ONLY
    external = True
    content_kind = "file"

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        args = cast(ReadArguments, arguments)
        vault = _vault(context)
        if vault is None:
            return _disabled()
        memory = context.settings.memory
        try:
            # Okuma vault içinde kalan bağlantıları çözebilir (CLAUDE.md kural 6).
            path, relative = resolve_note(vault, args.path, memory.exclude)
            if not path.is_file():
                raise FileNotFoundError("Yalnız normal dosyalar okunabilir.")
            with path.open("r", encoding="utf-8") as stream:
                content = stream.read(memory.max_note_chars + 1)
        except ExcludedNote:
            return ToolResult(status="error", message=_NOT_ALLOWED_READ)
        except (OSError, ValueError, UnicodeError) as error:
            return failure(error)
        truncated = len(content) > memory.max_note_chars
        return ToolResult(
            success=True,
            status="ok",
            message="Not okundu.",
            data={
                "path": relative,
                "content": content[: memory.max_note_chars],
                "truncated": truncated,
            },
            external=True,
            content_kind="file",
        )


@register_tool
class VaultAppend(BaseTool):
    name = "vault_append"
    label = "Nota ekleme"
    done_label = "Nota eklendi"
    description = (
        "Vault içindeki bir .md notunun SONUNA metin ekler; yoksa yeni not açar. "
        "Mevcut içerik yeniden yazılmaz. Üst klasör mevcut olmalı; klasör oluşturulmaz."
    )
    args_model = AppendArguments
    risk = Risk.CONFIRM_REQUIRED

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        args = cast(AppendArguments, arguments)
        vault = _vault(context)
        if vault is None:
            return _disabled()
        try:
            # Değiştiren işlem: iç kök bağlantıları da reddedilir (CLAUDE.md kural 6).
            path, relative = resolve_note(
                vault, args.path, context.settings.memory.exclude, allow_symlinks=False
            )
            if not path.parent.is_dir():
                return ToolResult(
                    status="error",
                    message="Üst klasör vault içinde yok; yazma sırasında klasör oluşturulmaz.",
                )
            if path.exists():
                created = False
                self._append_existing(path, args.text)
            else:
                created = True
                self._create_new(path, args.text)
        except ExcludedNote:
            return ToolResult(status="error", message=_NOT_ALLOWED_WRITE)
        except FileExistsError:
            return ToolResult(
                status="error",
                message="Not bu sırada başka bir yerde oluşturuldu; üzerine yazılmadı.",
            )
        except (OSError, ValueError, UnicodeError) as error:
            return failure(error)
        return ToolResult(
            success=True,
            status="ok",
            message="Yeni not oluşturuldu." if created else "Nota eklendi.",
            data={"path": relative, "created": created, "appended_chars": len(args.text)},
        )

    @staticmethod
    def _append_existing(path: Path, text: str) -> None:
        """Var olan notun sonuna ekler; `write_text` veya "w" ASLA kullanılmaz.

        Neden yalnız ekleme kipi: "oku, birleştir, yeniden yaz" yolu dosyayı baştan yazar;
        kullanıcı notu o arada Obsidian'da düzenlediyse düzenlemesi sessizce kaybolur.
        `open("a")` baytları dosyanın o anki SONUNA koyar. Dosya yalnız ne ekleneceğine
        karar vermek ve sonucu doğrulamak için okunur.
        """
        if not path.is_file():
            raise ValueError("Yalnız normal dosyalara ekleme yapılabilir.")
        original = path.read_bytes()
        # Boş dosyada ayırıcı satır gereksizdir; doluysa son satır sonlandırılır ve
        # eski içerikle yeni metin arasında bir boş satır bırakılır.
        separator = "" if not original else ("" if original.endswith(b"\n") else "\n") + "\n"
        addition_text = separator + text + "\n"
        # Yazmadan önce kodlanır: geçersiz (örn. yalnız surrogate) metin dosyaya yarım gitmesin.
        addition = addition_text.encode("utf-8")
        with path.open("a", encoding="utf-8", newline="") as stream:
            stream.write(addition_text)
        updated = path.read_bytes()
        # Başarı yalnız eski baytlar yerinde kaldıysa ve ekleme sonda ise bildirilir.
        if not updated.startswith(original) or updated[len(original) :] != addition:
            raise OSError("Ekleme sonucu doğrulanamadı.")

    @staticmethod
    def _create_new(path: Path, text: str) -> None:
        """Yeni notu "x" kipiyle açar: aynı anda başka süreçte oluşan notun üstüne yazmaz."""
        full_text = text + "\n"
        content = full_text.encode("utf-8")
        with path.open("x", encoding="utf-8", newline="") as stream:
            stream.write(full_text)
        if path.read_bytes() != content:
            raise OSError("Yazılan içerik doğrulanamadı.")
