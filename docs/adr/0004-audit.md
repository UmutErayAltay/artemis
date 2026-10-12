# ADR 0004 — JSONL denetim ve SQLite harcama

**Bağlam:** Yapılmayan işe başarı denmemeli; onay, hata, süre ve maliyet izlenmelidir.

**Karar:** Olaylar UTC zaman/tur kimliğiyle UTF-8 JSONL'ye, günlük maliyet ve
rezervasyonlar SQLite'a yazılır. İç içe sır anahtarları ve ortam sır değerleri
temizlenir. Denetim yazılamıyorsa yan etki başlamaz. Ham HTTP hata gövdesi loglanmaz.

**Sonuçlar:** Raporlar kolay işlenir; dosya döndürme ve tam olay veritabanı F2+
işidir. Çöküşte açık kalan rezervasyon tavanı korumak için harcanmış sayılır.
