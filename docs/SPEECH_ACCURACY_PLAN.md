# Genel konuşma doğruluğu kabul planı

Hedef, farklı kullanıcıların gündelik Türkçe konuşmasını ve cümle içindeki
İngilizce ifadeleri anlamaktır. Bir kullanıcının birkaç uygulamasına göre kalıp
komut yazmak bu hedefi karşılamaz. ASR yerel çok dilli modelle metin üretir;
niyet/araç planı sağlayıcıdan bağımsız beyin katmanında değerlendirilir.

## Kayıt seti

Örnek YAML yalnız kayıt planıdır. Gerçek WAV kayıtları sağlanmadan genel doğruluk
ölçülmüş olmaz. Aynı cümleyi tek kişinin tekrarlaması farklı konuşmacı testi değildir.
Kayıtları geliştirme ve değerlendirme gruplarına ayırın; son değerlendirmede
geliştirme grubundaki konuşmacıları ve özel adları tekrar kullanmayın.

- Farklı konuşmacılar, konuşma hızları, aksanlar ve mikrofonlar.
- Türkçe cümlede İngilizce özel adlar ve İngilizce cümle parçaları.
- Uygulama dışı sorular, gündelik kısaltmalar, tereddüt ve kendi sözünü düzeltme.
- “Açma”, “silme”, “onu değil” gibi niyeti değiştiren ifadeler.
- Sessizlik, arka plan konuşması ve gürültü; yanlış uyanma ayrı etiketlenir.

Ham kayıtlar kişisel veri olabilir. Yalnız ölçüm için kullanıcının hazırladığı
yerel dosyalar okunur; repoya konmaz, buluta gönderilmez. Normal ses kanalı
kendiliğinden kayıt biriktirmez.

## Karşılaştırma

Aynı ayrılmış kayıt setinde model, dil ve beam ayarlarını karşılaştırın.
Önce özel kelime listesi boş olsun; kelime desteğinin etkisini ayrı koşul olarak
ölçün. Değerlendirme için bilinen uygulama adları modele otomatik verilmez.
Model boyutu seçimini toplam WER yerine kategori sonuçları, özel ad hataları,
sessizlikte metin üretimi, bellek kullanımı ve medyan/p90 süreyle birlikte yapın.
Bir ayarı daha doğru diye varsayılan yapmak için henüz gerçek ölçüm yoktur;
beam 5 başlangıç denemesidir, kanıtlanmış iyileşme değildir.

## Ayrı tutulacak sonuçlar

ASR benchmark WER/CER, metindeki uygulama adları ve sessizlik hatalarını ölçer.
Bir uygulama adının metinde geçmesi onu açma isteği değildir. Bu yüzden
beklenmeyen ad üretimi gerçek bir tehlikeli araç çalıştırması diye sayılmaz.
Niyet ve hedef doğruluğu ayrıca izole masaüstü ve gerçek beyin modeliyle ölçülür;
onaysız yan etki ve yanlış hedefe işlem ayrı raporlanır. Mevcut belirsizlikte
soru sorma talimatı, hatalı tanınmış geçerli bir hedefi kesin engelleme garantisi
vermez. Çok turlu konuşma bağlamı bu değişikliğin kapsamına girmez.

Gerçek ses doğruluğu, Windows gecikmesi ve genel niyet başarısı henüz ölçülmedi.
Sahte testlerden kullanıcılar geneline yönelik doğruluk yüzdesi çıkarılmaz.
