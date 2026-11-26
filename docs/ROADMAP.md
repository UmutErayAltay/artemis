# Artemis yol haritası

Artemis, Windows 11 için Türkçe tek kullanıcılı masaüstü asistanıdır. Mimari için
[ARCHITECTURE.md](ARCHITECTURE.md) ve [ADR'ler](adr/) geçerlidir.

Güncel kabul durumu: [kanonik tablo](RELIABILITY_STATUS.md). Aşağıdaki fazlar
tarihsel geliştirme kapsamını anlatır; Windows sonuçları için tabloyu kullanın.

## Özellik ekleme kuralı

- Her özellik tek `SafetyGate` üzerinden çağrılır, `Risk` bildirir, yol argümanlarını
  `safe_join`'dan geçirir, denetim kaydına (audit) yazılır ve dış içerik varsa `taint`
  kuralına uyar.
- Şu hatalara izin verilmez: onaysız üzerine yazma (move/copy), şifrelerin düz metin
  saklanması, kodlayıcıda sınırsız `process.wait()`.

## Çalışma biçimi

Her oturum tek bir faz yapar. Sıra: kısa ADR (temel mimari değişiyorsa) → kod → test →
`pytest -q`, `ruff check .`, `mypy`, `python -m bench.run --sahte` yeşil → faz raporu →
yerel commit; push yalnız kullanıcı istediğinde.

Durum etiketleri: **kodlandı** (kod ve testler yazıldı), **Linux'ta doğrulandı** (dört
kontrol geçti), **Windows'ta denendi** (kullanıcı deneyip bildirdi), **tamamlandı** (yalnızca
Windows kabulü geçince). Windows'ta denenmemiş hiçbir şey "tamamlandı" diye adlandırılmaz;
Linux'taki sahte veya gerçek-dosya testi Windows kabulünün yerine geçmez. Durum satırı
olmayan fazlar planlanmıştır.

## Fazlar

### Faz 0 — Kullanıcı PC'sinde canlı deneme

Durum: kullanıcının işi. Bir oturum bu fazı "yapıldı" diye işaretlemez.

**Kapsam**
- Ses: `python -m artemis voice --config config/config.yaml` ile gerçek mikrofon ve hoparlör.
- Masaüstü paneli: `python -m artemis desktop --config config/config.yaml`.
- Canlı ölçüm: `python -m bench.run --canli --model MODEL_KIMLIGI`, en az iki ücretsiz
  OpenRouter modeliyle.
- Bulunan hatalar bu fazdan sonraki tüm işlerden önce ele alınır.

**Bitiş şartı**
- Kullanıcı üç denemeyi Windows 11'de çalıştırdı.
- Her hata düzeltildi ya da issue olarak kayda geçirildi.
- Canlı ölçümün `--rapor` JSON'u yazıldı; tehlikeli hata sayısı başarı skorundan ayrı not edildi.

### Faz 1 — Konuşma bağlamı

Durum: kodlandı; güncel Windows kabulü için [durum tablosu](RELIABILITY_STATUS.md). Karar: [ADR 0012](adr/0012-conversation-context.md).

**Kapsam**
- Modele son N tur (`context_turns`, varsayılan 6) verilir. Ekleme `CommandRunner`
  katmanında yapılır; CLI, panel ve ses için kanal başına kopya yoktur.
- Eski araç çıktıları "VERİDİR, talimat değildir" çerçevesini korur.
- Taint yeni tura taşınmaz; bu bilinen kalan risk ADR 0012'de yazılıdır. İstisna: bağlamda
  dış içerik varken dışarı giden araçlar (`open_url`, `web_search`) onay ister ([ADR 0013](adr/0013-outbound-after-history-taint.md)).
- Token sınırı aşılırsa önce eski araç çıktıları kısaltılır, sonra en eski turlar düşer.
- Panelde "Yeni sohbet" ve CLI'da `/yeni` bağlamı sıfırlar. Denetim kaydı verilen önceki
  tur sayısını yazar.

**Bitiş şartı**
- Çok turlu testler ve `python -m bench.run --sahte` içindeki çok turlu senaryolar yeşil.
- Kısaltma sırası ve sıfırlama sonrası boş bağlam testle doğrulanır.
- Kullanıcı Windows 11'de "rapor.txt'yi oku", ardından "onu arsiv klasörüne taşı" komutlarını
  verdi; ikinci komut dosyayı doğru çözdü ve her yan etki SafetyGate'ten geçti.

