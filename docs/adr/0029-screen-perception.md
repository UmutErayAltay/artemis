# ADR 0029 — Ekran algısı: onaylı, yerel OCR, görüntü saklanmaz

**Bağlam:** Faz 6c, "ekranda ne yazıyor", "bu hatayı oku" gibi istekler için Jarvis'e ekranı
okuma yeteneği ekler. Ekran en hassas veridir: parola, mesaj, banka, iş belgesi aynı anda
görünebilir. Ekran içeriği ayrıca dış içeriktir (web sayfası, mail) ve prompt injection
taşıyabilir. Mevcut kurallar: gizlilik izni olmadan dosya/pano içeriği buluta verilmez
(CLAUDE.md 12), dış içerik turu lekeler (4), görünür yan etki testleri gerçek makineye
dokunmaz (8), ham ses saklanmaz (ses kanalı kuralı).

**Karar:**

- **Varsayılan kapalı, her okuma onaylı.** `perception.screen_enabled: true` olmadan araç
  "kapalı" der. Açıkken `screen_read` CONFIRM_REQUIRED'dır: kullanıcı her seferinde hangi
  kapsamın (etkin pencere / tüm ekran) okunacağını onay penceresinde görür. Böylece
  "kullanıcının açıkça başlattığı" şartı kapıda sağlanır; araca ikinci politika eklenmez.
  Gözetimsiz turda (ADR 0027) CONFIRM_REQUIRED zaten reddedildiği için brifing ekranı
  okuyamaz.

- **Görüntü değil metin; yerel OCR.** Windows'un yerleşik OCR'ı (`Windows.Media.Ocr`)
  sabit bir PowerShell betiğiyle çalışır: ekran/pencere bellekte yakalanır, metne çevrilir,
  yalnız metin döner. Görüntü diske yazılmaz, saklanmaz, modele veya buluta gönderilmez
  (ham ses kuralıyla aynı ilke). Çok kipli (görsel) model kullanımı bu kararın dışındadır;
  ölçülür ve istenirse ayrı ADR ister.

- **Kapsam varsayılanı etkin pencere.** `scope: active_window` (varsayılan) ya da
  `scope: screen`. Daha dar kapsam, onaylanmamış bilgilerin okunma olasılığını azaltır.
  Etkin pencere Jarvis'in kendi paneliyse bu belirtilir.

- **Gizlilik: yeni içerik türü `screen`.** Ekran metni `content_kind="screen"` taşır ve
  `perception.allow_cloud_screen: true` olmadıkça dosya/pano gibi gizli sayılır: bulut
  modele verilmez, Telegram'a gitmez, kalıcı hafızada `answer_private` olarak işaretlenir.
  Ekran metni ayrıca `external=True`'dur: turu lekeler, sonraki yan etki onay ister.

- **Başarı dürüsttür.** OCR motoru yoksa (dil paketi), yakalama başarısızsa ya da metin
  boşsa araç bunu söyler; boş metin "ekranda bir şey yok" diye sunulmaz. Windows dışında
  dürüstçe başarısız olur. Betik sabittir; argüman yalnız ortam değişkeniyle ve izinli
  değerlerle gider; çıktı sınırlıdır (yaklaşık 16 000 karakter) ve modele kapının 4000
  karakter çerçevesiyle ulaşır. Alt süreç `child_environment()` alır, zaman aşımına tabidir.

**Bitiş şartı (testle):**
- Kapalı ayarda araç çalışmaz; açıkken her çağrı onay ister, reddedilince yakalama yapılmaz.
- Gözetimsiz turda ekran okunamaz.
- Ekran metni turu lekeler ve `allow_cloud_screen` kapalıyken bulut için gizli sayılır
  (Telegram yanıtı ve onayı gönderilmez).
- OCR hatası, boş metin, zaman aşımı ve Windows dışı platform dürüst hata üretir.
- Testler PowerShell çalıştırmaz: çalıştırıcı sahtedir ve argv, ortam ve betik sözleşmesini
  yakalar (toast testleriyle aynı desen). Gerçek ekran yakalama yalnız elle, Windows'ta
  ve `@pytest.mark.disruptive` ile denenir.

**Sonuçlar:**
- Her okuma bir onay tıklaması ister; sesle "evet" onay değildir (ADR 0014).
- OCR kalitesi Windows'un dil paketlerine bağlıdır; Türkçe paketi yoksa kullanıcı profili
  dilleriyle denenir ve bu sonuçta belirtilir.
- Windows OCR ve DPI davranışı yalnız kullanıcının Windows 11 kabulüyle doğrulanır.
