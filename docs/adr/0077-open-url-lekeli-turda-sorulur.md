# ADR 0077 — `open_url` lekeli turda sorulur, otomatik onayda sorulmaz

**Bağlam:** ADR 0036 ara yolu, bağlamda dış içerik varken yalnız `?`/`#` taşıyan adresi
soruyordu. Bilinen sınır açıktı: veri yolda (`kotu.com/GIZLI`) ya da alt alan adında
(`GIZLI.kotu.com`) taşınabiliyordu. Okunan bir sayfadaki enjeksiyon modeli böyle bir adresi
açmaya ikna ederse tarayıcı isteği gönderdiği anda veri karşı sunucunun kaydına (alt alan
adında yalnız DNS sorgusuyla bile) düşer; onay sorulmaz. 2026-10-31'de bu sınır "değişmesin"
kararıyla kalmıştı. Kullanıcı (2026-11-16): "otomatik onayda sormasın, normalde sorsun".

## Karar

1. **Normal kip (`computer_use.mode: accept`):** bağlamda dış içerik varken (bu tur ya da
   önceki tur, `turn_tainted`) `open_url` her adreste SIDE_EFFECT olur; taint/ADR 0013 onayı
   işler. Temiz turda site açmak ADR 0036'daki gibi sorulmaz (READ_ONLY).
2. **Otomatik onay (`computer_use.mode: auto`, üst çubuktaki "Oto onay"):** `open_url`
   `auto_mode_ok` taşır; lekeli turda da sorulmaz, atlama `approval_skipped` olarak denetime
   yazılır (ADR 0039 mekanizması, kapıda yeni kural yok).
3. Gözetimsiz tur (ADR 0027) otomatik kipten önce gelir: lekeli turda `open_url` reddedilir.
4. Onay penceresi, kapının bugünkü dış içerik uyarısını aynen gösterir; host hatırlama
   (`trusted_sites.json`) yalnız site okuyan araçlar içindir, `open_url`'e uygulanmaz.

## Sonuçlar

- Normal kipte web okuduktan sonra YouTube'u açmak bile bir kez sorulur; ADR 0036'dan önceki
  sürtünme yalnız lekeli turda geri döner. Bu, sessiz sızıntı kanalını kapatmanın bedelidir.
- Otomatik onayda sızıntı kanalı bilerek açık kalır: ekrandaki ya da sayfadaki bir enjeksiyon
  sorulmadan veri taşıyan adres açtırabilir. ADR 0039'daki takasla aynı türdendir; kipi açan
  uyarı kutusu bunu da söyler. Varsayılan `accept` kalır.
- `test_only_adr_0039_tools_skip_approval_in_auto_mode` artık `open_url`'i de kilitler;
  `test_open_url_asks_after_external_content` yol ve alt alan adı durumlarını kapsar.
