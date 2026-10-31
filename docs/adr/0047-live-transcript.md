# ADR 0047: İsteğe bağlı canlı yazı (yalnız gösterim)

- Tarih: 2026-10-27
- Durum: Kabul edildi (kullanıcı: konuşurken yazı akması istendi; varsayılan kapalı)

**Bağlam:** Artemis söz bitince (1 sn sessizlik) large-v3 ile tek seferde çözer; kullanıcı
konuşurken yazının akmasını istiyor. Kullanıcının 33 gerçek klibinde ölçüm (2026-10-27):

| Yöntem | İlk kelime | WER | Not |
| --- | --- | --- | --- |
| Mihu turkish-stt v2 (sherpa-onnx, CPU) | ~0,9 sn | %15,4 | RTF 0,083; "Artemis" 23/23 |
| Deepgram Nova-3 (bulut) | ~1,0 sn | %17,1 | ses buluta gider |
| Whisper large-v3 yerel-uyum akışı | ~2,1 sn | %13,8 | ilk pencerelerde halüsinasyon |
| Vosk small-tr | — | %65,9 | kullanılamaz |
| Whisper large-v3 (nihai komut, referans) | — | %10,6 | komutta kalır |

**Karar:**

1. **Yalnız gösterim.** Canlı metin `on_live_text` ile masaüstü petinin balonuna gider (ayarlar paneli yalnız nihai metni gösterir);
   komut, uyandırma, yankı ve söz kesme kararına hiçbir zaman girmez (CLAUDE.md: kısmi STT komut
   çalıştırmaz). Komut yine `FasterWhisperRecognizer` sonucudur; nihai `heard:` canlı metnin
   üzerine yazar. Uzun sözde balon en son sözcükleri gösterir (kullanıcı konuşurken pete bakar;
   2026-10-27 Umut'un isteğiyle ayarlar panelinden pete taşındı).
2. **Model:** Mihu turkish-stt-66m-v2, `sherpa_onnx` (Apache-2.0) ile; kendi ince sarmalayıcımız
   (`artemis/channels/voice/live_text.py`). Mihu'nun pip paketi **kullanılmaz** (kodu da ayrı
   lisanslı). Model kimliği koda gömülmez; `voice.live_transcript_model` ve
   `voice.live_transcript_revision` config'tedir, örnek dosyada sabit commit sha ile. Böylece
   tedarik zinciri sabittir.
3. **Çalışma:** özellik varsayılan kapalıdır. Besleme ~100 ms'lik gruplarla `asyncio.to_thread`
   üzerinden, tek uçuşta bir işle yapılır (olay döngüsü bloklanmaz); metin güncellemesi en çok
   ~150 ms'de bir. Söz bitince/atılınca ve onay duraklatma/kapanış noktalarında akış sıfırlanır.
   Çalışma hatası bir uyarı + `live:unavailable:runtime` verir, canlı yazı o oturumda kapanır,
   ses sürer. Eksik paket (`deps`) veya model (`model`) aynı şekilde bildirilir.
4. **Lisans (mihu Community License v1.0):** her ücretsiz kullanım (araştırma, kişisel, ticari)
   kullanıcının bağlı kuruluşlarıyla yıllık geliri 500.000 USD altındayken geçerli; ticari
   kullanım ayrıca mihu'nun istediği kaydı gerektirir, eşik ve üstü için ayrı anlaşma; rakip
   konuşma/temel model eğitimi yasak; yeniden dağıtım ve alt lisanslama yasak; atıf: modelle
   dağıtılan NOTICE ve "Powered by mihu"; ihlal, anlaşmasız eşik aşımı veya mihu'ya dava
   lisansı sonlandırır. Konumumuz:
   ağırlıklar depoda yoktur ve dağıtılmaz, kullanıcı çalışma sırasında sabit revision'dan indirir;
   özellik kapalıdır; açan kişi lisansın tarafı olur; Artemis'in lisansı modeli kapsamaz.
   Atıf `THIRD_PARTY_NOTICES.md`, README, ses rehberi, config örneği ve ayarlar panelindeki
   "Powered by mihu" ile karşılanır. Ticari dağıtımda `[stream]` ekstrası ve özellik çıkarılır
   veya mihu ile şartlar ayrıca belirlenir.
5. **Yedekler:** arayüz (`feed`/`reset`/`close`) küçüktür; Deepgram veya Whisper akışı aynı
   `LiveTranscriber` protokolüyle takılabilir. Bu ADR yalnız yerel sherpa-onnx'i ekler.

**Sonuçlar:** ek bağımlılık yalnız `[stream]` ekstrasındadır (`sherpa-onnx`, `huggingface-hub`).
Canlı doğruluk %15,4 WER olduğundan metin yalnız ipucudur; yanlış okunan bir canlı kelime hiçbir
eylem başlatmaz. Windows'ta gerçek mikrofon ve gerçek modelle kabul denemesi ayrıca yapılmalıdır.
