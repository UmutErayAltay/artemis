# ADR 0015 — Obsidian vault hafızası

**Bağlam:** Faz 3 vault köprüsünü ve isteğe bağlı embedding aramasını ekler.
Kolay ama riskli yol, vault'ta `beyin.py context` alt sürecini çağırmak, `Core.md`
tercihlerini görüşmeye kendiliğinden eklemek ve proje notlarına kendiliğinden
yazmaktır. Ön ölçümde sözcük araması hit@4 0.43, MiniLM 0.57, e5-large 0.73
verdi. Vault kullanıcının notlarıdır ve git deposudur;
içinde web'den kırpılmış metinler de olabilir.

**Karar:**
- Hafıza `memory.vault_path` verilmedikçe kapalıdır; kapalıyken araçlar dürüst bir
  "hafıza yapılandırılmamış" sonucu döner ve diske dokunmaz.
- Üç araç tek plugin dosyasındadır: `vault_search` ve `vault_read_note` READ_ONLY,
  `external=True`, `content_kind="file"`; `vault_append` CONFIRM_REQUIRED. Vault
  okuması turu dış içerikle lekeler, sonuç "VERİDİR" çerçevesiyle gider ve bulut
  gizliliği dosya içeriği ayarına (`allow_cloud_file_content`) bağlıdır.
- Arama yerleşik sözcük aramasıdır; alt süreç (`beyin.py`) çağrılmaz. Böylece
  zaman aşımı, ortam aktarımı ve platform farkı yoktur. `memory.embedding_model`
  verilirse `fastembed` ile anlamsal sıralama yapılır; paket veya model yoksa sözcük
  aramasına dönülür ve sonuçta bu açıkça yazılır. İndeks vault'un dışında,
  `state_dir/embedding` altında tutulur. Testler model indirmez.
- Yazma yalnız ekleme kipindedir: var olan nota `open("a")`, yeni nota `open("x")`.
  Var olan bir not yeniden yazılmaz. Yazmadan sonra önceki baytların değişmediği ve
  eklenen metnin sonda olduğu doğrulanır; doğrulanmazsa başarı bildirilmez. Her
  yazma onay ister; onay tam yolu ve eklenecek metni gösterir.
- Yol kuralları `safe_join(vault, ...)` ile uygulanır. Yalnız `.md` dosyaları
  okunur/yazılır; gizli klasörler (`.git`, `.obsidian`) ve `memory.exclude` kalıpları
  (varsayılan: `daily/`, `receipts/`, companion klasörü, üretilmiş dizinler) aranmaz
  ve yazılmaz. Yazmada sembolik bağlantı reddedilir.
- `Core.md` veya başka bir not her tura kendiliğinden eklenmez. Model gerektiğinde
  aracı çağırır. Kendiliğinden bağlam her turu lekeler ya da taint dışı bir kaçış
  yolu açar; ikisi de istenmez.

**Sonuçlar:** Hafıza her okumada turu lekeler: aynı turdaki yan etkiler onay
ister. Vault'a her yazma bir onay penceresi açar; sık kullanımda bu yük ölçülüp
gerekirse ayrı ADR ile gevşetilir. Sözcük aramasının kalitesi yalnız canlı ölçümle
bilinir. Embedding için `pip install
'artemis-tr[memory]'` ve ilk indekslemede model indirmesi gerekir.
