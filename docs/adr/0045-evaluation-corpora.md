# ADR 0045: Aşama 2 değerlendirme corpus'ları (Common Voice + sentetik TTS)

- Tarih: 2026-10-25
- Durum: Kabul edildi (kullanıcı: "1 ve 2 ile başla", gerçek gönüllü kaydı yerine açık veri
  ve ücretsiz/açık kaynak TTS)

**Bağlam:** Konuşma kalitesi planı aşama 2, en az 200 örnek ve 8 konuşmacıyla gerçek
WER/CER, uyandırma ve hedef ölçümü ister. Gönüllü kaydı yok. Kullanıcı açık veri ve ucuz ya
da açık kaynak modellerle test istedi. Plan: ham ses depoya girmez, konuşmacı ayrımı
sızıntısızdır, sentetik ve gerçek sonuçlar ayrı etiketlenir, ölçülmeyen alan `null` kalır.

**Karar:**

1. **Gerçek konuşmacı katmanı**, `bench/corpus_common_voice.py`: Mozilla Common Voice 17.0
   Türkçe test bölümü (CC0-1.0). Mozilla'nın HF deposu artık dosya sunmuyor, bu yüzden
   token istemeyen `fixie-ai/common_voice_17_0` aynası kullanılır. Bir parquet parçası
   (~40 MB) indirilir. Reddedilmiş (down_votes>0) klip alınmaz. Konuşmacılar sırayla
   dolaşılır, böylece az sayıda kişi corpus'a hâkim olmaz. `client_id` SHA-256 önekiyle
   kısaltılır. dev/test ayrımı kimliğin özetinden türetilir: aynı kişi iki tarafa düşemez.
   Okunmuş cümle olduğu için uyandırma, komut ve uygulama adı ölçmez.
2. **Sentetik komut katmanı**, `bench/corpus_synthetic.py` + `bench/synthetic_commands.yaml`:
   - **Şablonlar:** aç, kapat, kibar istek, tereddüt, olumsuzluk, düzeltme, durum sorusu,
     komut olmayan anma, uyandırmasız komut; ayrıca gündelik ve uyandırmaya benzeyen cümleler.
   - **Etiketler:** her örnekte uyandırma, niyet, hedef uygulama, olumsuzluk ve düzeltme.
   - **Uygulama adları:** referans metinde yazılı ad, TTS'te Türkçe okunuş ("breyv") kullanılır.
     **Test bölümünde dev'de görülmemiş uygulama adları ve sesler** kullanılır.
   - **Sesler** Artemis'in kendi TTS adaptörlerinden gelir: Piper (yerel) ve Edge (yalnız
     `--bulut-tts-izni` ile; şablon metni Microsoft'a gider, aktarım denetime yazılır).
   - **Koşullar:** her örnek temiz hâliyle ve sabit tohumla seçilmiş bir bozmayla yazılır
     (pembe gürültü 15/5 dB, 0,5 sn yankı, hız 0,9/1,15, telefon bandı). Bozmalar
     `bench/eval_audio.py` içinde yalnız numpy ile yapılır.
3. **Ölçüm**, `bench/speech_accuracy.py`:
   - **Etiketler:** corpus öğeleri `source` (real | common_voice | synthetic), `condition`,
     `wake`, `intent`, `target_app`, `negation`, `correction` alanlarını taşır.
   - **Konuşmacı sızıntısı:** aynı konuşmacı hem dev hem test'teyse corpus reddedilir.
   - **Uyandırma:** üretimdeki `find_wake_word` kullanılır. Kaçırma oranı ve yanlış
     uyanma, ölçülen saatle birlikte raporlanır.
   - **Hedef:** STT metni ADR 0044 okunuş çözücüsünden geçirilir. Doğru hedef ve yanlış
     hedef ayrı sayılır. Bu ölçüm model niyeti değildir; olumsuzluk ve niyet `bench.run`
     `intent` kategorisinde ölçülür.
   - **Kırılımlar:** kaynak, koşul ve ayrım başına; ayrıca kaynak başına planın rapor şeması
     (`plan_reports`).
4. **Ham ses depoya girmez:** çıktı klasörü depo içindeyse araç reddeder (varsayılan
   `~/.cache/artemis-eval/`). Her corpus yanında `DATASET_NOTE.json` (kaynak, lisans,
   sayılar, silme yolu) durur. Testler ağa çıkmaz, TTS veya model çalıştırmaz.

**Sınırlar:**
- **Sentetik ses temizdir:** sentetik WER gerçek kullanıcı hatasını olduğundan düşük gösterir.
  Ayar karşılaştırması ve gerileme içindir, kabul kanıtı değildir.
