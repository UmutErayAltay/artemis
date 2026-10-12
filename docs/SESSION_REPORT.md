# İlk oturum raporu — F0 + F1

Tarih: 12 Ekim 2026 UTC. Çalışma alanı: `/workspace/jarvis`.
Teslimat yerel Git deposundadır; kullanıcının sonraki talebi gereği **push yapılmadı**.
Mimari ve ilk dört ADR, uygulama kodundan önce yazılıp ayrı commit edildi.
Kod bağımsız görevlerle ajanlara dağıtıldı; kullanıcının model tercihi sonrasında
kodlama/düzeltme işleri `gpt-6-luna` ajanlarına verildi. Ana ajan entegrasyonu ve
son güvenlik incelemesini yürüttü.

## F0 sonucu

Python 3.12 paket iskeleti, sabit bağımlılıklar, pydantic ayarlar, JSONL denetim,
SQLite günlük maliyet rezervasyonları ve Ubuntu/Windows GitHub Actions matrisi
hazır. Sahte kabul koşucusu aynı CommandRunner/AgentLoop/SafetyGate yolunu kullanır.

Yerel Linux doğrulaması geçti. Push yapılmadığından GitHub Actions çalıştırılmadı;
**uzak CI ve Windows sonucu doğrulanmış değildir**. Bu nedenle F0'ın “CI yeşil”
bitiş şartının uzak çalıştırma kısmı açık kalır.

## F1 sonucu

Üç sağlayıcı, süreli Ollama yedeği, ajan durma koşulları, tek güvenlik kapısı,
onay/taint, güvenli yollar, gizlilik maskesi, 15 temel araç ve Türkçe CLI hazır.
Araç şemaları ve sağlayıcı dönüşümleri kayıt sisteminden otomatik üretilir.
CLI `python -m jarvis chat --config config/config.yaml` ile başlar.
Örnek config sır veya tahmin edilmiş model/fiyat içermez; gerçek kullanım için
resmi kaynaktan doğrulanan model kaydı eklenmelidir.

Sahte kabul ölçümü sonuçları:

| Kategori | Senaryo | Geçen | Çalıştırma | Tehlikeli hata |
| --- | ---: | ---: | ---: | ---: |
| Bağımlı çok adım | 8 | 8 | 24 | 0 |
| Belirsizlik | 3 | 3 | 9 | 0 |
| Tehlikeli işlemler | 4 | 4 | 12 | 0 |
| Prompt injection | 5 | 5 | 15 | 0 |
| Basit tek adım | 7 | 7 | 21 | 0 |
| Gürültü/anlaşılmayan girdi | 2 | 2 | 6 | 0 |
| **Toplam** | **29** | **29** | **87** | **0** |

`--sahte --tekrar 3`: onaysız yan etki **0**, toplam model adımı **231**,
tur süresi medyan **6.532 ms**, p90 **27.380 ms**. Girdi/çıktı token **0/0**,
model maliyeti **0 USD**. Bunlar senaryolu sağlayıcıyla orkestrasyon ölçümleridir;
gerçek modelin anlama başarısı veya ağ gecikmesi değildir. Süreye fixture kurulumu
dahil değildir. Her senaryonun başarı, onay, adım, süre, token ve maliyet ayrıntıları
yerel `reports/f0-f1-fake.json` içindedir.

## Denetim çıktıları

İstenen komutların son satırları birebir:

```text
pytest -q
252 passed in 3.19s

ruff check .
All checks passed!

mypy
Success: no issues found in 34 source files
```

Ham çıktılar `reports/pytest.txt`, `reports/ruff.txt`, `reports/mypy.txt` altında
tutuldu. Log/rapor dizini `.gitignore` kapsamındadır.

Güvenlik kontrolleri geçici bağımsız kaynak kopyalarında bozuldu:

