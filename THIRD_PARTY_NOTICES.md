# Üçüncü taraf bildirimleri

Artemis'in kendi kaynak kodu Apache License 2.0 ile lisanslıdır ([LICENSE](LICENSE)). Bu dosya,
Artemis'in kullandığı ya da Windows kurucusuna dahil edilen üçüncü taraf bileşenleri ve lisans
şartlarını belirtir. Artemis'in lisansı bu bileşenleri **kapsamaz**; her biri kendi lisansıyla
dağıtılır. Lisans metinleri [`LICENSES/`](LICENSES) klasöründedir ve kurucuya da girer.

## Windows kurucusuna dahil copyleft bileşenler

Kurucu (`Artemis-Setup-*.exe`) aşağıdaki bileşenleri değiştirilmemiş hâlleriyle,
`_internal\` altında ayrı dosyalar olarak içerir. Kaynak kodlarına bağlantılar aşağıdadır; Artemis'in
kendi kaynağı <https://github.com/UmutErayAltay/artemis> adresindedir.

- **Piper TTS 1.8.0** (`piper-tts`) — **GPL-3.0-or-later**
  ([LICENSES/GPL-3.0.txt](LICENSES/GPL-3.0.txt)) — kaynak: <https://github.com/OHF-Voice/piper1-gpl>.
  Kurucu bu bileşeni içerdiği için **ikili paketin bütünü GPL-3.0 şartlarıyla dağıtılır**;
  Apache-2.0 GPL-3.0 ile uyumludur. Kaynaktan kurulumda Piper `[voice]` ekstrasıyla gelir.
  Piper ses modelleri pakette yoktur; indirilen her sesin kendi model kartındaki lisansı geçerlidir.
- **PySide6 / shiboken6 6.12.0** (Qt for Python) — **LGPL-3.0**
  ([LICENSES/LGPL-3.0.txt](LICENSES/LGPL-3.0.txt), [GPL-3.0](LICENSES/GPL-3.0.txt)) —
  kaynak: <https://code.qt.io/cgit/pyside/pyside-setup.git/>. Kütüphaneler `_internal\PySide6\`
  altında ayrı DLL'lerdir; kullanıcı bunları uyumlu bir sürümle değiştirebilir.
- **edge-tts 7.3.0** — **LGPL-3.0** (`srt_composer.py` MIT) — kaynak: <https://github.com/rany2/edge-tts>.
- **certifi** — **MPL-2.0** ([LICENSES/MPL-2.0.txt](LICENSES/MPL-2.0.txt)) — kaynak:
  <https://github.com/certifi/python-certifi>.

Kurucudaki diğer Python paketleri (ör. numpy, pydantic, httpx, faster-whisper, onnxruntime, PyYAML)
MIT, BSD veya Apache-2.0 gibi serbest lisanslıdır.

## mihu turkish-stt-66m-v2 (isteğe bağlı canlı yazı) — Powered by mihu

- Model: [`mihuai/turkish-stt-66m-v2`](https://huggingface.co/mihuai/turkish-stt-66m-v2)
  (Hunters AI, mihu adıyla; destek: support@mihu.ai)
- Lisans: **mihu Community License v1.0**
  ([LICENSE](https://huggingface.co/mihuai/turkish-stt-66m-v2/blob/main/LICENSE))
- Kullanım: yalnız `voice.live_transcript: true` iken, konuşurken pet balonunda akan
  **gösterim metni** için (ADR 0047). Komut, uyandırma, yankı ve söz kesme kararlarına girmez.
  Özellik varsayılan olarak **kapalıdır**.

**Ağırlıklar bu depoda yoktur ve Artemis ile dağıtılmaz.** Özelliği açan kullanıcının makinesine,
`config/config.example.yaml`'da gösterilen sabit revision'dan
(`bc2f77287cfc3ec2568cf5ba9f9181e1a43bfff4`) çalışma sırasında doğrudan Hugging Face'ten
indirilir. Modelin pip paketi (`turkish-stt`) ve kodu kullanılmaz; Artemis modeli kendi yazdığı
ince bir sarmalayıcıyla Apache-2.0 lisanslı sherpa-onnx üzerinden çalıştırır.

Lisansın özeti (bağlayıcı olan lisans metnidir, asıl metni okuyun):

- Bütün ücretsiz kullanım (araştırma, kişisel, ticari olmayan ve ticari) modeli kullanan kişinin
  (lisans alanın) bağlı kuruluşlarıyla birlikte yıllık geliri **500.000 USD'nin altındayken**
  geçerlidir. Ticari kullanım ayrıca mihu'nun istediği kaydın yapılmasını gerektirir. Eşikte ve
  üstünde ayrı anlaşma gerekir (support@mihu.ai).
- Modeli veya çıktılarını rakip bir konuşma/temel model eğitmek için kullanmak yasaktır.
- Modeli yeniden dağıtmak veya alt lisanslamak yasaktır.
- Modele dayanan her ürün, arayüz veya belgede **"Powered by mihu"** gösterilmelidir (bu dosya, README
  ve ayarlar paneli). Lisans, modelle ya da türevleriyle birlikte dağıtılan bir NOTICE dosyasında
  lisans metninin ve bildirimlerin korunmasını ister; Artemis modeli dağıtmadığı için asıl metin
  modelin kendi deposundaki LICENSE dosyasıdır. Modeli kendiniz dağıtırsanız bu yükümlülük size düşer.
- Lisans şu durumlarda kendiliğinden sona erer: gelir eşiğine anlaşmasız ulaşmak, herhangi bir
  şartın ihlali veya mihu'ya model nedeniyle dava açmak. Sona erince kullanım durdurulmalı ve
  bütün kopyalar silinmelidir.

Özelliği etkinleştiren kişi lisansın tarafı olur ve bu şartlara uymakla yükümlüdür. Artemis'i
ticari olarak dağıtacaksanız `[stream]` ekstrasını ve canlı yazı özelliğini çıkarın ya da mihu ile
şartları ayrıca belirleyin.

## sherpa-onnx

- Lisans: Apache-2.0 — <https://github.com/k2-fsa/sherpa-onnx>
- İsteğe bağlı `[stream]` ekstrasıyla kurulur; canlı yazı için model çalıştırıcıdır.

## huggingface-hub

- Lisans: Apache-2.0 — <https://github.com/huggingface/huggingface_hub>
- Yalnız `[stream]` ekstrasında, model dosyalarını sabit revision'dan indirmek için.

## Silero VAD (konuşma algılama)

Pakete dahil model (MIT) ve lisans bildirimi için
[`artemis/channels/voice/models/silero/NOTICE`](artemis/channels/voice/models/silero/NOTICE) dosyasına bakın.

## Smart Turn v3.2 (konuşma sonu tahmini)

Pakete dahil model (BSD-2-Clause) ve lisans bildirimi için
[`artemis/channels/voice/models/smart_turn/NOTICE`](artemis/channels/voice/models/smart_turn/NOTICE) dosyasına bakın.

## DeepFilterNet3 (gürültü engelleme)

Pakete dahil model ve lisans bildirimi için
[`artemis/channels/voice/models/dfn3/NOTICE`](artemis/channels/voice/models/dfn3/NOTICE) dosyasına bakın.
