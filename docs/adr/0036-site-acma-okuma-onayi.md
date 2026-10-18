# ADR 0036 — Site açmak sorulmaz, site okumak sorulur

**Bağlam:** ADR 0013 sonrası YouTube/Google açmak bile, önceki turda dış içerik okunduysa
onay istiyordu. Kullanıcı (2026-10-18): "site açmak için onay istemesin, siteyi okuyacaksa
onay istesin, onay kısmında bu site için bir daha sorma seçeneği de olsun".

**Karar:**

- `open_url` READ_ONLY ve `outbound` değildir: hiçbir turda (lekeli ya da geçmişi dış
  içerikli) onay sormaz. Adres argümanındaki veri o siteye gidebilir; tarayıcıya adres
  göndermek kullanıcının kabul ettiği risktir. Gözetimsiz turda da çalışabilir.
- Dış içerik okuyan ve `url` argümanı alan araç (bugün tarayıcı MCP `browse`) **her** çağrıda
  onay ister; uyarı `Bu adım bir sitenin içeriğini okur (<host>)` olur.
- Masaüstü onay penceresi üçüncü düğme sunar: "Evet, <host> için bir daha sorma". Seçim
  `state_dir/trusted_sites.json`'a (tam host, alt alan adı kapsanmaz) yazılır. Bu host'a
  sonraki okumalar taint ve ADR 0013 kuralı dahil hiç sorulmaz; `tool_dispatch` denetimine
  `trusted_site=true` yazılır ve bench değişmezi bunu kabul eder.
- Güven yalnız `click`/`read_text`/`wait_for` adımlı çağrıyı kapsar (`fill` form verisi taşır); dış içerik bağlamdayken sorgu ya da parça (`?`, `#`) taşıyan adres yine sorulur (güvenilen sitedeki açık yönlendirme veriyi başka siteye taşıyabilir). İzin listesi dışı adımlı çağrıda "bir daha
  sorma" sunulmaz. Gözetimsiz turda güvenilen site de okunmaz (ADR 0027 önce gelir).
- Onay imzası `(tool_name, arguments) -> bool` kalır: hatırlama kanal tarafında, kapının
  `pending_site` değeriyle yapılır; politika yalnız `SafetyGate`'tedir. Telegram ve CLI
  hatırlama sunmaz. Liste dosyadan elle düzenlenir/silinir.

**Sonuç:** ADR 0013'ün `open_url` kısmı kalkar; `web_search` ve `notify_telegram` için
geçerlidir.