| Mutasyon | Özgün test çıkışı | Bozulmuş test çıkışı | Sonuç |
| --- | ---: | ---: | --- |
| Taint sonrası onayı kaldır | 0 | 1 | Assertion hatasıyla yakalandı |
| Reddedilen onayı atla | 0 | 1 | Assertion hatasıyla yakalandı |
| Başarısız dosya işlemini başarı göster | 0 | 1 | Assertion hatasıyla yakalandı |

**3/3 mutasyon yakalandı.** Import/toplama hataları başarı sayılmadı. Özgün kaynak
hash'leri değişmedi. Tekrar üretim: `python scripts/check_mutations.py`.
Yöntem [VALIDATION.md](VALIDATION.md), ham sonuç `reports/mutations.json` içindedir.

## Canlı ölçüm ve bilinçli sınırlar

- `OPENROUTER_API_KEY` ve `ANTHROPIC_API_KEY` ortamda yok. İki ücretsiz modelle
  canlı kabul ölçümü **yapılmadı**; canlı başarı/gecikme sayısı üretilmedi ve hiçbir
  ücretli model çağrısı yapılmadı. Resmi katalog/API doğrulaması
  [PROVIDERS.md](PROVIDERS.md) içinde kaynaklarıyla kayıtlıdır.
- Windows uygulama/pano/tarayıcı davranışı gerçek Windows makinesinde elle
  denenmedi. Testlerde gerçek masaüstü yerine izole adaptörler kullanıldı.
- Dosya/pano içeriğinin buluta gönderimi varsayılan kapalıdır. Bu ayar açılmadan
  bulut modeli içerik özeti veremez; yerel sağlayıcı içeriği görebilir.
- CLI her komutu bağımsız tur olarak işler. Kalıcı hafıza ve önceki komuta bağlı
  sohbet bağlamı yoktur; F4'ün yerine geçmişi varmış gibi davranılmaz.
- Zaman aşımı yeni adımları durdurur. Başlatılmış OS iş parçacıkları zorla iptal
  edilemez; kullanıcıya tamamlanmanın doğrulanamadığı söylenir. Süreç yalıtımı ve
  daha güçlü iptal F2'de ele alınmalıdır.
- DuckDuckGo HTML araması resmi arama API'si değildir; şema/challenge değişiminde
  açık hata verir. Sonuç sayfası gövdesini indiren ayrı araç F1'de yoktur.
- Gate aynı süreçteki düşmanca Python plugin'ine karşı sandbox değildir.
  Eşzamanlı yerel symlink değiştirme yarışına karşı OS düzeyinde tam koruma yoktur.
  Dosya içeriği ve model sonucu sınırlıdır; çok büyük dizin listeleme için
  sayfalama/sınırlandırma F2 sağlamlık çalışmasına bırakılmıştır.

## Uygulayıcı seçimleri ve sonraki faz

Mekanik seçimler: async httpx; UTF-8 JSONL; UTC günlük SQLite bütçe; bilinmeyen
tüketimde rezervasyonu gün sonuna kadar koruma; uygulamalarda config izin listesi;
çocuk süreçlere yalnız OS/profil/GUI ortam izin listesi; var olan dosyanın üstüne
yazmama; model/tool çıktıları için katı şema.

Web araması dışarı sorgu gönderdiği için yan etkili sayıldı. Dosya değiştirme
yollarında symlink reddedildi. Onayda araç açıklaması da gösterildi. Gerekçeler
[ADR 0005](adr/0005-outbound-and-symlinks.md) içindedir. Uygulama kapatma aynı
`.exe` adına uyan tüm süreçlere normal kapatma isteği yollar ve sonucu doğrular;
zorlamalı `/F` kullanılmaz.

F2 önceliği: gerçek Windows doğrulaması; süreç yalıtımı ve iptal; hata enjeksiyonu;
geri alma kaydını kullanıcıya sunma; büyük dizin ve denetim logu sınırlandırma.
Anahtar sağlandığında iki ücretsiz model aynı kabul setinde ölçülmeli; model
seçimi özellikle tehlikeli hata sayısına göre yapılmalıdır. Ses, UI, hafıza,
Telegram veya arka plan ajanları bu teslimata eklenmemiştir.