- **Ses sayısı:** Piper'da tek Türkçe ses var (dfki, CC BY-NC-SA). Edge'de iki ses var; bu
  oturumda `AhmetNeural` servis tarafında ses döndürmedi. Sentetik katmanda konuşmacı
  sayısı 2–3'tür. Daha fazla ses için XTTS-v2 klonlama veya MMS-TTS sonraki adımdır.
- **Common Voice:** okunmuş cümledir, gündelik komut değildir.

**Bulgu ve düzeltme:** üretimdeki uyandırma eşleştiricisi (`find_wake_word`) tek harf
farkını Levenshtein ile kabul ettiği için "artmış" sözcüğünü (harf düşmesi) "Artemis"
sayıyordu. Sentetik ölçümde 4 yanlış uyanmanın hepsi "Benzin fiyatları yine artmış"
cümlesiydi. Kullanıcı onayıyla kural daraltıldı: yalnız aynı uzunlukta tek harf
**değişimi** kabul edilir. Whisper'ın önceden gözlenen biçimleri (Artemiz, Arteniz,
Arkemiz, artemiş) hep değişimdir ve tanınmaya devam eder.

**Doğrulama:** `tests/test_eval_corpus.py`: depo dışı çıktı, yeniden örnekleme ve koşulların
belirlenimliliği ve SNR'ı, Common Voice konuşmacı ayrımı, dengeli seçim, reddedilen klip,
önbellek ve yarım indirme, gerçek parquet okuma, sentetik dev/test uygulama ve ses ayrımı,
uçtan uca kur + ölç, sızıntı reddi, hedef çözümü, uyandırma ve hedef hata sayımı.
Mutasyon (9): depo koruması, sızıntı denetimi, konuşmacı ayrımı, down_votes süzgeci, yanlış
hedef ve yanlış uyanma sayımı, uygulama ve ses ayrımı, yarım indirme — hepsi testleri kırdı.

## İlk ölçüm (2026-10-25, bulut oturumu, CPU)

Whisper `large-v3-turbo`, `stt_device: cpu`, `int8`, `stt_language: tr`, beam 5. Bulut
CPU'su (4 çekirdek, GPU yok); gecikme sayıları Windows/RTX 4050 için anlamlı değildir.

| Corpus | Örnek / konuşmacı | WER | CER | Diğer |
| --- | --- | --- | --- | --- |
| Common Voice tr (gerçek) | 300 / 300 (dev 158, test 142) | %16,3 (test %12,9) | %7,3 | — |
| Sentetik (Piper dfki dev, Edge Emel test) | 274 / 3 | %14,9 | — | ad yazımı %61, çözücüyle hedef %95, yanlış hedef 0 |

Sentetik koşul kırılımı: temizde uyandırma kaçırma %0; pembe gürültü 5 dB'de %29, telefon
bandında %19. Hedef doğruluğu dev'de %99, görülmemiş uygulama adlarıyla test'te %92.

Uyandırma kuralı değişikliğinin A/B'si (aynı STT çıktısı, aynı corpus): eski kural 4 yanlış
uyanma (hepsi "artmış"), yeni kural **0**; uyandırma kaçırma iki kuralda da **%3,8**
(değişmedi). Sentetik sonuç kabul kanıtı değildir; Windows/GPU karşılaştırması
[WINDOWS_CHECKLIST.md](../WINDOWS_CHECKLIST.md) A6'dadır.

## Gürültüde uyandırma (2026-10-26, Windows, RTX 4050)

`wake_noise_tr` (150 örnek: 15 wake + 15 wake-siz × pembe 10/5 dB, babble 10/5 dB, telefon),
`--canli --sentetik-metin`. Whisper `large-v3`, `cuda`, `float16`, `stt_language: tr_en`,
config'te `stt_hotwords: [Artemis, tarayıcı]` zaten açık.

| Deney | Kaçırma | Telefon | Yanlış uyanma | WER | Ad | Hedef | Gecikme medyan |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Bugünkü ayar | %8,0 | %40 | 0 | %25,4 | %69,3 | %82,5 | 941 ms |
| `--ek-hotword Artemis` | %8,0 | %40 | 0 | %25,4 | %69,3 | %82,5 | 1973 ms |
| `--gurultu-engelle orta` | %6,7 | %27 | 0 | %26,6 | %64,3 | %85,0 | 2784 ms |
| `stt_vad_filter: false` | **%0** | **%0** | 0 | **%19,8** | **%74,3** | **%90,0** | 2507 ms |

