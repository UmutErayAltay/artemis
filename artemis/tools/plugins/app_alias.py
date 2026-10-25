"""Kullanıcının düzelttiği uygulama adlarını hatırlama (ADR 0044).

"Hayır, Brave'i kastettim" gibi bir düzeltmeden sonra model söylenen adı izinli uygulamaya
bağlar; sonraki turlarda aynı söyleyiş soru sormadan doğru uygulamaya gider. Takma ad izin
listesini genişletmez: hedef ``settings.applications`` içinde tam adla bulunmalıdır ve
başka bir uygulamanın gerçek adı takma ad olamaz ("edge" Brave'e bağlanamaz).

SIDE_EFFECT'tir: temiz turda sormadan, dış içerikle lekeli turda onayla yazılır; böylece
web/dosya metni kullanıcı adına yönlendirme kuramaz. Kalıcı hafıza kapalıyken yazılmaz.
"""

from typing import cast

from pydantic import BaseModel, ConfigDict, Field

from artemis.tools.app_resolver import (
    MAX_ALIAS_CHARS,
    AliasStoreError,
    alias_store,
    normalize,
    resolve_application,
)
from artemis.tools.base import BaseTool, Risk, ToolContext, ToolResult
from artemis.tools.registry import register_tool

_DISABLED = "Kalıcı hafıza kapalı; uygulama takma adı kaydedilmez."


class AliasArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    spoken: str = Field(min_length=1, max_length=MAX_ALIAS_CHARS)


class RememberAliasArguments(AliasArguments):
    application: str = Field(min_length=1, max_length=200)


def _error(error: AliasStoreError) -> ToolResult:
    return ToolResult(status="error", message=f"Takma ad kaydı başarısız: {error}")


@register_tool
class RememberApplicationAlias(BaseTool):
    name = "app_alias_remember"
    label = "Uygulama adını hatırlama"
    done_label = "Uygulama adı hatırlandı"
    description = (
        "Kullanıcı yanlış anlaşılan bir uygulama adını düzeltince (ör. 'breyv' dediğinde "
        "Brave'i kastetmişse) söyleneni (spoken) izinli uygulamanın tam adına (application) "
        "bağlar; sonra aynı söyleyiş doğrudan o uygulamayı açar."
    )
    args_model = RememberAliasArguments
    risk = Risk.SIDE_EFFECT

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        args = cast(RememberAliasArguments, arguments)
        if not context.settings.persist_memory:
            return ToolResult(status="error", message=_DISABLED)
        applications = context.settings.applications
        key = normalize(args.spoken)
        if not key:
            return ToolResult(status="error", message="Söylenen ad harf veya rakam içermeli.")
        target = resolve_application(applications, {}, args.application, fuzzy=False)
        if target.name is None:
            return ToolResult(
                status="error", message="Hedef uygulama izin listesinde tam adla bulunamadı."
            )
        shadowed = resolve_application(applications, {}, args.spoken, fuzzy=False)
        if shadowed.name is not None:
            if shadowed.name == target.name:
                return ToolResult(
                    status="error", message="Söylenen ad zaten bu uygulamanın kendi adı."
                )
            return ToolResult(
                status="error",
                message=f"'{args.spoken}' başka bir uygulamanın ({shadowed.name}) adı; bağlanmadı.",
            )
        store = alias_store(context.settings.state_dir)
        try:
            aliases = store.load()
            aliases[key] = target.name
            store.save(aliases)
            saved = store.load().get(key)
        except AliasStoreError as error:
            return _error(error)
        if saved != target.name:
            return ToolResult(status="error", message="Takma ad yazıldıktan sonra doğrulanamadı.")
        return ToolResult(
            success=True,
            status="ok",
            message=f"'{args.spoken}' artık {target.name} olarak anlaşılacak.",
            data={"spoken": args.spoken, "application": target.name},
        )


@register_tool
class ForgetApplicationAlias(BaseTool):
    name = "app_alias_forget"
    label = "Uygulama adını unutma"
    done_label = "Uygulama adı unutuldu"
    description = "app_alias_remember ile öğretilmiş bir uygulama takma adını siler."
    args_model = AliasArguments
    risk = Risk.SIDE_EFFECT

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        args = cast(AliasArguments, arguments)
        if not context.settings.persist_memory:
            return ToolResult(status="error", message=_DISABLED)
        key = normalize(args.spoken)
        store = alias_store(context.settings.state_dir)
        try:
            aliases = store.load()
            if key not in aliases:
                return ToolResult(status="error", message="Böyle bir takma ad kayıtlı değil.")
            del aliases[key]
            store.save(aliases)
            still = key in store.load()
        except AliasStoreError as error:
            return _error(error)
        if still:
            return ToolResult(status="error", message="Takma ad silindikten sonra doğrulanamadı.")
        return ToolResult(success=True, status="ok", message=f"'{args.spoken}' unutuldu.")
