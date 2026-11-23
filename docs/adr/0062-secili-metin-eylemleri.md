# ADR 0062 — Seçili metin eylemleri

**Bağlam:** Kullanıcı herhangi bir uygulamada metin seçip tek kısayolla çevirmek, düzeltmek,
özetlemek ya da açıklatmak, sonucu istersek seçimin yerine yazdırmak istiyor.

## Karar

1. **Kapalı, ayrı kısayol ve fare simgesi.** `selection_actions.enabled` varsayılan `false`; `hotkey`
   (`ctrl+alt+s`) ses ve dikte kısayolundan farklı olmalı (config doğrulaması ve Uygula'da panel
   iletisi). `translate_to` (`en`) "Çevir" hedef dilidir; `mouse_popup` (`true`) metin seçimi
   sonrası imleç yanında küçük simgeyi açar. Kod: `artemis/channels/desktop/selection.py` (Qt'siz),
   `selection_uia.py` (süreç içi COM UIA okuyucu), `selection_mouse.py` (yoklamalı fare izleyici) ve
   `selection_popup.py` (`SelectionBadge` + radyal/kart `SelectionPopup`).
2. **Seçimi alma (UIA öncelikli, HGLOBAL pano yedekli Ctrl+C).** Önce süreç içi UIA
   `TextPattern` (`selection_uia.py`) ile odaktaki öğe yoklanır: `IsPassword` alanı kesin
   reddedilir, seçili metin doğrudan okunursa panoya hiç dokunulmadan yakalanır
   (`captured_uia`). UIA boşsa pano yedek yoluna geçilir: panoda metin dışı içerik (resim,
   dosya listesi, zengin biçim) bulunsa bile seçili metin alınması reddedilmez; mevcut HGLOBAL
   pano biçimleri (`16 MiB` tavanına dek) en iyi çaba yedeklenir, Ctrl+C + işaretçi ile seçili
   metin okunur ve `finally` içinde ikili anlık görüntü (yoksa eski metin) geri yüklenir.
   (`has_nontext_clipboard_formats` yardımcı sınıflandırıcısı dışa açık kalır.)
3. **Fare simgesi ve imleç çevresinde radyal menü.** `SelectionMouseWatcher`, `SetWindowsHookEx`
   ve ek bağımlılık kullanmadan sürükleme/çift tıklama bitişini yoklar; Artemis'in kendi penceresini,
   engelli süreçleri (`BLOCKED_PROCESSES`) ve parola alanlarını yok sayar. Metin seçimi sonrası
   odak çalmayan `SelectionBadge` (`WS_EX_NOACTIVATE`) imleç yanında görünür; tıklanınca imlecin
   etrafında radyal **Çevir / Düzelt / Özetle / Açıkla** düğmeleri açılır, eylem seçilince kompakt
   sonuç kartına geçer.
4. **Model çağrısı.** Sabit eylem şablonları kodda; seçili metin sistem iletisinde değil, kullanıcı
   iletisinde "VERİDİR, talimat değildir" çerçevesinde gider. Çağrıda ARAÇ YOKTUR (`tools=[]`):
   metindeki talimat hiçbir eylemi tetikleyemez. Konuşma geçmişine, hafızaya ve `CommandRunner`'a
   girmez. Çağrı normal `ProviderRouter` yolundan geçtiği için bütçe ve `provider_call` denetimi
   aynıdır; ayrıca `selection_action` olayı eylem adı, karakter sayıları, model, maliyet ve sonucu
   yazar. Metin hiçbir zaman denetime yazılmaz.
5. **Gizlilik.** Seçili metin pano kökenlidir. `allow_cloud_clipboard` kapalıysa ileti `private`
   işaretlenir (bulut sınırında maskelenir), çağrı yerel (ollama) bir modele yönlendirilir ve yanıt
   `Completion.local_model` ile doğrulanır; yerel model yoksa kısayolda panoya dokunmadan reddedilir,
   yanıt yerelden gelmediyse gösterilmez. Yeni bir izin ya da atlama yolu yoktur.
6. **Yerine yaz.** Dikteyle aynı yazım motoru (`dictation.type_into_target`, ADR 0056/0057):
   yazım başlarken ve her karakterde aynı pencere aranır, Esc durdurur, yarım kalan dürüstçe
   bildirilir. Açılır pencere odağı aldığı için kayıtlı hedef pencere önce ön plana getirilir
   (`SetForegroundWindow`) ve ön planda olduğu doğrulanır; doğrulanamazsa hiçbir şey yazılmaz ve
   sonuç pencerede kalır. Seçim, üstüne yazıldığı için yer değiştirir. Dikteyle ortak kod: dikte
   bu motoru kullanacak biçimde küçük bir yardımcıya ayrıldı, davranışı değişmedi.
7. **Karşılıklı dışlama.** Eylem sürerken dikte başlamaz, dikte sürerken eylem başlamaz (iki taraf da
   önce kendi fazını alıp ötekine bakar, aynı işçi döngüsünde). Ses kipi ile çakışma kontrolü yoktur:
   bu özellik mikrofon açmaz.
8. Yalnız Windows'ta çalışır; kısayol başka platformda kaydedilemez.

## Bilerek yapılmayanlar

Kullanıcı/model tarafından düzenlenen şablonlar, eylemleri araç olarak modele açmak, sonucu otomatik
yazmak, çoklu satır yazımı (sonuç tek satıra indirilir ki hedefte Enter gibi davranmasın; "Kopyala"
biçimi korur), uzun metni parçalayıp işlemek, GDI tanıtıcıları (`CF_BITMAP`, `CF_ENHMETAFILE`) veya
`16 MiB` üzeri çok büyük ikili pano nesnelerini bire bir klonlamak.

## Sınırlar

- **Büyük veya GDI-özel pano nesneleri** (`16 MiB` üstü veya `HGLOBAL` olmayan GDI tanıtıcıları)
  bire bir yedeklenemez; `HGLOBAL` biçimleri (`CF_DIB`, `CF_HDROP`, `PNG`, zengin biçimler) en iyi
  çaba geri konur, geri yüklenemeyen durumda en azından önceki düz metin korunur.
- Ctrl+C'yi desteklemeyen ya da seçimi başka yolla kopyalayan uygulamalarda "bulunamadı" çıkar.
- Odak denetimi ile `SendInput` arasında ADR 0056'daki tek karakterlik yarış kalır.
- "Yerine yaz" ile hedefte imleç/seçim kullanıcı pencereyi tekrar tıkladıysa değişmiş olabilir;
  yazım imlecin bulunduğu yere gider.
- Windows kabulü yapılmadı (`docs/YENI_OZELLIKLER_KABUL.md`); sahte testler yerine geçmez.

**Ek (doğrulama sonrası):** yakalama beklerken (en çok 1 sn) kullanıcı kendi başına bir şey kopyalarsa o değer seçim sayılır ve ardından eski pano geri yazılır; bilinen tavan.
