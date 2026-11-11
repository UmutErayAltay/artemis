# ADR 0063 — Arka plan araştırma işi

**Bağlam:** "Şunu araştır, rapor yaz" isteği kullanıcı başka işle uğraşırken arka planda çalışmalı.
Sorun: araştırma döngüsü web sayfası okur (turu lekeler) sonra yine `web_search` çağırır (dışarı
giden araç); normalde her çağrı onay ister, arka plan işi bu yüzden imkânsız olurdu. Sızıntı için
bağlamda özel veri gerekir; bu yüzden onay kuralı değil özel veri kaldırıldı.

## Karar

1. **Başlatma her zaman onaylı.** `research_start(topic, max_searches)` CONFIRM_REQUIRED'dır;
   onayda konu ve sayı görünür. Bu onay tam o sayıda (config'teki `research.max_searches` ile
   sınırlı, varsayılan 12) `web_search` için ön onaydır. Taslakta `depth` denmişti; derinlik
   doğrudan arama sayısı olarak verildi, onay ekranında görünsün diye.
2. **Yalıtılmış döngü.** İş kendi `AgentLoop`'unu çalıştırır: boş geçmiş, yalnız `web_search`
   içeren registry kopyası (`ToolRegistry.only`; kayıtlı bir sayfa getirme aracı yok, yenisi
   yazılmadı), kullanıcıdan ayrı `ToolContext` (yalnız arama işlevi servis). Sistem istemi
   `AgentLoop(system_prompt=...)` ile VERİLİR: doluyken hafıza notları, companion dosyaları,
   beceriler, uygulama listesi ve ipuçları hiç eklenmez. Kapı değiştirilmedi.
3. **Onay geri çağrısı.** `web_search` sayaç bitene dek onaylanır, geri kalan her şey reddedilir ve
   `research_denied` denetimine yazılır. Kapı ilk aramayı (temiz tur) sormadan geçirdiği için sayaç
   arama işlevinde tutulur; her arama `research_search` (iş kimliği, kalan) olarak denetlenir ve
   sayaç bitince arama hata döner.
4. **Sınırlar.** `research.max_seconds` (600) hem döngü süresi hem dış sınırdır; normal adım
   sınırı, günlük bütçe defteri ve `allow_paid` sağlayıcı yolunda aynen geçerli, bütçe aşımı iş
   `error` olur. Aynı anda tek iş.
5. **Çıktı.** `state_dir/arastirma/<iş-kimliği>.md` (rapor + kaynak URL'leri; geçici dosya +
   `os.replace`). Dosya adı yalnız iş kimliğinden gelir, konu yola girmez. `research_result` sonucu
   `external=True`, `content_kind="web"`. Kasaya kendiliğinden yazılmaz.
6. **Araçlar.** `research_start`, `research_status` (READ_ONLY), `research_result` (READ_ONLY,
   dış), `research_cancel` (SIDE_EFFECT; "sent" döner, durma doğrulanmadan başarı denmez).
7. **Durum.** `state_dir/research.sqlite3`; süreç ölürse çalışan iş açılışta `uncertain` olur,
   yeniden başlatılmaz. Tamamlanınca LLM'siz toast: "Araştırma bitti: <konu>".
8. `research.enabled` varsayılan `false`; kapalıyken `research_*` araçları modele hiç görünmez.

## Bilerek yapılmayanlar

- Kod-ajanı iş deposu paylaşılmadı (alt süreç ve depo kilidi taşıyor); kalıp küçükçe yinelendi.
- Telegram bildirimi eklenmedi (mevcut `notify_telegram` kuralları dışında); Telegram kanalı
  (`with_code_agent=False`) işi başlatamaz, ikinci runtime çalışan işi `uncertain` yapmasın diye.
- Sayfa getirme, sonuç özetleme için ikinci model, kasaya otomatik yazma yok.
- Paneldeki iş listesi eklenmedi; durum `research_status` ve bildirimle izlenir.
