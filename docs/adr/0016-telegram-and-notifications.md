# ADR 0016 — Telegram kanalı ve bildirimler

**Bağlam:** Faz 4 iki şey ekler: Telegram ve Windows toast ile giden bildirim, ve Telegram'dan
komut alıp onayı düğmeyle soran iki yönlü kanal. CLAUDE.md 3. kural "dışarı mesaj" gönderen
işlemlerin onay istemesini ister. Bildirimde iki ilke esastır: token yalnız ortamdan
okunur, toast betiği sabit tutulur ve metin ortam değişkeniyle verilir.

**Karar:**
- **Alıcı sabittir.** Telegram yalnız config'teki `telegram.chat_id`'ye, yani kullanıcının
  kendi özel sohbetine yazar. Alıcıyı model seçemez. Bu yüzden kendine bildirim 3. kuraldaki
  "dışarı mesaj" sayılmaz. `notify_telegram` aracı SIDE_EFFECT ve `outbound = True`'dur:
  temiz turda onaysız çalışır. Lekeli turda onay ister, bağlamda dış içerik kaldıysa
  ADR 0013 gereği yeni turda da onay ister. Başka bir kişiye mesaj atan bir araç eklenirse
  CONFIRM_REQUIRED olur.
- **Toast yereldir.** `notify_desktop` SIDE_EFFECT'tir. Ağa çıkmadığı için `outbound` değildir.
  - PowerShell betiği sabittir; başlık ve gövde yalnız ortam değişkeniyle gider ve XML için
    kaçırılır. Alt sürecin ortamı `child_environment()` ile kurulur, yani token taşımaz.
  - Başarı ancak Windows bildirim ayarı `Enabled` ve `Show()` hatasız ise bildirilir.
    Windows dışında araç dürüstçe başarısız olur.
- **İki yönlü kanal** `jarvis telegram` ile ayrı bir süreçte çalışır. Masaüstüyle aynı runtime
  fabrikasını ve aynı `SafetyGate`'i kullanır. Geçmişi kendi `CommandRunner`'ındadır; bu ayrı bir
  oturumdur, bir kanal kopyası değildir. Bir mesaj ancak şu koşulların hepsini sağlarsa
  çalıştırılır:
  - sohbet özel (`private`) ve `chat.id == from.id == telegram.chat_id`;
  - mesaj iletilmiş (forward) değil, çünkü iletilen metin başkasının yazdığı dış içeriktir;
  - mesaj düz metin;
  - mesaj `max_message_age_seconds`'tan yeni.

  Başlangıçta biriken eski güncellemeler atılır. Koşulları sağlamayan mesaj runner'a hiç
  ulaşmaz, cevaplanmaz ve yalnız denetime yazılır.
- **Onay Telegram'da düğmeyle alınır.**
  - Mesaj okunur adı, kapının uyarısını ve doğrulanmış argümanların tamamını düz metin olarak
    (parse_mode yok) gösterir.
  - Argümanlar tek mesaja sığmıyorsa onay sorulmaz ve işlem reddedilir.
  - Düğme verisi tek kullanımlık bir onay kimliği taşır. Yalnız izinli kullanıcının bekleyen
    onaya bastığı düğme sayılır.
  - Süre dolarsa, başka bir kullanıcı basarsa veya kimlik eskiyse cevap hayırdır.
  - Onay beklenirken gelen yeni mesaj çalıştırılmaz; bekleyen onay hatırlatılır.
- **Gizlilik:** Telegram bir bulut hedefidir. Turda gizli içerik işlendiyse
  (`record.answer_private`) ya da tur kaydı yoksa yanıt Telegram'a gönderilmez; yerine yanıtın
  gizli içerik nedeniyle gönderilmediği yazılır.
- **Token** yalnız ortamdan okunur; adı config'te `telegram.token_env`'dir. Bu ad `_TOKEN` ile
  bitmek zorundadır, böylece denetim temizleyicisi değeri her kayıtta gizler.
  - Token URL içinde olduğundan httpx'in istek satırı (INFO) günlüğe düşmez; o logger
    WARNING seviyesine çekilir.
  - Hata mesajlarına yalnız istisna türü yazılır.
  - `repr` token göstermez; argv, config ve alt süreç ortamına girmez.
- **Denetim:** dışarı giden her Telegram çağrısı (`sendMessage`, `editMessageText`,
  `answerCallbackQuery`) yöntemi, türü, chat id'yi, metni ve sonucu ile `telegram_out`
  olayına, her toast `desktop_notification` olayına yazılır.

**Sonuçlar:**
- Telegram'dan çalışan tur süresince yalnız onay düğmeleri dinlenir; `/iptal` bu fazda yoktur
  ve tur `max_seconds` ile sınırlıdır.
- Masaüstü ve Telegram ayrı geçmiş tutar.
- Gizli içerikli turların yanıtı Telegram'da görünmez.
- Gerçek bot ve toast davranışı yalnız kullanıcının Windows makinesinde doğrulanır; testler
  yalnız loopback sahte Telegram sunucusu ve sahte PowerShell çalıştırıcısı kullanır.
