# ADR 0018 — Kurulu uygulamalara erişim ve uyandırma geri bildirimi

**Bağlam:** Kullanıcı uygulama başına .exe seçmek istemiyor; bütün kurulu
uygulamalara erişim, belli hassas işlemlerde ek onay istiyor. Tek başına “Artemis”
eşleşmesi eski akışta sessiz takip penceresi açıyordu; kullanıcı algılanmadığını bildirdi.

**Karar:** Opt-in `allow_all_applications`, Windows Başlat menüsü kataloğunu
runtime'a ekler. Model katalogdaki adı önerir; modelin adı shell komutu veya
serbest .exe yolu olmaz. Bilinen kayıtlar Windows AppsFolder ile açılır; manuel
yollar öncelikli kalır. Başlat menüsünün kurulu kayıtlarını sorgulayan alt süreç
sağlayıcı anahtarlarını almaz. Uygulama erişimi kullanıcı onayıyla yerelde açık.

Temiz turda normal açma/kapatma tekrar onay istemez. Silme, kaldırıcı açma ve
kaydedilmemiş belge/kod/shell işi kaybı olabilecek kapatmalar onay ister. Dış içerik
okunan turdaki yan etkiler SafetyGate'te yine onaylanır. Dosya kökü ve sır/gizlilik
kuralları bu uygulama erişimi ayarıyla değiştirilmez.

Tek başına uyandırmada yerel “Dinliyorum” sesi ve `awaiting_command` durumu
üretilir; LLM çağrısı yoktur. Yanıt boyunca mikrofon duraklar ve eski kareler
atılır. Takip süresi sesli yanıttan sonra başlar. Mikrofon seviyesi ve son duyulan
metin yalnız canlı UI'da gösterilir, ayrı kalıcı ses/transkript kaydı oluşturulmaz.
Yerel profilde kullanılmayan kısmi STT kapatıldı; Artemis hotword ipucu verildi.

**Sınırlar:** Katalog bir kurulu uygulama listesi, sınırsız shell erişimi değildir.
Bazı paketlerin gerçek pencere süreci katalogdan çözülemeyebilir; başlatma kabul
edilse de doğrulanmamış sonuç `sent` kalır. Gerçek insan konuşması için mikrofon
seviyesi ve duyulan metinle kullanıcı kabulü gerekir; sentetik ses bu kabul yerine geçmez.
