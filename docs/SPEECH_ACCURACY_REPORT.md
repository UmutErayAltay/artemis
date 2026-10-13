# Genel konuşma hazırlığı — 2026-10-13

Kesin STT çözümlemesi yapılandırılabilir beam 5, kısmi çözümleme beam 1 kullanır.
Türkçe veya otomatik dil seçimi vardır; kelime ipuçları varsayılan boştur.
Bu ayarlar henüz gerçek ses üzerinde daha iyi doğruluk sağladığı ölçülmüş
seçimler değildir. Model değişmedi; ücretli API çağrısı yapılmadı.

15 satırlık kayıt planı günlük konuşma, İngilizce parçalar, uygulama adları,
düzeltme, olumsuz ifade, sessizlik ve gürültü içerir. Değerlendirme grubundaki
konuşmacılar geliştirme grubundan ayrıdır. WAV dosyaları repoda yoktur;
gerçek akustik doğruluk ve RTX 4050 performansı ölçülmedi.

| Kontrol | Sonuç |
| --- | --- |
| Temiz ortam, yalnız dev bağımlılıkları | 337 test geçti |
| Ruff | Geçti |
| Mypy | 42 kaynak dosyası geçti |
| Yerel WAV ölçüm hattı | Geçici WAV + sahte tanıyıcıyla doğrulandı |
| Gerçek WER/CER, genel niyet başarısı | Ölçülmedi |
| Windows mikrofon/gerçek gecikme | Ölçülmedi |

Yerel doğrulama çıktılarının son satırları:

```text
337 passed in 4.95s
All checks passed!
Success: no issues found in 42 source files
```

Ölçüm kodu yol kaçışını, bozuk/kesik WAV'ı ve aşırı uzun kaydı reddeder; ham
ses ve transcript çıktı üretmez. Metindeki uygulama adı ölçümü sınırlı bir
sözcüksel eşleştirmedir; gerçek araç hedefi doğruluğu değildir. Testler ağ,
mikrofon veya gerçek uygulama başlatma kullanmaz.

Önceki CI'da isteğe bağlı numpy importu temiz dev ortamında mypy hatası
üretiyordu; lazy import düzeltildi. POSIX chmod testi Windows için ayrıldı.
Senaryo fixture baytları platformlar arasında sabitlendi ve başarısız
senaryoların tanı bilgileri görünür yapıldı. Windows silme senaryosunda
çöp kutusu fixture'ının çözümlenmemiş çalışma kökü kullanması düzeltildi:
`safe_join` ile aynı kanonik kök kullanılır. Linux sembolik bağlantılı geçici
dizin testi, sözcüksel ve kanonik yol farkını ve düzeltmeyi yeniden üretir.
Sonraki Windows koşusunda bu senaryo geçti. Ayrı iptal testinin 10 ms bütçesi
sağlayıcı başlamadan bitebildiği için test bütçesi 0.5 saniye yapıldı ve
sağlayıcının başlama olayı beklendi; üretim süre sınırları değiştirilmedi.
Bu son test düzeltmesinin hedefli sonucu `1 passed in 0.62s`; Ruff/Mypy geçti.
Windows testleri geçtikten sonra raporun yönlendirilmiş stdout çıktısında Türkçe
karakter kodlaması hatası bulundu. CI ve Windows kurulum rehberi `PYTHONUTF8=1`
kullanır. Bu ayarla yönlendirilmiş sahte ölçüm JSON'u tekrar okundu; 29 senaryoda
başarısızlık 0 oldu. Bu sonuç ses tanıma doğruluğu değildir.

Sıradaki adım [kabul planındaki](SPEECH_ACCURACY_PLAN.md) farklı konuşmacı
kayıtlarıyla aynı ayar/model seçeneklerini karşılaştırmaktır. Genel anlama
başarısı veya donanım gecikmesi için sahte test yüzdesi kullanılmaz.
