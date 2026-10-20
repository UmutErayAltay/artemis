# ADR 0038 — İsteğe bağlı akustik yankı engelleme (WebRTC AEC3)

**Bağlam:** Jarvis, kendi TTS çıktısını hoparlörden duyduğunda mikrofon yolunda
akustik yankı oluşur. O ana kadar yankıyı yalnız metin düzeyinde ele alan korumalar
(ADR 0022) ve `require_wake_during_output` ayarı vardı. Bunlar asistanın kendi
sesini kullanıcı komutu sanmayı azaltır; ancak söz kesme ve kesintisiz mod bu
sorundan etkilenir. ADR 0037, AEC'nin olmadığını açıkça belirtmişti.

**Karar:** WebRTC ses işleme modülündeki AEC3, `livekit==1.1.20` Python paketinden
alınır:
`rtc.AudioProcessingModule(echo_cancellation=True, high_pass_filter=True)`.
Yalnız yerel ses işleme sınıfları kullanılır; LiveKit sunucusuna bağlanılmaz ve bu
işlemde ses dışarı gönderilmez.

Ses yolu `mikrofon → EchoCancelledMicrophone → DenoisedMicrophone → STT` olur.
Gürültü engelleme açıkken mikrofon 48 kHz, kapalıyken 16 kHz ile çalışır. AEC,
gürültü temizlemeden önce çalışır; yankı iptal edilen kare `raw_pcm` olarak
yeniden sunulmadığı için STT'ye giden ham yol da yankı iptal edilmiş sesi taşır.

`SoundDeviceSink`, hoparlöre yazdığı her ses bloğunu `EchoReference` tamponuna
besler. Referans mono PCM16'ya çevrilir, livekit'in yeniden örnekleme yordamıyla
yakalama hızına uyarlanır ve en fazla 2 saniye tutulur. `EchoCanceller`, her
10 ms'lik mikrofon karesi için 10 ms'lik referans çeker ve şu sırayla işler:
`process_reverse_stream` → `set_stream_delay_ms(echo_delay_ms)` (varsayılan 50 ms)
→ `process_stream`. Yankı iptalci ilk mikrofon karesinin hızında kurulur. Gürültü
engelleme açıkken 48 kHz denenir; aygıt bu hızı açamazsa mikrofon 16 kHz'e döner ve
iptalci 16 kHz'de yeniden kurulur.

`voice.echo_cancellation` varsayılan kapalıdır; `voice.echo_delay_ms` gecikme
ipucudur ve varsayılanı 50 ms'dir. Panelde **Yankı engelleme (hoparlörle
kullanırken)** onay kutusu bulunur. `livekit` eksikliği veya çalışma zamanı hatası
tek uyarı üretir; bu oturum boyunca ses işlenmeden iletilir ve sohbette `echo:off`
durumu gösterilir. `SoundDeviceSink` çıkışı referansa beslerken oluşan hata
oynatma akışını kesmez.

**Neden bu:** `speexdsp` ve `webrtc-audio-processing` PyPI paketlerinin cp312
Windows tekerleri yoktur. `aec-audio-processing` tek yazara ait ve bakımı
bırakılmış bir tekerdir. `livekit` ise sürdürülmektedir (Apache-2.0; paket,
WebRTC'nin BSD kodunu içerir) ve `win_amd64` tekerleri sunar. DeepFilterNet
gürültü bastırmadır, AEC değildir. Bu nedenle Windows hedefi için AEC3'ü taşıyan
canlı paket seçilmiştir.

**Ölçüm:** Linux'ta sentetik kipte uzak uç temiz konuşmaydı. Yankı, RT60'si
0.79 s olan ölçülmüş oda darbe yanıtıyla konvolüsyona sokulmuş, 0.3 katsayısıyla
ölçeklenmiş ve 40 ms geciktirilmiş olarak 16 kHz'e eklenmiştir. Çıktının ikinci
yarısında ölçülen yankı azaltma:

- 0 ms gecikme ipucu: **23.7 dB**
- 40 ms gecikme ipucu: **25.8 dB**
- 120 ms gecikme ipucu: **27.0 dB**

Bu sonuçlar, gecikme ipucu değiştiğinde çözümün kırılgan olmadığını gösterir. CI
testi sentetik kipte `>= 15 dB` yankı azaltma eşiğini doğrular.

**Sonuçlar / sınırlar:**

- Özellik henüz gerçek bir Windows cihazında doğrulanmadı.
- AEC3, doğrusal olmayan hoparlör distorsiyonunu ve çıkış ile giriş aygıtları
  arasındaki saat kaymasını yalnızca kısmen karşılayabilir.
- Metin düzeyindeki yankı korumaları (ADR 0022) kalır; AEC ses yankısını azaltır,
  metin tekrarlarını engellemez.
- Kulaklıkla çalışırken AEC'ye gerek yoktur; özellik kapalı bırakılabilir.
- İşlem maliyeti 10 ms'lik kare başına yaklaşık 1 ms'nin altındadır; `livekit`
  bağımlılığının paket boyutu yaklaşık 10 MB'dir.
