# ADR 0037 — İsteğe bağlı mikrofon gürültü engelleme (DeepFilterNet3)

**Bağlam:** Artemis'in ölçülen oda gürültüsü 0.019–0.075 (tam ölçekli RMS) aralığındaydı;
VAD ve Whisper bu seviyede boş/yanlış parça üretebiliyor. Mikrofon yolunda yankı giderme
(AEC) YOKTUR: asistanın kendi sesi mikrofona girerse bu özellik onu temizlemez (yankı
korumaları ADR 0022'dedir). Kullanıcı Krisp benzeri, RTX gerektirmeyen, ayardan açılıp
kapanan bir çözüm istedi.

**Karar:** DeepFilterNet3, resmi checkpoint'ten türetilmiş DURUMLU bir ONNX adım modeliyle
(`jarvis/channels/voice/models/dfn3/dfn3_step.onnx`, yaklaşık 8 MB, pakete dahil) onnxruntime CPU
(tek iş parçacığı) üzerinde çalışır: her 10 ms'lik hop için model bir kez çalışır, GRU gizli
durumları ve nedensel evrişim geçmişi açık girdi/çıktıdır. STFT, ERB bantları, özellik
normalizasyonu, derin filtre ve ISTFT `libDF` (Rust) kaynağından numpy'a taşındı
(`jarvis/channels/voice/denoise.py`). `voice.noise_suppression` (varsayılan kapalı),
`noise_suppression_level` ve `noise_model_dir` config alanlarıdır; panelde onay kutusu + düzey
kutusu vardır. Model depoda durur, İNDİRME YOKTUR; kodda sabitlenmiş sha256 ile her açılışta
doğrulanır. `noise_model_dir` verilmezse paket içi dizin kullanılır (tek sabit:
`jarvis/config/model_paths.py`; cwd ve config konumundan bağımsız); verilirse aynı hash'li
`dfn3_step.onnx` aranır, farklı dosya ValueError ile reddedilir.
Model kurulamazsa (eksik dosya, sha256 uyuşmazlığı, onnxruntime yok) ses hattı DÜŞMEZ: tek uyarı
kaydı + `denoise:unavailable:<neden>` durumu (sohbette görünür) ile ham mikrofonla sürülür.
Çalışma anında hata olursa `DenoisedMicrophone` TEK uyarı kaydı yazar, oturum boyunca ham
sese döner ve `denoise:off` durumu sohbette görünür; sessizce çalışıyormuş gibi davranmaz.

**Tarihçe / neden durumlu:** İlk sürüm resmi ONNX dışa aktarımını (enc, erb_dec, df_dec) kullandı;
bu grafikler TAM DİZİ çalışır ve GRU durumunu gizler. Akışta her 10 hop'ta son 110 hop yeniden
çalıştırılıyor, gizli durum 100 hop'luk bağlamla yaklaşıklanıyordu: her hop ~11 kez işlenir, akış
çevrimdışıdan 19.3 dB (SI-SDR 17.2 vs 20.0) uzaktı ve 80 ms blok toplama gecikmesi vardı.
Adım modeli bunların hepsini kaldırır. Resmi grafikler artık yalnız parite referansıdır
(`enhance_fullseq`, `OnnxGraphRunner`, `scripts/fetch_noise_model.py`).

**Adım modeli:** `scripts/export_stateful_dfn.py` (yalnız torch venv'inde çalışan geliştirme aracı;
çalışma anı bağımlılığı değildir) resmi `DeepFilterNet3.zip` checkpoint'ini (sha256 doğrulanır)
tek kare işleyen bir modüle çevirir. Durumlar dört adlı tensörde toplanır (float32, toplu boyut 1):
`gru_h [5,1,256]` (enc.emb_gru, erb_dec.emb_gru x2, df_dec.df_gru x2), `erb_hist [1,1,2,32]`
(erb_conv0 3x3 girdi geçmişi), `spec_hist [1,2,2,96]` (df_conv0 3x3), `c0_hist [1,64,4,96]`
(df_convp, `df_pathway_kernel_size_t=5`). Zaman çekirdeği 1 olan tüm diğer evrişimler (transposed
dahil) durumsuzdur; betik bunu doğrular. Başlangıç durumu sıfırdır (tam dizi modelin sıfır
dolgusuyla aynı). Betik, dışa aktarmadan ÖNCE adım modelini tam diziye karşı doğrular ve 1e-4 aşılırsa
yazmaz: torch `DfNet.forward` karşısında en büyük mutlak hata m 1.2e-6, coefs 1.0e-6, lsnr 2.1e-5;
resmi ONNX grafikleri karşısında 1.9e-6 / 1.5e-6 / 4.2e-5; dışa aktarılan ONNX (onnxruntime) için
8.6e-7 / 1.1e-6 / 2.5e-5 (600 kare). Her GRU katmanı ve her evrişim geçmişi için durumu bozan
mutasyon bu doğrulamada hatayı 0.04–26 mertebesine çıkarır. Çıktı deterministiktir (betik iki kez
çalıştırıldığında aynı sha256), `dfn3_step.json` manifesti kaynak sha256'ları, sürümleri ve
parite sayılarını taşır; `NOTICE` MIT/Apache-2.0 ve Hendrik Schröter'i belirtir (türev çalışma).

**48 kHz yakalama:** Mikrofon 16 kHz açılınca model 8 kHz üstünü hiç görmez (SI-SDR 16.21 dB;
48 kHz çevrimdışı 20.01 dB). Bu yüzden gürültü engelleme açıkken fabrika mikrofonu yerel 48 kHz
yakalar (`SoundDeviceMicrophone(capture_rate=48000)`, 960 örnek/20 ms; yeni config alanı yok).
Kareler `AudioFrame.sample_rate=48000` ile işaretlenir; `DenoisedMicrophone`
`process_frame_48k`'yı çağırır (yükseltici FIR atlanır) ve 16 kHz/20 ms kare çıkarır:
VAD/seviye/barge-in/STT hep 16 kHz görür, `VoiceConfig.sample_rate` 16000 kalır. Aygıt 48 kHz
açılamazsa (`PortAudioError`) tek Türkçe uyarıyla 16 kHz yakalama + yükseltici yola dönülür.
Hata durumunda ham geçiş ve halka tamponu akıtımı 48k kareleri 16 kHz'e indirerek verir.
Gecikme 48k modunda 32 ms (3 hop + 2 ms indirici FIR). Ölçüm: 48k akış ↔ 48k çevrimdışı
75.73 dB (gerçek konuşma), 75.76 dB (sentetik); SI-SDR 16 kHz çıktıda 6.31 → **20.01 dB**
(16 kHz yakalamada 16.21). Bench: 48k modunda ort. 2.03–2.26 ms, en kötü 3.5–4.6 ms, CPU
%10.1–11.2 (16k modu 2.08–2.29 ms, 5.1–5.8 ms, %10.2–11.3): maliyet farkı yok.

**ASR bulgusu ve tasarım (temizlenmiş ses algılar, STT ham ses alır):** Whisper small, tr, 20 Piper
cümlesi x 2 gürültü, WER (%) SNR 0/5/10/20: ham 49.1/40.2/31.3/23.4; d12 51.9/41.1/29.4/25.2;
d24 56.1/45.8/35.5/28.0; d100 64.5/49.5/39.7/27.1. Temizlenmiş ses Whisper'ı çoğunlukla
İYİLEŞTİRMEZ, kötüleştirir. Yine de VAD/cümle bitişi için yararlıdır (vendored model, WebRTC VAD
agresiflik 2, 20 ms kareler, 48k yakalama; iki gürültü dosyası x RMS 0.01/0.02/0.05/0.1, 8 koşul
ortalaması): yalnız gürültüde kareleri "konuşma" sayma oranı ham **0.714**, d12 **0.364**, d24 **0.275**,
d100 **0.213**. `AudioSegmenter` (varsayılan end_silence 600 ms, ~8.2 sn konuşma + 2 sn önce/3 sn
sonra gürültü, iki gürültü x SNR 0/5/10 = 6 koşul): konuşma bitişi (gerçek bitişe ±1 sn, en geç 1.5 sn
sonra kapanan segment) ham sesle **1/6**, d12 3/6, d24 5/6, d100 6/6. Ham sesle VAD gürültüde hiç
sessizlik görmediği için çoğu koşulda segment 15 sn sınırına kadar kapanmaz (atılır, 0 segment).
Segment sayısı (gerçek: 1): d12 1,1,1,0,0,1; d24 1,1,1,1,2,2; d100 3,2,2,2,1,3 (güçlü bastırma
başta kısa gürültü patlaması segmentleri üretir). Bu yüzden: temizlenmiş `pcm` algılama (VAD,
cümle bitişi, söz kesme, seviye ölçer), aynı aralığın gecikme hizalı HAM sesi `AudioFrame.raw_pcm`
ile STT'ye gider (`AudioSegmenter` ham baytları, ön yuvarlanma dahil, biriktirir; 48k yakalamada ham
yol kesintisiz 48k->16k indirilmiştir). `voice.noise_suppression_stt: true` STT'ye de temizlenmiş ses
verir (varsayılan false; panelde "Tanımaya da temizlenmiş ses ver"). Gürültü engelleme kapalıyken
hiçbir şey değişmez. Düzey eşlemesi (12/24/100 dB) değiştirilmedi: d24 dengelidir (5/6 bitiş, d12'den
az yanlış alarm), d100 en iyi bitiş/yanlış alarm verir ama kısa sahte segmentler üretir.

