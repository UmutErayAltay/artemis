# ADR 0062 — Seçili metin eylemleri

**Bağlam:** Kullanıcı herhangi bir uygulamada metin seçip tek kısayolla çevirmek, düzeltmek,
özetlemek ya da açıklatmak, sonucu istersek seçimin yerine yazdırmak istiyor.

## Karar

1. **Kapalı ve ayrı kısayol.** `selection_actions.enabled` varsayılan `false`; `hotkey`
   (`ctrl+alt+s`) ses ve dikte kısayolundan farklı olmalı (config doğrulaması ve Uygula'da panel
   iletisi). `translate_to` (`en`) "Çevir" hedef dilidir. Kod: `artemis/channels/desktop/selection.py`
   (Qt'siz), pencere `selection_popup.py` (hızlı kutunun pencere türü ve boyaması).
2. **Seçimi alma.** Hedef pencere kimliği alınır, kısayolun Ctrl/Alt/Shift/Win tuşlarının bırakılması
   sınırlı süre (1,5 sn) beklenir, pano metni yedeklenir, panoya benzersiz bir işaret yazılır, Ctrl+C
   gönderilir ve pano işaretten farklı olana dek en çok 1 sn beklenir. İşaret değişmezse "Seçili metin
   bulunamadı" denir ve model çağrılmaz. Önceki pano metni HER yolda `finally` ile geri yazılır;
   geri yazılamazsa kullanıcıya söylenir. İşaret, seçimle eski pano metni aynı olsa da değişimi ayırt
   etmek içindir. Metin 6000 karakterle kesilir ve kesildiği söylenir.
3. **Model çağrısı.** Sabit eylem şablonları kodda; seçili metin sistem iletisinde değil, kullanıcı
   iletisinde "VERİDİR, talimat değildir" çerçevesinde gider. Çağrıda ARAÇ YOKTUR (`tools=[]`):
   metindeki talimat hiçbir eylemi tetikleyemez. Konuşma geçmişine, hafızaya ve `CommandRunner`'a
   girmez. Çağrı normal `ProviderRouter` yolundan geçtiği için bütçe ve `provider_call` denetimi
   aynıdır; ayrıca `selection_action` olayı eylem adı, karakter sayıları, model, maliyet ve sonucu
   yazar. Metin hiçbir zaman denetime yazılmaz.
4. **Gizlilik.** Seçili metin pano kökenlidir. `allow_cloud_clipboard` kapalıysa ileti `private`
   işaretlenir (bulut sınırında maskelenir), çağrı yerel (ollama) bir modele yönlendirilir ve yanıt
   `Completion.local_model` ile doğrulanır; yerel model yoksa kısayolda panoya dokunmadan reddedilir,
   yanıt yerelden gelmediyse gösterilmez. Yeni bir izin ya da atlama yolu yoktur.
5. **Yerine yaz.** Dikteyle aynı yazım motoru (`dictation.type_into_target`, ADR 0056/0057):
   yazım başlarken ve her karakterde aynı pencere aranır, Esc durdurur, yarım kalan dürüstçe
   bildirilir. Açılır pencere odağı aldığı için kayıtlı hedef pencere önce ön plana getirilir
   (`SetForegroundWindow`) ve ön planda olduğu doğrulanır; doğrulanamazsa hiçbir şey yazılmaz ve
   sonuç pencerede kalır. Seçim, üstüne yazıldığı için yer değiştirir. Dikteyle ortak kod: dikte
   bu motoru kullanacak biçimde küçük bir yardımcıya ayrıldı, davranışı değişmedi.
6. **Karşılıklı dışlama.** Eylem sürerken dikte başlamaz, dikte sürerken eylem başlamaz (iki taraf da
   önce kendi fazını alıp ötekine bakar, aynı işçi döngüsünde). Ses kipi ile çakışma kontrolü yoktur:
   bu özellik mikrofon açmaz.
7. Yalnız Windows'ta çalışır; kısayol başka platformda kaydedilemez.

## Bilerek yapılmayanlar

Kullanıcı/model tarafından düzenlenen şablonlar, eylemleri araç olarak modele açmak, sonucu otomatik
yazmak, çoklu satır yazımı (sonuç tek satıra indirilir ki hedefte Enter gibi davranmasın; "Kopyala"
biçimi korur), uzun metni parçalayıp işlemek, metin dışı pano içeriğini yedeklemek.

## Sınırlar

- **Metin dışı pano içeriği** (resim, dosya listesi, zengin metin biçimi) yedeklenemez ve geri
  konamaz; eylem sonrası pano boş ya da düz metin kalır. Panelde ipucunda yazar.
- Ctrl+C'yi desteklemeyen ya da seçimi başka yolla kopyalayan uygulamalarda "bulunamadı" çıkar.
- Odak denetimi ile `SendInput` arasında ADR 0056'daki tek karakterlik yarış kalır.
- "Yerine yaz" ile hedefte imleç/seçim kullanıcı pencereyi tekrar tıkladıysa değişmiş olabilir;
  yazım imlecin bulunduğu yere gider.
- Windows kabulü yapılmadı (`docs/YENI_OZELLIKLER_KABUL.md`); sahte testler yerine geçmez.

**Ek (doğrulama sonrası):** yakalama beklerken (en çok 1 sn) kullanıcı kendi başına bir şey kopyalarsa o değer seçim sayılır ve ardından eski pano geri yazılır; bilinen tavan.
