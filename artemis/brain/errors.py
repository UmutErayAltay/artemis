"""Güvenli hata metinleri kanala ulaşır; ham HTTP gövdeleri burada tutulmaz."""

from __future__ import annotations

from typing import Literal


class ProviderError(Exception):
    """Sağlayıcının doğrulanmış bir tamamlamayı üretemediğini bildirir."""


class RetryableProviderError(ProviderError):
    """Yerel yedeğin denenmesine izin veren bağlantı veya sunucu sorunu."""


class RateLimitedError(RetryableProviderError):
    """HTTP 429: kota/hız sınırı. Yalnız bu hata, çağıranın modeli bir süre atlamasını haklı
    kılar; bağlantı/5xx gibi diğer geçici hatalar bir sonraki çağrıda düzelmiş olabilir."""


class MediaRejectedError(ProviderError):
    """Sağlayıcı ses veya görüntü girdisini reddettiğinde medya düşüşü kararını ayırır."""

    def __init__(
        self,
        message: str,
        *,
        media_kind: Literal["audio", "image", "unknown"] = "unknown",
    ) -> None:
        super().__init__(message)
        self.media_kind = media_kind


class BudgetExceeded(ProviderError):
    """Çağrıdan önce hesaplanan güvenli maliyet sınırı aşıldığında kullanılır."""
