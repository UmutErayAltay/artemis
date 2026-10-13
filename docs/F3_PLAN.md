# F3 — Ses kapsamı ve kabul planı

## Sabit sözleşmeler

Ses kanalı da `CommandRunner → AgentLoop → SafetyGate` akışını kullanır. Mikrofon,
STT ve TTS adaptörleri araç çalıştırmaz; onay gereğini yalnız SafetyGate belirler.
Her onay tool adını, doğrulanmış argümanları ve varsa taint uyarısını gösterir.
Onay alınamaması, timeout, iptal veya belirsiz tanıma ret sayılır.

Mikrofon yalnız açıkça başlatılmış ses komutunda açılır. Ham ses kalıcı olarak
saklanmaz ve STT yerelde çalışır. Edge TTS'ye yanıt metni gönderimi ayrı gizlilik
ayarıyla açılır ve denetlenir; varsayılan kapalıdır. Kapalıyken yerel TTS kullanılır.
Eksik adaptör veya ses aygıtı dürüst hata verir; sessizce başarı dönülmez.

Windows ve ses bağımlılıkları lazy import edilir. Çekirdek ve sahte adaptör testleri
Linux'ta mikrofon, hoparlör, dış ağ veya model indirmesi gerektirmez. Gerçek ses
testleri `disruptive` işaretlidir; varsayılan atlanır. Ses bağımlılıkları isteğe
bağlı kurulum grubunda tutulur; ağır ajan çerçevesi eklenmez.

## Akış hedefi

Uyandırma → komutu yakalama → yerel STT → ortak runner → yanıtı seslendirme.
VAD konuşmanın başını/sonunu belirler; kısa takip penceresi boyunca yeniden
uyandırma gerekmez. Konuşma sırasında kullanıcı söze girerse TTS durur ve aktif
tur iptal edilir. Eski görev temizlenmeden yeni komut yürütülmez. Hoparlörün kendi
sesinden doğan tekrar tetiklemeler ve kaçırılan konuşma ayrı test/ölçüm konusudur.

STT parça sonuçları ile kesin komut birbirinden ayrılır: eksik/parçalı metin araç
çalıştırmaz. faster-whisper tek başına gerçek akışlı STT değildir; pencereleme ve
segmentleme davranışı uygulamada açıkça tanımlanıp ölçülmelidir. Edge TTS'nin ses
verisini parça parça sağlaması, oynatıcı gerçekten erken başlamıyorsa düşük
gecikme kanıtı sayılmaz.

## Kullanıcı yanıtı beklenen mimari kararlar

1. “Jarvis” uyandırması: yerel STT anahtar sözcük algılama veya ayrı uyandırma modeli.
2. Ses onayı: tam argümanlarla terminal onayı veya ayrı sesli onay oturumu.
3. Windows işlemci/RAM/GPU bilgisi: STT aygıtı ve başlangıç model boyutu seçimi.

Bu kararlar kesinleşmeden ilgili uygulama kodu yazılmaz. Kararlar kısa ADR'ye
işlenir; model kimlikleri ve eşikler yapılandırmadan gelir.

## Ölçüm ve bitiş şartı

İlk ses gecikmesi komut konuşmasının bitişinden oynatıcının gerçekten ilk ses
örneğini sunmasına kadar ölçülür. STT sonlandırma, model yanıtı, TTS ilk parça ve
oynatıcı başlangıcı ayrı zaman damgalarıdır. Benchmark sahte ve canlı kiplerini
ayırt eder; sahte gecikme gerçek performans sonucu olarak sunulmaz.

Canlı ölçüm raporu örnek sayısı, medyan/p90, donanım, aygıt, STT/TTS ayarı ve
bulut/yerel yolunu içerir. Yanlış uyanma, kaçırılan uyanma, yanlış onay ve söz
kesme gecikmesi ayrıca sayılır. Ücretli model çağrısı yapılmaz.

F3 kabulü: izole sözleşme testleri, Windows'ta mikrofon/hoparlörle elle doğrulama
ve ölçülen ilk ses medyanı < 1.5 saniye. Bu ortam yalnız Linux olduğundan Windows
ölçümü tamamlanmadıkça F3'ün tamamlandığı iddia edilmez.
