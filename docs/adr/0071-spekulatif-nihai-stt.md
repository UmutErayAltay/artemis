# ADR 0071 — Konuşma sonu sessizliğinde spekülatif nihai STT

**Bağlam:** Dosya güdümlü gerçek donanım ölçümünde (F3_REPORT) her tur iki ardışık bekleme taşıyordu:
konuşma sonu sessizliği (~670 ms, `end_silence_ms` 600) ve ARDINDAN nihai Whisper çözmesi (~750 ms,
large-v3 beam 5). Daha hızlı model (turbo) uygulama adı doğruluğunu %71'den %50'ye düşürdüğü için seçilmedi
(kural 11); doğruluğu koruyarak süreyi gizlemek gerekiyordu.

## Karar

1. Segment açıkken konuşmadan sonra `stt_speculative_after_ms` (200) kesintisiz sessizlik görülünce, o ana
   kadarki SESLİ kısım (sondaki sessizlik kırpılmış; Smart Turn'e verilen kesitle aynı) normal nihai
   tanıyıcıyla (tam beam, kısmi çözücü değil) bir kez çözülmeye başlanır. Aynı anda en çok bir spekülasyon.
2. Konuşma dönerse spekülasyon iptal edilir/yok sayılır; sonraki duraklama yenisini başlatabilir.
3. Segment kapanınca sonuç YALNIZ aynı söz kimliği, aynı yakalama dönemi (epoch) ve segment sesi tam olarak
   spekülasyon kesitiyle başlıyorsa (arkasına yalnız sessizlik eklenmişse) nihai metin olarak benimsenir;
   aksi halde normal çözme çalışır. Spekülatif hata sessizce normal yola düşer.
4. Benimsenmeden önce hiçbir şey yayımlanmaz ve çalıştırılmaz: "Kısmi STT komut çalıştırmaz" kuralı korunur,
   çünkü benimsenen metin son segmentin (sessizlik hariç) aynısının nihai çözümüdür. Onay duraklaması/devamı,
   geçersizleştirme, kapanış, söz kesme, uyandırma kapısı ve kuyruk doluluğu spekülasyonu iptal eder.
5. `voice.stt_speculative` (varsayılan açık); kapalıyken davranış öncekiyle aynıdır.

## Ölçüm (2026-11-14, 21 tur, aynı akşam, Silero + Smart Turn, 600 ms)

| | STT (segment kapanışından metne) medyan / p90 | Yerel hat* |
| --- | --- | --- |
| Spekülasyon yok | 750 / – ms | ~1610 ms |
| Spekülatif nihai STT | 312 / 406 ms | ~1330 ms |

\* F3_REPORT tanımı. Uçtan uca medyan bu akşam sağlayıcının ilk token süresine (~3,4–3,7 sn) bağlıydı.

## Sonuçlar

- Duraklama başına ek bir GPU çözmesi olabilir (konuşma dönerse boşa gider); tanıyıcı kilidi çözmeleri
  sıralar. Konuşma çok sık duraklıyorsa `stt_speculative_after_ms` büyütülür.
- Test: `tests/test_voice_speculative.py`.
