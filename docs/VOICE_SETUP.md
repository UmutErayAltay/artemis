# Artemis — Windows ses kurulumu ve ölçüm rehberi

Windows PowerShell oturumunda önce UTF-8 çıktısını açın:

```powershell
$env:PYTHONUTF8 = '1'
```

Benchmark raporları Türkçe JSON içerir. Bu ayar, özellikle çıktıyı dosyaya
yönlendirirken Windows yerel kod sayfasının UnicodeEncodeError vermesini önler;
yeni PowerShell oturumunda yeniden ayarlanmalıdır.

## Donanım ve başlangıç tercihi

Kullanıcının bilgisayarı 32 GB RAM ve RTX 4050 içeriyor. İlk profil yerel Türkçe
STT için CUDA/float16'dır. Bu donanımın gerçek VRAM kapasitesi, sürücüsü ve ses
gecikmesi bu ortamda ölçülmedi. Model boyutu ve ses eşikleri yapılandırmadan
gelir; performans sonucu gibi sunulmaz.

faster-whisper güncel CUDA yolunda CUDA 12 cuBLAS ve CUDA 12 için cuDNN 9 ister.
Bu gereksinim ekran kartının varlığıyla otomatik sağlanmaz. Kurulumu resmi
[faster-whisper rehberi](https://github.com/SYSTRAN/faster-whisper#gpu)'nden ve
NVIDIA belgelerinden kontrol edin. CPU profili `device=cpu`, `compute_type=int8`
olabilir; uygulama GPU hatasını sessizce CPU başarısı diye sunmaz.

## İsteğe bağlı paketler ve modeller

Ses paketi, CLI çekirdeğinden ayrı kurulur:

```powershell
python -m pip install -e ".[dev,voice]"
New-Item -ItemType Directory -Force models
```

Model indirmeleri yalnız bu hazırlık aşamasında açıkça yapılır. Uygulama
çalışırken model indirmez; model eksikse kurulum hatası verir. Anahtar gerekmez ve
bu indirmeler bir model API çağrısı değildir.

STT için resmi faster-whisper dönüştürülmüş modelini yerel dizine hazırlama
örneği (kurulum ağ erişimi kullanır):

```powershell
python -c "from huggingface_hub import snapshot_download; snapshot_download(repo_id='Systran/faster-whisper-small', local_dir='models/whisper-small')"
```

Türkçe Piper sesi için model ve eşlik eden `.onnx.json` birlikte gerekir:

```powershell
python -m piper.download_voices tr_TR-dfki-medium --data-dir models
```

İndirdiğiniz sesin model kartını okuyun. Yerel Piper mikrofon sesini buluta
göndermez; yanıt metnini yerelde PCM'e çevirir. Gerçek Türkçe telaffuz ve ilk ses
gecikmesi bilgisayarda ayrıca ölçülmelidir.

`config/config.yaml` içindeki ses bölümünü hazırladığınız dosyalara ayarlayın.
Göreli yollar config dizinine göre çözülür:

```yaml
voice:
  wake_word: artemis
  stt_model: ../models/whisper-small
  stt_device: cuda
  stt_compute_type: float16
  stt_language: tr_en
  stt_beam_size: 5
  stt_partial_beam_size: 1
  stt_hotwords: []
  tts_backend: piper
  piper_model: ../models/tr_TR-dfki-medium.onnx
  piper_config: ../models/tr_TR-dfki-medium.onnx.json
  allow_cloud_tts_text: false
```

Mevcut beyin modeli kurulumu ayrıca gerekir; README'deki `primary_model` ve
sağlayıcı ortam değişkenlerini tamamlayın. Ses, masaüstü uygulamasının içinde
çalışır (`.[desktop,voice]` birlikte kurulur); mikrofonu panelde **Ses** düğmesi
ya da şu komut açar:

```powershell
python -m artemis desktop --ses --config config/config.yaml
# `python -m artemis voice --config ...` aynı komutun kısayoludur
```

Yerel modeller, aygıt ve sürücü hazır değilse bu komutun çalıştığını varsaymayın.
Edge denemesinde Türkçe ses kimliğini güncel `edge-tts --list-voices` çıktısından
seçip `voice.edge_voice` alanına yazın; `voice.tts_backend: edge`, FFmpeg yolu ve
`voice.allow_cloud_tts_text: true` gerekir. Bu ayar yanıt metninin dış servise
gönderilmesine izin verir.

### Canlı yazı (isteğe bağlı, ADR 0047) — Powered by mihu

```powershell
python -m pip install -e ".[stream]"   # sherpa-onnx + huggingface-hub
```

`config.yaml` içinde `voice.live_transcript: true` ile birlikte `live_transcript_model` ve
`live_transcript_revision` değerlerini `config/config.example.yaml`'daki örnekten alın. Ağırlıklar
ilk açılışta bu **sabit revision**'dan indirilir (model kimliği koda gömülü değildir); sonra
önbellekten çalışır. Kurulum eksikse ses sürer, panel nedenini yazar. Model mihu Community
License v1.0 altındadır: şartlar için [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md).

## Gürültü engelleme (isteğe bağlı, ADR 0037)

DeepFilterNet3, işlemcide çalışır (ekran kartı gerekmez) ve mikrofon sesini STT/VAD'a
gitmeden temizler. Bu bölüm yalnız gürültü engeller; akustik yankı için
[Yankı engelleme bölümüne](#yankı-engelleme-isteğe-bağlı-adr-0038) bakın.

1. `pip install -e ".[voice]"` (onnxruntime ve numpy dahildir; torch gerekmez). Model
   (`artemis/channels/voice/models/dfn3/dfn3_step.onnx`, yaklaşık 8 MB) pakete dahildir:
   İNDİRME ADIMI YOKTUR. Dosya her açılışta koda sabitlenmiş sha256 ile doğrulanır.
2. `config/config.yaml` içinde (göreli yol config dizinine göre çözülür):

   ```yaml
   voice:
     noise_suppression: true
     noise_suppression_level: orta   # hafif (12 dB) | orta (24 dB) | guclu (sınırsız)
     # noise_model_dir: ../models/dfn3   # verilmezse pakete dahil model; verilen dizinde aynı sha256'lı dfn3_step.onnx olmalı
   ```

   Ya da ayarlar panelinde **Gürültü engelleme** kutusunu işaretleyip düzeyi seçin;
   **Uygula** sesi yeniden başlatır ve değişiklik o zaman geçerli olur.

Gürültü engelleme açıkken mikrofon yerel 48 kHz yakalanır (modelin doğal hızı; 8 kHz üstü
bilgi korunduğundan kalite 16 kHz yakalamaya göre daha yüksektir) ve 16 kHz'e indirilerek
sonraki aşamalara verilir. Aygıt 48 kHz'i açamazsa günlüğe tek uyarı yazılır ve 16 kHz'e
dönülür. Varsayılan olarak temizlenmiş ses yalnız konuşma algılamaya (VAD, cümle bitişi, söz kesme,
seviye göstergesi) gider; konuşma tanıma (Whisper) aynı aralığın ham sesini alır, çünkü ölçümlerde
temizlenmiş ses Whisper'ı çoğunlukla kötüleştirdi (ADR 0037). `voice.noise_suppression_stt: true`
(panelde "Tanımaya da temizlenmiş ses ver") tanımaya da temizlenmiş ses verir. Gecikme yaklaşık 32–34 ms'dir (30 ms STFT+ileri bakış, 2–4 ms yeniden örnekleme). Model eksik,
bozuk veya onnxruntime yoksa ses hattı düşmez: sohbette "Gürültü engelleme açılamadı: <neden>" iletisi çıkar ve ham mikrofonla sürülür. Çalışma sırasında model hata verirse
oturum boyunca ham sese dönülür ve sohbette görünür bir ileti çıkar. Ölçüm:
`python -m bench.denoise_rtf`. Geliştirme: modeli yeniden üretmek için
`scripts/export_stateful_dfn.py` (torch venv'inde; çalışma anı bağımlılığı değildir), resmi
tam dizi grafiklerle parite testi için `scripts/fetch_noise_model.py` ve
`ARTEMIS_DFN_MODEL_DIR`.

## Yankı engelleme (isteğe bağlı, ADR 0038)

Yankı engelleme, asistanın hoparlörden oynattığı sesin mikrofon yolunda oluşturduğu
yankıyı azaltır; böylece söz kesme ve kesintisiz modda asistanın kendi sesini
kullanıcı komutu sanma riski düşer. İşlem tamamen yereldir; ses sunucusuna
bağlanılmaz. Özellik varsayılan kapalıdır.

Panelde **Yankı engelleme (hoparlörle kullanırken)** kutusunu işaretleyin veya
`config/config.yaml` içinde `voice.echo_cancellation: true` ayarını yapın:

```yaml
voice:
  echo_cancellation: true
  echo_delay_ms: 50
```

`voice.echo_delay_ms`, çıkış ile giriş arasındaki gecikmeyi AEC3'e ipucu olarak
verir; varsayılanı 50 ms'dir. Oda ve aygıt gecikmesi farklıysa değeri ayarlayın;
hatalı ipucu yankı azaltımını düşürebilir. Kulaklıkla çalışıyorsanız bu özelliğe
gerek yoktur; kapalı bırakın.

`livekit` eksikse veya çalışma anında hata verirse tek uyarı yazılır. Bu oturumda
ham ses olduğu gibi iletilir ve sohbette `echo:off` durumu gösterilir. Hoparlör
çıkışını referansa beslerken oluşan hata oynatma akışını kesmez.

## Gizlilik ve kullanım

Canlı yazı açıkken ses yine makineden çıkmaz: model yerelde, işlemcide çalışır; yalnız ilk
açılışta model dosyaları indirilir (ses gönderilmez).

Uyandırma sözcüğü **artemis**. Ham mikrofon kaydı diske yazılmaz. Mikrofon yalnız
ses komutu açıkken kullanılır; kendiliğinden başlangıçta açılmaz. STT kısmi
metinleri komut yürütmez. Kesinleşen komutlar normal denetim kaydına girebilir;
bu log kişisel veri içerdiğinden yerelde tutulur.

Onay gerektiren adımda ekrandaki pencere tool adını, tüm doğrulanmış argümanları ve
taint uyarısını gösterir. Yalnız o pencerede tıklanan açık onay geçerlidir
(varsayılan Hayır). Sesli “evet” onay vermez. Onay beklerken mikrofon duraklar ve
biriken ses sonraki komuta taşınmaz.

Edge TTS yalnız açık gizlilik ayarıyla kullanılabilir. Yanıt metni Edge servisine
gider; aktarım denetlenir. Bu yol ayrıca yerel FFmpeg gerektirir. FFmpeg yolu
yapılandırılır, shell komutu olarak çalıştırılmaz; tüm yanıtı biriktirmek yerine
MP3 akışı PCM'e çevrilir. İptal sırasında yerel yedeğe geçilip konuşmaya devam
edilmez. Varsayılan ağsız Piper yoludur.

Kesin STT varsayılanı beam 5, kısmi gözlem beam 1 kullanır; `stt_language: auto`
otomatik dil seçimini dener, fakat Türkçe ve İngilizceyi cümle içinde doğru
değiştirmeyi garanti etmez. `stt_hotwords` isteğe bağlı, sınırlı yerel kelime
ipuçlarıdır; boş liste genel kullanım başlangıcıdır. Bu ipuçları her kullanıcıya
veya modelin bilmediği özel adlara yönelik doğruluğu kanıtlamaz. STT'ye prompt
verilmez ve tanınan metne sonradan yaklaşık uygulama adı eklenmez. `stt_alias_hotwords:
true` yalnız "hayır, X'i kastettim" diye öğretilmiş uygulama adlarını ipucuna ekler
(varsayılan kapalı, ADR 0044); etkisi önce `bench/speech_accuracy.py --ek-hotword` ile
ölçülmelidir.

İlk doğrulamayı kulaklıkla yapın. Akustik yankı giderme artık isteğe bağlı bir
seçenek olarak vardır (ADR 0038); ardından hoparlörle, yankı engelleme açıkken
doğrulayın. Özellik kapalıyken hoparlör sesi VAD tarafından yeni konuşma sayılıp
yanlış söz kesme üretebilir.

## Windows elle kabul kontrolü

Gerçek WAV kayıtlarıyla doğruluk ölçüm şeması, konuşmacı ayrımı ve yorumlama için
[genel konuşma kabul planına](SPEECH_ACCURACY_PLAN.md) bakın. Repo içindeki örnek
manifest yalnız kayıt planıdır ve WAV dosyaları içermez; gerçek WAV olmadan
doğruluk sonucu üretilmez. Kayıtlar 16 kHz mono PCM16 WAV olmalı ve manifestteki
yollar manifest dizininin altında bulunmalıdır. Benchmark ham ses veya transcript
çıktıya yazmaz; yalnız yerel STT çalıştırır, LLM'e veya araca gitmez:

```powershell
python -m bench.speech_accuracy --sahte --corpus bench/speech_accuracy_corpus.yaml
python -m bench.speech_accuracy --canli --config config/config.yaml --corpus bench/speech_accuracy_corpus.yaml
```

Sahte kip ölçüm hattını sınar, tanıma doğruluğunu ölçmez. Canlı kip WAV ve önceden
kurulmuş yerel modeli okur. `expected_apps` yalnız transcript'te uygulama adının
geçip geçmediğini etiketler; açma niyetini belirtmez. Uygulama adı bulma sonucu
kelime sınırı ve sınırlı Türkçe ek kuralıyla yaklaşık ölçülür. WER/CER, uygulama
adı etiketleri, sessizlikte metin ve süreler niyet veya araç başarısı değildir.

1. “artemis saat kaç” ve “artemis” ardından ayrı komutla uyanma akışını deneyin.
2. Takip penceresinde tekrar uyandırmadan soru sorun; pencere dolduktan sonra
   sözcüksüz konuşmanın tool çalıştırmadığını doğrulayın.
3. Gürültü, boş ses ve “artemis” içeren daha uzun sözcüklerin komut üretmediğini
   kontrol edin. Yanlış ve kaçırılmış uyanmaları ayrı kaydedin.
4. Yanıt konuşulurken söze girin; eski sesin durduğunu ve eski turun yeni adım
   başlatmadığını doğrulayın. Model beklerken de iptali deneyin.
5. Geçici çalışma alanında silme isteyin; onay penceresinde argümanları kontrol edip
   Hayır deyin. Ardından sesle “evet” söyleyin; silme gerçekleşmemeli.
6. Mikrofon/oynatıcı hatası, eksik model, CUDA hatası ve Edge bağlantı hatasında
   dürüst hata/fallback kaydını kontrol edin. Sırlar veya ham kayıt bulunmamalı.
7. Aynı ücretsiz model, ses aygıtı ve STT/TTS profiliyle en az 20 yanıt ölçün.
   Isınma turunu ayrıca işaretleyin; başarısız/iptal/sessiz turlar raporda kalmalı.

Ölçüm komut konuşmasının bitişi ile ilk PCM oynatıcı olayı arasındadır;
sentez başlaması ilk ses sayılmaz. Medyan/p90 ve örnek sayısıyla birlikte STT,
model ve TTS sürelerini raporlayın. Fiziksel duyulabilirlik kulaklıkta elle
doğrulanır. F3 hedefi medyan < 1.5 saniyedir; Linux sahte test bu hedefin
sağlandığını kanıtlamaz.

Sahte orkestrasyon ölçümü mikrofon açmaz:

```powershell
python -m bench.voice_latency --sahte --iterations 20
```

Kurulumu tamamlanmış Windows makinesinde canlı ölçüm mikrofonu ve hoparlörü
açar; beyin sağlayıcısının normal token tüketimi olur. `allow_paid: false`
korunmalıdır. Çalışma süresince kulaklıkla birden fazla komut söyleyin:

```powershell
python -m bench.voice_latency --canli --config config/config.yaml --duration 120
```

Isınma dahil profilini ve başarısız/iptal/sessiz turları raporla birlikte
değerlendirin. Yanlış uyanma veya yanlış onay sayısı elle etiketlenmediyse
ölçülmemiştir; boş değer sıfır başarı anlamına gelmez.

## Resmi kaynaklar — 2026-10-13 kontrolü

- [faster-whisper](https://github.com/SYSTRAN/faster-whisper)
- [sounddevice](https://python-sounddevice.readthedocs.io/)
- [Piper Python API](https://github.com/OHF-voice/piper1-gpl/blob/main/docs/API_PYTHON.md)
- [Türkçe Piper model dosyaları](https://huggingface.co/rhasspy/piper-voices/tree/main/tr/tr_TR/dfki/medium)
- [Edge TTS](https://github.com/rany2/edge-tts)
- [WebRTC VAD wheels](https://github.com/daanzu/py-webrtcvad-wheels)
- [mihu turkish-stt-66m-v2](https://huggingface.co/mihuai/turkish-stt-66m-v2) ve
  [mihu Community License v1.0](https://huggingface.co/mihuai/turkish-stt-66m-v2/blob/main/LICENSE)
- [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx)

PyPI sürümleri hazırlık sırasında doğrulandı: faster-whisper 1.2.1,
sounddevice 0.5.6, edge-tts 7.3.0, piper-tts 1.8.0,
webrtcvad-wheels 2.0.14.post1. Bunlar canlı ses ölçümü sonucu değildir.
