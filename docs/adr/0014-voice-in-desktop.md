# ADR 0014 — Ses masaüstü uygulamasında, onay ekranda

**Bağlam:** Ses ayrı bir terminal kanalıydı (ADR 0008): onay `input()` ile
klavyeden alınıyordu, eller serbest kullanımda kullanıcı terminale dönmek
zorundaydı. Masaüstü paneli (ADR 0011) aynı runtime'ı kullanıyor ama mikrofonu
bağlamıyordu. İşlem adımları ham olay adlarını ("privacy decision", `read_file`)
gösteriyordu.

**Karar:**
- `VoiceController` masaüstü işçisinin asyncio döngüsünde çalışır; ikinci runtime
  veya ikinci süreç yoktur. Mikrofon yalnız paneldeki "Ses" düğmesiyle ya da
  `artemis desktop --ses` ile açılır; varsayılan kapalıdır, açıkken durum ekranda
  görünür. STT/TTS hazırlığı işçide yapılır; model yoksa ses dürüst bir hatayla
  kapalı kalır, uygulama çalışmaya devam eder.
- Sesli komut da köprünün istek modelinden geçer: her komut bir `request_id` alır,
  yazılı istekle aynı anda yürümez, onayı aynı onay aracısıyla ekrana gelir.
  Sohbette "Siz (ses)" balonu olarak görünür. Onay sırasında mikrofon duraklatılır,
  biriken ses atılır; sesli "evet" yine onay değildir.
- `VoiceController` durumunu bildirir: kapalı, dinliyor, düşünüyor, konuşuyor.
  Overlay ve panel bu durumu gösterir.
- `artemis voice` komutu masaüstünü ses açık başlatır; terminal onayı ses için
  kalkar. Yazılı CLI (`artemis chat`) terminal onayını korur.
- Araç sınıfı okunur Türkçe ad bildirir: `label` (onay başlığı, ör. "Dosya silme")
  ve `done_label` (adım satırı, ör. "Dosya okundu"). Ayrı ad tablosu tutulmaz;
  bildirmeyen araçta ham ad görünür. İşlem adımları tur başına sonuç satırlarıdır
  (✓ doğrulanmış başarı, "gönderildi" doğrulanmamış komut, ✗ hata/ret); iç olaylar
  (model yanıtı, gizlilik kontrolü) yalnız ayrıntı açılınca görünür. Onay
  başlığında okunur ad büyük, ham ad küçük yazılır; tam argümanlar değişmeden
  gösterilir.

**Sonuçlar:** Eller serbest kullanımda onay için terminale dönülmez, ama onay yine
ekranda tıklama ister. Masaüstü olmadan ses kullanılamaz (PySide6 gerekir).
Mikrofon, VAD ve oynatma gerçek Windows'ta denenmeden ses gecikmesi veya onay
odağı iddia edilmez; Linux testleri sahte mikrofon/ses adaptörleriyle orkestrasyonu
sınar.
