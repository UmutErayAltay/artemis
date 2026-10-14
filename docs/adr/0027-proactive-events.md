# ADR 0027 — Proaktif olaylar: hatırlatma, sabah brifingi ve gözetimsiz tur

**Bağlam:** Faz 6a, kullanıcı bir şey yazmadan Jarvis'in kendiliğinden konuşmasını
ekler: kurulan bir hatırlatmanın zamanı gelince bildirilmesi ve belirli saatte sabah
brifingi. Bu şimdiye kadarki her turdan farklıdır: turu kullanıcı başlatmaz ve tur
sırasında karşısında onay verecek biri olmayabilir. Mevcut kurallar (CLAUDE.md 1, 2,
4, 12) "kullanıcı ekranın başında, onayı görüyor" varsayımıyla yazıldı. Ayrıca
zamanlanmış bir istem kalıcıdır: lekeli bir turda kurulan bir zamanlama, dış içeriğin
ileride kullanıcı yokken yeniden çalışması anlamına gelir. Altyapının çoğu hazır:
bildirim adaptörleri (ADR 0016), SQLite durum ve süreç sahipliği (ADR 0026), günlük
harcama defteri ve tek giriş `CommandRunner`.

**Karar:**

- **İki ayrı olay türü vardır, ikisi aynı şey değildir.**
  - *Hatırlatma* sabit bir metindir. Zamanı gelince model çağrılmadan teslim edilir;
    LLM yoktur, araç çağrısı yoktur, maliyet yoktur.
  - *Brifing* config'te kullanıcının yazdığı sabit bir istemle başlayan bir model
    turudur. `CommandRunner` → `AgentLoop` → `SafetyGate` zincirinden geçer.

- **Gözetimsiz tur kuralı `SafetyGate`'te yaşar.** `TurnState`'e `unattended: bool`
  eklenir. Gözetimsiz turda READ_ONLY dışındaki her araç çağrısı (SIDE_EFFECT dahil,
  bilinmeyen risk dahil) onay gerektirir sayılır ve **onay sorulmadan reddedilir**;
  `approve` geri çağrısı hiç çağrılmaz. Sonuç `status="denied"` ve "Gözetimsiz turda
  yan etkili işlem yapılmaz" mesajıdır, `approval` olayı `unattended=True` ile denetime
  yazılır. Bu ikinci bir onay politikası değildir: tek politika kapıdadır, kanal
  yalnız `unattended=True` bayrağını verir (1. kural). Varsayılan cevabın hayır olması
  (2. kural) burada "sorulacak kimse yoksa hayır" olarak uygulanır.
  - v1'de gözetimsiz onay Telegram düğmesine **yönlendirilmez**. Kullanıcı cevap
    verene dek açık kalan bir onay, saatler sonra eski bağlamla yan etki üretebilir;
    bu ayrı bir karar ister ve ihtiyaç ölçülünce ayrı ADR ile açılır.
  - Brifing yine taint olabilir (ör. `vault_search` dış dosya okur). Bu yalnız READ_ONLY
    çağrılara izin verildiği için yeni bir yetki açmaz; taint kayıtta korunur.

- **Teslim, kanalın kendi çıktısıdır; model tool çağrısı değildir.** Telegram kanalının
  yanıtını göndermesi gibi (ADR 0016), proaktif katman da sonucu sabit hedeflere yazar:
  masaüstü paneli ve toast (yerel), yapılandırılmışsa kullanıcının kendi Telegram
  sohbeti (`telegram.chat_id`, alıcıyı model seçemez). Model `notify_*` çağırarak
  teslim yapmaz; gözetimsiz turda zaten yapamaz.
  - Gizlilik ADR 0016 ile aynıdır: tur gizli içerik işlediyse (`answer_private`) ya da
    tur kaydı yoksa brifing Telegram'a gitmez; yalnız yerelde gösterilir ve Telegram'a
    "gizli içerik nedeniyle gönderilmedi" yazılır.
  - Her teslim hedefi, metni ve sonucuyla `proactive_delivery` denetim olayına yazılır;
    başarı yalnız adaptör başarıyı doğruladıysa bildirilir (5. kural). Hiçbir hedef
    başarılı değilse olay `failed` olur, sessizce "teslim edildi" sayılmaz.

- **Zamanlama kurma yetkisi.**
  - `reminder_create` SIDE_EFFECT'tir (yalnız yerel SQLite'a yazar, ağa çıkmaz).
    Temiz turda onaysız, lekeli turda onaylı çalışır; onay tam metni ve zamanı gösterir.
    Kaydın kurulduğu turun taint durumu kayda yazılır ve teslimde gösterilir
    ("dış içerik okunduktan sonra kuruldu").
  - `reminder_list` READ_ONLY, `reminder_cancel` SIDE_EFFECT'tir.
  - **Brifing istemini model kuramaz ve değiştiremez.** Brifingler yalnız config'te
    (`proactive.briefings`) tanımlanır. Model yazımı kalıcı istem, dış içeriğin
    kullanıcı yokken yeniden çalışmasının en kısa yoludur; v1 bu yolu hiç açmaz.

