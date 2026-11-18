# ADR 0080: Ses modeli türü ve model listesinden bulut konuşma sesi

- Tarih: 2026-11-18
- Durum: Kabul edildi (kullanıcı: "bulutu seçince konuşma sunucusu kendimiz ekleyebilelim,
  normal model ekleme gibi ... resim modeli gibi ses modelini de ekleyebilelim ve filtrelerde
  görsel, ses ve yazı modelleri diye ayrılsın")

**Bağlam:** Bulut konuşma sesi (ADR 0047 eki, `voice.tts_cloud_*`) Ayarlar > Ses'te ayrı bir
sunucu + protokol + model alanıyla seçiliyor; Ayarlar > Model'de eklenen modellerle bağı yok.
Kullanıcı ses modelini de resim modeli gibi model listesine eklemek, listeyi türe göre
süzmek ve Ses panelinde bu listeden seçmek istiyor.

**Karar:**

1. `ModelConfig.kind` üçüncü değeri alır: `"speech"` (konuşma sesi, TTS). Resim modeli gibi
   yalnız `openai_compatible` sağlayıcı + uç ile tanımlanır. Ek alan `speech_protocol:
   "openai" | "gemini"` (varsayılan `openai`) yalnız ses modelinde anlamlıdır.
2. Sohbet dışı her model (`image`, `speech`) beyin, zor işler, bilgisayar kullanımı (ve
   yedekleri) ya da arıza yedeği olamaz; doğrulama tek yerde `Settings`'tedir. Model testi
   ses modelini sohbet senaryolarıyla koşmaz.
3. Çalışma zamanı değişmez: bulut TTS hâlâ `voice.tts_cloud_endpoint/protocol/model`
   okur. Ses panelindeki "Kayıtlı ses modeli" seçimi bu üç alanı seçilen modelden doldurur;
   elle ayar yolu geriye dönük uyum için kalır. Gizlilik izni (`tts_cloud_consent`) ve ücretli
   izin (`tts_cloud_allow_paid`) yine Ses panelindedir; model eklemek metin gönderme izni
   vermez.
4. Model listesi Tümü / Yazı / Görsel / Ses filtresiyle süzülür; seçim sıralama gibi yerel
   ayarda hatırlanır.

**Sonuç:** Yeni çalışma zamanı yolu ve yeni izin yok; tek kaynak yine `config.yaml`. Ses
modelinin fiyatı jeton başına değil karakter başına olduğundan fiyat alanları ses modelinde
gizlidir ve ücret kapısı Ses panelindeki ücretli izindir.

**Tavan:** Sesten yazıya (STT) bulut modeli bu türe taşınmadı (`voice.stt_cloud_*` ayrı
kalır); ihtiyaç olursa `kind: "transcription"` aynı kalıpla eklenir.
