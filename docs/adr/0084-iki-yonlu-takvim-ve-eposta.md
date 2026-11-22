# ADR 0084: Onaylı iki yönlü takvim (CalDAV) ve e-posta (SMTP) yazımı

- Tarih: 2026-11-22
- Durum: Kabul edildi

**Bağlam:** ADR 0059 takvim (ICS) ve e-posta (IMAP) okumayı yalnız salt okunur olarak
açmıştı. Kullanıcı Artemis'ten takvimine etkinlik eklemesini veya taslak bir e-postayı
göndermesini istediğinde yalnızca okuyabiliyordu. Ancak dışarıya veri çıkaran (outbound)
eylemler en yüksek risk sınıfındadır: yanlışlıkla, sessizce, otomatik onay kipinde ya da
gözetimsiz bir brifing turunda asla e-posta gönderilmemeli ve dış takvime yazılmamalıdır;
üstelik gönderilecek gövde 80 karakterlik özet satırına kırpılırsa kullanıcı neyi
onayladığını göremez.

**Karar:**

1. **Varsayılan kapalı opt-in yapılandırması (`artemis/config/settings.py`):**
   - `CalendarConfig`: `allow_write: bool = False`, `caldav_url: str = ""` (boş değilse
     `_validate_base_url` ile HTTPS veya yerel loopback zorunlu, userinfo/query/fragment
     yasak), `caldav_username: str = ""`.
   - `MailConfig`: `allow_send: bool = False`, `smtp_host: str = ""`, `smtp_port: int = 465`,
     `smtp_username: str = ""`.
   - Parolalar asla YAML'a yazılmaz; ortam değişkeni (`ARTEMIS_CALENDAR_CALDAV_PASSWORD`,
     `ARTEMIS_MAIL_SMTP_PASSWORD`, yoksa `ARTEMIS_MAIL_PASSWORD`) veya Windows kimlik
     kasasından (`Artemis/calendar_caldav_password`, `Artemis/mail_smtp_password`) okunur.
2. **Yalnız stdlib yazma çekirdeği (`artemis/tools/calendar_mail_write.py`):**
   - **SMTP (`send_mail`, `connect_smtp`):** `to`, `cc`, `subject`, `username` alanlarında
     `\r`, `\n`, `\0` (başlık enjeksiyonu) ve geçersiz e-posta biçimi ağ çağrısı öncesi
     reddedilir. 465'te `SMTP_SSL`, diğer portlarda zorunlu `STARTTLS` (`ssl.create_default_context()`)
     kullanılır. `send_message` dönüşündeki `refused` sözlüğü boş değilse hata verilir.
   - **CalDAV (`build_vevent_ics`, `put_caldav_event`):** ISO tarih/tarih-saat sınırları
     doğrulanır (`end > start` tüm gün, `end >= start` saatli), RFC 5545 metin kaçışı
     uygulanır ve `If-None-Match: *` + Basic Auth ile `PUT <caldav_url>/<uid>.ics` gönderilir.
     Hata iletileri ve denetim kayıtları sır/URL jetonu taşımaz.
3. **Araç eklentileri ve `SafetyGate` korumaları (`artemis/tools/plugins/calendar_mail_write.py`):**
   - `mail_send` ve `calendar_create_event`: `risk = Risk.CONFIRM_REQUIRED`, `outbound = True`,
     `auto_mode_ok = False`. Böylece otomatik onay kipinde (`auto`) bile sessizce çalışmaz
     ve gözetimsiz turlarda (`TurnState.unattended`) `SafetyGate.dispatch` tarafından
     doğrudan engellenir.
   - `refuse()` hem `enabled` hem de `allow_send`/`allow_write` bayrağını onay penceresinden
     önce denetler; `artemis/channels/runtime.py` de opt-in `False` iken araçları modelden
     gizler (`without_prefix`).
4. **Tam içerik önizlemesi ve Ayarlar UI:**
   - `ApprovalDialog._add_outbound_preview` (`artemis/channels/desktop/approval_dialog.py`)
     `mail_send` (Alıcı, Cc, Konu, kesintisiz tam Gövde) ve `calendar_create_event` (Başlık,
     Başlangıç, Bitiş, Konum, tam Açıklama) için 80 karakter sınırına takılmayan ayrı bir
     salt okunur önizleme kutusu gösterir; varsayılan yanıt yine `Hayır`dır.
   - *Ayarlar > Bağlantılar > Takvim ve posta* altına yazma izin kutuları, CalDAV/SMTP
     alanları ve kimlik kasası parola kontrolleri eklendi.

**Sonuç:** İki yönlü takvim ve e-posta desteği ek bağımlılık eklemeden, çift kapılı opt-in
(`enabled` + `allow_write`/`allow_send`), gözetimsiz tur yasağı, başlık enjeksiyonu
koruması ve tam gövdeli açık onay penceresiyle güvenli biçimde sunulur.
