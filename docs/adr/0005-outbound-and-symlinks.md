# ADR 0005 — Arama sorgusu ve bağlantılı dosya hedefleri

**Bağlam:** Web araması sorguyu harici sunucuya gönderir. Salt okunur sayılırsa
dış dosyadaki bir enjeksiyon metni sorgu üzerinden onaysız dışarı taşınabilir.
Sembolik bağlantıyı çözerek değiştirmek de kullanıcının gösterilen bağlantı yerine
bağlantı hedefini değiştirmesine neden olabilir.

**Karar:** `web_search` SIDE_EFFECT bildirir; temiz turdaki doğrudan arama çalışır,
dış içerik okunduktan sonraki arama gerçek sorguyla onay ister. Dosya değiştiren
araçlar `safe_join(..., allow_symlinks=False)` kullanır. Okuma/listeleme yalnız kök
içinde çözülen bağlantıları izleyebilir.

**Sonuçlar:** Ardışık aramalarda ek onay gerekebilir. Bağlantı üzerinden dosya
değişikliği yerine gerçek göreli yol gerekir. Reddedilen sorgunun servise
ulaşmadığı ve bağlantı hedefinin değişmediği izole testlerle doğrulanır.
