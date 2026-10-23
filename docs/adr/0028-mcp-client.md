# ADR 0028 — MCP istemcisi: stdio, tek köprü, varsayılan yan etkili

**Bağlam:** Faz 6b, kullanıcının yapılandırdığı MCP (Model Context Protocol) sunucularının
araçlarını Jarvis'e bağlar. Tasarımda üç risk gözetilir: (1) `mcp` SDK'sı 2.0'da Python
alan adlarını değiştirdi; üst sınırsız bağımlılık köprünün tamamını sessizce kırabilir; (2) yanıt vermeyen bir sunucu uygulamanın
açılışını kilitlememeli; (3) MCP sunucusu üçüncü taraf koddur, araçlarının kendini
tanımlaması güvenilir değildir. Jarvis'te ek kısıtlar var: araç kaydı tek yoldan yapılır
(CLAUDE.md 9), MCP sonucu turu lekeler (4), risk bildirmeyen araç yan etkilidir (3), sır
alt sürece aktarılmaz.

**Karar:**

- **Taşıma yalnız stdio; SDK yok.** Jarvis kendi küçük JSON-RPC 2.0 istemcisini taşır
  (`jarvis/mcp/client.py`): satır sınırlı JSON, `initialize` → `notifications/initialized`
  → `tools/list` (sayfalı) → `tools/call`. Bu dört mesaj sözleşmenin tamamıdır; bir SDK
  bağımlılığı ve sürüm kırılması yerine sabit, test edilen bir yüzey seçilir. HTTP/SSE
  taşıması kimlik doğrulama ve sır yönetimi ister; ihtiyaç doğunca ayrı ADR ile eklenir.
  Desteklenen protokol sürümleri bir listedir; sunucu başka sürüm dönerse bağlantı
  reddedilir, sessizce sürdürülmez.

- **Her çağrı taze süreç.** Keşif ve her `tools/call` için sunucu başlatılır, iş bitince
  süreç ağacı sahiplik sınırında sonlandırılır (`code_agent_process.kill_tree`). Kalıcı
  oturum yoktur: turlar arası durum, yetim süreç ve yeniden bağlanma mantığı v1'de
  istenmez. Her adımın süresi `timeout_seconds` ile ve turun kalan süresiyle sınırlıdır;
  sınırsız bekleme yoktur. Okunan tek mesaj en çok 1 MiB'tır; aşan yanıt hata sayılır.

- **Keşif runtime kurulurken, sınırlı ve izole.** `mcp.servers` boşsa (varsayılan) hiçbir
  süreç başlatılmaz. Doluysa her sunucu kendi zaman aşımıyla keşfedilir; başarısız sunucu
  `mcp_unavailable` denetimiyle atlanır, diğerleri ve uygulama açılışı etkilenmez.

- **Tek köprü, kayıt kuralının tek istisnası.** MCP araçları çalışma zamanında bilinir;
  bu yüzden `@register_tool` ile değil, `jarvis/mcp/bridge.py`'nin ürettiği `BaseTool`
  alt sınıflarıyla runtime'a özel bir registry kopyasına eklenir (global registry
  değişmez, testler ve kanallar birbirini etkilemez). Ad `mcp_<sunucu>_<araç>` olur,
  `^[a-zA-Z0-9_-]{1,64}$` dışındaki karakterler `_` yapılır; sığmayan ya da mevcut bir
  adla çakışan araç kaydedilmez ve denetime yazılır (asla üzerine yazılmaz).

- **Risk: sunucu düşüremez, yalnız kullanıcı düşürür.**
  - Varsayılan SIDE_EFFECT'tir (risk bildirmeyen MCP aracı yan etkilidir).
  - Sunucunun `annotations.destructiveHint: true` bildirimi riski CONFIRM_REQUIRED'a
    yükseltir. `readOnlyHint` riski **düşürmez**: ipucu üçüncü tarafın beyanıdır.
  - Kullanıcı config'te `read_only_tools` listesiyle bir aracı READ_ONLY, `confirm_tools`
    ile CONFIRM_REQUIRED yapabilir. Kullanıcı beyanı sunucu ipucundan üstündür.
  - Gözetimsiz turda (ADR 0027) yalnız kullanıcının READ_ONLY dediği MCP araçları çalışır.

