"""Güvenli hata metinleri kanala ulaşır; ham HTTP gövdeleri burada tutulmaz."""


class ProviderError(Exception):
    """Sağlayıcının doğrulanmış bir tamamlamayı üretemediğini bildirir."""


class RetryableProviderError(ProviderError):
    """Yerel yedeğin denenmesine izin veren bağlantı veya sunucu sorunu."""


class BudgetExceeded(ProviderError):
    """Çağrıdan önce hesaplanan güvenli maliyet sınırı aşıldığında kullanılır."""
