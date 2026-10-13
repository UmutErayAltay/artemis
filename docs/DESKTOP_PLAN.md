# Jarvis masaüstü — uygulanabilir ilk sürüm

Durum: uygulama öncesi plan. Kullanıcı F0–F3 üzerine masaüstünü şimdi istedi;
F4 hafıza beklenmez. Bu izin F3 kapsamını genişletmeme kuralına bu çalışma için
istisnadır. Hedef Windows 11; Linux çekirdeği Qt olmadan çalışmaya devam eder.
Karar: [ADR 0011](adr/0011-desktop-ui.md).

## Teslimat ve görsel yön

`jarvis desktop --config config/config.yaml`: tek QApplication, ana panel,
yüzen küçük komut penceresi ve sistem tepsisi. Mevcut metin komutları aynı
CommandRunner üzerinden yürür. İlk sürümde ses bağlantısı, kalıcı sohbet/hafıza,
Telegram, otomatik başlangıç, küresel kısayol, model seçici ve ayar editörü yoktur.
F3 ses CLI'sı korunur; masaüstünde çalışan mikrofon/dinleme animasyonu gösterilmez.
Gerekirse açık bilgi: “Ses bu masaüstü sürümünde bağlı değil.”

- Sade, kaliteli koyu yüzey: zemin `#0B1220`, panel `#111C2D`, yükseltilmiş
  yüzey `#17263A`, metin `#E6EDF5`, ikincil metin `#A9B8C9`, vurgu `#53D6C7`.
  Vurgu düğmesinde koyu metin; amber yalnız bekleyen onay, kırmızı yalnız hata.
  İnce sınırlar, 12–16 px köşeler, 8 px aralık sistemi, 24 px panel boşluğu.
  Sahte bilimkurgu göstergeleri, grafikler ve sürekli parıltı kullanılmaz.
- Başlık “Jarvis”, küçük alt başlık “Kişisel asistan”; gerçek çalışma durumunu
  ve yapılandırılmış model adını gösterir. “Hazır” yalnız çalışma zamanı kurulunca
  görünür; model adı bir bağlantı/sağlık doğrulaması değildir.
- Varsayılan pencere yaklaşık 1180×780 mantıksal piksel. Üstte kısa başlık,
  ortada yeniden boyutlanabilir %65 sohbet / %35 “İşlem adımları” bölmesi,
  altta çok satırlı komut alanı, “Gönder” ve çalışma sırasında “İptal et”.
  900 px altında adımlar açılır panel olur; kaybolmaz. Boş durumda kısa bir
  karşılama ve gerçek desteklenen komutları yazmaya davet vardır; uydurma geçmiş yoktur.
- Sohbet düz metin ve seçilebilir içerik kullanır; HTML/link otomatik çalışmaz.
  Enter gönderir, Shift+Enter satır açar; erişilebilir açıklaması görünürdür.
  Tur çalışırken yeni gönderim kapanır, metin taslağı korunur. Geçmiş yalnız
  bu oturumun görünümüdür; AgentLoop'a gizlice konuşma hafızası eklenmez.
- Yaklaşık 480×160 yüzen pencere, aynı durum ve komut alanının küçük görünümüdür;
  panel aç düğmesi vardır. Ayrı runner/sohbet yaratmaz. Tepsi menüsü “Paneli aç”,
  “Hızlı komut”, “Çıkış”; basit özgün J simgesi QPainter ile çizilebilir.
  Tepsi yoksa pencere kapanışı uygulamayı görünmez bırakmaz.
- Normal panel kapatma tepsiye gizleyebilir; bekleyen onay önce reddedilir.
  Onay kendi penceresinde ana paneli öne getirir. “Çıkış” temiz kapanışı başlatır.
  Gizleme ile aktif bir turun iptali aynı şey değildir; aktif durum tepside anlaşılır.
- Segoe UI / sistem yazı tipi, gövde en az 14 mantıksal px; sabit piksel tabanlı
  yerleşim yerine Qt düzenleri. Klavye odağı belirgin, ikon düğmeleri adlandırılmış,
  durumlar renk yanında metin taşır. Türkçe İ/ı/ş/ğ doğru görünür. 100/150/200% DPI,
  küçük pencere ve uzun argümanlar kaydırmayla kullanılabilir olmalıdır.

