# ADR 0076 — Konuşma hafızasının diskte şifrelenmesi (DPAPI)

**Bağlam:** Artemis'in kalıcı konuşma geçmişi ve öz notları (`state_dir/memory.sqlite3`,
ADR 0025) bugüne dek diskte düz metin olarak tutuluyordu. Aynı makinedeki başka bir Windows
kullanıcısı, yedek kopya ya da çalınan disk üzerinden geçmiş konuşmalar doğrudan okunabilirdi.
Öte yandan `search_turns` (ADR 0025) son N tur üzerinde Python tarafında Türkçe katlama ve
sözcük puanlaması yapar; tam dosya şifrelemesi yerine sütun düzeyinde şifreleme arama
davranışını korur.

## Karar

1. `artemis/memory/protect.py` içinde sütun düzeyinde koruma eklenir:
   - Windows'ta `DpapiProtector`, `crypt32.CryptProtectData` / `CryptUnprotectData`
     (kullanıcı kapsamı, `artemis-memory-v1` entropisi, `CRYPTPROTECT_UI_FORBIDDEN`) kullanır ve
     çıktıyı `dpapi1:<base64>` önekiyle yazar.
   - `PlainProtector` şifreleme kapalıyken veya Windows dışındaki ortamlarda düz metni korur;
     `dpapi1:` önekli bir satırı çözemez ve `UnreadableMemory` fırlatır.
2. `MemoryStore` şema sürümü 2'ye çıkarılır (`_SCHEMA_VERSION = 2`). Şifreleme açıkken 1 -> 2
   geçişinde `ensure_schema` geçici yedeğinden hemen sonra, tek işlemde yalnızca `dpapi1:` öneki
   taşımayan mevcut düz metin satırlar şifrelenir (`turns.record_json`, `turns.user_text`,
   `turns.answer`, `turns.search_text` ve `notes.text`). Şifreleme kapalıysa hiçbir satır
   yeniden yazılmaz.
3. Geriye dönük okuma ve veri bütünlüğü korunur: öneksiz eski satırlar şifresiz olarak okunmaya
   devam eder; çözülemeyen (`UnreadableMemory`) tek bir satır silinmez veya değiştirilmez,
   yalnızca tablo adı, satır kimliği ve hata sınıfı günlüğe yazılarak atlanır.
4. Mevcut yapılandırmaları bozmamak için `MemoryConfig.encrypt_at_rest` varsayılan olarak
   `false` kalır; `config/config.example.yaml` (yeni kurulumlar) içinde `true` olarak
   belgelenir.

## Sonuçlar

- DPAPI kullanıcı kapsamı diğer Windows kullanıcılarına ve fiziksel disk hırsızlığına karşı
  koruma sağlar; aynı kullanıcı oturumunda çalışan kötü amaçlı yazılımlara karşı korumaz.
- Arama ve geçmiş okuma Python tarafında çözülmüş metin üzerinde çalıştığı için mevcut
  sıralama, leke ve gizlilik kuralları değişmez.
- Companion değişiklik günlüğü (`degisiklikler.sqlite3`) ve Markdown hafıza dosyaları
  (`hafiza/*.md`) bu aşamada kapsam dışındadır; ayrı bir adımda ele alınacaktır.
- Test: `tests/test_memory_protect.py` (sahte koruyucu ile şifreleme, geçiş, idempotentlik,
  atlama; Windows'ta gerçek DPAPI gidiş-dönüş ve bozma sınaması).