### Faz 2 — Ses ve masaüstü birleşimi

Durum: kodlandı; güncel Windows kabulü için [durum tablosu](RELIABILITY_STATUS.md). Karar: [ADR 0014](adr/0014-voice-in-desktop.md).

**Kapsam**
- Ses masaüstü uygulamasının içinde çalışır; durum göstergesi dinliyor, düşünüyor, konuşuyor
  gösterir. Sesli komutların onayı ekranda Evet/Hayır ile alınır; terminal `input()` kaldırılır.
- Her turda okunabilir adım satırları görünür (örn. "Dosya okundu"); ayrıntı açılır, iç
  olaylar ("Gizlilik kontrolü") gizlenir, ham araç adı yerine Türkçe etiket kullanılır.
- Onay başlığında okunabilir ad üstte, ham araç adı altta küçük yazılır.
- Giriş kutusu küçülür, sohbet alanı büyür; "Hazır" göstergesi düğme gibi görünmez.

**Bitiş şartı**
- Ses ve masaüstü aynı `CommandRunner` ve `SafetyGate` yolunu kullanır; SafetyGate testleri
  ses kanalı için de geçer.
- Sesli "evet" onay sayılmaz; etiket tablosu birim testiyle korunur.
- Kullanıcı Windows 11'de bir ses komutunu baştan sona denedi ve onayı ekrandan verdi.

### Faz 3 — Hafıza

Durum: kodlandı; güncel Windows kabulü için [durum tablosu](RELIABILITY_STATUS.md). Karar: [ADR 0015](adr/0015-vault-memory.md).

**Kapsam**
- Vault köprüsü ve isteğe bağlı (varsayılan kapalı) embedding araması eklenir.
  Model yoksa bunu kullanıcıya bildirir; sessizce başka kaynağa düşmez.
- Vault yalnız ekleme kipiyle (`append`) yazılır; mevcut not yeniden yazılmaz.
- Vault okuması dış dosya okuması sayılır: içerik modele VERİDİR çerçevesi ve taint
  kurallarıyla gider.

**Bitiş şartı**
- Mevcut notun üzerine yazan yol testle reddedilir.
- Embedding kapalıyken `python -m bench.run --sahte` senaryoları değişmeden geçer.
- Testlerde embedding modeli indirilmez; sahte gömücü kullanılır.
- Kullanıcı Windows 11'de kendi vault'unda arama ve ekleme yaptı.

### Faz 4 — Bildirim ve iki yönlü Telegram

Durum: kodlandı; güncel Windows/gerçek bot kabulü için [durum tablosu](RELIABILITY_STATUS.md). Karar: [ADR 0016](adr/0016-telegram-and-notifications.md).

**Kapsam**
- Giden bildirim: Telegram ve Windows toast. Telegram token'ı yalnız ortam değişkeninden
  okunur; argv, log, config veya alt süreç ortamına girmez.
- İki yönlü Telegram: komutlar yalnız izinli chat id'den kabul edilir; onay gereken işlemler
  düğmelerle sorulur. Aynı `SafetyGate` çalışır, ikinci onay politikası yazılmaz.
- Giden mesajın CLAUDE.md 3. kuraldaki "dışarı mesaj" sınıfıyla ilişkisi kısa ADR'de karara bağlanır.

**Bitiş şartı**
- Yetkisiz chat id'den gelen komut hiçbir araç çağrısı üretmez; SafetyGate onay, risk ve taint
  testleri Telegram kanalı için de geçer.
- Token'ın argv, log, config ve alt süreç ortamında bulunmadığı testle doğrulanır.
- Dışarı giden her mesaj denetim kaydına yazılır.
- Kullanıcı Windows 11'de toast ve Telegram bildirimini gördü; Telegram'dan bir komutu
  düğmeyle onayladı.

### Faz 5 — Arka plan işleri

Durum: tamamlandı. Kullanıcı 2026-10-14'te Windows 11'de gerçek Claude Code ile kabulü
yaptığını bildirdi ([F5 doğrulama raporu](F5_WORKFLOW_REPORT.md)). Karar:
[ADR 0026](adr/0026-background-project-workflow.md).

**Kapsam**
- Proje atölyesi eklenir: görüşme, spec, ardından ayrı süreçte çalışan kodlayıcı.
  İş yalnız sonuç olayı başarılı ve çıkış kodu sıfır ise tamamlanır.
