# ADR 0035 — Claude aboneliği: Claude Code CLI sağlayıcısı

**Bağlam:** Claude Pro/Max abonesi API anahtarı almadan aboneliğini Jarvis'in beyni olarak
kullanmak ister. Abonelik OAuth belirtecini Jarvis'e kopyalamak sağlayıcı kullanım şartlarına
uymaz ve Jarvis'i abonelik kimlik bilgisinin sahibi yapar.

**Karar:**

- `provider: claude_cli` (`ClaudeCliProvider`): kullanıcının kendi kurulu ve girişli `claude`
  CLI'si alt süreç olarak çalışır. Giriş CLI'nin işidir; Jarvis hiçbir belirteç görmez,
  saklamaz, loglamaz. `model` bir Claude Code takma adı ya da tam adıdır (`haiku`, `sonnet`,
  `opus`). Uç yoktur; fiyatlar açıkça 0 yazılır (abonelik); CLI'nin `total_cost_usd` değeri API
  karşılığı tahmindir, yok sayılır. Bulut modeli sayılır: `cloud_transfer` denetimi yazılır,
  özel içerik diğer bulut sağlayıcılarıyla aynı `_cloud_messages` ile maskelenir.
- Komut: `claude -p --output-format json --model <m> --tools "" --strict-mcp-config
  --setting-sources "" --no-session-persistence --system-prompt-file <geçici dosya> --json-schema <şema>`. İstem argv'ye girmez (cmd.exe ve komut satırı sınırı); `.cmd`/`.bat` kılıfı reddedilir.
  Konuşma dökümü stdin'den gider (komut satırı sınırı ve süreç listesi sızıntısı yok).
  Yerleşik araçlar (Bash/Edit/Read…), MCP ve kullanıcı/proje ayarları (CLAUDE.md dahil)
  kapalıdır: CLI yalnızca metin üretir, makineye dokunamaz. Yetki Jarvis'in `SafetyGate`'indedir.
- Araç çağrısı istemle yapılır: Jarvis araçlarının ad, açıklama ve şeması sistem istemine
  yazılır; model `--json-schema` ile `{content, tool_calls:[{name, arguments}]}` döndürür.
  Şemadaki `name` araç adlarıyla sınırlıdır; tanımsız ad `ProviderError`. Çağrı kimlikleri
  Jarvis üretir. Sonuç `structured_output` alanından okunur (yoksa `result` JSON'u).
- Alt sürece `child_environment()` izin listesiyle gider: `ANTHROPIC_API_KEY` geçmez, böylece
  CLI API faturasına değil aboneliğe düşer. Çalışma dizini `state_dir`'dir, kullanıcının deposu değil.
- Hata iletileri sabittir; stdout/stderr kullanıcı verisi içerebileceğinden asla yankılanmaz.
  Zaman aşımında süreç ağacı `kill_tree` ile kapatılır ve `RetryableProviderError` döner.
- Ayarlar > Model sağlayıcıları: "Abonelik: Claude (Claude Code CLI)" seçeneği; CLI durumunu
  gösterir, "Test et" bir küçük gerçek istek atar (abonelik kotasından az harcar).

**Kapsam dışı:** Codex/Gemini CLI (bu makinede kurulu değil, doğrulanamadı); genel CLI
bağdaştırıcı çerçevesi. Gecikme API'den yüksektir (süreç başlatma + istem tabanlı araç çağrısı).