**Hizalama ve eşikler:** İleri bakış (2 hop) modelin DIŞINDADIR: t. çıktı spektrumun t-2. karesine
aittir (resmi grafiklerle aynı sözleşme), bu yüzden t. hop gelince t-2. kare tamamlanır; ilk iki
çıktı atılır. libDF (`tract.rs`) kodlayıcıyı her karede çalıştırır, ERB/DF kod çözücüyü yalnız
`apply_stages` aşamayı uygulayacaksa. Varsayılan eşiklerde (-15/35/35) lsnr çıktısı [-15, 35]
aralığındadır; kod çözücüler hiç atlanmaz, durum ilerlemesi libDF ile aynıdır. Adım modeli kod
çözücüleri her zaman çalıştırır; yalnız özel, daha dar eşiklerde libDF'in atlanan karelerde
dondurduğu kod çözücü GRU durumları burada ilerler (uygulanan aşamalar aynıdır).

**Neden başkası değil:** RNNoise daha düşük kalitelidir. Torch'lu DeepFilterNet ağırdır ve
`deepfilterlib` için cp312 tekeri yoktur. NVIDIA Broadcast RTX gerektirir.

**Ölçümler** (tests/test_voice_denoise.py; noisy_snr0.wav, 10.6 sn; bu makine, 1 iş parçacığı):

