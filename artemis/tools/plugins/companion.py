"""Açık/yarım iş sorusu için Artemis'in kendi dosyaları + seçilmiş vault dosyaları (ADR 0054).

Kendi dosyaları Artemis'in belleğidir (dış içerik değil). Kullanıcının açıkça seçtiği
vault dosyaları ise dış içeriktir: biri okunduysa sonuç `external=True` olur ve kapı
turu `vault_read_note` ile aynı biçimde lekeler.
"""

from pydantic import BaseModel, ConfigDict

from artemis.memory.companion import read_companion
from artemis.safety.paths import safe_join
from artemis.tools.base import BaseTool, Risk, ToolContext, ToolResult
from artemis.tools.registry import register_tool

_OWN_CHARS = 1000
_VAULT_CHARS = 1500


class OpenWorkArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


@register_tool
class MemoryOpenWork(BaseTool):
    name = "memory_open_work"
    label = "Açık işlere bakma"
    done_label = "Açık işlere bakıldı"
    description = (
        "Kullanıcı açık/yarım işleri sorduğunda kullan: \"nerede kalmıştık\", \"açık işimiz "
        "var mı\". Artemis'in kendi açık iş ve son oturum notlarını, ayarlanmışsa seçili "
        "vault dosyalarını (örn. Threads, Last-Session) döndürür."
    )
    args_model = OpenWorkArguments
    risk = Risk.READ_ONLY

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        settings = context.settings
        sections: dict[str, str] = {}
        for key in ("isler", "son_oturum"):
            text = read_companion(settings.state_dir, key, _OWN_CHARS).strip()
            if text:
                sections[key] = text
        vault = settings.memory.vault_path
        vault_read: list[str] = []
        if vault is not None and vault.is_dir():
            for rel in settings.memory.companion_vault_files:
                try:
                    # Açık kullanıcı seçimi exclude'u aşar ama yol kaçışı yine reddedilir.
                    path = safe_join(vault, rel)
                    if not path.is_file():
                        continue
                    with path.open("r", encoding="utf-8") as stream:
                        sections[rel] = stream.read(_VAULT_CHARS)
                except (OSError, ValueError, UnicodeError):
                    continue
                vault_read.append(rel)
        if not sections:
            return ToolResult(message="Okunabilir açık iş kaydı yok (dosyalar boş veya eksik).")
        external = bool(vault_read)
        return ToolResult(
            success=True,
            status="ok",
            message=f"{len(sections)} kaynak okundu.",
            data={"sources": list(sections), "vault_sources": vault_read, "content": sections},
            external=external,
            content_kind="file" if external else "text",
        )