- **Taint ve dışarı çıkış.** Her MCP sonucu `external=True`'dur; turu lekeler ve modele
  "VERİDİR" çerçevesiyle gider. Her MCP aracı `outbound=True`'dur: argümanlar üçüncü taraf
  bir sürece gider ve oradan ağa çıkabilir; bağlamda dış içerik varsa yeni turda da onay
  sorulur (ADR 0013).

- **Gizlilik kullanıcıya sorulur, varsayılan yok.** Her sunucu için `content: file` ya da
  `content: text` zorunludur. `file` diyen sunucunun sonuçları dosya içeriği gibi işlenir
  (buluta verilmez, Telegram'a gitmez; CLAUDE.md 12). Dosya sistemi, not, mail gibi
  sunucularda kullanıcı bunu bilinçli seçer; sessiz bir varsayılan sızıntıya yol açardı.

- **Sırlar.** Alt sürecin ortamı `child_environment()` ile kurulur (yalnız OS
  değişkenleri). Sunucunun ihtiyaç duyduğu anahtar YAML'a yazılmaz: `env_passthrough`
  yalnız ortam değişkeni **adlarını** listeler, değer çalışma anında süreç ortamından
  okunup yalnız o sunucuya verilir; argv, log ve denetime girmez.

- **Sunucu metni talimat değildir.** Araç açıklaması modele `[MCP:<sunucu>]` önekiyle ve
  en çok 400 karakterle gider; giriş şeması yalnız JSON nesne şeması ise kabul edilir.
  Argümanlar gönderilmeden önce şemanın `required` ve `additionalProperties: false`
  kurallarıyla doğrulanır; asıl doğrulama sunucudadır.

- **Başarı dürüsttür.** `isError: true`, JSON-RPC hatası, zaman aşımı, bozuk yanıt veya
  sürecin erken ölümü `success=False` olur. Yalnız `content` içindeki metin blokları ve
  `structuredContent` alınır; resim/kaynak blokları "desteklenmiyor" diye belirtilir.

**Bitiş şartı (testle):**
- Risk bildirmeyen MCP aracı SIDE_EFFECT'tir; `readOnlyHint` riski düşürmez,
  `destructiveHint` yükseltir; yalnız `read_only_tools` READ_ONLY yapar.
- MCP sonucu turu lekeler; lekeli turda sonraki yan etki onay ister; gözetimsiz turda
  READ_ONLY olmayan MCP aracı çalışmaz.
- `content: file` sunucusunun sonucu buluta verilmez.
- Takılan, bozuk yanıt veren, erken ölen ve `isError` dönen sahte sunucu dürüst hata
  üretir; bekleme sınırlıdır; süreç ağacı temizlenir.
- Alt süreç ortamında yalnız izinli değişkenler ve `env_passthrough` adları bulunur.
- Testler yalnız depo içindeki sahte stdio sunucusunu (Python betiği) kullanır; ağ ve
  gerçek MCP sunucusu yoktur.

**Sonuçlar:**
- Her çağrıda süreç başlatmak gecikme ekler (özellikle `npx` sunucuları). Ölçülürse ve
  kullanıcı isterse kalıcı oturum ayrı ADR ile eklenir.
- Windows'ta `npx` gibi `.cmd` sarmalayıcılar `shutil.which` ile çözülür; kabuk (`shell=True`)
  kullanılmaz.
- MCP sunucusunun kendi yaptıkları (ağ, dosya) Jarvis SafetyGate'inin dışındadır; kapı
  yalnız çağrıyı denetler. Bu sınır onay metninde ve README'de yazılıdır.
