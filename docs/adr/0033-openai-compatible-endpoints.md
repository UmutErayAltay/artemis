# ADR 0033 — Kullanıcı tanımlı OpenAI uyumlu uçlar

**Bağlam:** Sağlayıcılar `openrouter`/`anthropic`/`ollama` olarak sabitti; kullanıcı kendi
ucunu (yerel proxy, LM Studio, vLLM, OpenAI, Gemini/GLM uyumlu) kullanamıyordu. Ayrıca
sağlayıcı hataları sabit bir cümlenin arkasında kayboluyordu.

**Karar:**

- `endpoints:` eşlemesi uç kimliğini `base_url` (+ `api_key: bool`) ile tanımlar; model
  `provider: openai_compatible` ve `endpoint: <id>` ile bağlanır. Adres kuralları mevcut
  sağlayıcı adresleriyle aynıdır (http yalnız localhost; query/fragment/userinfo yasak).
  Eski sağlayıcı ayarları değişmez, config taşıması yoktur.
- Anahtar YAML'da durmaz: önce `JARVIS_<ID>_API_KEY` ortam değişkeni, sonra Kimlik Bilgisi
  Yöneticisi `Jarvis/<id>`. Anahtar `os.environ`'a yazılmaz. `api_key: false` uçta
  `Authorization` başlığı gönderilmez.
- İstek gövdesine OpenRouter'a özel `usage`/`provider` alanları konmaz (OpenAI bilinmeyen
  parametreyi 400 ile reddeder). Maliyet token × config fiyatıdır; yanıtta `usage` yoksa
  yalnız ücretsiz (iki fiyat 0) modelde token 0 sayılır, ücretli modelde hata korunur.
- Bulut aktarım denetim kaydı `ollama` dışındaki her sağlayıcıda yazılır; yerel uç da
  yazılır (adres sonradan değişebilir, fazladan kayıt zararsızdır).
- 401/403/404 ve eksik anahtar kullanıcıya yönlendirici Türkçe mesajla gider; ajan döngüsü
  `ProviderError` mesajını gösterir. Bu mesajlar sabit metindir, URL/anahtar/yanıt içermez.

**Sonuç:** Arayüzden uç tanımlama ayrı bir fazdır.