- Pembe ve babble gürültüde kaçırma GPU large-v3'te her deneyde %0'dır (DFN'de babble 5 dB
  tek kaçırma); buluttaki %29 turbo/int8/CPU'ya aittir.
- Tek sorun telefon bandı: Whisper "Artemis" yerine "Tertemiz", "Temiz" yazar ya da sözcüğü
  düşürür. "Tertemiz" gerçek sözcük olduğundan eşleştirici gevşetilmez.
- Hotword deneyi taban çizgisiyle aynıdır, çünkü ipucu zaten config'tedir.
- DFN telefonu iyileştirir ama ad doğruluğunu düşürür; değmez.
- VAD süzgecini kapatmak telefon kaçırmalarını sıfırlar ve tüm doğruluk ölçülerini
  iyileştirir: VAD, bant sınırlı seste baştaki "Artemis"i kesiyor.

Yeniden ölçüm (aynı oturum, dönüşümlü, başka GPU yükü yok): doğruluk sayıları iki koşuda da
birebir aynı (belirlenimli). Gecikme medyanı taban 1035 / 916 ms, VAD kapalı 848 / 927 ms:
fark yok; ilk tablodaki 1973–2784 ms art arda koşulan iki ağır işin GPU çekişmesiydi.

Sessizlik corpus'u (`silence_tr`, 48 × 4 sn: dither −70 dBFS, pembe −50/−40/−30 dBFS, telefon
bandı pembe −40, 50 Hz vınlama + pembe): iki ayarda da halüsinasyon **0**, yanlış uyanma **0**.
VAD kapalıyken saf gürültü STT'si 346 → 892 ms sürer; canlı döngüde STT'ye yalnız webrtc VAD'in
konuşma diye kestiği parça gider, bu yüzden boşta maliyet oluşmaz.

**Karar:** `stt_vad_filter: false` önerilir: telefon bandında kaçırma %40 → %0, WER %25,4 →
%19,8, hedef %82,5 → %90, gecikme ve yanlış uyanma değişmez. Ölçülmeyen: ≥1 saat gerçek komutsuz
konuşmada yanlış uyanma ve gerçek mikrofonla canlı deneme (A1/A5 ile birlikte).

## A6: GPU STT karşılaştırması (2026-10-26, Windows, RTX 4050 6 GB)

Taban: Umut'un config'i (`large-v3`, `float16`, beam 5, `tr_en`, `stt_vad_filter: false`,
hotword Artemis/tarayıcı). Her satır tek değişken; Artemis'in sesi kapalı, GPU boşta 439 MiB.
Corpus: Common Voice tr 600 gerçek konuşmacı; sentetik 294 (Piper dfki 134, Edge Emel 134,
Edge Ahmet 26 — Ahmet akışı sık hata verdi).

| Ayar | CV WER | Sentetik WER | Hedef | Yanlış hedef | Wake kaçırma | Gecikme medyan (CV/sent.) | VRAM tepe |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Taban | %13,6 | %18,8 | %90,2 | 4 | %2,7 | 979 / 920 ms | 4,75 GB |
| `large-v3-turbo` | %19,6 | %13,4 | %93,3 | 0 | %0,9 | 698 / 678 ms | 2,91 GB |
| beam 1 | %14,3 | %19,0 | %90,9 | 2 | %0,9 | 931 / 902 ms | 4,86 GB |
| `stt_language: tr` | %13,4 | %18,6 | %90,2 | 4 | %2,7 | 641 / 640 ms | 4,77 GB |

Yanlış uyanma her satırda 0 ("artmış" eşleştirici düzeltmesi tutuyor; karar maddesi kapanır).

- **turbo:** sentetikte iyi, gerçek konuşmacıda belirgin kötü (WER %13,6 → %19,6); sentetik
  kabul sayılmadığından `large-v3` kalır. VRAM 1,8 GB az; GPU'yu oyunla paylaşma gerekirse seçenek.
- **beam 1:** hız kazancı yok, doğruluk hafif düşer; beam 5 kalır.
- **`tr`:** doğruluk `tr_en` ile aynı, gecikme ~300 ms az. Fark `tr_en`'in her sözde yaptığı
  ek `detect_language` geçişidir (ADR 0021). Corpus'ta İngilizce cümle yoktur; `tr`'nin bedeli
  tam İngilizce cümlenin Türkçe zorla yazılmasıdır ve burada ölçülmedi. Kiril riski `tr`'de de
  yoktur (dil sabit). Karar Umut'ta: İngilizce cümle kurmuyorsa `tr` önerilir.
- Yanlış hedef 4 (taban, sentetik): çözücü belirsiz adı yanlış uygulamaya bağladı; ayrı incelenmeli.
