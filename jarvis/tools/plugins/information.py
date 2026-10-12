"""Bilgi araçları doğrulanabilir yerel değerler ve dış arama verisi üretir."""

import platform
from datetime import datetime
from html.parser import HTMLParser
from typing import cast
from urllib.parse import parse_qs, urlsplit

import httpx
from pydantic import BaseModel, Field

from jarvis.tools.base import BaseTool, Risk, ToolContext, ToolResult
from jarvis.tools.plugins.desktop import EmptyArguments
from jarvis.tools.registry import register_tool


class SearchArguments(EmptyArguments):
    query: str = Field(min_length=1, max_length=2000)


class SearchParser(HTMLParser):
    """Resmî API olmayan HTML adaptörü hata/challenge sayfasını sonuç saymaz."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self._title: list[str] | None = None
        self._url = ""
        self.no_results = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        classes = (attributes.get("class") or "").split()
        if "no-results" in classes or "no-results__message" in classes:
            self.no_results = True
        if tag == "a" and "result__a" in classes:
            target = attributes.get("href") or ""
            if target.startswith("//"):
                target = "https:" + target
            parsed = urlsplit(target)
            if parsed.hostname and parsed.hostname.endswith("duckduckgo.com"):
                target = parse_qs(parsed.query).get("uddg", [target])[0]
            parsed = urlsplit(target)
            if parsed.scheme in {"http", "https"} and parsed.hostname:
                self._url = target
                self._title = []

    def handle_data(self, data: str) -> None:
        if self._title is not None:
            self._title.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._title is not None:
            self.results.append({"title": "".join(self._title).strip(), "url": self._url})
            self._title = None


async def search_web(query: str) -> list[dict[str, str]]:
    """HTML değişirse sessiz boş başarı yerine açık hata verir; fixture ağsız çalışır."""
    async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
        response = await client.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            headers={"User-Agent": "Jarvis/0.1 personal-assistant"},
        )
        response.raise_for_status()
    if response.status_code != 200 or len(response.content) > 2_000_000:
        raise ValueError("Arama servisi geçerli boyutta sonuç sayfası döndürmedi.")
    parser = SearchParser()
    parser.feed(response.text)
    if not parser.results and not parser.no_results:
        raise ValueError("Arama sonuçları okunamadı; servis şeması veya erişimi değişmiş olabilir.")
    return parser.results[:8]


@register_tool
class WebSearch(BaseTool):
    name = "web_search"
    description = (
        "Arama sorgusunu DuckDuckGo servisine gönderir; başlık ve URL sonuçları "
        "güvenilmeyen dış veridir."
    )
    args_model = SearchArguments
    risk = Risk.SIDE_EFFECT
    external = True
    content_kind = "web"

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        args = cast(SearchArguments, arguments)
        try:
            search = context.services.get("web_search", search_web)
            results = await search(args.query)
            return ToolResult(
                success=True,
                status="ok",
                message=f"{len(results)} arama sonucu alındı.",
                data={"results": results},
                external=True,
                content_kind="web",
            )
        except (httpx.HTTPError, ValueError, OSError) as error:
            # HTTP exception metni URL/query içerip ortam sırlarını yansıtabilir.
            if isinstance(error, httpx.TimeoutException):
                reason = "istek zaman aşımına uğradı."
            elif isinstance(error, httpx.ConnectError):
                reason = "bağlantı kurulamadı."
            elif isinstance(error, httpx.HTTPStatusError):
                reason = f"servis HTTP {error.response.status_code} yanıtı verdi."
            elif isinstance(error, ValueError):
                reason = "arama sonuçları okunamadı."
            else:
                reason = "ağ işlemi başarısız oldu."
            return ToolResult(status="error", message=f"Web araması başarısız: {reason}")


@register_tool
class SystemInfo(BaseTool):
    name = "system_info"
    description = "İşletim sistemi, sürüm ve mimariyi ortam sırlarını okumadan döndürür."
    args_model = EmptyArguments
    risk = Risk.READ_ONLY

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        return ToolResult(
            success=True,
            status="ok",
            message="Sistem bilgisi alındı.",
            data={
                "system": platform.system(),
                "release": platform.release(),
                "machine": platform.machine(),
                "python": platform.python_version(),
            },
        )


@register_tool
class CurrentTime(BaseTool):
    name = "current_time"
    description = "Yerel saat dilimi bilgisiyle güncel tarih ve saati verir."
    args_model = EmptyArguments
    risk = Risk.READ_ONLY

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        now = datetime.now().astimezone()
        return ToolResult(
            success=True,
            status="ok",
            message="Tarih ve saat okundu.",
            data={
                "iso": now.isoformat(),
                "date": now.date().isoformat(),
                "time": now.strftime("%H:%M:%S"),
                "timezone": str(now.tzinfo),
            },
        )
