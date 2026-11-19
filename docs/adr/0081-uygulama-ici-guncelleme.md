# ADR 0081: Uygulama içi güncelleme

- Tarih: 2026-11-19
- Durum: Kabul edildi

**Bağlam**

ADR 0075 gereği güncelleme denetimi yalnız sürüm sorgusu yapıyor ve kullanıcıyı tarayıcıdaki
sürüm sayfasına yönlendiriyordu. Paketlenmiş Windows kurulumunda kullanıcının Ayarlar >
Hakkında > Güncelleme bölümünden kurucuyu uygulama içinde indirip başlatabilmesi isteniyor.

**Karar**

1. **Yalnızca açık tıklama:** Arka planda otomatik indirme yapılmaz; indirme yalnızca
   dondurulmuş (`frozen`) kurulumda kullanıcının "İndir ve kur" düğmesine tıklamasıyla başlar.
2. **Sıkı varlık seçimi:** GitHub API `assets` listesinden yalnızca adı
   `^Artemis-Setup-\d+(\.\d+)*\.exe$` desenine uyan, indirme adresi
   `https://github.com/UmutErayAltay/artemis/releases/download/` önekiyle başlayan,
   boyutu 1 bayt ile 1,5 GB arasında olan ve `sha256:<64 hex>` özeti bulunan ilk varlık kabul
   edilir.
3. **Parça ve bütünlük doğrulaması:** Kurucu geçici dizindeki `artemis-update` altına `.part`
   uzantısıyla yazılır; beklenen bayt sayısını aşarsa, eksik kalırsa, iptal edilirse veya
   SHA-256 özeti uyuşmazsa `.part` silinir ve hata bildirilir. Yalnızca tam eşleşmede hedef
   dosyaya atomik olarak taşınır.
4. **Güvenli çıkış ve başlatma:** İndirme doğrulandıktan sonra kaydedilmemiş ayarlar için
   `leave_settings` sorusu tamamlanır; ardından kurucu `QProcess.startDetached` ile başlatılır
   ve Artemis kapanır.

**Güvenlik**

- Yönlendirmeler yalnız `https://` adreslerinde izlenir; beklenmeyen dosya adları ve kaynak
  dışı bağlantılar reddedilir.
- Kurucu bütünlüğü GitHub API'sinin sağladığı SHA-256 özetiyle doğrulanır.

**Kalan risk**

- Bütünlük doğrulaması GitHub API + TLS bağlantısına ve sürüm sahibinin hesabına duyulan
  güvene dayanır.
- Kurucuda henüz Authenticode kod imzası yoktur (SignPath beklenmektedir).
- İndirilen kurucu dosya sisteminin geçici klasöründeki `artemis-update` dizininde tutulur.
