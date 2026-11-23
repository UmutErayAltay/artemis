"""Onaylı iki yönlü takvim ve posta araçları (ADR 0084): `mail_send`, `calendar_create_event`.

İkisi de `Risk.CONFIRM_REQUIRED`, `outbound=True`, `auto_mode_ok=False` taşır:
otomatik kipte bile sessiz çalışmaz, gözetimsiz turda reddedilir ve onay penceresinde
tam içerik önizlemesiyle sorulur. İlgili opt-in bayrağı kapalıyken `refuse` reddeder,
runtime da araçları modelden gizler.
"""

from __future__ import annotations

from datetime import tzinfo
from typing import cast
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator

from artemis.i18n import t
from artemis.tools import calendar_mail as core_read
from artemis.tools import calendar_mail_write as core_write
from artemis.tools.base import BaseTool, Risk, ToolContext, ToolResult, run_blocking
from artemis.tools.registry import register_tool

_CTL_CHARS = ("\r", "\n", "\0", "\x0b", "\x0c", "\x85", "\u2028", "\u2029")


class MailSendArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    to: str = Field(
        min_length=3,
        max_length=500,
        description="Alıcı e-posta adresi (virgülle birden fazla olabilir).",
    )
    subject: str = Field(min_length=1, max_length=200, description="E-posta konusu.")
    body: str = Field(min_length=1, max_length=10000, description="E-posta gövdesi.")
    cc: str = Field(default="", max_length=500, description="İsteğe bağlı bilgi (Cc) alıcısı.")

    @field_validator("to", "subject", "cc")
    @classmethod
    def no_control_chars(cls, value: str) -> str:
        if any(ch in value for ch in _CTL_CHARS):
            raise ValueError("Başlık alanı satır sonu veya denetim karakteri içeremez.")
        return value


class CalendarCreateEventArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200, description="Etkinlik başlığı.")
    start: str = Field(
        min_length=10,
        max_length=40,
        description="Başlangıç (ISO: YYYY-MM-DD veya YYYY-MM-DDTHH:MM).",
    )
    end: str = Field(
        min_length=10,
        max_length=40,
        description="Bitiş (ISO: YYYY-MM-DD veya YYYY-MM-DDTHH:MM).",
    )
    location: str = Field(default="", max_length=200, description="İsteğe bağlı konum.")
    description: str = Field(default="", max_length=4000, description="İsteğe bağlı açıklama.")

    @field_validator("title", "start", "end", "location")
    @classmethod
    def no_control_chars(cls, value: str) -> str:
        if any(ch in value for ch in _CTL_CHARS):
            raise ValueError("Etkinlik alanı satır sonu veya denetim karakteri içeremez.")
        return value


@register_tool
class MailSend(BaseTool):
    name = "mail_send"
    label = "E-posta gönder"
    done_label = "E-posta gönderildi"
    description = (
        "Kullanıcının açık onayıyla belirtilen alıcıya SMTP üzerinden e-posta gönderir. "
        "Her zaman tam alıcı, konu ve gövde ile onay ister; sessiz gönderim yapmaz."
    )
    args_model = MailSendArguments
    risk = Risk.CONFIRM_REQUIRED
    outbound = True
    auto_mode_ok = False
    external = False

    def refuse(self, arguments: BaseModel, context: ToolContext) -> str | None:
        if not context.settings.mail.enabled:
            return t("Posta kapalı: config içinde mail.enabled: true gerekir.")
        if not context.settings.mail.allow_send:
            return t("E-posta gönderme kapalı: config içinde mail.allow_send: true gerekir.")
        return None

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        args = cast(MailSendArguments, arguments)
        config = context.settings.mail
        username = (config.smtp_username or config.username).strip()
        password = core_write.resolve_smtp_password()
        if not (config.smtp_host and username and password):
            return ToolResult(
                message=t(
                    "SMTP ayarı eksik: mail.smtp_host, SMTP kullanıcı adı ve parola "
                    "(Ayarlar > Takvim ve posta ya da {env}) gerekir."
                ).format(env=core_write.SMTP_PASSWORD_ENV)
            )
        secret: str = password
        try:
            data = await run_blocking(
                lambda: core_write.send_mail(
                    context.services.get("smtp_connect", core_write.connect_smtp),
                    host=config.smtp_host,
                    port=config.smtp_port,
                    username=username,
                    password=secret,
                    to=args.to,
                    subject=args.subject,
                    body=args.body,
                    cc=args.cc,
                )
            )
        except core_read.SourceError as error:
            return ToolResult(message=str(error))
        return ToolResult(
            success=True,
            status="ok",
            message=t("E-posta gönderildi: {to}.").format(to=", ".join(data["to"])),
            data=data,
        )


@register_tool
class CalendarCreateEvent(BaseTool):
    name = "calendar_create_event"
    label = "Takvim etkinliği oluştur"
    done_label = "Takvim etkinliği oluşturuldu"
    description = (
        "Kullanıcının açık onayıyla CalDAV takviminde yeni bir etkinlik oluşturur. "
        "Her zaman tam etkinlik ayrıntıları ile onay ister; sessiz yazma yapmaz."
    )
    args_model = CalendarCreateEventArguments
    risk = Risk.CONFIRM_REQUIRED
    outbound = True
    auto_mode_ok = False
    external = False

    def refuse(self, arguments: BaseModel, context: ToolContext) -> str | None:
        if not context.settings.calendar.enabled:
            return t("Takvim kapalı: config içinde calendar.enabled: true gerekir.")
        if not context.settings.calendar.allow_write:
            return t("Takvim yazma kapalı: config içinde calendar.allow_write: true gerekir.")
        return None

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        args = cast(CalendarCreateEventArguments, arguments)
        config = context.settings.calendar
        if not config.caldav_url.strip():
            return ToolResult(
                message=t(
                    "CalDAV adresi eksik: Ayarlar > Takvim ve posta'dan "
                    "CalDAV adresi tanımlayın."
                )
            )
        password = core_write.resolve_caldav_password() or ""
        local_tz: tzinfo = ZoneInfo(context.settings.proactive.timezone)
        try:
            data = await run_blocking(
                lambda: core_write.create_calendar_event(
                    context.services.get("calendar_put", core_write.put_caldav_event),
                    caldav_url=config.caldav_url,
                    username=config.caldav_username.strip(),
                    password=password,
                    title=args.title,
                    start=args.start,
                    end=args.end,
                    location=args.location,
                    description=args.description,
                    local_tz=local_tz,
                )
            )
        except core_read.SourceError as error:
            return ToolResult(message=str(error))
        return ToolResult(
            success=True,
            status="ok",
            message=t("Takvim etkinliği oluşturuldu: {title}.").format(title=data["title"]),
            data=data,
        )
