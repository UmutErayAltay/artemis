# Masaüstü ilk sürüm raporu — 2026-10-13

PySide6 ile tek süreçte Türkçe sohbet paneli, işlem adımları, hızlı komut
penceresi ve sistem tepsisi eklendi. Tasarım [masaüstü planını](DESKTOP_PLAN.md)
ve [ADR 0011'i](adr/0011-desktop-ui.md) izler. Kullanıcı GUI'yi istediği için
F4 hafıza beklenmedi; kalıcı hafıza veya ses bağlantısı bu teslimata eklenmedi.

## Davranış ve güvenlik

CLI, ses ve masaüstü aynı runtime fabrikası üzerinden CommandRunner,
AgentLoop ve SafetyGate zincirini kurar. Panel/overlay aynı işçiyi paylaşır;
GUI araç çalıştırmaz veya ayrı risk/onay politikası belirlemez.

Onay penceresi doğrulanmış tam argümanları ve kapının taint uyarısını gösterir.
Varsayılan Hayır; Enter, Escape ve pencere kapanışı onay vermez. Eski/çift
karar yeni onaya uygulanamaz. İptal önce bekleyen onayı reddeder. Qt ana
thread'de akıcı kalırken işçi kendi asyncio döngüsünü kullanır; temizlenmeyen
görev varken yeni tur veya loop kapanışı yapılmaz.

İşlem görünümü yalnız sırları temizlenmiş, başarıyla yazılmış gerçek denetim
olaylarını tüketir. Görünüm kuyruğu 256 kayıt, liste 500 satır, olay özeti
yaklaşık 16 KiB ile sınırlıdır. Tam JSONL ve onay argümanları kısaltılmaz.
Kaybolan görünüm olayları belirtilir. `sent` sonucu doğrulanmış başarı gibi
gösterilmez; modelin yanıt vermesi tüm araçların başarılı olduğu anlamına gelmez.

Demo açıkça etiketlenir. Sahte sağlayıcı ve özel bellek araçları kullanır;
gerçek plugin, config, ağ, model veya kullanıcı dosyası erişimi yoktur.
Geçici çalışma alanı/denetim dosyası işçi kapandıktan sonra temizlenir.
README'deki dört PNG gerçek Qt demo pencerelerinden alındı.

## Ölçümler

| Kontrol | Sonuç |
| --- | --- |
| Qt dahil tüm testler, Linux offscreen | 359 geçti |
| Qt olmadan çekirdek | 349 geçti, 1 Qt modülü atlandı |
| Ruff | Geçti |
| Mypy, linux ve win32 hedefleri | Her biri 47 dosyada geçti |
| Sahte kabul senaryoları | 29 senaryo × 3; 87 koşu geçti |
| Tehlikeli hata / onaysız yan etki | 0 / 0 |
| Sahte senaryo medyan / p90 | 0.006001 / 0.030526 saniye |
| Güvenlik mutasyonları | 3/3 yakalandı, kaynaklar değişmedi |
| Canlı model token / maliyet | Ölçülmedi; sahte kip 0 / 0 USD |
| Windows tepsi, odak, fiziksel DPI | Elle doğrulanmadı |

Son yerel kontrol çıktıları birebir:

```text
359 passed in 10.47s
All checks passed!
Success: no issues found in 47 source files
```

Mypy linux ve win32 hedeflerinde aynı son satırı üretti; bu Windows çalışma
zamanı veya masaüstü doğrulamasının yerine geçmez. Ubuntu/Windows çekirdek CI
korundu; ayrıca iki platformda Qt offscreen testleri ve Qt tip kontrolü eklendi.

## Bilinçli sınırlar ve mekanik kararlar

Ses hâlâ `python -m artemis voice` ile ayrı kullanılır; panelde çalışan mikrofon
gösterilmez. Görünen sohbet geçmişi modelin sonraki turuna eklenmez. Ayar
editörü, model seçici, küresel kısayol, otomatik başlangıç ve kurulum paketi yoktur.
F5'in gerçek Windows elle kabulü bekler; sahte/offscreen testle tamamlandı denmez.

QtCore/QtGui/QtWidgets için yalnız `PySide6-Essentials==6.12.0` kullanılır;
kullanılmayan WebEngine/Addons kurulmaz. Çekirdek strict tip kontrolünü korur.
Ubuntu Qt testleri için ayrıca sistemde `libegl1` ve `libopengl0` gerekir;
CI ve README bu kurulumu içerir. Windows çekirdek ve Qt CI kontrolleri geçti.
Qt'nin isteğe bağlı ve dinamik bağları için yalnız GUI adaptöründe `misc` ve
`no-any-return` istisnaları vardır; Qt kurulu ortamda ayrıca tip kontrolü çalışır.
Dar pencerede işlem listesi düğmeyle açılır; uzun satırların tamamı tooltip'te,
onay argümanlarının tamamı kaydırılabilir salt okunur alanda bulunur.

Sıradaki kabul, Windows 11'de 100/150/200% DPI, tepsi mevcut/yok, klavye,
onay sırasında kapatma/iptal ve uzun Türkçe argüman kontrolüdür. Sesin aynı GUI
runtime'ına bağlanması bundan sonra ayrı bir uygulama adımıdır.
