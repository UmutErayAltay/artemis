# ADR 0019 — Karışık dilde STT ve doğrulanmış söz kesme

**Bağlam:** Kullanıcı ilk komuttan sonra “tarayıcı aç” yerine video kapanış
cümlelerinin tanındığını bildirdi. Türkçe ve İngilizce karışık konuşma desteği
istiyor. VAD etkinliği önceki model çağrısını metin tanınmadan iptal ediyordu.

**Karar:** Yerel kullanıcı profili mevcut large-v3-turbo modeline, CUDA float16
ve otomatik dil algılamaya geçti. `task=transcribe` korunur; çeviri yapılmaz.
Windows CUDA DLL dizinleri yapılandırılabilir; global PATH değiştirilmez.
Silero VAD, segment güven eşikleri ve bilinen video kapanış cümlelerinde yalnız
tam eşleşme süzgeci eklenir. Daha uzun gerçek dikte metni bu süzgece takılmaz.

VAD aday ses segmentini toplar; yalnız kesinleşmiş, kabul edilmiş uyandırma
veya geçerli takip komutu önceki turu iptal eder. Yeni tur eski iptal temizliği
bittikten sonra başlar. Sessiz segment ve uyandırma içermeyen arka plan sözü
aktif turu kesmez. Mikrofon akışı sonlanırsa bekleyen tur temizlenir.

**Sınırlar:** Otomatik dil algılama cümle içi dil değişimini kusursuz kılmaz.
Yerel sentetik karma test İngilizce uygulama adını korudu; Türkçe bölümde
küçük yazım hataları kaldı. Kullanıcının gerçek mikrofonla kabulü bekleniyor.
Tam eşleşmeli video süzgeci tek başına söylenen aynı gerçek cümleyi de reddeder;
dikte komutuyla birlikte söylendiğinde korunur. Google TTS STT değildir;
bu değişiklik seslendirmeyi veya buluta ses gönderme politikasını değiştirmez.