- Durumlu çevrimdışı yol ↔ resmi tam dizi grafikler (48 kHz): **135.62 dB** SNR (en büyük mutlak
  fark 1.3e-7, yani float hassasiyeti).
- Durumlu çevrimdışı ↔ resmi Rust `deep-filter -D` çıktısı: **65.15 dB** (önceki sürümle aynı).
- Akış ↔ çevrimdışı (16 kHz, gecikme hizalı): **73.67 dB** (eski bağlam yaklaşıklığı: 17.6 dB). Kalan
  fark akışın int16 nicemlemesidir. Sentetik sinyalde 75.6 dB (CI testi, eşik 40 dB).
- SI-SDR (temiz referansa göre): gürültülü 6.1 dB → 48 kHz çevrimdışı 20.0 dB. 16 kHz giriş yolunda
  (mikrofonun gerçek yolu) çevrimdışı ve akış ikisi de **16.21 dB**: akış artık çevrimdışıyla
  birebir aynıdır; 20.0'a kıyasla fark akışın değil 16 kHz bant sınırlı girdinin etkisidir
  (model 8 kHz üstünü görmez). Eski akış aynı protokolde 17.17 dB ölçtü (çevrimdışıdan "iyi" olması
  yaklaşıklığın bir yan etkisiydi, eşdeğerlik değil).
- Gecikme: **34 ms** (30 ms STFT+ileri bakış, 4 ms yeniden örnekleme FIR), eskiden 114 ms.
- Bench (`python -m bench.denoise_rtf`, 20 sn gürültü, 20 ms kare): yeni kare başına ort. 2.2–2.4 ms,
  en kötü 3.7–5.7 ms, CPU %10.8–11.9; eski ort. 4.0–4.3 ms, en kötü 39.6–56.4 ms, CPU %20.0–21.1.
  Maliyetin ana kalemi hop başına tek model çağrısıdır (~1 ms).

**Sonuçlar:**

- Bağlam/blok ayarları kalktı; akış yolunda blok toplama, bağlam penceresi ve yeniden çıkarım yok.
  Çıkarım hop başına bir kez ve eşit sürelidir, en kötü kare 20 ms bütçesinin çok altındadır.
  Çıkarım gerçek zamandan yavaşlar veya çıktı tamponu boş kalırsa (`underruns`, `late_frames`)
  hız sınırlı Türkçe uyarı yazılır. Ses iş parçacığında (`asyncio.to_thread`) çalışır.
- Model dosyası depoda durur (`.gitignore`'daki `models/` kuralına bu dizin için istisna eklendi).
  Yeniden üretim: `scripts/export_stateful_dfn.py` (torch venv'i); yeni dosyanın sha256'sı
  `denoise.py::STEP_MODEL_SHA256` ile birlikte güncellenir.
- ASR ödünleşimi: güçlü bastırma Whisper doğruluğunu düşürebilir (konuşma bozulur). Düzeyler
  bastırma sınırıdır (libDF `atten_lim_db`): hafif 12 dB, orta 24 dB (varsayılan), güçlü
  sınırsız. Bu repoda Whisper WER'i ölçülmedi; kabul için kendi mikrofonunuzla
  `bench/speech_accuracy.py` ile karşılaştırın (kural 11).
- Bilinçli sapmalar: post filter yok (libDF'te de varsayılan kapalı); 5+ ardışık dijital
  sessizlik hop'unda libDF çıktıyı sıfırlayıp durumu bekletir, burada normal işlenir.
- DeepFilterNet yankı gidermez; asistan sesi için kulaklık veya ADR 0022 korumaları geçerlidir.
