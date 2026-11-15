# ADR 0067 — Silero VAD varsayılan konuşma algılayıcı

**Bağlam:** Konuşma algılama yalnız WebRTC VAD'dı (enerji/GMM tabanlı). Konuşmasız gürültüde
(fan, klavye, şebeke uğultusu) kareleri konuşma sayıyor; bu yanlış segment açar, boşuna STT çalıştırır
ve söz kesmeyi (ADR 0040) tetikleyebilir. Gürültü engelleme (ADR 0037) varsayılan kapalı olduğundan
VAD ham mikrofonu görür.

## Karar

1. **Silero VAD v5.1.2 (MIT) pakete gömülür** (`artemis/channels/voice/models/silero/`, 2,3 MB,
   sabit sha256). İndirme adımı yok; çevrimdışı çalışır. onnxruntime zaten ses bağımlılığıdır.
2. **`voice.vad_backend: auto|webrtc|silero`, varsayılan `auto`.** `auto`/`silero` Silero'yu kurar;
   kurulamazsa (dosya yok, sha uyuşmaz, onnxruntime yok) ses hattı WebRTC ile sürer ve durum
   `vad:webrtc` olarak görünür. VAD hatası sesi asla kapatmaz. `webrtc` eski davranıştır.
3. **Akış:** 20/30 ms kareler 512 örneklik pencerelere tamponlanır, 64 örnek bağlam ve RNN durumu
   pencereler arası taşınır; karar son pencerenin olasılığı ≥ `vad_threshold` (0,5) ile verilir.
   CPU, tek iş parçacığı.

## Ölçüm (2026-11-14, bu makine)

- `python -m bench.vad_noise` (sentetik, RMS ~3000): beyaz/pembe gürültü ve 50 Hz uğultuda
  konuşma sayılan kare oranı WebRTC **1,00**, Silero **0,00**.
- Dosya güdümlü gecikme (21 tur, gerçek STT/LLM/TTS): uçtan uca medyan WebRTC 2938 ms,
  Silero 2875 ms. Gecikmeyi VAD değil sessizlik eşiği belirler (bkz. ADR 0068).

## Sonuçlar

- Gürültülü odada yanlış tetik ve gereksiz STT azalır. Gerçek mikrofonda eşik kabulü kullanıcıdadır.
- Test: `tests/test_voice_silero.py` (sahte oturumla akış/durum/bağlam, gerçek model duman testi),
  `tests/test_voice_vad_select.py` (seçim ve görünür geri düşüş), `tests/test_vad_noise.py`.
