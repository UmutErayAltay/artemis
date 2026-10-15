# ADR 0032 — Tarayıcı otomasyonu: Jarvis'e ait MCP sunucusu

**Bağlam:** Faz 7c, "şu sayfayı aç, şu düğmeye bas, şu tablodaki değeri oku" gibi
istekler için sayfanın DOM'una erişen bir araç ister. `open_url` yalnız tarayıcıyı açar,
`web_search` yalnız arama sonucu getirir. Artemis bunu Playwright + MCP SDK ile ayrı bir
sunucu olarak yapıyordu; `file://` ile SSH anahtarı okunabildiği ölçülmüş, sonra
`_validate_url` eklenmişti. Ancak bu doğrulama yalnız ilk adrese bakıyordu: genel bir
sayfanın yönlendirmesi veya alt isteği yerel ağa gidebiliyordu. Jarvis'te MCP istemcisi
hazırdır (ADR 0028): her çağrı taze süreç, varsayılan SIDE_EFFECT + outbound + external.

**Karar:**

- **Sunucu Jarvis deposundadır, ayrı süreçtir.** `jarvis/mcp_servers/browser.py`, SDK'sız
  küçük bir stdio JSON-RPC sunucusudur (`initialize`, `tools/list`, `tools/call`).
  Taşıma kuralı gereği Artemis dosyası kopyalanmaz, davranışı yeniden yazılır. Playwright
  isteğe bağlı `browser` ekidir (`pip install -e ".[browser]"` ve bir kez
  `python -m playwright install chromium`); yoksa sunucu çökmeden dürüst hata döner.
  Kurulmadıkça ve config'e eklenmedikçe hiçbir şey çalışmaz.

- **Tek araç, tek oturum.** `browse(url, steps)` tek çağrıda tarayıcıyı açar, adımları
  sırayla uygular ve kapatır (istemci her çağrıda taze süreç başlatır; oturum taşınamaz).
  Adımlar: `click`, `fill`, `read_text`, `wait_for`; en çok 20 adım. `read_text` yoksa
  sayfanın başlığı ve görünür metni döner. Toplam metin yaklaşık 12 000 karakterle
  sınırlıdır.

- **Her istek doğrulanır, yalnız ilki değil.** İzinli yalnız `http`/`https`; host çözülür
  ve loopback, özel, link-local, çoklu yayın, ayrılmış ve belirtilmemiş adresler reddedilir.
  Kural bağlam düzeyinde `context.route` ile HER isteğe (yönlendirme, iframe, alt kaynak,
  tıklamayla açılan yeni sekme) ve `context.route_web_socket` ile WebSocket bağlantılarına
  uygulanır; reddedilen istek iptal edilir. Ad çözülemeyen ve küresel olmayan (ör. CGNAT)
  adresler de reddedilir. `file:`, `data:`, `javascript:`, `chrome:` asla
  açılmaz. Kalan risk: Chromium'un adresi doğrulamadan sonra yeniden çözmesi (DNS
  yeniden bağlama); ölçülürse ayrı ADR ile ele alınır.

- **Temiz profil.** Her çağrı çerez, oturum ve profil taşımayan yeni bir bağlamdır;
  indirme kapalıdır, service worker engellenir. Tarayıcı kullanıcının girişli
  oturumlarını görmez: bu sunucuyla hesap adına işlem yapılamaz.

- **Risk köprüde kalır.** Araç MCP varsayılanlarıyla gelir: SIDE_EFFECT, outbound,
  external, sonuç `content: text` (genel web içeriği). Sonuç turu lekeler; sonraki her
  yan etki onay ister. Config örneği `read_only_tools` ÖNERMEZ: `fill` ve `click`
  form gönderebilir.

- **`{python}` komutu.** MCP `command` değeri tam olarak `{python}` ise Jarvis'in kendi
  yorumlayıcısıyla (`sys.executable`) değiştirilir. Böylece açılışta (ADR 0030, etkin
  sanal ortam yokken) da doğru ortam kullanılır; başka hiçbir yer tutucu yoktur.

**Bitiş şartı (testle):**
- URL politikası: şemalar, IPv4/IPv6 yerel aralıklar, çözümlemesi yerel olan ad,
  çözülemeyen ad; yönlendirme ve alt istek de aynı kuraldan geçer.
- Sunucu protokolü gerçek `StdioMCPClient` ile uçtan uca, sahte tarayıcı arka ucuyla
  sınanır; Playwright yokken dürüst hata.
- Adım sınırı, metin sınırı, geçersiz adım reddi.
- `{python}` yalnız tam eşleşmede değişir.
- Gerçek Chromium testi yalnız Playwright kuruluysa ve elle çalışır.

**Sonuçlar:**
- Sayfa etkileşimi mümkün olur, ama her çağrı Chromium başlatır (~1–3 sn).
- Girişli siteler (mail, banka) bu araçla kullanılamaz; bu bilinçlidir.
- Davranış ancak kullanıcının Windows 11 kabulüyle doğrulanır.
