# ADR 0057 — Dikte kipi

**Bağlam:** Yerel STT yalnız asistan komutu için kullanılıyordu. Kullanıcı herhangi bir
uygulamaya konuşarak yazabilmek istiyor: kısayola bas, konuş, tekrar bas.

## Karar

1. **Komut yolu değildir.** Tanınan metin `CommandRunner`'a, modele, konuşma geçmişine ya da
   hafızaya gitmez; LLM çağrısı yoktur. Uyandırma sözcüğü içerse bile olduğu gibi yazılır.
   Kod: `artemis/channels/desktop/dictation.py` (Qt'siz), köprüde `toggle_dictation()`.
2. **Aç/kapa kısayolu.** `dictation.hotkey` (varsayılan `ctrl+alt+d`), mevcut `GlobalHotkey`
   (RegisterHotKey) ile kendi iş parçacığında kaydedilir. Basılı tutma yok: RegisterHotKey tuş
   bırakmayı bildirmez ve klavye kancası kurulmaz. `dictation.enabled` varsayılan `false`;
   kapalıyken kısayol hiç kaydedilmez. `voice.listening_hotkey` ile aynı tuş reddedilir
   (config doğrulaması ve Uygula'da panel iletisi).
3. **Mikrofon yalnız iki basış arasında** açılır (`SoundDeviceMicrophone`, 16 kHz). Ham ses
   saklanmaz; yalnız bitişte tek parça halinde çözülür, kısmi tanıma yazılmaz.
   `dictation.max_seconds` (varsayılan 120) kaydı kendiliğinden bitirir.
4. **Ses kipiyle çakışma:** ses açıkken dikte reddedilir (panelde ileti), dikte sürerken ses
   açılamaz. İki taraf da önce kendi bayrağını kaldırıp sonra ötekine bakar. Asistan hattını
   duraklatıp sürdürmek, denetleyicinin dahili durumuna (segmenter, canlı yazı, söz kesme) çok
   fazla dokunacağı için bilerek yapılmadı.
5. **Hedef pencere:** kayıt başlarken ön plan pencere kimliği alınır. Artemis'in kendi penceresi
   ön plandaysa ya da pencere okunamazsa hedef yoktur. Yazım başlarken ve her karakterden önce
   (ADR 0056 `type_text(text, should_continue)`) aynı pencere aranır; değişirse yazım durur.
   Yazım sırasında Esc (geçici `GlobalHotkey("escape")`) yazımı durdurur.
6. **Dürüst sonuç:** yazım yarıda kalırsa `partial` ve "geri alınmadı" denir, başarı sayılmaz.
   Yazılmayan ya da yarım kalan metin panelde gösterilir (pano kullanılmaz).
7. **Denetim:** `dictation` olayı: `duration_seconds`, `chars`, `typed`, `outcome`
   (`typed|partial|no_target|empty|cancelled|error`). Metin hiçbir zaman yazılmaz.
8. **Bulut STT:** `build_dictation_recognizer` ses yolundaki ayarı izler: `voice.stt_mode:
   hybrid` seçiliyse ses aynı uca gider ve aynı `cloud_stt_transfer` denetimi yazılır, değilse
   yerel Whisper. Yeni bir izin ya da atlama yolu yoktur. Fark: ses yolunda buluta yalnız komut
   olacak söz gider, dikte hybrid'de tüm kaydı gönderir (kullanıcı hybrid'i seçmiştir).
9. **Arayüz:** kayıt sürerken durum satırı, hızlı kutu ve pet "Dikte dinliyor…" gösterir; yeni
   pencere yok. Ayarlar > Ses altında onay kutusu ve kısayol alanı. Kayıt hatası ses
   kısayoluyla aynı yolla bildirilir. Kısayol açılışta ve Uygula'dan sonra yeniden kaydedilir.
10. Yalnız Windows'ta çalışır; Windows modülleri lazy import edilir, başka platformda kısayol
    kaydı başarısız olur ve özellik etkisizdir.

## Bilerek yapılmayanlar

Basılı tutarak konuşma, noktalama/komut ("yeni satır") sözcükleri, pano ile yapıştırma, kısmi
metni canlı yazma, ses açıkken asistan hattını duraklatma, dikteyi bir araç olarak modele açma.
Yazım metni tek satıra indirilir (satır sonu/sekme boşluğa döner) ki hedefte Enter/Tab gibi
davranmasın.

## Sınırlar

- Windows kabulü yapılmadı (`docs/YENI_OZELLIKLER_KABUL.md`); sahte testler yerine geçmez.
- Odak denetimi ile `SendInput` arasında ADR 0056'daki tek karakterlik yarış kalır.
- Dikte için Whisper modeli ilk kullanımda ayrıca yüklenir ve bellekte kalır (ses kipinin
  modeliyle paylaşılmaz).
