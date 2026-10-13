# ADR 0022 — Tekrarlayan STT ve asistan sesi yankı koruması

**Bağlam:** Kullanıcı “Tarayıcı aç” sözünün çok kez tekrarlandığını, ardından
kendi kendine “Tarayıcıyı kapat” dendiğini bildirdi. Önceki sentetik negatif
testler gerçek ses döngüsünü dışlamadı. Komut örnekleri veren başlangıç prompt'u
uydurulmuş eylem metni riskini artırdı; konuşma sırasında mikrofon açıktır.

**Karar:** `stt_initial_prompt` kaldırıldı; yerel örnek aç/kapat komutları
Whisper'a verilmiyor. Türkçe öncelikli `tr_en`, Kiril/video süzgeci korunur.
Yüksek sıkıştırma oranlı veya bütünü aynı kısa kalıbı en az üç kez tekrarlayan
STT çıktısı komut olarak işlenmez. Ret yalnız tüm çıktı için geçerlidir;
uzun dikte talebindeki tekrar parçası tek başına ret nedeni değildir.

Denetleyici gerçekten ilk PCM çıktığında seslendirilen metni geçici olarak
hatırlar. Oynatma sırasında ve bitişinden sonraki 1.2 sn yakalama aralığında,
bu metnin aynı/yakın söz parçası yeni komut sayılmaz, takip hakkını tüketmez.
STT'nin tamamlanma saati yerine mikrofon segmentinin başlama saati kullanılır.
Farklı yeni komut aynı aralıkta açık Artemis ile kabul edilir; zaman geçince aynı insan sözü de
yeniden kabul edilebilir. Dinliyorum yanıtı da bu korumaya dahildir.

**Sınırlar:** Bu metin yankı korumasıdır; akustik yankı giderme veya konuşmacı
kimliği değildir. Asistanın sözünün aynısını hemen tekrarlayan kullanıcı sözü
de kısa aralıkta reddedilebilir. Üç kez bütünü tekrarlanan gerçek insan komutu
ve yüksek sıkıştırmalı gerçek dikte de reddedilebilir. Kiril reddi yanlış sözü
Türkçeye çevirmeye çalışmaz. Komut sözcüklerinde hata kalabilir; sahte ses
başarısı insan kabulü değildir. Gerçek cihaz döngü kabulü kullanıcıda açık.

**Doğrulama:** Kullanıcının 22 tekrar örneği, kapat tekrarları, uzun dikte
istisnası ve sıkıştırılmış metin reddi; asistanın kapattım sözünden kapat
yankısının tur başlatmaması, aynı aralıkta farklı aç komutunun işlemesi ve
yankı süresinden sonra tekrarın alınması test edilir. Ses kaydı veya ayrı
kalıcı transcript eklenmez. Gönderilmiş uygulama eylemi geri alınmış sayılmaz.

Metin eşleşmesinin yakalayamadığı bozuk yankı için varsayılan
`require_wake_during_output` uzun yanıttaki ve 1.2 sn yankı aralığındaki yeni
komutta açık Artemis ister. VAD yine sesi hemen keser. Mikrofon duraklayan
Dinliyorum yanıtı bu yeni-komut kuralına dahil değildir; onu izleyen komut
uyanma sözcüğünü tekrarlamadan alınır. Bu, akustik yankı giderme olmadan
asistan sesine otomatik takip turu hakkı vermeyen açık bir kullanım sınırıdır.
