"""Güvenli hata metinleri kanala ulaşır; ham HTTP gövdeleri burada tutulmaz."""


class ProviderError(Exception):
    """Sağlayıcının doğrulanmış bir tamamlamayı üretemediğini bildirir."""


class RetryableProviderError(ProviderError):
    """Yerel yedeğin denenmesine izin veren bağlantı veya sunucu sorunu."""


class RateLimitedError(RetryableProviderError):
    """HTTP 429: kota/hız sınırı. Yalnız bu hata, çağıranın modeli bir süre atlamasını haklı
    kılar; bağlantı/5xx gibi diğer geçici hatalar bir sonraki çağrıda düzelmiş olabilir."""


class BudgetExceeded(ProviderError):
    """Çağrıdan önce hesaplanan güvenli maliyet sınırı aşıldığında kullanılır."""
