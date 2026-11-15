# ADR 0064 — Telegram'dan ses, fotoğraf ve dosya girdisi

**Bağlam:** Kanal yalnız özel, iletilmemiş, taze, düz metin mesajı çalıştırıyordu (ADR 0016).
Telefondan sesli komut, fotoğraftaki yazıyı okutma ve dosya yollama isteniyor. Bu ADR yalnız
"düz metin" koşulunu değiştirir; özel sohbet, iletilmemiş ve taze olma koşulları her yeni
mesaj türünde aynen sürer (kimlik, iletme, bayatlık denetimi `check_message` içinde ortak).

## Karar

1. **Üç ayrı bayrak**, hepsi varsayılan `false`: `telegram.accept_voice`, `accept_photo`,
   `accept_files` (Ayarlar > Telegram'da kutular). Kapalıyken mesaj indirilmez; kullanıcı kısa
   bir "kapalı" yanıtı alır, denetime `telegram_rejected` (`voice_disabled` vb.) yazılır.
2. **Ses.** Sınır 1 MB / 60 sn. Boyut Telegram'ın bildirdiği `file_size` ile indirmeden ÖNCE,
   sonra okurken (indirme sınırda bırakılır) ve çözülürken (süre) denetlenir. Ses diske HİÇ
   yazılmaz: OGG/Opus bellekte PyAV ile çözülür (PyAV zaten kilitli `voice` ekindeki
   faster-whisper'ın bağımlılığıdır; yeni paket yok). `faster_whisper.decode_audio` kilitli
   PyAV 19 ile uyumsuz olduğundan kullanılmadı. STT, dikteyle aynı `build_dictation_recognizer`
   (etiket `telegram_audio`): `voice.stt_mode: hybrid` ise bulut ve `cloud_stt_transfer`
   denetimi, değilse yerel Whisper; yeni izin yolu yok. Önce "Şunu duydum: ..." yanıtlanır, sonra
   metin sıradan komut olarak AYNI `CommandRunner` turundan geçer. Ses onay değildir: onay
   beklerken gelen ses indirilmez bile ("Önce bekleyen onayı yanıtlayın"); yazıya çevrilmiş
   "/yeni" gibi metin kanal komutu sayılmaz. Boş çıktı "anlaşılamadı" der, hiçbir şey çalışmaz.
   Çözücü ya da STT yoksa dürüst ret iletisi verilir (çağrı döngüden istisna çıkarmaz).
3. **Fotoğraf.** En büyük boyut, 5 MB sınırı altında indirilir (JPEG imzası aranır), yerel
   Windows OCR çalışır (`perception.read_image`, ekran OCR'ıyla aynı desen: sabit betik, yol
   ortam değişkeniyle), geçici dosya `finally` ile her yolda silinir. Görüntü modele hiç gitmez.
   OCR metni DIŞ veridir: `AgentLoop.run(attachment=ToolResult(external, content_kind="screen"))`
   turu lekeler, metin "VERİDİR" çerçevesiyle ayrı bir kullanıcı mesajı olarak gider ve
   `perception.allow_cloud_screen` kapalıyken gizlidir (bulut sağlayıcısı maskeler, yanıt gizli
   sayılır). Başlık (caption) varsa kullanıcının komutudur; yoksa yalnız tanınan metin geri
   yazılır, model çağrılmaz (bu yanıt denetimde metinsiz tutulur: `redact_audit`).
   Windows dışında ya da OCR dil paketi yoksa dürüst ileti verilir.
4. **Gizli yanıt.** "Turda/geçmişte gizli içerik varsa onay ve yanıt Telegram'a gitmez" kuralı
   gevşetilmedi. Fotoğraf turunda yanıt gizliyse kullanıcı sessizlik yerine "Yanıt fotoğraftaki
   gizli içeriği kullandığı için Telegram'a gönderilmedi. Konuyu masaüstünden sorun." alır.
   (Telegram turları masaüstü sohbetinde görünmediğinden "masaüstünde gösterildi" denmedi.)
5. **Belge.** Çalışma alanında `Telegram/` altına, `safe_join(allow_symlinks=False)` ile,
   `O_EXCL` ("xb") kipinde yazılır; var olan dosya ezilmez (`ad-2.uzanti`). Ad saldırgan girdisi
   sayılır: NFKC, kontrol/biçim/gizli karakterler, yol parçaları, Windows'ta geçersiz karakterler,
   sondaki nokta/boşluk, aygıt adları (CON/NUL...) ve 100 karakter sınırı. İzin SON uzantıya
   bakar (`x.pdf.exe` reddedilir); liste `telegram.file_extensions` (varsayılan pdf, txt, md,
   docx, xlsx, csv, png, jpg, zip). Boyut 20 MB, indirmeden önce ve okurken. Hiçbir şey açılmaz
   ya da çalıştırılmaz. Yanıt kaydedilen göreli yoldur.
6. **Denetim.** Her indirme `telegram_download` (tür, bayt, sonuç) yazar; içerik ve dosya adı
   yazılmaz. Token yönetimi değişmedi: indirme URL'si (`/file/bot<token>/...`) hata metnine
   girmez, yönlendirme kapalıdır, `file_path` URL'ye girmeden biçim denetiminden geçer.

## Bilerek yapılmayanlar

- **Belge başlığı komut sayılmaz** (v1). "Dosya + talimat" daha büyük bir adımdır (dosya içeriği
  dış veri olur, hangi araç okur?); gerçek kullanımdan sonra ayrı karar.
- Fotoğraf albümü, belge olarak gönderilen resmin OCR'ı, sesli onay, sesli uyandırma yok.
- Fotoğraf OCR metni sonraki turların geçmişine kopyalanmaz (kayıtta yalnız başlık kalır);
  tur lekeli ve yanıt gizli bayrağıyla saklanır.
- `perception.screen_enabled` bu yol için aranmaz: bu ekran okuma değil, kullanıcının kendi
  yolladığı görüntüdür; bunun yerine `accept_photo` ayrı bayraktır.

**Ek (doğrulama sonrası):** başlıksız fotoğrafta tanınan metin, kullanıcının kendi gönderdiği görüntünün yazısı olduğu için Telegram'a geri yazılır; `allow_cloud_screen` bulut MODELE gidişi yönetir, bu yolda model yoktur. PyAV'dan gelen `OSError`/`RuntimeError` de "ses çözülemedi" sayılır.
