# Yeni özellikler: elle kabul listesi

Her bölüm gerçek Windows'ta, en sonda elle işaretlenir; sahte testler yerine geçmez.

## Dikte kipi (ADR 0057)

Önkoşul: config'te `dictation: {enabled: true}`, ses modeli kurulu, ses kapalı. Gerçek Windows'ta elle.

- [ ] Ayarlar > Ses altında "Dikte kısayolu" ve "Dikte tuşu" görünüyor; Uygula sonrası kısayol çalışıyor.
- [ ] `dictation.hotkey` ile `voice.listening_hotkey` aynı yazılınca Uygula reddediyor ve ileti gösteriyor.
- [ ] Not Defteri açık: kısayola bas, "Günaydın, çalışkan öğrenci şişe" de, tekrar bas. Metin Türkçe karakterlerle (ğ ü ş ı ö ç) doğru yazılıyor.
- [ ] Kayıt sürerken durum satırında ve petde "Dikte dinliyor…" görünüyor; bitince "Hazır"a dönüyor.
- [ ] Yazım sürerken başka pencereye geç: yazım duruyor, ileti "yarım kaldı ... geri alınmadı" diyor, tam metin panelde.
- [ ] Yazım sürerken Esc: yazım duruyor, gönderilen kısım yerinde kalıyor (geri alınmıyor), ileti bunu söylüyor.
- [ ] Artemis penceresi öndeyken dikte et: hiçbir şey yazılmıyor, metin panelde gösteriliyor, pano değişmiyor.
- [ ] Kayıt sırasında başka pencereye geçip bitir: hiçbir şey yazılmıyor, metin panelde.
- [ ] `max_seconds` küçük (ör. 5) iken ikinci basış olmadan kayıt kendiliğinden bitiyor ve yazılıyor.
- [ ] Ses kipi açıkken dikte kısayolu: "Ses açıkken dikte kullanılamaz" iletisi, mikrofon dikte için açılmıyor.
- [ ] Dikte sürerken "Ses" düğmesi: açılmıyor, ileti veriyor.
- [ ] Dikte sırasında "Artemis dosyaları listele" de: metin olduğu gibi yazılıyor, komut çalışmıyor, sohbet geçmişine girmiyor.
- [ ] Denetim dosyasında `dictation` olayı var (süre, karakter sayıları, sonuç) ve söylenen metin hiçbir yerde yok.
- [ ] Mikrofon göstergesi yalnız iki basış arasında yanıyor.

## Rutinler (ADR 0058)

Önkoşul: config'te `routines: {enabled: true}` ve zamanlı bildirim için `proactive: {enabled: true}`; izinli uygulamalar listesinde en az VS Code/Spotify benzeri iki uygulama. Gerçek Windows'ta elle.

- [ ] Ayarlar > Rutinler görünüyor; kutu kapalıyken liste boş ve "Rutinler kapalı" yazıyor, düğmeler pasif.
- [ ] Sohbette "işe başla adında rutin kaydet: VS Code'u aç, Spotify'ı aç, sesi 30 yap" de. Onay penceresi adı ve tüm adımları argümanlarıyla gösteriyor; reddedince hiçbir şey kaydedilmiyor, onaylayınca kaydediliyor.
- [ ] Ayarlar > Rutinler listesinde "işe başla · 3 adım: ..." görünüyor; "Dosyayı aç" `routines.json`'u açıyor.
- [ ] Sesle "Artemis, işe başla rutinini çalıştır" de: üç uygulama/ayar sırayla çalışıyor, yanıt "3/3 adım" diyor.
- [ ] Panelden rutini seçip "Çalıştır"a bas: onay penceresi çıkıyor, onaylayınca aynı şekilde çalışıyor.
- [ ] İçinde `close_application` olan bir rutin kaydet ve çalıştır: kapatma adımında onay soruluyor; reddedince rutin orada duruyor, sonraki adımlar çalışmıyor, yanıt hangi adımın onaylanmadığını söylüyor.
- [ ] Bir web sayfası okuttuktan sonra aynı konuşmada "rutin kaydet" de: onay penceresi hiç açılmıyor, "bu turda dış içerik okundu" iletisi geliyor.
- [ ] Rutini sohbetle silmeyi iste ve panelden "Sil"e bas: ikisinde de onay soruluyor.
- [ ] Ayarlar'da saati 2 dakika sonraya ve bugünün gününe kur, "Zamanı kaydet": zamanı gelince sohbette "işe başla rutininin zamanı. Çalıştırılsın mı?" ve "Çalıştır" düğmesi çıkıyor; rutin kendiliğinden BAŞLAMIYOR.
- [ ] Bildirimdeki "Çalıştır"a bas: normal onaylı çalıştırma başlıyor. "Kapat" satırı kaldırıyor, rutin çalışmıyor.
- [ ] Artemis kapalıyken geçen zamanlı saatte açınca: grace süresi içindeyse bildirim geliyor, dışındaysa gelmiyor ve rutin çalışmıyor.
- [ ] `routines.json`'u bozuk bırak (örn. `{`): panel ve araçlar "Rutin dosyası bozuk" diyor, dosya değişmiyor, Artemis açılıyor.
- [ ] Denetim dosyasında `routine_run_start` / `routine_run_end` ve her adımın kendi `tool_dispatch` olayları var.
