# ADR 0011 — Tek Qt masaüstü ve mevcut güvenlik kapısı

**Durum:** Uygulama öncesi kabul edilen yön; ayrıntılar
[masaüstü planında](../DESKTOP_PLAN.md). Kullanıcı F4 hafızayı beklemeden GUI'yi
istedi; mevcut F0–F3 davranışı korunur.

**Bağlam:** Windows 11 üzerinde Türkçe panel, hızlı komut penceresi ve tepsi
gerekiyor. CLI/ses aynı CommandRunner'ı kullanıyor. SafetyGate'in senkron
`(tool_name, arguments) -> bool` onayı ve tur temizliği güvenlik sınırıdır.
Linux CI'nin Qt veya gerçek masaüstü olmadan çalışması gerekir.

**Karar:** İsteğe bağlı PySide6 6.12.0 ile tek QApplication kullanılır. Qt yalnız
ana thread'de, ortak runtime/asyncio ayrı tek işçi thread'inde yaşar; qasync veya
ikinci GUI süreci eklenmez. CLI/ses/GUI, çıkarılan tek runtime fabrikasından
aynı sağlayıcı, AgentLoop ve SafetyGate zincirini kurar. GUI araç dispatch etmez
ve risk/onay politikası oluşturmaz. Mikrofon bu teslimatta bağlı değildir.

GUI komutları işçi döngüsüne thread-safe iletilir; asyncio.Event GUI'den
değiştirilmez. Senkron onay yalnız işçiyi bekleten, deadline ile sınırlı
concurrent.futures.Future üzerinden köprülenir. Karar, bloke işçi döngüsüne
sıraya konmaz; kilitli aracı eşleşen tur/istek/onay UUID'sini doğrudan çözer.
İptal ve kapanış önce bekleyen onayı False ile geçersizleştirir. Eski karar
sonraki onayı çözemez. GUI gerçek doğrulanmış argümanları ve kapının taint
uyarısını gösterir; varsayılan Hayır'dır. Temizlik bitmeden yeni tur/loop kapanışı yoktur.

AuditLog'a isteğe bağlı, başarılı disk yazımı ve sır temizliği sonrası gözlemci
eklenir. Sınırlı kuyruktan gerçek olaylar görüntülenir; düşen görünüm olayları
belirtilir, tam JSONL korunur. Kontrol/onay/nihai sonuç iletileri düşürülmez.
`sent`, hata, belirsizlik ve iptal başarı gibi sunulmaz.

**Sonuçlar:** Çekirdek Qt'ye bağımlı olmaz; aynı güvenlik testleri bütün kanalları
korur. Senkron onay sırasında işçi asyncio döngüsü bekler; GUI akıcı kalır,
deadline/ret yolu beklemeyi sonlandırır. Bu bilinçli sınıra ayrı iptal/onay
yarış testleri gerekir. Gerçek Windows tepsi, DPI ve odak kabulü Linux offscreen
testinden ayrıdır. Açık demo, fake provider ve yalnız bellek içi demo araçlarıyla
ücretsiz görsel inceleme sağlar; üretim davranışını taklit ederek başarı iddia etmez.
