# Jarvis masaüstü — ilk sürüm planı

Uygulama öncesi plan; [ADR 0011](adr/0011-desktop-ui.md). Kullanıcı F0–F3 üzerine
GUI'yi şimdi istedi: F4 hafıza beklenmez; F3'ü büyütmeme kuralına bu çalışma için
istisnadır. Windows 11 hedeflenir, Linux çekirdeği Qt olmadan çalışır.

## Kapsam ve görsel yön

`jarvis desktop --config config/config.yaml`: tek QApplication, ana panel,
yüzen hızlı komut penceresi, sistem tepsisi. Metin komutları mevcut runner'dan
geçer. Ses bağlantısı, kalıcı hafıza, Telegram, otomatik başlangıç, küresel
kısayol, ayar editörü ve model seçici kapsam dışıdır. F3 ses CLI'sı korunur;
GUI'de çalışan mikrofon/dinleme animasyonu yoktur. Bilgi gerekirse “Ses bu
masaüstü sürümünde bağlı değil.” olur. Marka Jarvis; artemis uyandırma sözcüğüdür.

- Zemin `#0B1220`, panel `#111C2D`, yükseltilmiş yüzey `#17263A`, metin
  `#E6EDF5`, ikincil metin `#A9B8C9`, vurgu `#53D6C7`; vurgu üstünde koyu metin.
  Amber onay, kırmızı hata içindir; durumlar renk yanında metin taşır. 8 px
  aralık sistemi, 24 px panel boşluğu, 12–16 px köşeler, ince sınırlar kullanılır.
- Yaklaşık 1180×780 mantıksal px: Jarvis başlığı, gerçek durum, yapılandırılmış
  model adı; %65 sohbet / %35 “İşlem adımları” yeniden boyutlanabilir bölmesi.
  900 px altında adımlar açılır panel olur. Altta çok satırlı komut, “Gönder”,
  çalışırken “İptal et”. Hazır durumu yalnız runtime kurulunca gösterilir;
  model adı bağlantı sağlığı değildir. Yüzde ilerleme/sahte grafik yoktur.
- Enter gönderir, Shift+Enter satır açar; çalışan turda gönderim kapanır, taslak
  korunur. Sohbet seçilebilir düz metindir; HTML/link çalıştırılmaz. Görünen
  oturum geçmişi AgentLoop'a gizli hafıza olarak eklenmez. Boş ekran kısa ve sakindir.
- Yaklaşık 480×160 overlay aynı runtime/durumu kullanır. Tepsi: “Paneli aç”,
  “Hızlı komut”, “Çıkış”; QPainter ile sade özgün J simgesi yeterlidir.
  Panel kapanışı tepsiye gizleyebilir; bekleyen onay önce reddedilir. Tepsi yoksa
  uygulama görünmez kalmaz. Gizleme aktif turu iptal etmez; “Çıkış” temizler.
- Segoe UI/sistem fontu, en az 14 mantıksal px; Qt düzenleri ve kaydırma.
  Belirgin klavye odağı, erişilebilir ikon adları, Türkçe İ/ı/ş/ğ ve okunabilir
  kontrast gerekir. 100/150/200% DPI, dar pencere ve uzun argümanlar incelenir.

## Modüller ve ortak kurulum

| Dosya | Sorumluluk |
| --- | --- |
| `channels/runtime.py` | Qt'siz ortak kurulum ve Runtime referansları |
| `channels/desktop/bridge.py` | Qt'siz işçi, mesajlar, onay aracısı, olay kuyruğu |
| `channels/desktop/app.py` | Lazy Qt girişi; panel, overlay, tepsi ve tema |
| `channels/desktop/demo.py` | Açık demo için izole fake provider/araçlar |
| `safety/audit.py` | İsteğe bağlı temizlenmiş kayıt gözlemcisi |

Yollar `jarvis/` altındadır. `desktop` extra: `PySide6==6.12.0`; paket __init__
Qt import etmez. CLI yalnız desktop dalında Qt yükler; eksikse Türkçe kurulum
mesajı verir. Chat/voice kurulum tekrarı ortak fabrikaya çıkarılır: settings,
workspace, audit, registry, gate, context, build_provider, AgentLoop, runner.
Onay fabrikası gate/context alıp tam `(tool_name, arguments) -> bool` döndürür;
CLI terminal, GUI köprü sunar. Ses pause/resume ve SIGINT korunur. Provider ve
registry enjeksiyonu test/demo içindir; ikinci güvenlik/sağlayıcı kurulumu yoktur.

## İşçi ve onay sözleşmesi

Qt ana thread'de, ayrı tek Python işçisi kendi asyncio loop/Runtime'ını oluşturur.
GUI'de asyncio.run/qasync, Future.result veya blocking join; işçide QWidget yoktur.
Komutlar loop.call_soon_threadsafe ile gider. Runner iptali ve asyncio.Event.set
yalnız işçide yapılır. Tek aktif tur; kuyruk yoktur. İşçi de çift gönderimi reddeder;
cleanup_pending bitmeden yeni tur başlamaz. Frozen dataclass mesajları yeterlidir:

| Yön | Mesaj |
| --- | --- |
| GUI → işçi | Submit(request_id, text), Cancel(request_id), Shutdown() |
| GUI → onay aracısı | Decide(request_id, turn_id, approval_id, approved) |
| İşçi → GUI | State(request_id?, state, message), Finished(request_id, RunResult), Failed(request_id, error_type, message) |
| İşçi → GUI | Approval(request_id, turn_id, approval_id, tool_name, arguments, notice, deadline) |
| Audit → GUI | Activity(request_id, turn_id, seq, event, fields), ActivityGap(dropped_count) |