## Sınırlar ve ortak kurulum

| Dosya | Sorumluluk |
| --- | --- |
| `jarvis/channels/runtime.py` | Qt'siz Runtime ve ortak kurulum fabrikası |
| `jarvis/channels/desktop/bridge.py` | Qt'siz işçi, mesajlar, onay aracısı ve sınırlı olay kuyruğu |
| `jarvis/channels/desktop/app.py` | Lazy Qt giriş noktası, panel/overlay/tepsi, görünüm ve tema |
| `jarvis/channels/desktop/demo.py` | Yalnız açık demo kipinin sahte sağlayıcısı ve izole araçları |
| `jarvis/safety/audit.py` | İsteğe bağlı temizlenmiş kayıt gözlemcisi |

Küçük `__init__.py` Qt import etmez. `pyproject.toml` içine isteğe bağlı
`desktop = ["PySide6==6.12.0"]` eklenir. CLI yalnız desktop dalında Qt girişini
import eder; eksik bağımlılık Türkçe kurulum mesajıyla sonlanır. `jarvis chat`,
`jarvis voice` ve çekirdek testleri bu extra olmadan çalışır.

Mevcut `chat()` ve `voice()` içindeki yinelenen kurulum runtime fabrikasına
taşınır: ayarlar, çalışma dizini, AuditLog, keşfedilmiş kayıt, SafetyGate,
ToolContext, build_provider, AgentLoop, CommandRunner. `Runtime` bunların
gerekli referanslarını taşır. Onay fabrikası kapı/bağlamı alıp **tam olarak**
`(tool_name, arguments) -> bool` döndürür; CLI terminal sunumu, GUI köprüsü
aynı enjeksiyon noktasını kullanır. Sesin pause/resume onay davranışı korunur.
Sağlayıcı ve kayıt enjeksiyonu yalnız test/demo bağımlılıklarıdır; üretim güvenlik
ve sağlayıcı kurulumunun ikinci kopyası oluşmaz. Kurulum model çağrısı yapmaz.

## İş parçacıkları ve mesaj sözleşmesi

Qt yalnız ana iş parçacığında; ayrı tek Python işçisi kendi asyncio döngüsünü,
Runtime'ını ve aktif turunu oluşturur. QApplication içinde asyncio.run/qasync,
GUI'de Future.result/thread.join veya işçiden QWidget erişimi kullanılmaz.
Normal komutlar `loop.call_soon_threadsafe(...)` ile işçiye gider; işçi coroutine
başlatır. `runner.cancel_current()` ve asyncio.Event.set yalnız işçide çağrılır.
En fazla bir aktif tur vardır; gönderim kuyruğu oluşturulmaz. Çift tıklama işçi
tarafından da reddedilir. `cleanup_pending` bitmeden yeni tur kabul edilmez.

Qt'siz frozen dataclass mesajları yeterlidir; genel RPC/olay çatısı yazılmaz.
Metin/argüman taşıyan alanlar `repr=False`; Settings/provider nesneleri GUI'ye taşınmaz.

| Yön | İleti ve temel yük |
| --- | --- |
| GUI → işçi | `Submit(request_id, text)`, `Cancel(request_id)`, `Shutdown()` |
| GUI → onay aracısı | `Decide(request_id, turn_id, approval_id, approved: bool)` |
| İşçi → GUI | `State(request_id?, state, message)`; başlatılıyor/hazır/çalışıyor/onay/temizleniyor/kapalı/hata |
| İşçi → GUI | `Approval(request_id, turn_id, approval_id, tool_name, arguments, notice, deadline)` |
| İşçi → GUI | `Finished(request_id, RunResult)` veya sır içermeyen `Failed(request_id, error_type, message)` |
| Audit → GUI | `Activity(request_id, turn_id, seq, event, fields)` ve `ActivityGap(dropped_count)` |

