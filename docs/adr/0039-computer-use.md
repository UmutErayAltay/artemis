# ADR 0039: Bilgisayar kullanımı (ekranı görüp fare ve klavyeyle iş yapma)

Durum: kabul edildi (2026-10-21), uygulama sürüyor.

## Bağlam

Kullanıcı "ekranımı gör, şunu şöyle yap" diyebilmek istiyor. Var olanlar yetmiyor:
`screen_read` yalnız OCR metni döner (ADR 0029), `window_control` pencerenin içine dokunmaz
(ADR 0031), tarayıcı MCP'si ayrı ve girişsiz bir Chromium'dur (ADR 0032).

Bu, Artemis'in en riskli yeteneğidir: ekranda görünen her şey (mesaj, parola alanı, belge)
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
   - Girdinin düşeceği pencere (tıklama/kaydırmada noktadaki pencere, yazma/tuşta ön plan)
     Artemis'in kendi süreci, bir
     kabuk/terminal (`cmd`, `powershell`, `pwsh`, `WindowsTerminal`, `conhost`), bir başlatıcı
     (`explorer`, Başlat/arama süreçleri, `mshta`, `wscript`, `cscript`, `rundll32`, `msiexec`,
     `control`), Unix kabuğu ve yorumlayıcı (`wsl`, `bash`, Git Bash `mintty`, `python`),
     gömülü terminalli editör (VS Code, Cursor), `regedit`, `mmc`, `taskmgr` ya da
     parola yöneticisi ise eylem çalışmaz. Engel modele bildirilir, üçüncü engelde oturum
     biter; engel kullanıcı reddi sayılmaz. Hassas onaydan sonra yeniden denetlenir.
     Model Artemis'in onay penceresine tıklayamaz. Uygulama açmak ana döngünün
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
   RegisterHotKey yolu); basınca o anki eylemden sonra hiçbir eylem çalışmaz ve tur İptal
   düğmesiyle aynı yoldan iptal edilir: süren model çağrısı ve bekleme hemen kesilir, model
   aracı yeniden çağıramaz. OS'ye gönderilmiş eylem geri alınmaz. Panel ve pet
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

## Ek karar (2026-10-22): çoklu ekran ve kipler

Kullanıcı ilk canlı denemelerden sonra iki şey istedi: oturum ikinci ekranı da görsün ve
Claude Code'daki gibi bir otomatik kip olsun ("kontrol bende, gerekirse Esc ile durdururum").

1. **Çoklu ekran.** Her monitör ayrı görüntü olarak modele gider (aynı mesajda, sırayla
   "Ekran 1", "Ekran 2"); sanal masaüstünü tek görüntüye küçültmek metni okunmaz yapardı.
   Eylem `screen` (varsayılan 1) taşır, koordinat o ekranın görüntüsüne göredir. Ekran 1
   birincil monitördür. Fare koordinatları sanal masaüstü üzerinden verilir.
2. **Pencereyi ekrana taşıma.** `window_control` yeni `move_to_screen` eylemi ve `screen`
   argümanı alır; pencere hedef monitörde ekranı kaplar. Risk SIDE_EFFECT kalır (ADR 0031).
3. **Kipler.** `computer_use.mode: accept | auto` (varsayılan `accept`), Ayarlar'da seçilir.
   - `accept`: bu ADR'nin 1. ve 5. maddeleri aynen.
   - `auto`: oturum içindeki hassas adım ve Enter onayları sorulmaz (denetime
     `auto_approved=true` yazılır); temiz turda oturum başlangıcı da sorulmaz (risk
     SIDE_EFFECT). `computer_use` ve `window_control` (`auto_mode_ok`) ekran adımları
     her turu lekeliyor, bu yüzden `auto` kipinde lekeli turda bile onaylanmaz;
     atlama `approval_skipped` olarak denetime yazılır. `screen_read` de `auto_mode_ok`
     taşır (2026-10-22, kullanıcı isteği): bu kipte computer_use ekran görüntüsünü zaten
     sormadan modele verirken yerel OCR daha azını açar. Gözetimsiz tur yine okuyamaz,
     buluta gönderim `allow_cloud_screen`'e bağlı kalır. Diğer araçlar kural 4'le
     dış içerikten sonra onay beklemeye devam eder.
   - Kodla zorlanan engeller (4. madde), adım/süre sınırı ve Esc iki kipte de aynıdır.
   - Bedel: ekrandaki bir enjeksiyon `auto` kipte sorulmadan mesaj gönderebilir ya da form
     onaylayabilir. Bu kullanıcının bilerek seçtiği bir takastır; varsayılan `accept` kalır.

## Uygulama dalgaları

1. `artemis/computer/actions.py`: eylem modeli, JSON ayrıştırma, koordinat ölçekleme,
   tuş/metin kuralları, hassaslık (saf Python, testli).
2. `artemis/computer/png.py`: BGRA ekran tamponundan küçültülmüş PNG (numpy + zlib).
3. Sağlayıcılarda görüntü parçası (OpenRouter/OpenAI uyumlu/Anthropic/Ollama).
4. `artemis/computer/windows.py`: ekran görüntüsü (GDI), SendInput, ön plan süreç adı, DPI.
5. `artemis/computer/session.py` + `computer_use` aracı + masaüstü bağlantısı (Esc, onay, durum).

## Ek karar (2026-10-28): ekranda doğrulama, hız ve doğruluk

Canlı denemede "YouTube'u aç, rastgele video aç, 5. dakikaya sar, o sekmeyi kapat" işi
hedef sekme ayırt edilmeden üç sekmeyi kapattı ve sonuç hep "doğrulanmadı" döndü.

1. **Son ekran doğrulaması.** Model `done` dedikten sonra (yalnız o zaman) oturum tüm
   monitörlerin TAZE görüntüsünü alır ve aynı sağlayıcıya `VERIFY_PROMPT` ile hedef, modelin
   bitti notu ve görüntüleri verir; yanıt `{"verified": bool, "evidence": "..."}` olarak katı
   ayrıştırılır (bozuksa bir yeniden deneme). Temkinli kural: ekranda görünmüyorsa `false`.
   Sağlayıcı hatası, süre, Esc ve görüntü hatası nedeni "error" yapmaz; sonuç `done` +
   `verified=False` kalır. Denetime `computer_verify` yazılır.
2. **Kural 5'in bu araç için değişikliği.** `done` + `verified=True` ise sonuç `ok`/
   `success=True`, mesaj "Ekranda doğrulandı" der; aksi hâlde `sent`/`success=False`.
3. **Sınırı.** Bu, bağımsız bir ekran kontrolüdür ama yine bir MODEL yargısıdır: aynı model
   ailesi hem eylemi hem doğrulamayı yapar, yanlış pozitif olabilir; ekranda izi kalmayan işler
   (ör. gönderilmiş mesajın karşıya ulaşması) doğrulanamaz. Bu yüzden mesaj "kesin" demez.
4. **Hız ve doğruluk.** PNG üretimi `asyncio.to_thread`'e alındı; `step_delay_seconds`
   varsayılanı 0.6 -> 0.3. Hiçbir güvenlik denetimi gevşetilmedi. Sistem istemine
   klavye/URL tercihi, URL zaman parametresiyle sarma, kapatmadan önce sekme başlığını okuyup
   yalnız hedefi kapatma, her eylem sonrası doğrulama ve "ekranda görünmeden done deme" eklendi.