İstek/onay kimlikleri yeni UUID; turn_id çekirdeğin gerçek UUID'sidir. Eski
iletiler mevcut turu değiştirmez. Metin/argüman alanları repr=False; Settings ve
provider GUI'ye taşınmaz. Qt queued signal veya QTimer + thread-safe kuyruk kullanılır.
Kontrol/onay/nihai sonuç iletileri aktivite kuyruğundan ayrıdır ve düşürülmez.

SafetyGate callback'i senkron çağırır; imza değişmez. Callback doğrulanmış argüman
kopyasını ve gate.pending_notice'ı yakalar; kilitli aracıda üç kimlikle eşleşen
concurrent.futures.Future[bool] oluşturur. Yalnız işçi, tur deadline'ına kadar
Future.result bekler; timeout False. Karar bloke asyncio loop'a sıraya konmaz:
GUI, aracının thread-safe metoduyla Future'ı çözer. asyncio.Future kullanılmaz.
İptal/kapanış önce pending onayı kilit altında False ile geçersizleştirir, sonra
loop'a iptal yollar. Callback iptal niyetini denetler, gerekirse kendi thread'inde
runner iptalini uygular; kapının onay sonrası iptal/deadline kontrolü korunur.
Finally pending kaydını kaldırır; eski/çift/geç karar yeni onayı çözemez.

Onay paneli gerçek araç adını, tam seçilebilir/kaydırılabilir doğrulanmış JSON'u
ve kapının notice/taint cümlesini gösterir. Argüman düzenlenmez. “Hayır” varsayılan
ve odaklıdır; Enter/Escape/çarpı onay vermez. “Evet” açık etkinleştirme gerektirir.
GUI risk politikası kurmaz ve araç dispatch etmez. Çıkış gönderimi kapatır, onayı
reddeder, turu iptal eder; Qt açıkken “Temizleniyor” gösterilir. Aktif görev ve
çekirdek temizliği bitince işçi/uygulama kapanır; thread terminate veya çalışan
göreve loop.close yoktur. OS'ye ulaşan eylemin geri alındığı söylenmez.

## Gerçek olaylar ve çevrimdışı inceleme

AuditLog'da bugün observer yoktur. İsteğe bağlı on_record, mevcut redact ve başarılı
JSONL yazımından sonra, dosya kilidi dışında bağımsız kayıt kopyası alır. Disk
hatası hata olarak kalır. Gözlemci yalnız put_nowait yapar; queue-full durumunu
kendi içinde işler. 256 kayıt, kayıt başına yaklaşık 16 KiB görünüm özeti;
taşınca eski aktivite düşer ve boşluk sayacı görünür. Tam JSONL korunur. QTimer
50–100 ms'de en çok 32 olay alır; liste 500 satırdır. Onay argümanları kısaltılmaz.
Tool requested/dispatch/result ve approval olayları gerçek durumlarına çevrilir;
`sent`: “Komut gönderildi; sonuç doğrulanmadı”; interrupted: belirsiz. Turn_end
nihai tur sonucudur, bütün araçların başarısı değildir. Token akışı uydurulmaz.
Anahtar alanı yoktur; sırlar config/argv/repr/loga taşınmaz, ücretli çağrı yapılmaz.

`jarvis desktop --demo`: kalıcı “DEMO · Gerçek işlem yapılmaz”; TemporaryDirectory,
fake provider, özel ToolRegistry ve gerçek runner/loop/gate. Gerçek config,
anahtar, sağlayıcı ve plugin keşfi kullanılmaz. Demo araçları yalnız bellek içi
örnek veri okur/değiştirir: external okuma ardından taint nedeniyle onay isteyen
yan etki. Sonuç açıkça demo sonucudur. Normal kip bu fixture'ları kullanmaz.
Offscreen Qt testinde window.grab() ile hazır/onay/dar pencere PNG'leri alınır;
yalnız demo verisi kullanılır. Windows tepsi/DPI/odak kabulü ayrıca yapılır.

## İki uygulama parçası ve kabul

1. Ortak runtime, audit observer, Qt'siz işçi/onay köprüsü. Testler: aynı gate,
   tam argüman/taint, ret/onay/timeout, iptal/kapanış, eski/çift karar, pending
   cleanup sırasında yeni tur engeli, audit taşması/redaction. Qt'siz çekirdek,
   CLI SIGINT ve ses onay regresyonları korunur; gerçek araç/ağ kullanılmaz.
2. Qt panel/overlay/tepsi, lazy CLI/extra, açık demo ve README/ARCHITECTURE.
   Qt yoksa smoke testleri importorskip; varsa offscreen açılış/gönderim,
   varsayılan Hayır/Enter/Escape/kapanış, açık Evet, iptal, ortak overlay durumu,
   temiz kapanış. GUI timer heartbeat çalışma ve onay sırasında ilerlemelidir.
   Screenshot'lar görsel incelenir; Windows gerçek kabulü ayrı raporlanır.

Son kontroller: pytest -q, ruff check ., mypy, python -m bench.run --sahte.
Geçici kopyada taint/onay kontrolünü kaldırınca güvenlik testinin kırıldığı
kanıtlanır. Yapılmayan Windows/canlı model kontrolü tamamlandı denmez. Uygulama
ajanları kullanıcı talebiyle sırayla çalışır; bu plan kod uygulamaz.
