# ADR 0001 — İnce ajan döngüsü

**Bağlam:** Çok adımlı işlerin araç sonuçlarına göre ilerlemesi, güvenliğin her
çağrıda gözlenebilmesi gerekir.

**Karar:** Python async tabanlı kendi AgentLoop ve CommandRunner katmanımızı
yazarız; LangChain benzeri bir çerçeve eklemeyiz.

**Sonuçlar:** Sağlayıcı ve araç bağımsız test edilir. Durma koşulları açık kod ve
kabul testleriyle korunur; orkestrasyon bakımını biz üstleniriz.
