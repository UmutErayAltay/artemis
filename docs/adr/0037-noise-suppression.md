# ADR 0037 — İsteğe bağlı mikrofon gürültü engelleme (DeepFilterNet3)

**Bağlam:** Artemis'in ölçülen oda gürültüsü 0.019–0.075 (tam ölçekli RMS) aralığındaydı;
VAD ve Whisper bu seviyede boş/yanlış parça üretebiliyor. Mikrofon yolunda yankı giderme
(AEC) YOKTUR: asistanın kendi sesi mikrofona girerse bu özellik onu temizlemez (yankı
korumaları ADR 0022'dedir). Kullanıcı Krisp benzeri, RTX gerektirmeyen, ayardan açılıp
kapanan bir çözüm istedi.

**Karar:** DeepFilterNet3'ün resmi ONNX dışa aktarımı (enc, erb_dec, df_dec) onnxruntime CPU
(tek iş parçacığı) ile çalışır; STFT, ERB bantları, özellik normalizasyonu, derin filtre ve
ISTFT `libDF` (Rust) kaynağından numpy'a taşındı (`jarvis/channels/voice/denoise.py`).
`voice.noise_suppression` (varsayılan kapalı), `noise_suppression_level` ve `noise_model_dir`
config alanlarıdır; panelde onay kutusu + düzey kutusu vardır. Model dosyaları
`scripts/fetch_noise_model.py` ile bir kez indirilir; arşiv ve her dosya sha256'ya sabitlidir
(çalışma anında indirme yok, yükleme öncesi her açılışta yeniden doğrulanır).
`noise_model_dir` verilmezse `config/../models/deepfilternet3` kullanılır (betiğin varsayılanıyla aynı, cwd'den bağımsız).
Model kurulamazsa (eksik dizin, sha256 uyuşmazlığı, onnxruntime yok) ses hattı DÜŞMEZ: tek uyarı
kaydı + `denoise:unavailable:<neden>` durumu (sohbette görünür) ile ham mikrofonla sürülür.
Çalışma anında hata olursa `DenoisedMicrophone` TEK uyarı kaydı yazar, oturum boyunca ham
sese döner ve `denoise:off` durumu sohbette görünür; sessizce çalışıyormuş gibi davranmaz.

**Neden başkası değil:** RNNoise daha düşük kalitelidir. Torch'lu DeepFilterNet ağırdır ve
`deepfilterlib` için cp312 tekeri yoktur. NVIDIA Broadcast RTX gerektirir.

**Mühendislik:** ONNX grafikleri TAM DİZİ çalışır, GRU durumu dışarı verilmez. Akışta her
`block_frames` (10) yeni hop'ta son `context_frames + block_frames` hop üzerinde çıkarım yapılır,
yalnız ileri bakışı (2 hop) karşılanmış kareler tutulur; gizli durum bağlam penceresiyle
yaklaşıklanır. Grafiğin t. çıktısı t-2. kareye aittir (ölçülerek doğrulandı). Üç ölçüm
(tests/test_voice_denoise.py; noisy_snr0.wav, 10.6 sn):

- Çevrimdışı yol ↔ resmi Rust `deep-filter -D` çıktısı: **65.15 dB** SNR (hedef ≥ 30).
- Akış yolu ↔ çevrimdışı (16 kHz): bağlam 50 → 11.8 dB, 100 → 17.6 dB, 300 → 22.8 dB,
  tüm dosya (1000) → 73.6 dB (yani akış mekaniği tam; fark yalnız GRU durumunun yaklaşıklığı).
- SI-SDR (temiz referansa göre): gürültülü 6.1 dB → çevrimdışı 20.0 dB.

**Sonuçlar:**

- Varsayılan bağlam 100: 50'de akış çevrimdışıdan belirgin kötü (11.8 dB); 100 ≈ %19 CPU ile
  makul ara nokta (17.6 dB; SI-SDR akış 17.2 dB, çevrimdışı 20.0, gürültülü 6.1). 300 → 22.8 dB
  ama ≈ %35+ çekirdek.
- Gecikme (varsayılan blok 10, bağlam 100): 114 ms = 30 ms STFT+ileri bakış, 80 ms blok
  toplama (en kötü durum, hop başına 10 ms), 4 ms yeniden örnekleme FIR. Blok küçülürse gecikme
  ve ardışık çıkarım yükü artar/azalır (blok 5: 74 ms, blok 2: 34 ms).
- CPU (1 iş parçacığı, bu makine): bağlam 50 ≈ %12 çekirdek (kare başına 2.4 ms ort.),
  bağlam 100 ≈ %19 (3.8 ms). Çıkarım blok başına bir kez olduğundan en kötü kare 28–32 ms
  sürebilir; ses iş parçacığında (`asyncio.to_thread`) çalışır, olay döngüsünü bloklamaz.
- ASR ödünleşimi: güçlü bastırma Whisper doğruluğunu düşürebilir (konuşma bozulur). Düzeyler
  bastırma sınırıdır (libDF `atten_lim_db`): hafif 12 dB, orta 24 dB (varsayılan), güçlü
  sınırsız. Bu repoda Whisper WER'i ölçülmedi; kabul için kendi mikrofonunuzla
  `bench/speech_accuracy.py` ile karşılaştırın (kural 11).
- Bilinçli sapmalar: post filter yok (libDF'te de varsayılan kapalı); 5+ ardışık dijital
  sessizlik hop'unda libDF çıktıyı sıfırlayıp durumu bekletir, burada normal işlenir.
- DeepFilterNet yankı gidermez; asistan sesi için kulaklık veya ADR 0022 korumaları geçerlidir.
