"""Resim modeli çağrısı (ADR 0048 eki: pet üretici resim yolu); Qt'siz, eşzamanlı.

Yalnız `kind: image` + `openai_compatible` modeller. Ücretli kurallar sohbet yolundakiyle aynıdır:
fiyat sıfırdan farklıysa `allow_paid` şart, çağrı başına sabit `image_cost_usd` bütçe defterinden
ayrılır. Anahtar yalnız Authorization başlığına gider; hata metnine URL/gövde/anahtar girmez.
Görüntü çözme ve boyut doğrulaması çağıranın (Qt) işidir; burada yalnız PNG/WebP imzası bakılır.
"""

from __future__ import annotations

import base64
import binascii
import json
import time
from decimal import Decimal

import httpx

from artemis.brain.budget import BudgetLedger
from artemis.brain.errors import BudgetExceeded, ProviderError, RetryableProviderError
from artemis.brain.providers import _check_status, _endpoint_key
from artemis.config.settings import Settings
from artemis.safety.audit import AuditLog

TIMEOUT = 240.0
MAX_RESPONSE_BYTES = 25 * 1024 * 1024


def _is_image(data: bytes) -> bool:
    return data.startswith(b"\x89PNG\r\n\x1a\n") or (
        data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    )


def _emit(audit: AuditLog, event: str, **fields: object) -> None:
    try:
        audit.emit(event, **fields)
    except OSError:
        raise ProviderError("Sağlayıcı denetim kaydı güvenli biçimde yazılamadı.") from None


def _send(
    url: str, headers: dict[str, str], *, json_body: dict[str, object] | None,
    data: dict[str, str] | None, files: dict[str, tuple[str, bytes, str]] | None,
    transport: httpx.BaseTransport | None,
) -> bytes:
    """İsteği yollar, yanıt gövdesini sınırlı okur. Ağ hatasında URL/istisna zinciri taşınmaz."""
    body = bytearray()
    failed = False
    try:
        with (
            httpx.Client(follow_redirects=False, timeout=TIMEOUT, transport=transport) as client,
            client.stream(
                "POST", url, json=json_body, data=data, files=files, headers=headers
            ) as response,
        ):
            _check_status(response)
            for chunk in response.iter_bytes():
                body += chunk
                if len(body) > MAX_RESPONSE_BYTES:
                    raise ProviderError("Resim yanıtı 25 MB sınırını aştı.")
    except httpx.RequestError:
        failed = True
    if failed:
        raise RetryableProviderError("Sağlayıcıya bağlanılamadı veya yanıt süresi doldu.")
    return bytes(body)


def _extract(body: bytes) -> bytes:
    try:
        item = json.loads(body)["data"][0]
    except (ValueError, KeyError, IndexError, TypeError):
        raise ProviderError("Resim yanıtı beklenen biçimde değil.") from None
    encoded = item.get("b64_json") if isinstance(item, dict) else None
    if not isinstance(encoded, str):
        # url ile dönen görseli indirmek ikinci bir (güvenilmez) sunucuya gitmek olurdu.
        raise ProviderError(
            "Sağlayıcı resmi b64_json yerine adresle döndürdü; desteklenmiyor."
        )
    try:
        image = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise ProviderError("Resim yanıtı çözülemedi (geçersiz base64).") from None
    if not _is_image(image):
        raise ProviderError("Yanıt PNG/WebP resmi değil.")
    return image


def generate_image(
    settings: Settings, alias: str, prompt: str, *, reference: bytes | None = None,
    size: str = "1024x1024", audit: AuditLog, transport: httpx.BaseTransport | None = None,
) -> bytes:
    """Tek resim üretir (referans varsa `/images/edits`, yoksa `/images/generations`)."""
    model = settings.models.get(alias)
    if model is None or model.kind != "image" or model.provider != "openai_compatible":
        raise ProviderError("Seçilen model bir resim modeli değil.")
    priced = bool(
        model.input_per_million or model.output_per_million or model.image_cost_usd
    )
    if priced and not settings.allow_paid:
        raise BudgetExceeded("Ücretli model çağrısı ayarlarda açıkça izinli değil.")
    if priced and model.image_cost_usd <= Decimal(0):
        raise ProviderError(
            "Ücretli resim modelinde image_cost_usd tanımlı değil; yanlış yapılandırma."
        )
    endpoint = settings.endpoints[model.endpoint or ""]
    key = _endpoint_key(settings, model)
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    reservation = None
    ledger = None
    if priced:
        ledger = BudgetLedger(settings.state_dir / "budget.sqlite3", settings.daily_budget_usd)
        try:
            reservation = ledger.reserve(model.image_cost_usd)
        except BudgetExceeded:
            _emit(audit, "provider_budget_denied", provider=model.provider, model=model.model,
                  reserved_usd=str(model.image_cost_usd))
            raise
    try:
        _emit(audit, "cloud_transfer", provider=model.provider, model=model.model,
              content_kind="pet_image_prompt", has_reference=reference is not None)
    except ProviderError:
        if ledger is not None and reservation is not None:
            ledger.release(reservation)  # istek henüz çıkmadı
        raise
    started = time.monotonic()
    fields = {"model": model.model, "prompt": prompt, "size": size,
              "background": "transparent", "n": 1}
    try:
        if reference is None:
            raw = _send(f"{endpoint.base_url}/images/generations", headers, json_body=fields,
                        data=None, files=None, transport=transport)
        else:
            raw = _send(
                f"{endpoint.base_url}/images/edits", headers, json_body=None,
                data={k: str(v) for k, v in fields.items()},
                files={"image": ("reference.png", reference, "image/png")}, transport=transport,
            )
        image = _extract(raw)
    except ProviderError as error:
        # Sunucu isteği işlemiş olabilir: rezervasyon tutulur (router ile aynı muhafazakârlık).
        _emit(audit, "provider_call", provider=model.provider, model=model.model, success=False,
              error_type=type(error).__name__, elapsed_seconds=time.monotonic() - started,
              retained_reservation_usd=str(model.image_cost_usd) if priced else "0")
        raise
    if ledger is not None and reservation is not None:
        ledger.settle(reservation, model.image_cost_usd)
    _emit(audit, "provider_call", provider=model.provider, model=model.model, success=True,
          elapsed_seconds=time.monotonic() - started,
          cost_usd=str(model.image_cost_usd if priced else Decimal(0)))
    return image