`request_id` gönderimde UUID, `turn_id` çekirdeğin gerçek UUID'sidir; audit
turn_start ile bağlanır. Onay için ayrıca yeni UUID üretilir. Eski nesilden gelen
durum/sonuç mevcut turu değiştirmez. Sonuç ve onay gibi kontrol iletileri
kaybedilmez; aktivite kuyruğundan ayrıdır. Qt sinyalleri açık queued bağlantı ile
GUI'ye iletilir; alternatif olarak GUI QTimer aynı thread-safe kuyruğu boşaltır.

### Senkron onayın güvenli köprüsü

SafetyGate bugün callback'i senkron çağırır; bu sözleşme değişmez. Callback gerçek
doğrulanmış argüman kopyasını ve `gate.pending_notice` değerini hemen yakalar.
Kilitli bir aracıda `(request_id, turn_id, approval_id)` anahtarlı
`concurrent.futures.Future[bool]` oluşturur, onay olayını gönderir ve **yalnız
işçi thread'i** bu Future üzerinde bekler. Bekleme üst sınırı bağlamın mevcut
monotonik tur deadline'ıdır; dolunca False. Bekleyen asyncio döngüsüne karar
çözümünü sıraya koymak deadlock oluşturur: GUI kararı aracının thread-safe
metodu üzerinden Future'a doğrudan iletir. asyncio.Future kullanılmaz.

Onay görünümü tam araç adını, kaydırılabilir/seçilebilir doğrulanmış JSON
argümanlarını ve kapının notice metnini gösterir; taint cümlesi aynen korunur.
Argümanlar düzenlenemez. “Hayır” varsayılan/odaklıdır; Enter, Escape ve pencere
çarpısı onay vermez. “Evet” yalnız açık etkinleştirmeyle True döndürür. Onay
metni bir komut veya HTML değildir; otomatik yürütme/kopyalama yapılmaz.

İptal/kapanış önce eşleşen onayı kilit altında geçersizleştirip False ile çözer,
sonra işçi döngüsüne iptali yollar. Callback, iptal işaretli kararın True olmasına
izin vermez ve kendi thread'inde runner iptalini uygular; kapının onay sonrası
iptal/deadline denetimi korunur. Çift karar, eski kimlik, timeout sonrası Evet
ve kapanmış pencere yeni onayı çözemez. Bekleme bitince pending kaydı finally
ile kaldırılır ve GUI onay kartı kapanır. Yeni bir onay için eski UI callback'i
yeniden bağlanmaz. Kapı dışında risk/taint değerlendirmesi yapılmaz.

Çıkış yeni gönderimi kapatır, onayı reddeder ve turu iptal eder. Qt açık kalırken
işçi aktif görev/çekirdek temizliğinin bitmesini bekler; “Temizleniyor” gösterir.
İşçi tamamlanınca uygulama çıkar; GUI'de bloklayan join, thread terminate veya
çalışan görev üstüne loop.close yoktur. Geç kalan temizlik başarı diye gösterilmez.
OS'ye gönderilmiş bir eylemin geri alındığı söylenmez.

## Doğru ve sınırlı işlem görünümü

AuditLog'da şu an callback yoktur. Tek isteğe bağlı `on_record` eklenir: kayıt
mevcut redact ve başarılı JSONL yazımından **sonra**, dosya kilidi dışında,
bağımsız kopya olarak gözlemciye verilir. Disk hatası eskisi gibi hata olur;
gözlemci yalnız put_nowait yapan kısa bir fonksiyondur, yürütme veya onay yapmaz.
Gözlemcinin beklenen kuyruk-dolu durumu kendi içinde işlenir; geniş exception
yutma veya ham kayıt loglama eklenmez. Mevcut AuditLog(path) kullanımları korunur.

Aktivite için en çok 256 kayıtlık thread-safe kuyruk, kayıt başına yaklaşık
16 KiB görünüm özeti; taşınca en eski aktivite düşer ve görünür boşluk sayacı
artar. Tam JSONL değişmez. QTimer 50–100 ms aralıkla en çok 32 olay tüketir;
ekrandaki liste 500 satırla sınırlanır. Büyük sonuç alanları görünümde açıkça
kısaltılır; **onay argümanları bu kısaltmaya tabi değildir**.

