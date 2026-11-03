# ADR 0053: Telegram'dan bilgisayar kullanımı

Durum: kabul edildi

## Bağlam

`computer_use` (ADR 0039) yalnız masaüstü uygulamasında çalışıyordu çünkü Esc ile durdurma ve
adım onayı `services["computer_host"]` ile oradaydı. Masaüstü içi Telegram kanalı (ADR 0052)
host taşımadığından araç orada "yalnız masaüstü" diye reddediyordu.

## Karar

- **Yalnız gömülü kip:** `serve_telegram(embedded=True)` kanalın kendi runtime'ına bir
  `ComputerHost` kurar (`TelegramChannel.enable_computer_use`). `artemis telegram` (CLI) host
  taşımaz ve araç orada reddetmeye devam eder.
- **Onay masaüstü kipini izler (ikinci politika yok):** `computer_use.mode: auto` ise SafetyGate'in
  mevcut `auto_mode_ok` yolu ve oturum içi hassas adım atlaması aynen çalışır; `accept` ise hem
  başlangıç hem hassas adımlar mevcut Telegram düğmeleriyle (`channel.approve`) sorulur, masaüstü
  penceresi hiç açılmaz. Telegram runtime'ı ayrı `Settings` taşıdığından kip, panelin yazdığı
  config'ten HER TURDAN ÖNCE yeniden okunur; okunamazsa `accept` seçilir (belirsizlikte sor).
  Bu seçimin nedeni: kullanıcı oto onayı panelden kapattığında Telegram'ın eski kipte kalması
  sessiz bir güvenlik gevşemesi olurdu.
- **`/dur`:** özel, yetkili sohbetten, normal mesajla aynı denetimlerden (`check_message`) geçen
  `/dur` oturum bayrağını kaldırır ve turu Esc ile aynı yoldan iptal ettirir
  (`ComputerHost.stop_active`). Tur sürerken ana yoklama döngüsü bloklu olduğundan, yalnız host
  aktifken `get_updates(offset, 0)` ile ONAYLAMADAN (offset ilerlemez) bakan bir izleyici görev
  çalışır; görülen `/dur` sonra normal akışta sessizce atlanır. Onay beklerken gelen `/dur`
  onay yolunda işlenir (adım reddedilir, oturum durur). İki yoklama aynı anda `getUpdates`
  açmasın (409) diye ortak bir kilit vardır. Masaüstü Esc kısayolu da çalışır (aynı makine);
  ikisi de aynı `stop_active`'e gider.
- **Ortak ekran kilidi:** tek fiziksel ekran için süreç genelinde tek `SCREEN_LOCK`
  (`artemis/computer/session.py`); `ComputerHost.session()` onu bloklamadan alır, alamazsa
  `ScreenBusyError` fırlatır ve araç "başka bir oturum ekranı kullanıyor" der (çökmez, beklemez).
  En küçük doğru tasarım olarak ana bilgisayar başına `_active` yerine bu seçildi: masaüstü köprüsü
  ve Telegram servisi ayrı iş parçacıklarında, birbirinden habersiz host kurar; kilidi enjekte
  etmek iki kurulum yoluna ortak nesne taşımayı gerektirirdi. Kilit onay/ayar/denetim durumu
  değil, fiziksel kaynağın (fare, klavye) tekliğidir; bu yüzden kural 10'un yasakladığı global
  durum sayılmaz. Tek tek kilit ve tek host yerine ortak kilit seçildi çünkü host'ların onay ve
  durum çıktıları kanala özeldir.
- **Son kare (`telegram.send_computer_screenshot`, varsayılan `false`):** oturum son taze kareyi
  `on_frame` ile verir; kanal onu yalnız RAM'de tutar (yalnız bayrak açıkken) ve tur sonunda
  gönderip atar. Gönderim şu üç kapı da açıksa olur: bayrak, `perception.allow_cloud_screen`
  (Telegram buluttur) ve tur/yanıt/geçmiş gizli değil (`answer_private` ve `_private_reason`).
  `TelegramClient.send_photo` multipart yollar; `telegram_out` olayı (`kind: photo`) yazılır,
  görüntü baytları denetime/diske girmez. Birden çok monitörde yalnız ilk ekran gönderilir.
- **Adım bildirimi yok:** adım gerekçesi ekrandan türeyen model metnidir ve gizlilik kapıları
  yalnız son kare/yanıt için tanımlıdır; adımları Telegram'a akıtmak bu kapıyı delerdi. Nihai
  sonuç normal Telegram yanıtıdır.

## Riskler

- **Kullanıcı başında olmayabilir:** uzaktan komutla ekran, fare ve klavye kullanılır. Güvence
  zinciri: yalnız yetkili özel sohbet, mod `auto` ise kullanıcının bilinçli seçimi, `accept`
  ise her hassas adım telefonda onay, kod kuralları (uygulama/tuş engelleri, adım ve süre
  sınırı), `/dur` ve masaüstü Esc. `auto` kipinde Telegram hesabı ele geçirilirse ekran
  uzaktan sürülebilir: kullanıcı bunu kabul ederek `auto`'yu seçmiştir.
- **Ekran görüntüsü bulut sohbetine gider** (bayrak açıkken): ekranda görünen sır PNG'de
  kalır, kodla süzülemez; bu yüzden bayrak varsayılan kapalıdır ve ayrıca
  `allow_cloud_screen` ister.
- **Sınır:** `/dur` ve onay izleyicisi yoklama aralığı (1 sn) kadar gecikebilir.

## Sonuçlar

- ADR 0039 ve 0052 bu karara bağlanır. Gerçek ekran/Telegram doğrulaması kullanıcının
  makinesindedir; testler sahte arka uç ve loopback sahte sunucu kullanır.
