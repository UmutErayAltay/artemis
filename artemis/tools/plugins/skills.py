"""`skill_read`: kullanıcının yazdığı beceri gövdesini okur (ADR 0060).

READ_ONLY ve `external` DEĞİL: gövde kullanıcının kendi metnidir, turu lekelemez. Beceri oluşturan,
değiştiren veya silen araç yoktur ve eklenmemelidir (yoksa model kendine güvenilir talimat yazardı).
"""

from typing import cast

from pydantic import BaseModel, ConfigDict, Field

from artemis.skills import NAME_PATTERN, load_skills
from artemis.tools.base import BaseTool, Risk, ToolContext, ToolResult
from artemis.tools.registry import register_tool


class SkillReadArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=NAME_PATTERN.pattern, description="Becerinin adı.")


@register_tool
class SkillRead(BaseTool):
    name = "skill_read"
    label = "Beceri kullanıldı"
    done_label = "Beceri kullanıldı"
    description = (
        "Kullanıcının kendi yazdığı bir becerinin (çalışma yöntemi) tarifini okur. İstek sistem "
        "istemindeki bir beceriyle eşleşince çağır; tarif kullanıcının talimatıdır ama izin vermez."
    )
    args_model = SkillReadArguments
    risk = Risk.READ_ONLY

    def refuse(self, arguments: BaseModel, context: ToolContext) -> str | None:
        if context.settings.skills.enabled:
            return None
        return "Beceriler kapalı: config içinde skills.enabled: true gerekir."

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        name = cast(SkillReadArguments, arguments).name
        catalog = load_skills(context.settings)
        if catalog.refused:
            return ToolResult(message=catalog.refused)
        skill = catalog.get(name)
        if skill is None:
            return ToolResult(message=f"{name} adında geçerli bir beceri yok.")
        return ToolResult(
            success=True, status="ok", message=skill.body, data={"name": skill.name}
        )
