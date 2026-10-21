# ADR 0039: Bilgisayar kullanımı (ekranı görüp fare ve klavyeyle iş yapma)

Durum: kabul edildi (2026-10-21), uygulama sürüyor.

## Bağlam

Kullanıcı "ekranımı gör, şunu şöyle yap" diyebilmek istiyor. Var olanlar yetmiyor:
`screen_read` yalnız OCR metni döner (ADR 0029), `window_control` pencerenin içine dokunmaz
(ADR 0031), tarayıcı MCP'si ayrı ve girişsiz bir Chromium'dur (ADR 0032).

Bu, Jarvis'in en riskli yeteneğidir: ekranda görünen her şey (mesaj, parola alanı, belge)
modele gider ve ekrandaki bir metin (web sayfası, e-posta) modeli yönlendirmeye çalışabilir.
Kural 4 dış içerikten sonra her yan etkiye onay ister; oturumun her adımı ekran görüntüsünden
doğduğu için adım adım onay kullanılamaz hâle gelir. Emsal ADR 0023'tür: kod ajanı tek onayla
başlar, sonra kendi başına çalışır; riski sınırlar ve görünürlük taşır.

## Karar

1. **Varsayılan kapalı.** `computer_use.enabled: false`. Açıkken tek araç vardır:
   `computer_use(goal)`. Risk CONFIRM_REQUIRED; onay penceresi hedef metnini tam gösterir.
   `external=True`, `content_kind="screen"`. Gözetimsiz tur (ADR 0027) bunu kapıda reddeder.
2. **Yalnız masaüstünde.** Oturum `context.services["computer_session"]` ister; bunu yalnız
   masaüstü uygulaması kaydeder. Telegram ve CLI'de araç "yalnız masaüstünde" der: kullanıcı
   ekranın başında değilse durduramaz. Kanal switch'i eklenmez (kural 9).
3. **Oturum kendi döngüsüdür.** Her adımda: birincil ekranın görüntüsü bellekte alınır
   (diske yazılmaz, `screenshot_width`'e küçültülür, PNG), hedef ve önceki adımların kısa özeti
   ile `computer_use.model` görsel modele gider. Model araç şeması değil tek bir JSON eylem
   döner; pydantic ile katı doğrulanır, bozuk yanıt bir kez yeniden istenir, sonra oturum biter.
   Eylemler: `click`, `type`, `key`, `scroll`, `wait`, `done`, `ask_user`. Her eylem `reason`
   ve `sensitive` taşır.
4. **Kodla zorlanan sınırlar** (modelin beyanına güvenilmez):
   - Ön plandaki pencere ya da tıklama noktasındaki pencere Jarvis'in kendi süreci, bir
     kabuk/terminal (`cmd`, `powershell`, `pwsh`, `WindowsTerminal`, `conhost`), bir başlatıcı
     (`explorer`, Başlat/arama süreçleri, `mshta`, `wscript`, `cscript`, `rundll32`, `msiexec`,
     `control`), Unix kabuğu ve yorumlayıcı (`wsl`, `bash`, Git Bash `mintty`, `python`),
     gömülü terminalli editör (VS Code, Cursor), `regedit`, `mmc`, `taskmgr` ya da
     parola yöneticisi ise eylem çalışmaz, oturum durur. Hassas onaydan sonra yeniden denetlenir.
     Model Jarvis'in onay penceresine tıklayamaz. Uygulama açmak ana döngünün
     `open_application` aracıyla olur, Dosya Gezgini'nden değil.
   - Engelli tuşlar: Win tuşu içeren her kombinasyon, `ctrl+alt+delete`, Ctrl+Esc içeren her
     kombinasyon (Başlat menüsü, Görev Yöneticisi). `alt+f4` ve Enter içeren her tuş kodla
     hassastır (5. madde).
   - `type` en çok 500 karakter, satır sonu içeremez (Enter ayrı `key` eylemidir).
   - `max_steps` (varsayılan 25), `max_minutes` (varsayılan 5) aşılırsa oturum durur.
5. **Hassas adım onayı.** Model `sensitive: true` derse (gönder, satın al, sil, öde, paylaş)
   ya da 4'teki hassas kural tutarsa adım çalışmadan önce ekrandaki onay penceresinde
   eylem, koordinat ve gerekçe gösterilir; varsayılan hayırdır, ret oturumu bitirir.
6. **Durdurma ve görünürlük.** Oturum boyunca Esc genel kısayolu kaydedilir (ADR'deki
   RegisterHotKey yolu); basınca o anki eylemden sonra hiçbir eylem çalışmaz. Panel ve pet
   "Bilgisayarı kullanıyorum · adım n: gerekçe" gösterir.
7. **Gizlilik.** Görüntü buluta yalnız `perception.allow_cloud_screen: true` iken gider;
   model Ollama ise yerel kalır. Denetime her adımın eylem türü, koordinatı, gerekçesi,
   hassaslık ve onay sonucu yazılır; yazılan metnin yalnız uzunluğu, görüntü hiç yazılmaz.
8. **Dönüş.** Ana döngüye özet, adım sayısı ve bitiş nedeni döner (`done`, `ask_user`,
   sınır, ret, durdurma, engel); dış içerik olarak işaretlenir. `done` başarıyı kanıtlamaz:
   sonuç `status="sent"` olur, model "yaptım" dese bile `success=True` dönülmez (kural 5).
9. **v1 kapsamı.** Yalnız birincil monitör, süreç DPI farkındalığı ile fiziksel piksel.
   Sürükleme, çoklu monitör, sesle adım adım yönlendirme sonraki sürümdür.

## Sonuçlar

- Engel listesi eksik kalabilir (yeni bir başlatıcı, terminali olan başka bir uygulama).
  Enter'ın kodla onaylı olması komut çalıştırma ve klavyeyle gönderme yollarını kapatır;
  tıklamayla yapılan gönderim (ör. "Gönder" düğmesi) yine modelin `sensitive` beyanına kalır.
  Bunu adım sınırı ve Esc azaltır, tamamen önlemez; özellik bu yüzden varsayılan kapalıdır.
- v2 adayı: oturum başında kullanıcının seçtiği hedef uygulamalar için izin listesi
  (engel listesi yerine).
- Görüntü gönderebilmek için sağlayıcı mesajları görüntü parçası taşır (`Message.image_png`).
  Gizli (private) mesajda görüntü de maskelenir; görüntüyü desteklemeyen sağlayıcı
  (Claude CLI) dürüstçe hata verir.

## Uygulama dalgaları

1. `jarvis/computer/actions.py`: eylem modeli, JSON ayrıştırma, koordinat ölçekleme,
   tuş/metin kuralları, hassaslık (saf Python, testli).
2. `jarvis/computer/png.py`: BGRA ekran tamponundan küçültülmüş PNG (numpy + zlib).
3. Sağlayıcılarda görüntü parçası (OpenRouter/OpenAI uyumlu/Anthropic/Ollama).
4. `jarvis/computer/windows.py`: ekran görüntüsü (GDI), SendInput, ön plan süreç adı, DPI.
5. `jarvis/computer/session.py` + `computer_use` aracı + masaüstü bağlantısı (Esc, onay, durum).
