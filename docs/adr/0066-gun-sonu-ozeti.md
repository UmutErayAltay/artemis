# ADR 0066 — Gün sonu özeti

**Bağlam:** Kullanıcı günlük tutmak ister ama emek vermek istemez. Artemis gün içinde ne yaptığını
zaten denetim kaydına (ADR 0004) yazıyor.

## Karar

1. **LLM'siz ve deterministik.** Özet günün denetim olaylarından ve proaktif depodan sayılarla
   üretilir. Model çağrılmaz, maliyet yok, hiçbir şey makineden çıkmaz. (LLM ile yazılmış bir anlatı
   bilinçli olarak dışarıda; ileride ayrı ADR ve açık izinle eklenebilir.)
2. **İçerik.** Tur sayıları, araç kullanımı ve başarısızlar, dosya oluşturma/taşıma/silme (yol
   denetimde kaydedildiği gibi), biten kod ve araştırma işleri, rutinler, hatırlatma durumları
   (depodan), reddedilen onaylar, dikte/seçili metin/Telegram indirme sayıları ve günün model
   harcaması. Konuşma metni, model yanıtı ve araç sonuç gövdesi OKUNMAZ.
   Not: denetimde tur başına kanal alanı yok; tur "sohbet", "gözetimsiz" (brifing) ve "panel"
   (tipli düğme) olarak ayrılır, Telegram/ses ayrımı yapılamaz. Harcama, defterin (UTC günü,
   günlük sorgu yok) yerine turların `turn_end` maliyet toplamından gelir; yerel gün sınırına uyar.
3. **Güvenli okuma.** Denetim dosyası sondan, en çok 64 MB / 400 000 satır ve satır başına 512 KB
   okunur; bozuk ya da bilinmeyen şekilli satır atlanır, hiçbir hata zamanlayıcı taramasından
   çıkmaz (`daily_summary_error` denetime yazılır). Döndürülmüş `audit-*.jsonl` dosyaları da
   taranır. Gün sınırı `proactive.timezone`'dur. Dosya yolu ve araç adları dış metin gibi
   `sanitize_name` ile temizlenir.
4. **Yazım.** `state_dir/gunluk/YYYY-MM-DD.md`, geçici dosya + `os.replace` ile aynı günün
   dosyası atomik yenilenir. Boş gün dosya da bildirim de üretmez.
5. **Zamanlama.** `daily_summary: {enabled: false, at: "22:30"}`, mevcut zamanlayıcıda
   (`proactive.enabled` gerekir) `briefing_occurrence` + SQLite talebi ile. Kapalıyken kaçan gün
   `briefing_grace_hours` içinde açılışta üretilir ve "gecikmeli" yazılır (deterministik olduğu için
   yanıltıcı değildir). Günlük proaktif sınıra sayılır.
6. **Bildirim.** "Günün özeti hazır", düğmeler "Aç" (dosyayı açar) ve yalnız `memory.vault_path`
   tanımlıysa "Vault'a ekle". Mevcut tek-düğmeli satır genişletildi (`Deliverer.deliver(summary_day=)`).
   "Vault'a ekle" `bridge.submit_tool` ile normal onaylı `vault_append` turudur (onay metni gösterir,
   en çok 4000 karakter, not adı `daily_summary.vault_note`); doğrudan dosya yazımı yoktur,
   kendiliğinden hiçbir şey eklenmez.
7. **Gizlilik.** Özet kullanıcının etkinliğidir: `private=True`, `local_only` (Telegram'a gitmez).
8. **Araç.** `daily_summary_read(day?)` READ_ONLY, `content_kind="file"`, `private=True`. Gün katı
   `YYYY-MM-DD`; dosya adı yalnız ayrıştırılan tarihten türer. Kapalıyken `refuse` + `without_prefix`.

## Bilerek yok

LLM anlatısı, geçmiş günleri toplu yeniden üretme, özeti kendiliğinden vault'a yazma, Telegram'a
gönderme, üretimi tetikleyen araç, kanal bazlı (Telegram/ses) tur dökümü.

**Ek (doğrulama sonrası):** tarama bayt/satır sınırıyla kesilirse ya da dev bir satır atlanırsa özet "eksik olabilir" der. Uç tarihli bozuk satır atlanır. Bilinen tavan: özet saatinden sonra o gün olanlar (varsayılan 22:30-24:00) o günün özetine girmez; saati geç seçmek bunu küçültür.