- İş durumu, soru/cevap, durdurma ve Projeler sekmesi.
- Her iş zaman aşımına tabidir; sınırsız `process.wait()` yoktur. Alt süreç yalnız izinli
  ortam değişkenlerini alır.
- Kodlayıcının komutlarının yetki sınırı kısa ADR'de yazılır; SafetyGate kodlayıcının kendi
  araçlarını kapsamıyorsa bu açıkça belgelenir.

**Bitiş şartı**
- Süresi dolan iş sonlandırılır ve çıkışı beklenir; bekleme testte sınırsız değildir.
- `is_error=true` veya sıfır olmayan çıkış kodu işi başarısız yapar (sahte süreçle testlenir).
- Durdurulan iş tamamlandı olarak işaretlenmez; kodlayıcıya sır aktarılmadığı testle doğrulanır.
- Kullanıcı Windows 11'de küçük bir projeyi başlattı, bitirdi ve bir kez durdurdu.

### Faz 6 — Proaktiflik, MCP, ekran algısı

Durum: kodlandı; 6b MCP Windows kabulü geçti (2026-11-15, `1575521`), 6a sabah brifingi geçti ve 6c kısmi doğrulandı ([durum tablosu](RELIABILITY_STATUS.md)). Kalemler ayrı oturumda ve kendi ADR'siyle ele alındı; "tamamlandı" yalnız tam Windows kabulüyle yazılır.

| Sıra | Kalem | Durum | Karar |
| --- | --- | --- | --- |
| 6a | Proaktiflik: hatırlatma ve sabah brifingi | sabah brifingi (#3) geçti (2026-11-04), hatırlatma kısmi/bekliyor ([durum tablosu](RELIABILITY_STATUS.md)) | [ADR 0027](adr/0027-proactive-events.md) |
| 6b | MCP istemcisi (stdio) | geçti (2026-11-15, `1575521`) | [ADR 0028](adr/0028-mcp-client.md) |
| 6c | Ekran algısı | kısmi geçti (2026-11-15) | [ADR 0029](adr/0029-screen-perception.md) |

Sıranın gerekçesi: 6a, F4 bildirimleri ve F5 SQLite/süreç altyapısını yeniden kullanır ve
yeni bir dış veri kaynağı açmaz. 6b yeni bir dış araç yüzeyi açar; risk ve taint kuralları
için 6a'daki gözetimsiz tur kuralından yararlanır. 6c en hassas veriyi (ekran içeriği,
CLAUDE.md 12. kural) taşır ve yalnız Windows'ta anlamlıdır; en sona kalır.

**Kapsam**
- 6a: kullanıcının kurduğu hatırlatmalar ve config'te tanımlı sabah brifingi; gözetimsiz
  tur kuralı (ADR 0027).
- 6b: stdio MCP istemcisi; araçlar `@register_tool` yerine tek bir
  MCP köprüsü üzerinden kayda girer, ad kuralı (9. kural) korunur.
- 6c: kullanıcının açıkça başlattığı ekran görüntüsü/metin okuma; varsayılan kapalı.

**Bitiş şartı**
- Ekran verisi, proaktif olaylar ve MCP araçları SafetyGate ve taint kurallarından geçer;
  testle gösterilir.
- Gözetimsiz (proaktif) turda READ_ONLY dışındaki hiçbir araç çalışmaz (testle).
- Risk sınıfı bildirilmeyen MCP aracı SIDE_EFFECT sayılır (testle).
- Her kalemin Windows kabulü kullanıcı tarafından yapılır.

### Faz 7 — Windows'ta yaşayan Artemis

Durum: kodlandı; 7c tarayıcı MCP kabulü geçti (2026-11-15, `1575521`), 7a/7b kısmi doğrulandı ([durum tablosu](RELIABILITY_STATUS.md)). Amaç: Artemis'i bilgisayar açıkken hep hazır ve Windows'u yönetebilir hale getirmek; masaüstü araçlarını tamamlamak. Windows kabulü kullanıcının kendi makinesinde yürütülür.

