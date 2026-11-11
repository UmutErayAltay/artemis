# ADR 0059 — Takvim ve e-posta (salt okunur)

**Bağlam:** "Bugün/yarın ne var", "önemli okunmamış postam var mı" soruları ve sabah brifingi için
takvim ve posta okunmalı. OAuth, sağlayıcıya özel API ve yazma yetkisi istenmiyor.

## Karar

1. **Kaynaklar:** takvim = gizli iCalendar (ICS) adresi (Google ve Outlook verir); posta = IMAP/TLS.
   Yalnız stdlib (`urllib`, `imaplib`, `email`, `ssl`). `calendar_events(start_day?, days?)` ve
   `mail_unread(limit?)` araçları, kod `artemis/tools/calendar_mail.py` + tek plugin dosyası.
2. **Risk:** ikisi `READ_ONLY` (uç noktaları kullanıcı sabitler, modelin seçtiği veri ağa gitmez)
   ve `external=True` (posta klasik enjeksiyon yoludur; sonuç "VERİDİR" çerçevesiyle gider, tur
   lekelenir). `outbound` DEĞİL. READ_ONLY olduğu için brifing gibi gözetimsiz turda çalışır;
   brifing kodu değişmedi, yalnız config örneğine istem eklendi.
3. **Sırlar:** ICS adresi (içinde jeton var) ve IMAP parolası ADR 0051 ile aynı yoldan: önce
   `ARTEMIS_CALENDAR_ICS_TOKEN` / `ARTEMIS_MAIL_PASSWORD` ortam değişkeni (adları denetim
   temizleyicisine takılır), sonra kimlik kasası (`Artemis/calendar_ics`, `Artemis/mail_password`).
   YAML'da alan yoktur. Hata iletileri adresi/parolayı taşımaz, yalnız hata türü yazar.
4. **Ağ güvenliği:** ICS yalnız https, yönlendirme yalnız https, 10 sn, 2 MB sınırı. IMAP:
   `ssl.create_default_context()` (sertifika ve ad doğrulaması), 10 sn.
5. **Posta salt okunur:** `select(readonly=True)` ve `BODY.PEEK[]<0.32768>`: \Seen değişmez, STORE/
   COPY/EXPUNGE yok. Alanlar: gönderen, konu, tarih, 300 karakterlik düz metin özeti (HTML
   ayıklanır, script/style atılır). Ek parçaları ayrıştırılmaz/döndürülmez.
6. **Gizlilik:** sonuç `ToolResult.private = not allow_cloud` (ADR 0056 mekanizması; yeni
   `content_kind` ve döngüde ikinci koşul eklenmedi). `calendar.allow_cloud` / `mail.allow_cloud`
   varsayılan `false`: bulut modele ve Telegram'a maskeli gider, yerel modele açık.
7. **Kapalı özellik:** `calendar.enabled` / `mail.enabled` varsayılan `false`; kapalıyken araçlar
   modelden gizlenir (`without_prefix`) ve `refuse` onaysız reddeder (ADR 0058 deseni).
8. **Saat dilimi:** `proactive.timezone` (varsayılanı Europe/Istanbul). Tüm gün olayları `date`
   olarak, bitişi hariç tutularak ele alınır. TZID'li olay kendi diliminde açılır (yaz saati
   duvar saatiyle), `Z` UTC, yüzen saat yerel kabul edilir. Tanınmayan TZID uyarıyla yerel varsayılır.
9. **Arayüz:** Ayarlar > Takvim ve posta: kutular ve alanlar `PANEL_BINDINGS` ile; ICS adresi ve
   parola Telegram token'ı gibi parola kipinde alanlardan, "Kaydet ve dene" doğrulayınca kasaya yazar.

## Bilerek yapılmayanlar

- **python-dateutil eklenmedi** (`uv.lock`'ta yok; bağımlılık kazandırmamak için): yinelenme DAILY/
  WEEKLY(+BYDAY)/MONTHLY/YEARLY + INTERVAL/COUNT/UNTIL/EXDATE/RECURRENCE-ID için küçük bir
  açıcıyla yapıldı. Başka kural (BYSETPOS, ayın n. günü, HOURLY...) sessizce düşmez: olay ilk
  oluşumuyla listelenir ve sonuçta `warnings` ile "sonrakiler eksik olabilir" denir.
- Yazma/gönderme/silme/okundu işareti/taşıma, ek indirme, OAuth, çoklu hesap, davet kabulü.
- "Önemli" tespiti: sunucu yalnız `UNSEEN` süzer; önemi modelin yargısına kalır.

## Sınırlar

- Gövdenin ilk 32 KB'ı çekilir; bu aralığa denk gelen ek baytları ağdan gelebilir ama hiç
  ayrıştırılmaz, saklanmaz ve döndürülmez. Tam "ek hiç çekilmez" için BODYSTRUCTURE adımı gerekirdi.
- Windows kabulü yapılmadı (`docs/YENI_OZELLIKLER_KABUL.md`).

**Ek (doğrulama sonrası):** `INTERVAL` 1-1000 dışındaysa kural desteklenmeyen sayılır; UTC `UNTIL` an olarak karşılaştırılır; aralıklı haftalıkta Pazartesi dışı `WKST` desteklenmez ve uyarı verir; bozuk tek olay takvimin kalanını engellemez. Bilinen tavan: çok eski başlangıçlı günlük yinelenme 20 000 adım sınırına takılırsa uyarıyla eksik gösterilir.
