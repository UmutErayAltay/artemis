# ADR 0082: İngilizce ses

- Tarih: 2026-11-18
- Durum: Kabul edildi

**Bağlam:** Arayüz ve yanıt dili (`language: "en"`) İngilizceye çevrilebilirken sesten yazıya
(STT) yalnız `"tr"`, `"tr_en"` ve `"auto"` değerlerini kabul ediyor, uyandırma karşılığı ise
Türkçe sabit metinle okunuyordu. İngilizce konuşan kullanıcının doğrudan İngilizce STT
seçebilmesi ve sesli geri bildirimleri İngilizce duyması gerekiyor.

**Karar:**

1. `VoiceConfig.stt_language` alanına `"en"` eklendi. Yerel `FasterWhisperRecognizer` bu
   değerde Whisper'a `language="en"` geçirir; bulut `OpenAICompatibleTranscriber` multipart
   formda `language="en"` gönderir. `"tr"`, `"tr_en"` ve `"auto"` davranışı değişmedi.
2. Uyandırma karşılığı (`_acknowledge_wake`) ve kip onayları `t()` üzerinden seslendirilir.
   Varsayılan `"Seni dinliyorum."` İngilizcede `"I'm listening."` olur; kullanıcının katalogda
   olmayan özel `wake_ack_text` metni `t()` geri dönüşüyle olduğu gibi okunur. PCM önbelleği
   çevrilmiş metinle anahtarlanır.
3. Uyandırma sözcüğü (`"artemis"`) değişmedi; her iki dilde aynı şekilde çalışır.
4. Yerel Piper modeli yalnız Türkçedir; İngilizce yanıtlar için kullanıcı Edge TTS
   (ör. `en-US-AriaNeural`) veya bir bulut konuşma sesi modeli seçer.
5. Araç sonucu iletileri de `t()` ile çevrilir (paralel iş paketlerinde tamamlandı).

**Sonuç:** Ses gizliliği, bulut STT/TTS izin kapıları ve uyandırma mantığı değişmeden
İngilizce sesli komut ve karşılık desteklenir.

**Arayüz ve kalan tavan:** Masaüstü Ayarlar > Ses sayfasında **Tanıma dili** (`stt_language_combo`)
açılır kutusu eklendi (`"tr"`, `"tr_en"`, `"en"`, `"auto"`); seçim `settings_io.py` üzerinden
doğrudan `voice.stt_language` olarak kaydedilir ve İngilizce arayüzde yerel Piper seçiliyse
Edge ya da bulut konuşma sesi öneren ipucu (`tts_piper_hint`) gösterilir. Yerel Piper TTS
modelinin yalnız Türkçe olması kalan tavandır; gerçek mikrofonla İngilizce canlı kabul henüz
yapılmamıştır.