- **Zaman ve kaçırılan olaylar.**
  - Zaman dilimi config'tedir (`proactive.timezone`, IANA adı, ör. `Europe/Istanbul`);
    `zoneinfo` kullanılır. Windows'ta sistem IANA veritabanı olmadığı için `tzdata`
    bağımlılığı eklenir. Yaz saati geçişinde var olmayan yerel saat bir sonraki geçerli
    dakikaya, iki kez yaşanan saat ilkine çözülür.
  - Zamanlayıcı yalnız uzun yaşayan süreçte çalışır: `jarvis desktop` (tepsi) ve
    `jarvis telegram`. v1'de Windows Görev Zamanlayıcısı, otomatik başlangıç veya
    yeni servis eklenmez.
  - Her olay oluşumu SQLite'ta `(event_id, scheduled_for)` benzersiz anahtarıyla
    *önce talep edilir* (`claimed`), sonra işlenir. İki süreç aynı anda çalışsa da bir
    oluşum en fazla bir kez teslim edilir. Durumlar: `claimed` → `delivered` /
    `failed` / `missed`. Talep edilip süreci ölen oluşum yeniden başlatmada otomatik
    tekrar çalıştırılmaz; `uncertain` olarak raporlanır (ADR 0026 ile aynı ilke).
  - Uygulama kapalıyken kaçırılan hatırlatma, `reminder_grace` (varsayılan 12 saat)
    içindeyse "gecikmeli" notuyla teslim edilir, daha eskiyse `missed` olur ve açılışta
    tek bir özet olarak bildirilir. Kaçırılan brifing `briefing_grace` (varsayılan
    2 saat) dışında geç çalıştırılmaz; bayat brifing yanıltıcıdır.

- **Maliyet ve sınırlar.**
  - Brifing config'te açıkça tanımlandığında ücretli model çağrısına izin verilmiş
    sayılır; varsayılan config'te brifing yoktur. Her brifing turu mevcut günlük harcama
    defterine yazılır; `daily_budget_usd` aşılmışsa brifing çalışmaz ve bu teslim edilir.
  - Brifing turu normal `max_seconds` ve araç adımı sınırlarına tabidir; ayrıca günde
    en fazla `proactive.max_events_per_day` (varsayılan 20) proaktif olay teslim edilir.

- **Ayrı geçmiş, paylaşılan kilit yok.** Brifing kendi `CommandRunner`'ında, her seferinde
  boş bir `ConversationContext` ile çalışır. Kullanıcının panel geçmişine eklenmez ve
  oradaki dış içerikli turları devralmaz (ADR 0013'ün `history_external` durumu
  brifinge taşınmaz). Kullanıcının yürüyen turunu kilitlemez veya iptal etmez; panelde
  ayrı bir "Jarvis kendiliğinden" mesajı olarak görünür. Kullanıcı ona cevap verirse
  bu, brifing metni bağlama eklenmemiş yeni bir turdur.

- **Kod yerleşimi.** `jarvis/proactive/` zamanlayıcı, SQLite deposu ve teslimi taşır;
  Qt'siz çekirdektir ve saat/uyku enjekte edilebilir. Hatırlatma araçları
  `jarvis/tools/plugins/reminders.py` içinde `@register_tool` ile kaydolur (9. kural).
  Bekleme döngüsü sınırlıdır ve kapanışta iptal sinyaliyle biter; sınırsız `sleep` yoktur.

**Bitiş şartı (testle):**
- Gözetimsiz turda SIDE_EFFECT, CONFIRM_REQUIRED ve risk bildirmeyen araç çalışmaz;
  `approve` çağrılmaz; READ_ONLY araç çalışır. Bu test bir güvenlik mutasyonuyla
  (kuralı kaldırınca kırılmalı) korunur.
- Lekeli turda `reminder_create` onay ister; brifing istemi değiştiren hiçbir araç yoktur.
- Aynı oluşumu iki zamanlayıcı yarışarak işlese bile tek teslim olur.
- Gizli içerikli brifing Telegram'a gitmez; başarısız teslim başarı sayılmaz.
- Bütçe aşımında brifing model çağrısı yapmaz.
- Testler sahte saat, sahte sağlayıcı, loopback sahte Telegram ve sahte toast
  çalıştırıcısı kullanır; gerçek bildirim göstermez (8. kural).

**Sonuçlar:**
- v1 yan etki yapan bir otomasyon değildir: "her sabah 9'da X'i aç" kurulamaz. Bu
  bilinçli bir sınırdır; gözetimsiz onay kanalıyla birlikte ayrı ADR'de açılır.
- Jarvis kapalıyken hiçbir şey çalışmaz; kaçırılan olaylar dürüstçe raporlanır.
- Kullanıcıdan onay beklenirken (masaüstü penceresi ya da Telegram düğmesi) onay geri
  çağrısı senkron olduğu için o sürecin döngüsü durur; zamanlayıcı o süre tarama yapmaz.
  Hatırlatma gecikir ve pencere içindeyse "gecikmeli" teslim edilir, kaybolmaz. Uzun bir
  bekleme (ya da bilgisayarın uykusu) iki süreç açıkken diğer sürecin yürüyen talebini
  `uncertain` yapabilir; bu çift teslim üretmez, yalnız teslim edilmiş oluşum belirsiz
  görünür. Canlılık sinyali (heartbeat) ihtiyaç ölçülürse eklenir.
- Kayıttaki "dış içerikten kuruldu" bayrağı turun taint'ini ve bağlamda kalan dış içeriği
  (ADR 0013 `history_external`) birlikte kapsar; hatırlatma kurmanın onay kuralı değişmez.
- Windows'ta saat dilimi, toast ve tepsi davranışının kabulü kullanıcı tarafından
  yapılır. Sahte saatli testler gerçek uyku/uyanma ve yaz saati davranışını kanıtlamaz.
- MCP (ADR 0028) ve ekran algısı (ADR 0029) proaktif turlara ancak kendi ADR'lerinde
  açıkça izin verilirse dahil olur; varsayılan olarak gözetimsiz turda yalnız READ_ONLY
  kuralı onlar için de geçerlidir.
