# ADR 0021 — Türkçe öncelikli sınırlı dil seçimi

**Bağlam:** Kullanıcı “tarayıcı aç” yerine Kiril harfli Rusça kişi adı
tanındığını bildirdi. `auto` bütün Whisper dillerini seçebiliyordu; kısa
mikrofon komutları için önceki sentetik karma dil testi bu riski dışlamadı.

**Karar:** Yerel profil `tr_en` kullanır. Whisper'ın dil olasılıkları alınır;
varsayılan çözümleme açıkça `tr` olur. Yalnız en üst dil İngilizce, olasılığı
en az 0.7 ve Türkçenin en az iki katıysa açıkça `en` seçilir. Rusça veya başka
bir dil seçilemez. `task=transcribe` korunur. Bu modda Kiril harfli metin ayrıca
reddedilir; yanlış tanıma uygulama komutuna dönüştürülmez. Eski `auto` ve `tr`
profilleri kullanıcının açık tercihi için mevcuttur.

Artemis/tarayıcı hotword'ları kullanılır. Aynı kısa sentetik PCM'de yalnız dil
kısıtlaması “Sarayıcı/Tereyici” ürettiği için yerel `stt_initial_prompt` ile
Türkçe/İngilizce uygulama-komut bağlamı verilir. Bu bağlam araç komutu yürütmez.
Geniş hotword listesi sentetik uyandırmada yan etki ürettiği için eklenmedi.
Dil seçimi kelime düzeyinde anlama garantisi
değildir: karışık dil uygulama adlarını koruyabilir, Türkçe ek/eylem hataları
kalabilir. Mikrofonun Razer MME olduğu adla kontrol edildi; seçim değişmedi.

**Doğrulama:** Rusça baskın ve belirsiz İngilizce adayların Türkçeye dönmesi,
güçlü İngilizcenin İngilizce seçilmesi ve Kiril çıktı reddi test edilir. Kısa
tarayıcı aç/kapat, Artemis, İngilizce ve karma sentetik sesler ayrıca ölçülür;
insan mikrofon kabulü bu örneklerden çıkarılmaz.

Ses logunda görülen Windows pipe `InvalidStateError` için Edge temizliği
`wait_closed` Future'ını iptalden korur ve süreç durduktan sonra tamamlanmasını
bekler; `communicate` process çıkışı sonrası pipe transport'u kapatır.
Ses metni veya mikrofon kaydı buluta yeni bir izinle gönderilmez.
