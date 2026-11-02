# ADR 0051: Telegram bağlama paneli

Durum: kabul edildi

## Bağlam

Telegram'ı bağlamak için token'ı ortam değişkenine elle yazmak ve `chat_id`'yi YAML'da elle
bulup düzenlemek gerekiyordu (ADR 0016). Bu, masaüstü kullanıcısı için zor ve hataya açık.

## Karar

- **Token kaynağı sırası:** önce `telegram.token_env` ortam değişkeni, sonra Windows Kimlik
  Bilgisi Yöneticisi kaydı `Artemis/telegram` (uç anahtarlarıyla aynı kasa). Kasaya yalnız üretim
  yolunda bakılır; `build_telegram_client(env=...)` verilirse yalnız o eşleme kullanılır.
  Token YAML'a (`TelegramConfig` hâlâ `extra="forbid"`, token alanı yok), denetime, log'a, `repr`'e,
  durum metnine, argv'ye ve alt süreç ortamına girmez.
- **Ayarlar > Telegram paneli:** "Kaydet ve doğrula" (önce `getMe`; yalnız doğrulanan token
  kasaya yazılır), "Eşleştir", "Test mesajı gönder", "Bağlantıyı kes" (`chat_id`'yi YAML'dan ve
  kasa kaydını siler; ortam değişkenine dokunmaz). Ağ çağrıları QThread'dedir, tek seferde bir
  iş çalışır.
- **Eşleştirme:** `secrets` ile 6 haneli kod üretilir; `getUpdates` kısa sorgularla (en çok 120 sn,
  iptal edilebilir) beklenir. Yalnız özel sohbette (`chat.id == from.id`), iletilmemiş, düz metni
  kodun kendisi olan mesaj kabul edilir; gerisi sessizce yok sayılır. Eşleşince `telegram.chat_id`
  (tam sayı) doğrulanmış atomik yazımla config'e yazılır, kod mesajı onaylanır (offset), "Artemis
  bağlandı." bildirimi gider. HTTP 409 (başka bir süreç botu dinliyor, örn. `artemis telegram`)
  döngüye girmeden açık bir mesajla bildirilir.
- **Kapsam dışı:** iki yönlü kanalı (`artemis telegram`) arayüzden başlatmak/durdurmak ayrı
  karardır; panel yalnız bunun ayrıca çalıştırılacağını söyler.
- **Çalışma zamanı:** `chat_id`/token değişince masaüstü, sağlayıcı kaydıyla aynı yoldan
  (`_request_restart`) çalışma zamanını yeniden kurar; proaktif Telegram teslimi Artemis'i elle
  kapatmadan yeni durumu kullanır (meşgulken iş bitince).

## Sonuçlar

- Token Windows dışında saklanamaz; panel ortam değişkenini önerir.
- Eşleştirme sırasında kod dışındaki bekleyen güncellemeler tüketilir (Telegram tarafında
  onaylanır), bu yüzden eşleştirmeden önce `artemis telegram` durdurulmalıdır.
- Gerçek bot ve Telegram sunucusu yalnız kullanıcının makinesinde doğrulanır; testler loopback
  sahte sunucu ve sahte kasa kullanır.

## Ek (2026-11-03, verifier bulgusu)

Eşleştirme kodu 6 hane + 120 sn pencere tek başına tahmini engellemiyordu (bot adını bilen biri ~%0,4 şansla kodu bulabilirdi). Özel sohbetten gelen 5 yanlış düz metinden sonra eşleştirme `PairingLocked` ile iptal olur ve yeni kod gerekir (şans ~5/10^6). Grup, iletilmiş ve metin dışı gürültü sayılmaz.
