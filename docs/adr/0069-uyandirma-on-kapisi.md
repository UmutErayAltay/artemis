# ADR 0069 — Cihaz içi uyandırma sözcüğü ön-kapısı (isteğe bağlı)

**Bağlam:** Uyandırma sözcüğü yalnız Whisper'ın nihai metninde aranıyor (`wake.py`). Ayrı bir
anahtar sözcük modeli (KWS) yok: ortamdaki her konuşma (TV, sohbet) GPU'da tam STT'den geçiyor ve
Whisper'ın uydurma metinleri ("Artemis. Abone olmayı unutmayın.") uyandırma yanılgısına yol açabiliyor.
Hazır KWS modelleri (sherpa-onnx gigaspeech, openWakeWord) İngilizce/Çince; İngilizce model Türkçe
"Artemis"i eşik 0,01'de bile hiç yakalamadı (2026-11-14).

## Karar

1. **Model:** sherpa-onnx `KeywordSpotter`, canlı yazının (ADR 0047) AYNI Türkçe akışlı transducer'ı
   (`mihuai/turkish-stt-66m-v2`, sabit revision, mihu Community License; çalışma anında indirilir,
   dağıtılmaz) üzerinde anahtar sözcük kısıtlı kod çözme. Eğitim yok, yeni bağımlılık yok (`[stream]`).
2. **Anahtar satırları:** uyandırma sözcüğünün sözlükteki TÜM token parçalamaları (en çok 64). Modelin
   BPE bölmesi ("▁ar t em is") açgözlü tek seçimden ("▁ar te m is") farklı; tek tahmin hiç tetiklemedi.
3. **Yalnız ön-kapı.** `voice.wake_backend: kws` iken takip penceresi/kesintisiz kip/söz kesme dışındaki
   bir segment, segment başından (−0,5 sn) beri dedektör tetiklenmediyse STT'ye gitmez
   (`no_wake_kws`). Tetiklenme TEK BAŞINA hiçbir şey yapmaz: geçen segment yine STT'den ve
   `find_wake_word` mantığından geçer. Yani KWS yalnız uyanmayı azaltabilir, asla artıramaz;
   ADR 0047'nin "canlı yazı uyandırma kararına girmez" ilkesi korunur (karar hâlâ Whisper metnidir).
4. **Varsayılan `transcript`.** KWS isteğe bağlıdır: kaçırma (aşağıda) Whisper'ın yakalayacağı bir
   uyandırmayı düşürebilir; kullanıcı kendi sesinde ölçüp açar. Model yoksa/kurulamazsa
   `wake:unavailable:*` ve transcript yoluna dönüş; çalışırken hata → oturum boyunca transcript.

## Ölçüm (2026-11-14, bu makine, CPU)

| Veri | İsabet | Yanlış alarm |
| --- | --- | --- |
| Gerçek kullanıcı sesi (`umut_tr`, kulaklık/oda, sessizlik ve gürültü patlaması dahil) | 26/27 | 0/20 |
| Gürültülü sentetik (`wake_noise_tr`: pembe/babble SNR 10/5, telefon) | 61/75 | 0/75 |
| Piper TTS cümleleri (8 olumlu, 12 benzer sesli olumsuz: "Artemide", "Arda temiz"…) | 8/8 | 0/12 |

Gerçek zaman oranı ~0,075 (tek iş parçacığı). Kaçırmaların çoğu SNR 5 dB'de.

## Sonuçlar

- `kws` açıkken uyandırma içermeyen konuşma GPU STT'ye gitmez (gizlilik ve kaynak kazancı), Whisper
  uydurmasından gelen yanlış uyandırma riski düşer.
- Gerçek mikrofonda (farklı oda/mikrofon) yanlış alarm/kaçırma ölçümü ve varsayılanın değişmesi
  kullanıcı kararıdır.
- Test: `tests/test_voice_kws.py`, `tests/test_voice_wake_gate.py`.
