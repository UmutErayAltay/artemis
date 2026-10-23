"""Telegram Bot API'ye ince, sızıntıya kapalı bir HTTP istemcisi.

Bot token'ı URL'nin İÇİNDEDİR (`/bot<token>/<method>`). Bu yüzden bu modülde iki kural
mutlaktır: (1) URL ve httpx istisnalarının `str()`'i hiçbir hata mesajına, log'a ya da
denetim kaydına girmez; yalnız yöntem adı, HTTP durumu ve istisna TÜRÜ yazılır, (2) token
yalnız ortam değişkeninden gelir ve `repr`'de görünmez (ADR 0016).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from artemis.config.settings import Settings
from artemis.safety.audit import AuditLog

# httpx her isteği INFO seviyesinde `HTTP Request: POST https://.../bot<TOKEN>/...` diye
# yazar; kök logger'a INFO açan herhangi bir uygulama token'ı log dosyasına düşürürdü.
# Seviye WARNING'e çekilir ve ayrıca URL'de "/bot" geçen kayıtlar süzgeçle atılır, çünkü
# seviye sonradan tekrar düşürülebilir ama logger süzgeci yerinde kalır.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


class _DropTokenUrlRecords(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return "/bot" not in record.getMessage()


logging.getLogger("httpx").addFilter(_DropTokenUrlRecords())

# Telegram sendMessage sert sınırı. Uzun metni sessizce kesmek onay mesajında tehlikelidir,
# bu yüzden aşan metin ağa çıkmadan ValueError verir; ne yapılacağına çağıran karar verir.
TELEGRAM_TEXT_LIMIT = 4096


class TelegramError(RuntimeError):
    """Mesajı token veya URL taşımaz; yalnız yöntem ve durum/istisna türü içerir."""

    def __init__(self, message: str, *, error_type: str = "TelegramError") -> None:
        super().__init__(message)
        self.error_type = error_type


@dataclass(frozen=True)
class SentMessage:
    message_id: int


class TelegramClient:
    def __init__(
        self,
        token: str,
        api_base: str,
        *,
        audit: AuditLog,
        timeout: float = 10.0,
        http: httpx.Client | None = None,
    ) -> None:
        self._token = token
        self._api_base = api_base.rstrip("/")
        self._audit = audit
        self._timeout = timeout
        self._owns_http = http is None
        # Yönlendirme kapalı: bir 30x ile token başka bir adrese taşınmasın.
        self._http = http if http is not None else httpx.Client(
            timeout=timeout, follow_redirects=False
        )

    def __repr__(self) -> str:
        # Varsayılan repr token'ı içeren alanları da basardı.
        return f"TelegramClient(api_base={self._api_base!r}, token=***)"

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def _call(self, method: str, payload: dict[str, Any], timeout: float) -> Any:
        """Başarıyı yalnız HTTP 200 + `ok: true` kanıtlar; gerisi TelegramError'dır."""
        url = f"{self._api_base}/bot{self._token}/{method}"
        try:
            response = self._http.post(url, json=payload, timeout=timeout)
        except (httpx.HTTPError, httpx.InvalidURL, ValueError) as error:
            # str(error) istek URL'sini (yani token'ı) taşıyabilir; yalnız tür adı yazılır.
            name = type(error).__name__
            raise TelegramError(f"Telegram {method} başarısız ({name})", error_type=name) from None
        if response.status_code != 200:
            raise TelegramError(
                f"Telegram {method} başarısız (HTTP {response.status_code})",
                error_type=f"HTTP {response.status_code}",
            )
        try:
            body = response.json()
        except ValueError:
            raise TelegramError(
                f"Telegram {method} başarısız (geçersiz JSON)", error_type="InvalidJSON"
            ) from None
        if not isinstance(body, dict) or body.get("ok") is not True:
            raise TelegramError(
                f"Telegram {method} başarısız (ok değil)", error_type="NotOk"
            )
        return body.get("result")

    def get_updates(self, offset: int | None, timeout: int) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "timeout": timeout,
            "allowed_updates": ["message", "callback_query"],
        }
        if offset is not None:
            payload["offset"] = offset
        try:
            # Uzun bekleme sunucu tarafında `timeout` sürer; HTTP zaman aşımı bunun üstüne çıkar.
            result = self._call("getUpdates", payload, timeout + self._timeout)
            if not isinstance(result, list) or not all(isinstance(item, dict) for item in result):
                raise TelegramError(
                    "Telegram getUpdates başarısız (beklenmeyen sonuç)", error_type="BadResult"
                )
        except TelegramError as error:
            self._audit.emit("telegram_poll_error", error_type=error.error_type)
            raise
        return result

    def _outbound(
        self,
        method: str,
        payload: dict[str, Any],
        *,
        kind: str,
        chat_id: int | None,
        text: str,
        need_message_id: bool = False,
    ) -> int | None:
        """Her giden çağrı sonuç ne olursa olsun, deneme SONRASI denetime yazılır."""
        if len(text) > TELEGRAM_TEXT_LIMIT:
            raise ValueError("Telegram metni 4096 karakter sınırını aşıyor")
        message_id: int | None = None
        try:
            result = self._call(method, payload, self._timeout)
            if need_message_id:
                raw_id = result.get("message_id") if isinstance(result, dict) else None
                if not isinstance(raw_id, int) or isinstance(raw_id, bool):
                    # ok:true ama kimlik yok: gönderildi diyemeyiz (onay mesajı düzenlenemez).
                    raise TelegramError(
                        f"Telegram {method} başarısız (message_id yok)", error_type="BadResult"
                    )
                message_id = raw_id
        except TelegramError as error:
            self._audit.emit(
                "telegram_out",
                method=method,
                kind=kind,
                chat_id=chat_id,
                text=text,
                ok=False,
                error_type=error.error_type,
            )
            raise
        fields: dict[str, Any] = {} if message_id is None else {"message_id": message_id}
        self._audit.emit(
            "telegram_out",
            method=method,
            kind=kind,
            chat_id=chat_id,
            text=text,
            ok=True,
            **fields,
        )
        return message_id

    def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        kind: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> SentMessage:
        # parse_mode yok: düz metin; model/araç çıktısıyla biçim (HTML/Markdown) enjekte edilemez.
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        message_id = self._outbound(
            "sendMessage", payload, kind=kind, chat_id=chat_id, text=text, need_message_id=True
        )
        if message_id is None:  # need_message_id=True iken ulaşılmaz; mypy için dürüst dal
            raise TelegramError("Telegram sendMessage başarısız (message_id yok)")
        return SentMessage(message_id)

    def edit_message_text(self, chat_id: int, message_id: int, text: str, *, kind: str) -> None:
        # reply_markup verilmediği için düğmeler de kaldırılır: eski onay tekrar basılamaz.
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        self._outbound("editMessageText", payload, kind=kind, chat_id=chat_id, text=text)

    def answer_callback_query(self, callback_query_id: str, text: str, *, kind: str) -> None:
        payload: dict[str, Any] = {"callback_query_id": callback_query_id, "text": text}
        self._outbound("answerCallbackQuery", payload, kind=kind, chat_id=None, text=text)


def build_telegram_client(
    settings: Settings,
    audit: AuditLog,
    env: Mapping[str, str] | None = None,
    http: httpx.Client | None = None,
) -> TelegramClient | None:
    """Telegram kapalıysa (chat_id yok) ya da token yoksa None; değeri asla yazmaz."""
    config = settings.telegram
    if config.chat_id is None:
        return None
    source = os.environ if env is None else env
    token = source.get(config.token_env, "").strip()
    if not token:
        return None
    return TelegramClient(
        token,
        config.api_base,
        audit=audit,
        timeout=config.request_timeout_seconds,
        http=http,
    )
