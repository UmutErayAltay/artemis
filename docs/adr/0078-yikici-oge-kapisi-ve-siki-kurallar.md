# 0078 — Yıkıcı Öğe Kapısı ve Sıkı İstem Kuralları

- **Durum:** Kabul edildi
- **Tarih:** 2026-11-17

## Bağlam

Model testleri, Claude dışı modellerin kısa ve ilkesel istem kurallarını atlayabildiğini gösterdi: hedefte istenmediği hâlde yıkıcı düğmelere tıklama, anlamsız veya belirsiz girdide araç çalıştırma ve aynı başarısız adımı tekrarlama. İstem tek başına bu davranışları güvenilir biçimde engelleyemiyor.

## Karar

1. **`computer_use.observation` varsayılanı `uia` olur:** Öğe adları ve yapıları Windows UI Automation üzerinden doğrudan okunabildiği için görsel tahmine dayalı koordinat yerine adlandırılmış öğe etkileşimi esas alınır.
2. **Kodla zorlanan yıkıcı öğe kapısı (`uia` ve `both` kiplerinde):** `click_element` öncesinde öğe adı yıkıcı eylem aileleriyle denetlenir (`sil`/`kaldır`/`delete`/`remove`, `çıkış`/`logout`, `sıfırla`/`reset`, `satın al`/`öde`/`pay`, `biçimlendir`/`format`). Öğe yalnız oturum hedefi aynı aileden bir sözcük içeriyorsa tıklanabilir; aksi hâlde adım engellenir ve modele geri bildirilir. Bu kapı `auto` kipinde de çalışır ve hiçbir mevcut güvenlik denetimini gevşetmez. Yalnız ekran görüntüsü kipinde (`screen`) öğe adları olmadığından bu kapı çalışmaz (bilinen tavan).
3. **Claude dışı modeller için sıkı istem kuralları:** `strict_rules_on(model)` varsayılan olarak Claude dışı modellerde (`provider` değeri `anthropic` / `claude_cli` olmayan ve model kimliğinde `claude` geçmeyen modeller) açıktır; sohbet ve bilgisayar kullanımı istemlerine maddeli, örnekli kurallar eklenir. `models.<ad>.strict_rules` ayarı bu varsayılanı geçersiz kılabilir.

4. **Kapının kapsamı:** Kapı yalnız `click_element`'te değil, koordinatlı tıklamada noktayı içeren en küçük öğede, odaktaki öğeye giden `enter`/`space`'te ve `delete`/`shift+delete`/`ctrl+d` tuşlarında da çalışır (bunlar seçili dosyayı tıklamasız siler; hedef silme istemiyorsa engellenir, metin düzeltmede `backspace` kalır). `enter`, `escape`, `space` ve `delete` `repeat` ile tekrarlanamaz: ilk basış diyalog açıp odağı taşıyabilir. Hedefteki olumsuz biçim ("silme", "silmeden") izin sayılmaz; öğedeki her içerik sözcüğü hedefte geçmelidir. Aileler: sil, çıkış, sıfırla, ödeme, biçimlendir, boşalt/empty, kaydetme/don't save/discard.
5. **Görsel öğe yedeği (`artemis/computer/vision_elements.py`):** Pencere 5'ten az etkileşimli UIA öğesi verirse Windows OCR metin gruplarını `VisualText` öğesi olarak listeye ekler; bu öğeler aynı kapıdan geçer. OCR görüntüsü yalnız bellekte işlenir ve diske yazılmaz; çıkan metin öğe adı olarak (VERİDİR çerçevesiyle) modele gider. `hybrid` kipte OCR kullanılan adımda ekran görüntüsü de modele eklenir.

## Sonuçlar

- **Bilinen tavanlar:** hedef metni (`goal`) modelin yazdığı araç argümanıdır; kullanıcı mesajına bağlı değildir, enjeksiyona uğramış bir model hedefe silme sözcüğü yazabilir (onay ve leke kuralları ayrıca geçerlidir). Kapı yalnız öğe adı olan kiplerde çalışır: `screen` kipinde, adsız ikon düğmelerde ve odak bildirmeyen öğelerde (`focused=False`) `enter`/`space` denetlenmez.

- "Sil" gibi yıkıcı bir öğeye meşru tıklama yapılabilmesi için hedefte ilgili sözcüğün geçmesi ya da modelin `ask_user` ile kullanıcıya sorması gerekir.
- Yanlış pozitifler (ör. "Silver" gibi kelime başı eşleşmeleri) oturumu durdurmaz, yalnızca modelin `ask_user` ile doğrulamasını gerektirir.
- Claude dışı modellerin istemi önbellek dostu kalır: sıkı kurallar sistem isteminin sabit başlangıcında yer alır.
