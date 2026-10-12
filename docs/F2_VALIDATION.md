# F2 doğrulama düzenekleri

Bu belge F2 davranış senaryolarını ve her senaryoda toplanacak kanıtı tanımlar.
Sayısal sonuçlar ve kabul özeti [F2_REPORT.md](F2_REPORT.md) içindedir.

| Senaryo | İzole düzenek | Kanıt yöntemi |
| --- | --- | --- |
| Aktif tur iptali ve sohbetin devamı | Sahte sağlayıcıda bekleyen ilk tur; ikinci tur metin yanıtı verir. Yan etki aracı gerçek `tmp_path` içine yazar. | İlk turun iptal sonucu, yan etki dosyasının oluşmaması, ikinci turun başarılı sonucu ve tur denetim kayıtları birlikte incelenir. |
| Sağlayıcı timeout'u ve yerel yedek | Zaman aşımına uğrayan sahte bulut sağlayıcısı, hızlı sahte Ollama yanıtı ve gerçek geçici SQLite bütçe defteri. Ağ çağrısı yapılmaz. | Bulut görevinin temizlenmesi, yedeğe ayrılan kalan süre, fallback denetimi ve bilinmeyen bulut maliyetinin rezervasyonda kalması kontrol edilir. |
| Silme onayı ve işlem sırası | Gerçek `tmp_path` dosyası ve SQLite UndoJournal; çöp kutusu adaptörü yalnızca aynı geçici kökte dosya siler. | Onay ve `tool_dispatch` denetim sırası, etkinin öncesinde `prepared` kaydı ve sonrasında `completed` kaydı doğrulanır. |
| Silme hatasının dürüst sonucu | Bir enjeksiyon dosyayı taşımadan hata verir; diğeri dosyayı kaldırdıktan sonra hata verir. Her senaryo için gerçek geçici dosya ve SQLite kullanılır. | İlk durumda dosyanın kaldığı ve kaydın `failed` olduğu; ikinci durumda dosyanın olmadığı ve kaydın `uncertain` olduğu, sonucun başarı bildirmediği doğrulanır. |
| Dış dosya verisinden sonra yan etki | Gerçek geçici dosya okuması, sahte model yanıtları, SafetyGate ve yine yalnız geçici kökte yazan araç. | Onayın taint uyarısı taşıdığı, sonraki model bağlamının özel olarak işaretlendiği ve onaysız yazmanın gerçekleşmediği doğrulanır. |
| Masaüstü işçisi iptali ve timeout'u | Yalnız kontrollü Python test işçisi; girdi/çıktı dosyaları `tmp_path` içindedir. Gerçek GUI veya kullanıcı uygulaması kullanılmaz. | İptal/timeout sonrası işçi sürecinin sonlandığı, beklenen sınır içinde toplandığı ve izin dışı eylem isteğinin reddedildiği ölçülür. |

Testler sahte sağlayıcı veya loopback HTTP ile çalışır; canlı sağlayıcı, ücretli API,
push, gerçek masaüstü, pano ya da kullanıcı dosyaları kullanılmaz. Kabul raporunda
başarılı senaryolar yanında iptal, timeout, belirsiz yan etki ve onaysız yan etki
sonuçları ayrı verilir. Test kanıtı OS'ye daha önce ulaşmış bir eylemin geri
alındığını göstermez; böyle bir durumda yalnız kayıtlı ve doğrulanmış sonuç
bildirilebilir.
