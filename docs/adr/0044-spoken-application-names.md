# ADR 0044: Söylenen uygulama adının çözümü ve öğretilen takma adlar

- Tarih: 2026-10-25
- Durum: Kabul edildi (kullanıcı: düzeltmeler hatırlansın; konuşma kalitesi planı aşama 4)

**Bağlam:** `open_application` yalnız tam ya da boşluk/büyük harf farkıyla eşleşen adı
açıyordu. Konuşma tanıma İngilizce uygulama adlarını Türkçe okunuşla yazar ("breyv",
"kroum", "spotifay"); kullanıcı "Brave" dediğinde uygulama bulunamıyordu. Plan aşama 4
şunu ister: yanlış transcript başka bir komuta dönüşmez, belirsizlikte sorulur, adlar
canlı kurulu katalogdan bulunur, depoya kullanıcıya özel ad listesi girmez, tehlikeli
yanlış hedef sıfırdır.

**Karar:**

1. **Çözüm sırası** (`artemis/tools/app_resolver.py`): tam/normalize ad → kullanıcının
   öğrettiği takma ad → yakın eşleşme. Aday kümesi her zaman `settings.applications`
   (izin listesi + `allow_all_applications` ile Başlat menüsü kataloğu); takma ad listeyi
   genişletmez, hedefi listede yoksa yok sayılır.
2. **Yakın eşleşme** iki ölçüyü birleştirir: Türkçe harfleri katlanmış düz benzerlik ve
   okunuş iskeleti (sesli harfler atılır, c/q/x→k, e/i/y önünde c→s, w→v, ph→f, h düşer;
   "Brave", "breyv", "brev" → `brv`). Adın tek sözcüğü de aday olur ("edge" → Microsoft
   Edge). Kabul: en iyi skor ≥ 0,85 ve ikinciden ≥ 0,08 fark, ya da ≥ 0,72 ve ≥ 0,25 fark
   (rakipsiz aday). Arada kalan ≥ 0,70 adaylar **belirsiz** döner (en çok üç); model
   kullanıcıya sorar ("kod": Claude Code mu VS Code mu). Daha zayıfı hiç önerilmez.
   Ad listesi yoktur; kural okunuşa dayanır ve testte görülmemiş katalogla sınanır.
3. **Güvenlik sınırları:**
   - Kaldırıcı ("uninstall", "kaldır") yakın eşleşmeyle asla seçilmez; takma adın
     bağlandığı uygulama kaldırıcıysa `open_application` CONFIRM_REQUIRED olur.
   - Yakın eşleşme yalnız **temiz turda** kendiliğinden açar. Lekeli turda kullanıcı onayda
     söylenen adı gördü, seçilen uygulamayı değil; araç açmaz, adayı döner ve tam adla
     yeniden istenir ki onay gerçek hedefi göstersin (kural 2).
   - `close_application` yakın eşleşmeyle **hiç** kapatmaz; yalnız öneri döner. Takma ad
     kesin eşleşmedir ve hedefin `close_without_confirmation` tercihini taşır.
   - Açılan uygulama yakın eşleşmeyse sonuç mesajı ve `data.application` bunu söyler,
     denetime `app_resolution` yazılır.
4. **Öğretilen takma adlar** (`app_alias_remember`, `app_alias_forget`): kullanıcı
   düzeltince ("hayır, Brave'i kastettim") model söyleneni uygulamanın tam adına bağlar.
   Hedef tam adla (yakın eşleşmesiz) izin listesinde olmalı; başka bir uygulamanın gerçek
   adı takma ad olamaz. SIDE_EFFECT: temiz turda sormadan, lekeli turda tam argümanlı
   onayla yazılır (web metni yönlendirme kuramaz). Kayıt `state_dir/app_aliases.json`
   (kullanıcıya özel, depoda değil; en çok 200), geçici dosya + atomik değiştirme ile
   yazılır ve geri okunarak doğrulanmadan başarı denmez. `persist_memory: false` iken ne
   yazılır ne okunur. Bozuk dosya uygulama açmayı engellemez (`app_alias_unavailable`).
5. Sistem istemi: aday dönerse tahminle işlem yapma, sor; düzeltmede `app_alias_remember`;
   "açma/kapatma" gibi olumsuz emirde işlem yapma.

**Sınırlar:** Eşikler sentetik okunuş örnekleriyle seçildi; gerçek Windows kataloğu ve
gerçek STT çıktısıyla ölçülmedi. Çok sözcüklü kısaltmalar ("vs kod") yakın eşleşmeyle
bulunmaz; bir kez öğretilince bulunur. STT `hotwords`'e uygulama adlarını eklemek ayrı
ölçüm ister (uzun liste doğruluğu düşürebilir) ve bu adımda yapılmadı.

**Doğrulama:** `tests/test_app_resolver.py` (okunuş varyantları, belirsiz rakipler,
ilgisiz adın tahmin edilmemesi, görülmemiş katalog, kaldırıcı, lekeli tur, kapatma,
takma ad yaşam döngüsü, onay argümanları, kalıcı hafıza kapalı, bozuk dosya, geri okuma).
Mutasyon: lekeli turda yakın eşleşme, kapatmada yakın eşleşme, kaldırıcı dışlama, takma
ad izin listesi, gerçek adı gölgeleme, takma ad kaldırıcı riski, fark eşiği, kalıcı hafıza
okuması, hedefte yakın eşleşme ve geri okuma doğrulaması kaldırılınca testler kırıldı.
