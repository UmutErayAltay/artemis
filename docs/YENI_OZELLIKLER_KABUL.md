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
