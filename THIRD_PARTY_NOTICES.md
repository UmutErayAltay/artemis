# Üçüncü taraf bildirimleri

Bu dosya, Artemis'in isteğe bağlı olarak kullanabildiği üçüncü taraf bileşenleri ve lisans
şartlarını belirtir. Artemis'in kendi lisansı bu bileşenleri **kapsamaz**.

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

## DeepFilterNet3 (gürültü engelleme)

Pakete dahil model ve lisans bildirimi için
[`artemis/channels/voice/models/dfn3/NOTICE`](artemis/channels/voice/models/dfn3/NOTICE) dosyasına bakın.
