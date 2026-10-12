# ADR 0002 — Sağlayıcı sözleşmesi

**Bağlam:** Bulut birincildir; fiyatlar ve model isimleri değişir, bağlantı kopabilir.

**Karar:** Ortak mesaj/araç/usage modelleri ve async Provider protokolü kullanılır.
OpenRouter, Anthropic ve Ollama yalnız HTTP/şema çeviricileridir. Modeller ve token
fiyatları config'den gelir; resmi katalog doğrulaması ölçüm kaydına yazılır.
Bulut bağlantı hataları süreli, tek araçla sınırlı yerel yedeği etkinleştirir.

**Sonuçlar:** Model seçimi kod değişikliği gerektirmez. Bilinmeyen fiyatla buluta
çağrı yapılmaz. Ücretli çağrı ayrıca açık izin gerektirir. Yedek olayları görünürdür.
