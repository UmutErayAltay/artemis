# ADR 0040 — Seslendirmeyi doğrulanmış sözle kesme

**Bağlam:** ADR 0020'deki 180 ms VAD eşiği, STT boş metin, arka plan sözü veya
yankı döndürse bile seslendirmeyi kalıcı olarak kesiyordu. Kullanıcı en küçük
gürültüde yanıt cümlesinin yarıda kaldığını bildirdi. VAD konuşma niyetini
doğrulamaz; yalnız eşiği yükseltmek bu sorunu çözmez.

**Karar:** VAD yalnız segment toplar; tek başına oynatmayı veya model/araç turunu
iptal etmez. Söz kesme, nihai STT sonucu boş değilse, mevcut metin yankı
korumasından ve uyandırma/takip/kip kurallarından geçerse yapılır. Varsayılan
`require_wake_during_output: true` kesintisiz kipte de korunur. “Dinliyorum.”
yanıtını izleyen komut uyandırmayı tekrarlamadan alınır. Eski tur ve oynatıcı
temizlenmeden yeni tur başlamaz. Kısmi STT yalnız gözlemdir; söz kesmez ve
komut çalıştırmaz. Mikrofon akışının bitmesi veya kullanıcının sesi kapatması
ayrı bir kapatma nedenidir; bekleyen görevler yine temizlenir.

Bu karar ADR 0020'nin VAD ile anında oynatma kesmesini ve ADR 0022'deki aynı
ifadeyi değiştirir. `barge_in_ms` eski yapılandırmaların okunabilmesi için
korunur; artık söz kesme eşiği olarak kullanılmaz.

**Sınırlar:** Söz kesme artık cümle sonu sessizliği ve nihai STT çözümlemesini
bekler; 180 ms yanıt garantisi yoktur. STT'nin yanlış tanıdığı ama bu kuralları
geçen ses yine komut olabilir; anlam veya konuşmacı kimliği garantisi verilmez.
Gerçek Windows mikrofon/hoparlör kabulü ayrıca yapılmalıdır.

**Doğrulama:** Gerçek denetleyici döngüsü, sahte mikrofon ve bekleyen oynatıcıyla
uzun VAD etkinliğinin, boş/uyandırmasız/yankı metninin sesi kesmediğini; kabul
edilen nihai komutun eski görevi temizleyerek söz kestiğini sınar. Kesintisiz
kip, uyandırma ayarı, kısmi STT, “Dinliyorum.” ve akış sonu ayrı sınanır.

2026-10-24 yerel doğrulaması (Linux, sahte ses aygıtları):

- Ses çekirdeği: `60 passed`; ses adaptörleri/factory/yankı/config dahil hedefli
  küme: `174 passed, 3 skipped in 1.02s`.
- Ruff: `All checks passed!`; mypy: `Success: no issues found in 102 source files`.
- Sahte kabul senaryoları 39/39; tehlikeli hata ve onaysız yan etki 0.
- Eski VAD iptal bloğu geçici kopyaya geri konduğunda yeni sekiz regresyon
  örneğinin tamamı oynatmanın korunması kontrolünde kırıldı; üretim kodu korunmuştur.
- Genel test koşusu: `2 failed, 1643 passed, 6 skipped, 4 deselected in 72.02s (0:01:12)`.
  İki hata `tests/test_mcp_client.py` süreç sonlandırma kontrollerindedir; değişmemiş
  `765c5cb` ana dalında da `2 failed in 7.72s` ile yeniden üretildi. Kapanmış alt
  süreçler bu container'da PID 1 altında zombi durumunda kalıyor. MCP kodu değiştirilmedi.
  Genel koşudan sonra eklenen son ses testi hedefli kümede doğrulandı.
- İsteğe bağlı ses bağımlılıkları/model koşulları nedeniyle atlanan testler gerçek
  ses kabulü sayılmaz. Ücretli çağrı, mikrofon veya hoparlör kullanılmadı.
