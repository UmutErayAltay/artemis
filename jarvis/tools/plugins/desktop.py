"""Görünür masaüstü eylemlerinde doğrulanmış başarı ile isteği ayırır."""

import asyncio
import subprocess
import webbrowser
from typing import cast
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from jarvis.tools.base import BaseTool, ContentKind, Risk, ToolContext, ToolResult
from jarvis.tools.platform import UnsupportedPlatformError, desktop_service
from jarvis.tools.processes import DesktopProcessResult, run_desktop_action
from jarvis.tools.registry import register_tool


class EmptyArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ApplicationArguments(EmptyArguments):
    name: str = Field(min_length=1)


class ClipboardArguments(EmptyArguments):
    text: str = Field(max_length=1_000_000)


class URLArguments(EmptyArguments):
    url: str = Field(min_length=1, max_length=8192)


def desktop_error(error: Exception) -> ToolResult:
    # Exception metinleri komut satırı veya süreç ayrıntısı taşıyabilir; kullanıcıya
    # ham repr/argv sızdırmadan hatayı dürüstçe bildir.
    if isinstance(error, UnsupportedPlatformError):
        message = "Bu masaüstü işlemi yalnız Windows'ta destekleniyor."
    else:
        message = "Masaüstü işlemi yapılamadı."
    return ToolResult(status="error", message=message)


async def run_default_action(
    action: str, arguments: dict[str, object], context: ToolContext
) -> DesktopProcessResult:
    return await run_desktop_action(
        action,
        arguments,
        cancelled=context.cancelled,
        deadline=context.deadline,
    )


def process_result(
    result: DesktopProcessResult,
    *,
    external: bool = False,
    content_kind: ContentKind = "text",
) -> ToolResult:
    return ToolResult(
        success=result.status == "ok",
        status=result.status,
        message=result.message,
        data=result.data,
        external=external,
        content_kind=content_kind,
    )


@register_tool
class OpenApplication(BaseTool):
    name = "open_application"
    description = "Yalnız ayarlardaki izinli uygulama takma adını açmayı ister."
    args_model = ApplicationArguments
    risk = Risk.SIDE_EFFECT

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        args = cast(ApplicationArguments, arguments)
        executable = context.settings.applications.get(args.name)
        if executable is None:
            return ToolResult(status="error", message="Uygulama izin listesinde yok.")
        try:
            if "desktop" in context.services:
                await asyncio.to_thread(
                    desktop_service(context.services).open_application, executable
                )
            else:
                return process_result(
                    await run_default_action(
                        "open_application", {"executable": executable}, context
                    )
                )
            return ToolResult(
                status="sent", message="Uygulama açma komutu gönderildi; açıldığı doğrulanmadı."
            )
        except (OSError, UnsupportedPlatformError, ValueError, subprocess.SubprocessError) as error:
            return desktop_error(error)


@register_tool
class CloseApplication(BaseTool):
    name = "close_application"
    description = (
        "Uygulamanın adıyla eşleşen tüm süreçlerini kapatır; kaydedilmemiş işler kaybolabilir."
    )
    args_model = ApplicationArguments
    risk = Risk.CONFIRM_REQUIRED

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        args = cast(ApplicationArguments, arguments)
        executable = context.settings.applications.get(args.name)
        if executable is None:
            return ToolResult(status="error", message="Uygulama izin listesinde yok.")
        try:
            if "desktop" in context.services:
                closed = await asyncio.to_thread(
                    desktop_service(context.services).close_application, executable
                )
            else:
                return process_result(
                    await run_default_action(
                        "close_application", {"executable": executable}, context
                    )
                )
            if not closed:
                return ToolResult(status="error", message="Uygulamanın kapandığı doğrulanamadı.")
            return ToolResult(
                success=True,
                status="ok",
                message="Uygulamanın adıyla eşleşen tüm süreçler kapandı.",
            )
        except (OSError, UnsupportedPlatformError, ValueError, subprocess.SubprocessError) as error:
            return desktop_error(error)


@register_tool
class ReadClipboard(BaseTool):
    name = "read_clipboard"
    description = "Windows panosundaki metni okur; içerik dış veri olarak işaretlenir."
    args_model = EmptyArguments
    risk = Risk.READ_ONLY
    external = True
    content_kind = "clipboard"

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        try:
            if "desktop" in context.services:
                value = await asyncio.to_thread(desktop_service(context.services).read_clipboard)
            else:
                return process_result(
                    await run_default_action("read_clipboard", {}, context),
                    external=True,
                    content_kind="clipboard",
                )
            return ToolResult(
                success=True,
                status="ok",
                message="Pano okundu.",
                data={"text": value},
                external=True,
                content_kind="clipboard",
            )
        except (OSError, UnsupportedPlatformError) as error:
            return desktop_error(error)


@register_tool
class WriteClipboard(BaseTool):
    name = "write_clipboard"
    description = "Windows panosuna metin yazar ve geri okuyarak doğrular."
    args_model = ClipboardArguments
    risk = Risk.SIDE_EFFECT

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        args = cast(ClipboardArguments, arguments)
        try:
            if "desktop" in context.services:
                written = await asyncio.to_thread(
                    desktop_service(context.services).write_clipboard, args.text
                )
            else:
                return process_result(
                    await run_default_action("write_clipboard", {"text": args.text}, context)
                )
            if not written:
                return ToolResult(status="error", message="Panoya yazma doğrulanamadı.")
            return ToolResult(success=True, status="ok", message="Pano metni yazıldı.")
        except (OSError, UnsupportedPlatformError, ValueError) as error:
            return desktop_error(error)


@register_tool
class OpenURL(BaseTool):
    name = "open_url"
    description = "Yalnız http/https adresini tarayıcıya gönderir; sayfanın açıldığını iddia etmez."
    args_model = URLArguments
    risk = Risk.SIDE_EFFECT

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        args = cast(URLArguments, arguments)
        try:
            parsed = urlsplit(args.url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError("Yalnız geçerli http/https adresleri açılabilir.")
            if parsed.username or parsed.password or any(ord(char) < 32 for char in args.url):
                raise ValueError("URL kimlik bilgisi veya kontrol karakteri içeremez.")
            opener = context.services.get("url_opener")
            if opener is None:
                if "desktop" in context.services:
                    await asyncio.to_thread(
                        desktop_service(context.services).open_browser, args.url
                    )
                else:
                    return process_result(
                        await run_default_action("open_browser", {"url": args.url}, context)
                    )
            else:
                submitted = await asyncio.to_thread(opener, args.url)
                if not submitted:
                    return ToolResult(
                        status="error", message="Tarayıcı açma isteğini kabul etmedi."
                    )
            return ToolResult(
                status="sent", message="URL tarayıcıya gönderildi; sayfanın açıldığı doğrulanmadı."
            )
        except (
            OSError,
            RuntimeError,
            ValueError,
            subprocess.SubprocessError,
            webbrowser.Error,
        ) as error:
            return desktop_error(error)
