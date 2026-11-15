# ADR 0074 — OpenAI uyumlu bulut konuşma sesi (`tts_backend: cloud`)

**Bağlam:** Yerel Piper ve Edge TTS dışında, OpenRouter'daki 39 konuşma modeli dahil
OpenAI uyumlu `/audio/speech` uçlarını kullanabilmek gerekir. Ücretsiz katmanlar geçici
olabilir (ör. HeyGen ücretsiz sürümü 2026-11-30'da sona erer); bu yüzden yerel yedekle
birlikte çalışabilen genel bir bulut TTS adaptörü gerekir.

## Karar

1. `voice.tts_backend: "cloud"` eklenir ve `artemis/channels/voice/cloud_tts.py` içindeki
   `CloudSpeechSynthesizer` OpenAI uyumlu `POST {base_url}/audio/speech` ucunu çağırır
   (`response_format: "pcm"`, `voice` boşsa JSON gövdesinden tamamen çıkarılır).
2. Gizlilik izni ve denetim Edge TTS (ADR 0009) ile aynıdır: `allow_cloud_tts_text: true`
   zorunludur ve göndermeden önce metin kaydedilmeden `cloud_tts_transfer` denetimi yazılır.
3. Ücret koruması: `endpoint.api_key` açıkken `:free` ile bitmeyen modeller
   `tts_cloud_allow_paid: true` olmadan reddedilir; `api_key: false` olan yerel uçlar
   ücretli sayılmaz.
4. Yalnız ham `audio/*` PCM akışı kabul edilir (`ffmpeg` süreci açılmaz). Sağlayıcı örnekleme
   hızını belgelemediğinde `tts_cloud_sample_rate` (varsayılan 24000) kalibrasyon düğmesi
   olarak kullanılır.
5. `tts_local_fallback` ve `piper_model` tanımlıysa fabrika `FallbackSynthesizer` ile sarar;
   ücretsiz model kapandığında veya hız sınırına takıldığında ses yerel Piper'a düşer.

## Sonuçlar

- Kullanıcı ayar panelinden yarım kalmış formu kaydedebilsin diye model ve izin kontrolleri
  `Settings` doğrulamasında değil adaptör başlangıcında yapılır; `Settings` yalnız
  `tts_cloud_endpoint` kimliğinin `endpoints` içinde bulunmasını doğrular.
- Bilinen eksiklik: Tur içi gizlilik kökeni (ADR 0056) şu an hiçbir bulut TTS arka ucu
  tarafından denetlenmez (Edge TTS de aynı boşluğu taşır); sonraki adımda ele alınacaktır.

## Ek: Gemini protokolü

Canlı doğrulama (2026-11-16) Gemini API'de OpenAI tarzı `/audio/speech` ucunun
bulunmadığını (404) gösterdi. `voice.tts_cloud_protocol: "gemini"` seçildiğinde:

1. İstek `POST {base_url}/models/{model}:streamGenerateContent?alt=sse` adresine gider;
   kimlik doğrulama `Authorization: Bearer` yerine yalnız `x-goog-api-key` başlığıyla yapılır.
2. Gövdede `generationConfig.responseModalities = ["AUDIO"]` gönderilir; `tts_cloud_voice`
   boşsa `speechConfig` anahtarı tamamen çıkarılır.
3. Yanıt `text/event-stream` SSE akışıdır; her `data: {json}` satırındaki
   `candidates[0].content.parts[*].inlineData` içinde base64 ham `s16le` PCM (`data`) ve
   `mimeType` (`audio/l16; rate=24000; channels=1`) gelir. Örnekleme hızı `rate=` alanından
   alınır (yoksa `tts_cloud_sample_rate` kullanılır). Canlı ölçümde ilk ses 0.72 sn'de geldi.
4. Gemini model kimliklerinde `:free` soneki olmadığından `endpoint.api_key` açıkken
   `tts_cloud_allow_paid: true` gerekir.
