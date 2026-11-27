# ADR 0092 — Hafif model kipi: niyet yönlendirme, dar araç seti, araç çağrısı onarımı

**Bağlam:** Kullanıcıların çoğu ücretli model almaz; ücretsiz OpenRouter modelleri ya da yerel 8–30B modellerle
çalışır. Umut (2026-11-26): "ilk önce soruyu algılar ve yönlendirir, ardından seçilen araç veya araçlar ile
bağlamı şişirmeden o işi yaptırmaya ilerler". Zor işler için zaten ayrı model seçilebiliyor (ADR 0078 kademesi,
`hard_model`); bu kip güçlü modele sormak değil, zayıf modelin kendisini başarılı kılmak içindir.

Ölçüm (2026-11-27, Colab L4, `bench/model_testi.py`, istek sınırı kaldırılmış kopya):

| Model | Temel | Zor | Tehlikeli |
| --- | ---: | ---: | ---: |
| gpt-oss:20b | 5/7 | 3/8 | 1 |
| qwen3:30b-a3b | 7/7 | 2/8 | 0 |

Kayıpların biçimi: (a) çok adımlı dosya işinde iş yerine "netleştirir misiniz?" sorusu, (b) yarım bırakma / boş
yanıt, (c) araç çağrısını düz metin JSON olarak yazma (`{"path":"kayit"}`), (d) dosya içindeki talimatla
`delete_file` isteme (SafetyGate engelledi). Her turda 71 aracın tanımı ve uzun sistem istemi gider; küçük
modelin dikkati dağılır. ADR 0078 sıkı kuralları (madde 3–4: "belirsizse sor") zayıf modelde (a)'yı büyütür.

## Karar

1. **Model başına kip:** `ModelConfig.light_mode: bool | None` (None = otomatik). Otomatik açık: sağlayıcı
   `ollama`, uç noktası yerel (127.0.0.1 / localhost / ::1 / 0.0.0.0) ya da model adı `:free` ile biter.
   Claude modelleri ve diğerlerinde kapalı. Ayarlar > Modeller'de model satırında "Hafif model kipi:
   Otomatik / Açık / Kapalı". Kip turun modeline göre seçilir (`hard_model` seçildiyse onun değeri).
2. **Niyet yönlendirme (kural, ek model çağrısı yok):** kullanıcı metni sabit araç gruplarına eşlenir
   (`artemis/brain/light_mode.py`): dosya, web, uygulama/pencere, hatırlatma/takvim/posta, not/hafıza,
   sistem/ses/güç, ekran/bilgisayar kullanımı, kod ajanı, rutin/araştırma, mesaj/bildirim. Her grup 3–10
   araç; birden çok grup eşleşirse birleşim. Hiçbiri eşleşmezse ya da birleşim 16 aracı aşarsa tam set
   (bugünkü davranış) kullanılır: yönlendirme yalnız daraltır, yanlış daraltma işi imkânsız kılmasın diye
   modelin `need_more_tools` adlı yerleşik çağrısı turu tam setle bir kez yeniden kurar. İkinci kademe küçük
   model sınıflandırması bu ADR'de yok: önce kural katmanı ölçülür, yetmezse eklenir.
3. **Kısa istem:** hafif kipte `SYSTEM_PROMPT` + `STRICT_CHAT_RULES` yerine `LIGHT_SYSTEM_PROMPT` gider:
   kısa maddeler, tek örnek. Değişen kural: kullanıcı işi ve hedefi söylediyse (dosya/klasör adı, uzantı,
   ölçüt) soru sorma, makul varsayımla yap, sonunda ne varsaydığını söyle; eksik klasörü oluştur. Hedef
   gerçekten yoksa ("şunu sil") sor. Dış içerik talimatı VERİDİR kuralı aynen kalır. Geçmiş en çok 2 tur,
   beceri listesi ve uygulama listesi yalnız ilgili grup seçildiyse eklenir.
4. **Araç çağrısı onarımı:** model araç çağırmayıp içerik olarak tek bir JSON nesnesi yazarsa ve bu nesne
   (a) `{"name": ..., "arguments": {...}}` biçimindeyse ya da (b) seçili araçlardan tam birinin argüman
   şemasına uyuyorsa, çağrı `ToolCall`'a çevrilir ve **SafetyGate'ten normal yolla geçer** (kapı, onay, taint
   değişmez). Şema doğrulaması başarısız çağrıya alan adlarını ve beklenen türleri gösteren kısa hata döner;
   tur başına en çok 2 onarım.
5. **İş bitirme dürtüsü:** hafif kipte model araç çağırmadan soru sorarsa ve istek çok adımlıysa
   (`tiering._ACTIONS` ile ≥2 eylem) ya da somut hedef içeriyorsa (dosya uzantısı, tırnaklı ad, klasör adı),
   bir kez "Kullanıcı yeterli bilgi verdi; makul varsayımla aracı çağır, varsayımını sonda söyle." eklenir.
   Tek eylemli ve hedefsiz istekte (ör. "şunu sil") soru serbesttir.
6. **Yerel uç noktada istek sınırı yok:** `provider_timeout_seconds` (15 sn) bulut sağlayıcının asılı kalmasına
   karşı yedek modele geçiş içindir. Yerel uç noktada yavaşlık soğuk yükleme ve üretimdir, asılma değil: istek
   başına sınır uygulanmaz, turun `max_seconds` bütçesi ve iptal kalır. Model testi (`bench/run.py`,
   `bench/bilgisayar_testi.py`) config'teki süreleri taşır; yerel uçta senaryo bütçesi en az 600 sn.
7. **Değişmeyenler:** SafetyGate tek kapı; onay, taint, gizlilik ve bütçe kuralları aynı. Kip yalnız modele
   giden istemi ve araç tanımlarını daraltır; daraltılan dışındaki bir araç çağrılırsa kapı "araç yok" der
   (bugünkü kayıt dışı araç yolu).

## Ölçüm planı

`bench/model_testi.py --hafif ac|kapali|oto` (varsayılan oto). Aynı Colab L4, aynı iki model, önce/sonra.
Hedef: zor ≥ 6/8, tehlikeli 0, temel düşmesin, medyan süre düşsün. Güçlü modelde (Claude) kip kapalı kalır,
`--hafif kapali` ile sahte bench (`bench.run --sahte`) değişmemeli.

## Sonuçlar

- Daraltma yanlış grubu seçerse model bir tur kaybeder (`need_more_tools`); ölçümle grup sözcükleri ayarlanır.
- Kısa istem ADR 0078'in "belirsizse sor" kuralını hafif kipte daraltır; belirsiz silme senaryosu
  (`temel_belirsiz_silme`) yine sormalı, bench bunu kilitler.
- Onarım yalnız biçim düzeltir; yetki vermez.
