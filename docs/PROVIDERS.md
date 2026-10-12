# Sağlayıcı kaynakları ve doğrulama

Model kimliği, fiyat veya araç desteği ezberden seçilmez. Üretim config'i model
içermeyen bir örnekle gelir. Bu belge 12 Ekim 2026 UTC oturumunda yapılan salt
okunur resmi kaynak kontrolünün kaydıdır; gelecekteki fiyat ve erişim garantisi
değildir. Bu kontrolde hiçbir model tamamlama çağrısı yapılmadı.

## OpenRouter

- [Canlı model kataloğu](https://openrouter.ai/api/v1/models)
- [API istek ve yanıt biçimi](https://openrouter.ai/docs/api/reference/overview)
- [Chat completion referansı](https://openrouter.ai/docs/api/api-reference/chat/send-chat-completion-request)

Üç kaynak HTTP 200 ile okundu. API referansının yönlendirdiği güncel yol
`/docs/api_reference/overview`, chat referansınınki
`/docs/api/api-reference/chat/create-a-chat-completion` idi. İstek
`POST /api/v1/chat/completions` adresine `tools` listesi gönderir; yanıtın
`message.tool_calls` alanı çağrı kimliği, işlev adı ve argümanları taşır.
Jarvis'in pydantic JSON şeması sağlayıcının işlev `parameters` alanına çevrilir.

Güncel [sağlayıcı yönlendirme belgesi](https://openrouter.ai/docs/guides/routing/provider-selection#max-price)
`provider.max_price.prompt` ve `completion` değerlerini **milyon token başına USD**
olarak tanımlar (örneğin `1`, prompt için en çok $1/m token kabul eder). Bu birim
Jarvis'in `ModelConfig` fiyat birimiyle aynıdır; isteğe config değerleri doğrudan
aktarılır. Böylece sıfır fiyatlı config, ücretli sağlayıcı rotalarını dışarıda tutar.

Katalogda **458 model** bulundu. `supported_parameters` içinde `tools` bulunan,
`pricing.prompt` ve `pricing.completion` alanlarının ikisi de sıfır olan
**16 model** vardı. O anki örnekler:

| Model kimliği | Girdi USD/token | Çıktı USD/token |
| --- | ---: | ---: |
| `inclusionai/ling-3.1-flash` | 0 | 0 |
| `apodex/apodex-1.1-mini:free` | 0 | 0 |
| `dots-studio/dots-3-note-preview:free` | 0 | 0 |

Bunlar öneri veya kabul ölçümü sonucu değildir; yalnız katalog gözlemidir. Katalog
fiyatları **token başına**, Jarvis config fiyatları **milyon token başına** USD'dir:
`config_fiyati = katalog_fiyati × 1_000_000`. Model adında `free` geçmesi tek başına
ücretsizliğin kanıtı değildir. Canlı koşucu çağrı öncesinde güncel katalogdaki
fiyatı ve araç desteğini doğrular; katalogdaki ek fiyat alanlarından herhangi biri
sıfır değilse de çağrıyı reddeder.

## Anthropic

- [Araç tanımı](https://docs.claude.com/en/docs/agents-and-tools/tool-use/implement-tool-use)
  → güncel hedef: `https://platform.claude.com/docs/en/agents-and-tools/tool-use/define-tools`
- [Messages API](https://docs.anthropic.com/en/api/messages)
  → `https://platform.claude.com/docs/en/api/messages`
- [Güncel modeller](https://docs.anthropic.com/en/docs/about-claude/models/overview)
  → `https://platform.claude.com/docs/en/models/overview`
- [Resmi fiyatlandırma](https://www.anthropic.com/pricing)
  → `https://claude.com/pricing`

Bu kaynaklar yönlendirmeler takip edilerek HTTP 200 ile okundu. Araç tanımında
`name`, `description`, `input_schema` kullanılır. Modelin `tool_use` blokları ortak
`ToolCall` modeline, yanıtlar `tool_result` bloklarına çevrilir. Araç adı için
Jarvis bütün sağlayıcılarda aynı 64 karakter sınırını korur; bir sağlayıcının daha
geniş sınırı ortak sözleşmeyi büyütmez.

Model karşılaştırma tablosunda o an `claude-opus-5-5` için girdi **4 USD/MTok**,
çıktı **20 USD/MTok**; `claude-sonnet-5-5` için **2 USD/MTok** ve **10 USD/MTok**
gösteriliyordu. Bu gözlem ücretli model çağrısı için izin değildir. Önbellek,
uzun bağlam veya diğer tarife ayrıntıları kullanılan modelin güncel sayfasından
ayrıca kontrol edilmelidir; F1 metin/araç çağrı yolu için doğru tarife config'e
yazılmalıdır.

## Ollama

- [Araç çağırma](https://docs.ollama.com/capabilities/tool-calling)
- [Chat API](https://docs.ollama.com/api/chat)
- [Kurulu modelleri listeleme](https://docs.ollama.com/api/tags)

Üç kaynak HTTP 200 ile okundu. `POST /api/chat` için `messages` ve `tools`
kullanılır. F1 akışsız `stream: false` isteği yapar; akışlı ses sonraki fazdadır.
`prompt_eval_count` girdi, `eval_count` çıktı token sayısıdır. Kullanılabilir model
yerel kurulumun `GET /api/tags` çıktısından seçilir ve araç desteği resmi model
bilgisinden doğrulanır. Yerel çıkarımda sağlayıcı API ücreti sıfır kabul edilir;
cihaz/elektrik maliyeti bu token bütçesinin kapsamı dışındadır. Uzak ücretli Ollama
hizmeti bu varsayımın kapsamında değildir.

## Bu oturumdaki canlı ölçüm durumu

Yalnız ortam değişkeninin var/yok bilgisi kontrol edildi:
`OPENROUTER_API_KEY` **yok**, `ANTHROPIC_API_KEY` **yok**. Anahtar değeri okunup
yazdırılmadı. Bu nedenle iki ücretsiz modelle canlı kabul ölçümü yapılmadı;
model başarı, gecikme veya maliyet sonucu üretilmedi. Katalogdaki model sayısı,
sahte kabul başarısı ve canlı model başarısı farklı ölçümlerdir.

Canlı ölçüm için ortam anahtarı sağlanınca güncel katalogdan iki ücretsiz,
araç destekleyen kimlik seçin ve her biri için
`python -m bench.run --canli --model MODEL_KIMLIGI` çalıştırın. Fiyatı sıfır olmayan
veya ücretsizliği doğrulanamayan modelle bu görev kapsamında çağrı yapmayın.
