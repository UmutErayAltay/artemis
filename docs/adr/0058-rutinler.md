# ADR 0058 — Rutinler

**Bağlam:** Kullanıcı "işe başla" gibi çok adımlı bir tarifi (VS Code aç, Spotify aç, sesi 30
yap) tek adla, sesle/yazıyla/panelden ve istenirse günün bir saatinde çalıştırmak istiyor.
Çalıştırmak için modele ihtiyaç yok.

## Karar

1. **Rutin = `{name, steps[{tool, arguments}], schedule?{at, days}}`.** Adımlar SABİT araç
   çağrılarıdır, argümanlar düz değerdir; serbest metin istemi ve çalışırken model yoktur
   (ADR 0027: model kalıcı istem yazamaz). En çok 12 adım, 30 rutin. Adımlar aracın `args_model`'ine
   karşı hem kayıtta hem çalıştırmada doğrulanır (`validate_steps`); biri geçersizse hiçbiri
   çalışmaz. Ad karşılaştırması büyük/küçük harf, boşluk ve Türkçe İ/I'ya duyarsızdır.
2. **Depo:** tek dosya `state_dir/routines.json`, atomik yazılır (geçici dosya + `os.replace`),
   yüklerken şemayla doğrulanır. Bozuk dosyada rutinler kapanır ve dürüst ileti verilir; dosya
   sessizce sıfırlanmaz, kaydet/sil de reddedilir. Kod: `artemis/proactive/routines.py`.
3. **Araçlar** (tek plugin, `artemis/tools/plugins/routines.py`): `routine_list` READ_ONLY;
   `routine_run` SIDE_EFFECT; `routine_save` ve `routine_delete` HER ZAMAN CONFIRM_REQUIRED
   (onayda tüm adımlar görünür). `routine_save` bu turda dış içerik okunduysa onay HİÇ sorulmadan
   reddedilir (`memory_rule_add` ile aynı kural, ADR 0054). Kayıt zaman ayarlayamaz, `routine_*`
   adımı içeremez (özyineleme yok), olmayan aracı içeremez. Var olan rutin yeniden kaydedilirse
   kullanıcının kurduğu zaman korunur.
4. **Çalıştırma kapıdan geçer.** `routine_run` her adımı `context.gate_handle.dispatch` ile
   çalıştırır: bu, çağrıyı yürüten turun `SafetyGate.dispatch`'idir ve AYNI `TurnState` (leke,
   gözetimsiz bayrağı) ve AYNI onay geri çağrısıyla bağlıdır. Leke, onay, gözetimsiz kuralı ve
   denetim adım adım, modelin çağırmasıyla aynı uygulanır; CONFIRM_REQUIRED adım yine sorar, bir
   adımın okuduğu dış içerik sonraki adımları onaylı yapar. Rutin kaydetmek onay atlatmanın yolu
   değildir. İlk başarısız ya da onaylanmayan adımda durulur; sonuç hangi adımların çalıştığını
   söyler. Kapıya ulaşma yolu `ToolContext.gate_handle` (kapı her `dispatch`te kurar); global
   durum yoktur.
5. **Yeni kapı yeteneği:** `BaseTool.refuse(arguments, context)` doluysa kapı onay penceresi
   açmadan çağrıyı reddeder. Yalnız reddedebilir; hiçbir onayı gevşetmez. Rutin araçları kapalı
   bayrakta ve `routine_save` lekeli turda bunu kullanır.
6. **Zaman yalnız Ayarlar > Rutinler'den kurulur** (hiçbir araç kuramaz). Zamanı gelince Artemis
   rutini ÇALIŞTIRMAZ: proaktif katman LLM'siz "<ad> rutininin zamanı. Çalıştırılsın mı?" bildirimi
   verir, panelde "Çalıştır" düğmesi çıkar; tıklama normal, onaylı bir `routine_run` turu başlatır.
   Gözetimsiz kural kapıda olduğu gibi kalır. Mevcut proaktif talep/grace/kaçırılan mantığı
   kullanılır (`routine:<ad>` oluşumu, `briefing_grace_hours`, günlük sınır); `proactive.enabled`
   gerekir. Telegram'da çalıştır düğmesi yoktur (metin bildirimi gider, çalıştırma yalnız panelde).
7. **Ayar:** `routines.enabled` (varsayılan `false`). Kapalıyken araçlar modele hiç gösterilmez
   (`ToolRegistry.without_prefix`), ayrıca her biri `refuse` ile reddeder.
8. **Denetim:** `routine_run_start` (ad, adım sayısı) ve `routine_run_end` (tamamlanan adım,
   tamamlandı mı); her adım normal `tool_requested/approval/tool_dispatch/tool_result` olaylarını
   yazar.
9. **Arayüz:** Ayarlar > Rutinler: açma kutusu, liste (ad, adım araçları, zaman), Çalıştır, Sil
   (ikisi de araç çağrısıdır, aynı köprü ve onay penceresi), saat + gün seçici, "Dosyayı aç".

## Bilerek yapılmayanlar

Adım düzenleyici arayüz (rutin sohbetle ya da JSON elle düzenlenerek oluşur), araç çıktısını
sonraki adıma aktarma, koşullu/dallanan adım, rutinden rutin çağırma, aracın zaman kurması,
zamanı gelince kendiliğinden çalıştırma, Telegram'dan çalıştırma, adımlar arası bekleme.

## Sınırlar

- Rutin tek turdur: `max_seconds` (varsayılan 60 sn) süresi tüm adımlara ortaktır; uzun rutinde
  süre dolarsa kalan adımlar çalışmaz ve ileti bunu söyler.
- Çalışan rutin iptal edilirse sıradaki adımlar başlamaz; başlamış adımın yan etkisi geri alınmaz.
- Windows kabulü yapılmadı (`docs/YENI_OZELLIKLER_KABUL.md`).

**Ek (doğrulama sonrası):** `sent` dönen adım (ör. `open_application`: komut gitti, açıldığı doğrulanmadı) rutini durdurmaz; durdursaydı uygulama açan her rutin ilk adımda biterdi. Böyle bir adım varsa rutinin kendi sonucu da `sent` olur, başarı denmez (kural 5). UTF-8 olmayan dosya bozuk sayılır.
