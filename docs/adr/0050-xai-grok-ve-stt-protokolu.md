# ADR 0050 — xAI Grok bağlantısı ve bulut tanıma protokolü seçimi

**Durum:** Kabul (2026-11-02). ADR 0046'yı genişletir; SafetyGate ve gizlilik anlamı değişmez.

**Bağlam:** Kullanıcı xAI Grok'u sohbet modeli ve karma tanıma sunucusu olarak kullanmak
istedi. Sohbet ucu (`https://api.x.ai/v1`) OpenAI uyumludur. xAI'nin STT REST ucu ise
uyumlu DEĞİLDİR (resmi belge docs.x.ai, 2026-11-02): yol `/stt`, çok parçalı gövdede
`file` SON alan olmalı, `model` alanı yoktur, yanıt `text` taşır. Karma tanıma sunucusu
ve modeli yalnız YAML'da tanımlanabiliyordu.

**Karar:**
1. Model sağlayıcıları ön ayarlarına "xAI (Grok)" (`xai`) eklendi. Model kimlikleri koda
   gömülmez; kullanıcı mevcut arayüzden ekler.
2. `voice.stt_cloud_protocol: openai | xai` (varsayılan `openai`, göç gerekmez).
   `OpenAICompatibleTranscriber` adı korunur; `xai`'de `{base_url}/stt` adresine yalnız
   `language` (stt_language `tr` ise) ve son parça olarak WAV gönderilir. Denetim kaydı,
   yönlendirme yasağı, zaman aşımı, hata iletisinde URL/gövde/anahtar olmaması ve hayalet/
   tekrar süzgeci ortaktır.
3. Doğrulama: `xai`'de `stt_cloud_model` boş olabilir; `openai`'de zorunlu kalır.
4. Ayarlar > Ses'te "Karma tanıma" altında sunucu, protokol ve model alanları vardır
   (`PANEL_BINDINGS` ile Uygula/kirli takibi). `xai` sunucusu seçilince protokol önayarlanır.

**Sonuçlar:** Varsayılan davranış değişmez. Grok STT'nin Türkçeyi desteklediği belgede
doğrulanmadı; arayüz ipucu bunu söyler, hata olursa yerel metne düşülür. Anahtar olmadığından
canlı doğrulama yapılmadı; uyum yalnız loopback sahte sunucuyla sınandı. WebSocket akışı
kullanılmaz (komut sesi tek parça gider).

## Ek: anahtar girişi, model listesi, resim modeli (arayüz)

- Ayarlar > Ses > Karma tanıma: seçili sunucu için parola kipli "API anahtarı" alanı ve
  "Anahtarı kaydet" düğmesi. Anahtar Model sağlayıcıları ile aynı yere (`Artemis/<uç>`, kasa
  yoksa `ARTEMIS_<UÇ>_API_KEY` uyarısı) yazılır; `PANEL_BINDINGS`'e, YAML'a ve kirli
  izlemeye girmez, kayıttan sonra alan temizlenir, Uygula gerekmez. Anahtar isteyen sunucuda
  kayıtlı anahtar yoksa ipucu söyler (Uygula engellenmez).
- Model alanı düzenlenebilir açılır liste; "Modelleri getir" Model sağlayıcıları'ndaki
  `ConnectionTest`/`probe_models`'i (tek HTTP yoklaması) arka plan iş parçacığında kullanır.
  xAI protokolünde (REST'te model alanı yok) ve demo'da kapalıdır.
- Model ekle formuna "Tür" (Sohbet/Resim) eklendi: Resim `kind: image` ve `image_cost_usd`
  yazar, token fiyatları 0 kalır; Claude aboneliğinde seçilemez, beyin yapılamaz; listede
  "· resim" etiketi görünür. Ücretli resim için `allow_paid` ve çağrı başına bütçe
  rezervasyonu uyarısı verilir. Ekleme/kaldırma `providers.changed` ile pet üreticinin model
  listesini yeniler (zaten bağlıydı).
- Hâlâ gerçek xAI anahtarıyla canlı test yapılmadı; Grok STT'nin Türkçesi doğrulanmadı.