`tool_requested`: istendi; `tool_dispatch`: başlatıldı; `approval`: onaylandı /
reddedildi; `tool_result`: gerçek success/status/message; `tool_interrupted`:
sonucu belirsiz. `status=sent` “Komut gönderildi; sonuç doğrulanmadı”dır.
`turn_end` tek nihai tur sonucu; modelin bitmesi bütün araçların başarılı olduğu
anlamına gelmez. Yüzde ilerleme, uydurma adım listesi ve token akışı yoktur.
Ücret/token yalnız mevcut gerçek Usage verisinden gösterilebilir; MVP'de bu
ikincil detay kaldırılabilir. Sağlayıcı hatası sırları temizlenmiş tür + mesajdır.
API anahtarı alanı yoktur; sırlar YAML, argv, repr, screenshot fixture ve loga
taşınmaz. Canlı ücretli çağrı geliştirme/doğrulamanın parçası değildir.

## Çevrimdışı inceleme

`jarvis desktop --demo` kalıcı “DEMO · Gerçek işlem yapılmaz” işareti taşır.
Gerçek config/anahtar/sağlayıcı/araç keşfi kullanılmaz. TemporaryDirectory içinde
audit ve workspace, senaryolu fake provider, özel izole ToolRegistry ve gerçek
CommandRunner/AgentLoop/SafetyGate kullanılır. Demo araçları yalnız bellek içi
örnek veri okur veya değiştirir; üretim plugin listesine kaydolmaz. Örnek okuma
external olarak taint yaratır, ardından örnek yan etki aynı kapıdan onay ister.
Sonuç “Demo örneği güncellendi” der; gerçek dosya/uygulama işlemi iddia etmez.
Demo yalnız verilen örnek senaryoyu canlandırdığını açıklar. Normal kipte bu
fixture'lar veya sahte aktivite kullanılamaz.

Offscreen test QTimer ile gerçek pencereyi gösterip `window.grab().save(...)`
ile boş/hazır, aktif/onay ve dar pencere görüntülerini geçici artifact dizinine
alabilir. Ana masaüstü açılmaz; screenshot yalnız demo verisidir. Gerçek Windows
tepsi/odak/DPI doğrulaması bu görüntülerle tamamlanmış sayılmaz.

## İki uygulama parçası ve kabul

1. **Ortak runtime + Qt'siz köprü.** CLI/ses kurulumunu çıkar, audit gözlemcisini,
   işçiyi ve onay aracısını ekle. Testler: aynı kapıdan geçiş, gerçek argüman/taint,
   Evet/Hayır, timeout, iptal/kapanış, eski/çift karar, onay beklerken iptal,
   cleanup_pending sırasında yeni tur engeli, taşan audit kuyruğu ve sır temizleme.
   Çekirdek Qt yüklü olmadan import/CLI/test çalıştırır. CLI SIGINT ve ses onay
   duraklatma regresyonları korunur. Gerçek ağ/masaüstü araçları kullanılmaz.
2. **Qt arayüzü + açık demo + inceleme.** Görsel yönü panel/overlay/tepsiye uygula,
   lazy CLI komutunu ve extra'yı bağla. İsteğe bağlı testler Qt yoksa importorskip;
   varsa `QT_QPA_PLATFORM=offscreen` ile açılış, gönderim, varsayılan Hayır,
   pencere/Escape reddi, iptal, overlay ile ortak durum ve temiz kapanış çalışır.
   Evet yalnız açık tıklamayla doğrulanır; GUI timer heartbeat işçi çalışırken ve
   onay beklerken ilerler. Hazır ve onay screenshot'ları görsel olarak incelenir.
   README/ARCHITECTURE yeni komutu ve gerçek ses sınırını açıklar.

Her parçada ilgili testler; sonunda `pytest -q`, `ruff check .`, `mypy`,
`python -m bench.run --sahte`. Güvenlik regresyonu için geçici kopyada taint/onay
denetimi kaldırılınca ilgili testin kırıldığı doğrulanır. Windows 11 kabulünde
tepsi mevcut/yok, DPI, klavye, uzun Türkçe argüman, onay sırasında kapatma ve
iptal ayrıca denenir. Yapılmayan Windows/canlı model kontrolü tamamlandı denmez.
Uygulama ajanları kullanıcı talebiyle sırayla çalışır; bu belge kod yazmaz.
