# Güvenlik mutasyon kontrolleri

`python scripts/check_mutations.py` üç ayrı geçici depo kopyasında regresyon
testlerini önce özgün kaynakla, sonra tek bir düşürülmüş güvenlik koşuluyla
çalıştırır. Her testin özgün hali geçmelidir; mutasyonlu hali pytest assertion
hatasıyla başarısız olmalıdır. Test toplama, import veya altyapı hatası beklenen
başarısızlık sayılmaz.

Kontroller dış dosya okumasından sonra yan etki onayının hâlâ istendiğini,
kullanıcı onayı reddedildiğinde aracın çalışmadığını ve geri dönüşüm adaptörü
işlemi yapmadığında aracın başarı bildirmediğini sınar. Betik yalnız `artemis/`,
`tests/` ve `pyproject.toml` dosyalarını her mutasyon için ayrı bir geçici dizine
kopyalar. Alt süreç geçici çalışma dizininde çalışır ve ortam değişkenlerinden
yalnız işletim için gereken izin listesini alır. Başlangıç ve bitiş kaynak
özetleri karşılaştırılır; asıl çalışma ağacındaki değişiklik başarısızlıktır.

Rapor her mutasyon için başlangıç testi, beklenen assertion başarısızlığı ve
kopya bütünlüğünü gösterir. Kontrol Windows/masaüstü etkisi yaratmaz ve canlı
sağlayıcı çağrısı yapmaz.
