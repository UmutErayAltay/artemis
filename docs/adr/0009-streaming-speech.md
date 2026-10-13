# ADR 0009 — Akışlı ses adaptörleri ve gizlilik

**Bağlam:** Edge TTS yanıt metnini dış servise gönderir; yerel yedek ve söz kesme
gerekir. Akış API'si tek başına ilk ses gecikmesinin kanıtı değildir.

**Karar:** Ses bağımlılıkları isteğe bağlı ve lazy import olur. Mikrofon/PCM
oynatıcı sounddevice, VAD WebRTC, yerel STT faster-whisper, yerel TTS Piper'dır.
Piper sesi kullanıcı tarafından önceden hazırlanır; ham mikrofon kaydı tutulmaz.
Edge TTS yalnız `allow_cloud_tts_text` açıkken kullanılır ve metin aktarımı
denetlenir; ağ hatası yerel TTS'ye düşebilir. İptal hata diye fallback başlatmaz.

Edge MP3 akışı sabit argümanlı FFmpeg işçisiyle PCM'e çevrilip oynatılır; tüm yanıt
indirildikten sonra çalma akış sayılmaz. Alt süreçler izinli ortam alır, shell yoktur.
TTS kaynakları ve oynatıcı iptalde kapanır; eski ses parçaları yeni yanıta taşınmaz.
Yerel inference iptalinin hesaplamayı anında durduramadığı yerlerde artık sonuç
atılır ve aynı modelde eşzamanlı inference engellenir.

İlk ses ölçümü STT konuşma bitişi → ilk gerçek PCM oynatıcı olayı arasıdır;
STT/model/TTS evreleri ayrı kaydedilir. Sahte ve canlı ölçüm ayrı raporlanır.
Sesli söz kesme VAD üzerinden çalışır; ilk Windows doğrulaması kulaklıkla yapılır.

**Sonuçlar:** Ağsız yerel yol çalışabilir; Edge yolu FFmpeg kurulumu gerektirir.
CUDA/Piper modelleri çalışma anında kendiliğinden indirilmez. Akustik yankı
giderme bu sürümde yoktur; hoparlör sesi yanlış söz kesme üretebilir ve ayrıca
ölçülmelidir. F3 kabulü Windows elle doğrulama ve medyan < 1.5 sn ölçümü olmadan
tamamlandı sayılmaz.
