# ADR 0068 — Anlamsal konuşma sonu (Smart Turn v3.2)

**Bağlam:** Tur yalnız sessizlikle biter (`end_silence_ms`, varsayılan 600; kullanıcı config'i 1000).
Cümle ortasında düşünmek için duran kullanıcı ikiye bölünür; eşiği büyütmek ise her turu uzatır.
Dosya güdümlü ölçümde (ADR 0067) uçtan uca medyanın ~1 sn'si yalnız bu beklemedir.

## Karar

1. **Smart Turn v3.2 (BSD-2, pipecat-ai) pakete gömülür** (`models/smart_turn/`, 8,7 MB int8 CPU,
   sabit sha256). Girdi son 8 sn, soldan sıfır dolgu, Whisper log-mel (kurulu `faster_whisper`
   çıkarıcısı; resmi HF çıkarıcısıyla ortalama fark 1,4e-5, üç referans sinyalde olasılık farkı
   < 0,01 — eşlik testi). Yeni bağımlılık yok.
2. **Yalnız sabır ekler.** Sessizlik `end_silence_ms`'e ulaşınca dedektöre o duraklama için bir kez
   sorulur; "tamamlanmadı" derse tur `turn_max_silence_ms`'e (1600) kadar açık kalır, konuşma
   dönerse sonraki duraklama yeniden sorulur. Erken kesme (eşikten önce bitirme) YOK: ölçümde
   model cümle ortası duraklamaların çoğunu "tamam" sandı; erken kesme bugünden sık bölerdi.
3. **Dedektöre ses son konuşma karesinde kırpılıp verilir.** Sondaki sessizlik modeli "bitti"ye iter.
4. Hata (dosya, sha, onnxruntime) turu asla bloklamaz: bir kez uyarı, tur sessizlikle biter;
   kurulamazsa `turn:off`. `voice.turn_detection` (varsayılan açık), `turn_max_silence_ms`,
   `turn_threshold` (0,5).

## Ölçüm (2026-11-14, gerçek Türkçe konuşma: Common Voice 17 TR, 80 klip, CC0)

| Durum | Sonuç |
| --- | --- |
| Tam cümle (+200 ms gerçek arka plan) | %97,5 "tamam" (yanlış sabır %2,5) |
| Cümle ortası kesim, sessizlik kırpılmış | %30 "yarım" → bölünme yerine bekleme |
| Aynı kesim + 600 ms sessizlik (kırpmasız) | %5 "yarım" (bu yüzden karar 3) |

Okunmuş cümleler doğal duraksamadan farklıdır; gerçek kullanımda oran farklı olabilir. Piper TTS
kesitleri devam tonlaması taşımadığı için ölçüm aracı olarak kullanılamadı.

## Sonuçlar

- Duraklamaların yaklaşık üçte biri bölünmek yerine birleşir; tam cümlede gecikme değişmez
  (%2,5 durumda en çok `turn_max_silence_ms`'e kadar bekler). CPU maliyeti duraklama başına bir çağrı.
- Çağrı segmenter içinde, ses döngüsünde eşzamanlıdır: ölçülen süre medyan 40 ms, en çok 44 ms
  (bu makine, 4 sn ses). Mikrofon kuyruğu 32 kare (640 ms) tuttuğu için kare kaybı olmaz; duraklama
  başına tek sefer olduğundan iş parçacığına taşınmadı. Daha yavaş makinede ölçülüp taşınmalı.
- Kazanç sınırlıdır; gerçek mikrofon kabulü ve eşik ayarı kullanıcıdadır.
- Test: `tests/test_voice_turn.py`.
