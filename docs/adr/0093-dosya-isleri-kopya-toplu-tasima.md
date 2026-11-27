# ADR 0093 — Dosya işleri: kopyalama, toplu taşıma, yol kuralı ve tekrar uyarısı

**Bağlam:** ADR 0092 ölçümünde (2026-11-27, Colab L4 yerel modeller + OpenRouter ücretsiz modeller) zor dosya
senaryolarındaki kayıpların çoğu modelden değil araç setinden geliyordu:

- `zor_yedek_kopya`: kopyalama aracı yok; model `read_file` + `create_file` ile kopyalamaya çalışıyor, içerik
  ya da yol tutmuyor, yine de "kopyası oluşturuldu" diyor.
- "Masaüstü klasörünü bulamadım", "acil klasörünün tam yolu nedir?": model dosya yollarının çalışma köküne
  (masaüstü) göre olduğunu bilmiyor, kök listelemede yol uyduruyor.
- `zor_uzantiya_gore_ayir`, `zor_icerige_gore_sec`: dosyalar tek tek taşınırken adım sınırı (`max_steps`) doluyor
  ya da aynı çağrı tekrarlanınca tur `repeated_call` ile hemen bitiyor.

## Karar

1. **`copy_file` aracı** (`filesystem.py`, SIDE_EFFECT): kaynak ve hedef `safe_join`'dan geçer, hedef
   `allow_symlinks=False`; yalnız normal dosya; hedef varsa reddedilir (üzerine yazma yok); hedef klasör yoksa
   reddedilir (model `create_directory` çağırır); boyut sınırı vardır. Başarı yalnız hedef dosya oluşup boyutu
   kaynakla eşitse döner. Kopyalama veri silmez; ADR 0092 yıkıcı listesine girmez.
2. **`move_files` aracı** (SIDE_EFFECT, `move_file` ile aynı risk): tek çağrıda bir klasördeki birden çok
   dosyayı başka bir klasöre taşır. Seçim ya açık ad listesi (en çok 50) ya da yalnız ad üzerinde çalışan basit
   bir kalıp (`*.txt`; yol ayırıcı ve `..` içeremez, alt klasöre inmez; kalıp en çok 50 dosyaya eşleşebilir; fazlası hiçbir şey taşımadan reddedilir). Her dosya `move_file` ile aynı kurallarla
   taşınır, üzerine yazılmaz. Onay tek çağrıdır ve kullanıcı argümanları (klasör, kalıp ya da ad listesi, hedef)
   olduğu gibi görür; kapı değişmez. Sonuç dosya başına durum listesidir; hepsi doğrulanmadıkça `success=True`
   dönülmez, kısmi sonuç dürüstçe `partial` olarak bildirilir.
3. **Yol kuralı:** dosya araçlarının açıklamalarına ve `LIGHT_SYSTEM_PROMPT`'a tek satır: yollar çalışma köküne
   (masaüstü) göredir, kökü listelemek için yol boş bırakılır, `C:\...` gibi mutlak yol kullanılmaz. Kök ya da
   mutlak yol hatasında araç sonucu modele doğru kullanımı söyler.
4. **Tekrar uyarısı (yalnız hafif kip):** aynı araç çağrısı aynı argümanla tekrarlanınca tur ilk seferde
   durdurulmaz: çağrı ÇALIŞTIRILMADAN modele "bu çağrının sonucu zaten yukarıda; sıradaki adıma geç ya da bitir"
   araç sonucu döner. İkinci tekrarda bugünkü `repeated_call` durması geçerlidir. Yan etki iki kez çalışmaz.

## Değişmeyenler

SafetyGate tek kapı; onay, taint, gizlilik ve bütçe kuralları aynı. Yeni araçlar `@register_tool` ile tek plugin
dosyasından keşfedilir, ADR 0092 araç gruplarında `files` grubuna girer.

## Ölçüm

Aynı OpenRouter ücretsiz modeller ve `bench/model_testi.py` 15 sohbet senaryosu, kip açık/kapalı. Hedef: ortak
kalan üç zor senaryodan en az ikisi geçsin, tehlikeli 0.
