# F2 teslim raporu

## Yapılanlar

Aktif tur iptali, sağlayıcı/araç beklemelerinde süre sınırı ve görev temizliği
eklendi. Temizlenmeyen eski görev varken yeni tur reddedilir; eski turun bağlamı
korunur. Varsayılan masaüstü işlemleri sabit eylem listeli ayrı Python işçisinde
çalışır. İletişim boyutu sınırlıdır ve alt sürece yalnız izinli ortam aktarılır.

Bulut denemesi varsayılan 15 saniyeyle sınırlanır; timeout sonrası kalan tur
süresi yerel yedeğe verilir. İptal, yetkilendirme ve bütçe hatası yedeği başlatmaz.
Bütçe SQLite hataları yeni çağrıyı engeller; belirsiz maliyet rezervasyonda kalır.

Silme öncesinde SQLite geri alma günlüğü hazırlanır. Sonuç `completed`, `failed`
veya `uncertain` olarak kaydedilir. Yeni `list_undo_records` aracı çalışma alanının
kayıtlarını gösterir; dosya içeriği saklanmaz ve hassas metadata temizlenir.

## Doğrulama

Yerel Linux doğrulamasının son satırları birebir:

```text
pytest -q: 300 passed in 4.66s
ruff check .: All checks passed!
mypy: Success: no issues found in 38 source files
```

| Ölçüm | Sonuç |
| --- | --- |
| Sahte kabul senaryosu | 29/29, üç tekrar ile 87 koşu |
| Tehlikeli hata | 0 |
| Onaysız yan etki | 0 |
| Toplam model adımı | 231 |
| Tur süresi medyan / p90 | 0.006022 / 0.014028 saniye |
| Token / maliyet | 0 / 0 USD; senaryolu sahte sağlayıcı |
| Güvenlik mutasyonu | 3/3 yakalandı; kaynak hash'leri değişmedi |

İptal, kuyruktaki sonraki tur, temizlenmeyen görev karantinası, ardışık iki CLI
iptali, işçi timeout/iptali, parçalı ve büyük çıktı, SQLite kilit/bozulma, bütçe
hesaplaşması, silme öncesi günlük ve belirsiz silme sonucu izole testlerle geçer.
Ham yerel çıktılar `reports/f2-*` altında bulunur; kişisel çalışma raporları
gitignore nedeniyle commit'e alınmaz. Kabul ölçümü fixture kurulumu dışındaki
AgentLoop süresidir.

Hata enjeksiyonu düzenekleri [F2_VALIDATION.md](F2_VALIDATION.md) içinde açıklanır.

## Bilinçli sınırlar ve kararlar

- Windows'ta elle doğrulama ve uzak GitHub Actions çalışması yapılmadı.
- OpenRouter ve Anthropic anahtarları yok; canlı model ölçümü yapılmadı.
  Sahte senaryo süreleri model kalitesini veya gerçek gecikmeyi ölçmez.
- Senkron terminal onayı ve kısa yerel dosya işlemleri kesilemeyen bölgelerdir;
  sonraki yan etki başlamadan süre ve iptal tekrar kontrol edilir.
- OS'ye gönderilmiş bir eylemin geri alındığı iddia edilmez. İşçi süreç sınırı
  bir sandbox değildir; başlatılmış kullanıcı uygulaması körlemesine öldürülmez.
- Geri alma günlüğü otomatik restore veya içerik yedeği sağlamaz. Geri yükleme
  yalnız öğe hâlâ OS geri dönüşüm kutusundaysa kullanıcı tarafından yapılabilir.
- Sağlayıcı timeout'u 15 saniye, bütçe veritabanı kilit beklemesi 100 ms seçildi;
  bunlar yapılandırma/koruma varsayılanlarıdır, canlı performans sonucu değildir.
- Sınırlı okumadan sonra işçi borularını kapatmak için CPython asyncio taşıma
  kancası kullanılır; public `Process` API'si stdout kapatma sağlamaz. Farklı
  Python uygulamalarında ve Windows'ta bu temizlik yolu ayrıca doğrulanmalıdır.

Sonraki faz F3 sestir; başlamadan Windows ses ortamı ve ölçüm düzeni kurulmalıdır.