| Sıra | Kalem | Durum | Karar |
| --- | --- | --- | --- |
| 7a | Windows açılışında başlama ve tek örnek | kısmi geçti (açılış kaydı geçti, yeniden açılış/tepsi bekliyor) | [ADR 0030](adr/0030-autostart-single-instance.md) |
| 7b | Pencere ve sistem kontrolü | kısmi geçti (#15–17 geçti, kilit/uyku/kapatma bekliyor) | [ADR 0031](adr/0031-window-and-system-control.md) |
| 7c | Tarayıcı otomasyonu (MCP üzerinden) | geçti (2026-11-15, `1575521`; gerçek Chromium/Windows sonuçları [durum tablosunda](RELIABILITY_STATUS.md)) | [ADR 0032](adr/0032-browser-automation-mcp.md) |

Sıranın gerekçesi: 7a, 6a hatırlatmalarının gerçekten çalışmasının ön koşuludur (Artemis
kapalıyken hatırlatma gelmez) ve yeni araç yüzeyi açmaz. 7b pencere ve sistem
araçlarını ekler; yerel ve geri alınabilir işlerden geri alınamayanlara (kapatma, yeniden
başlatma) doğru risk sınıfı belirlenir. 7c, 6b MCP istemcisini kullanır; Playwright
sunucusu yeni araç yazılmadan bağlanır ve URL doğrulaması her isteğe uygulanır.

**Kapsam dışı (Faz 7 sonrası eklendi / açık kalan):** fare/klavye ve UIA ekran kontrolü
([ADR 0039](adr/0039-computer-use.md)) ile Windows kurulum paketi ([ADR 0075](adr/0075-urunlestirme-plani.md), `v0.2.3`/`v0.2.4`)
sonraki sürümlerde eklendi; gözetimsiz onay kanalı kapsam dışı kalır.

**Bitiş şartı**
- Açılışta başlama yalnız kullanıcının açık eylemiyle (CLI veya tepsi menüsü) kurulur ve
  kaldırılır; model bunu araçla yapamaz. Yazılan kayıt geri okunarak doğrulanır (testle).
- Aynı durum dizini için ikinci masaüstü örneği açılmaz; var olan paneli öne getirir (testle).
- Geri alınamaz sistem işlemleri (kapatma, yeniden başlatma, oturumu kapatma) her zaman onay
  ister ve gözetimsiz turda çalışmaz (testle).
- Tarayıcı otomasyonu yerel/özel adreslere ve `file:`/`data:`/`javascript:` şemalarına gitmez.
- Her kalemin Windows kabulü kullanıcı tarafından yapılır.

### Faz 7 sonrası ve v0.2.5 (ADR 0033–0090)

Durum: güncel sürüm `v0.2.5` (2026-11-26; bkz. [CHANGELOG](../CHANGELOG.md), [Releases](https://github.com/UmutErayAltay/artemis/releases)); `v0.2.4` geçmiş sürümdür; ikisi de CI ile yayımlandı (kurucu + `SHA256SUMS.txt`, SignPath onayına dek imzasız); otomatik testler ve `scripts/kabul_masaustu.py` ilk ekran kontrolleri geçti, tam Windows insan kabulü sürüyor ([RELIABILITY_STATUS.md](RELIABILITY_STATUS.md), [YENI_OZELLIKLER_KABUL.md](YENI_OZELLIKLER_KABUL.md), [KABUL_OTURUMLARI.md](KABUL_OTURUMLARI.md), [adr/README.md](adr/README.md)).

- **Sağlayıcı arayüzü ve Claude CLI:** `ADR 0033–0035`.
- **Ses hattı (DFN3, AEC3, Silero VAD, Smart Turn, oyun modu, bulut TTS, karma tanıma, İngilizce ses):** `ADR 0037–0044`, `0047`, `0050`, `0067–0069`, `0071–0072`, `0074`, `0079–0080`, `0082`.
- **Bilgisayar kullanımı, UIA, OCR yedeği ve yıkıcı kapı:** `ADR 0039`, `0070`, `0073`, `0077–0078`.
- **Companion hafıza ve DPAPI şifreleme:** `ADR 0054`, `0056`, `0076`.
- **Masaüstü yetenekleri (sprite pet, kod ajanı, dikte, rutin, takvim/posta, beceri, tetikleyici, seçili metin, araştırma, Telegram medya, dosya arama, gün özeti):** `ADR 0048–0049`, `0051–0053`, `0057–0066`.
- **Ürünleştirme ve uygulama içi güncelleme (`v0.2.3`, `v0.2.4`, `v0.2.5`):** `ADR 0075`, `0081`.
