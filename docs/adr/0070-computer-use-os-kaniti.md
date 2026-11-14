# ADR 0070 — computer_use "ok" için OS düzeyi kanıt

**Bağlam:** `ComputerSession._verify` (ADR 0039 ek karar 3) yalnızca ikinci modelin taze ekran
görüntüsü üzerindeki yargısını döndürüyordu; "ok"un arkasında işletim sisteminden gelen bir olgu
yoktu.

## Karar

1. **Oturum olguları.** Oturum girdi adımlarını izler: sonucu "sent" olmayan her adım (iptal, süre,
   hedef pencere kayması) ya da eksik yazılan metin kısa bir Türkçe nedenle `_os_issues`e yazılır.
   Son `type` adımının metni ve onaylı hedef pencere kimliği yalnız bellekte tutulur; loga ve
   denetim kaydına ASLA girmez.
2. **UIA okuması.** `windows.focused_value_contains(expected, timeout)`: sabit PowerShell betiği
   odaktaki denetimin `ValuePattern` (yoksa `TextPattern`) değerini beklenen metinle karşılaştırır ve
   yalnız `1`/`0`/`?` basar. Beklenen metin (ekran içeriği kadar özeldir) yalnız `ARTEMIS_EXPECTED_TEXT`
   ortam değişkeniyle çocuğa gider, argv'de yoktur. Windows dışı, zaman aşımı, desteklenmeyen denetim
   (parola alanı dahil) ve her hata `None` (doğrulanamadı) döner.
3. **VE kuralı.** `os_ok`: sorun varsa False; yoksa ve bir yazım adımı olduysa ve ön plan hâlâ onaylı
   hedef penceredeyse UIA sonucu (True/False/None); aksi halde bilinmiyor. `verified = model_kararı
   and os_ok is not False`. OS kanıtı bir "hayır"ı asla "evet" yapmaz, yalnız "evet"i geri çevirebilir
   (model "hayır" dediyse UIA hiç çalıştırılmaz). Kanıt metnine ` | OS: doğrulandı|çelişki|doğrulanamadı`
   eklenir; `computer_verify` denetim olayına metinsiz `os_check: ok|contradiction|unknown` alanı girer.
   Mevcut durdurma/süre korumaları aynen geçerlidir.

## Sınırlar

- Olumlu OS kanıtı yalnız YAZMA görevlerinde vardır; tıklama/kaydırma/tuş görevlerinde yalnız "girdi
  gönderildi mi" sonuç denetimi çalışır, sonucun doğruluğu hâlâ model yargısıdır.
- UIA yalnız odaktaki tek denetime bakar; metin başka bir alana/uygulamaya yazılmışsa ya da denetim
  UIA'yı desteklemiyorsa sonuç "doğrulanamadı"dır (model kararı kalır).
- "İçeriyor" karşılaştırması: alan önceden aynı metni içeriyorsa yanlış olumlu verebilir.
