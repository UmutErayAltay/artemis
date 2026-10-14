# Faz 5 doğrulama raporu

2026-10-14. Bu çalışma kümesinde Faz 5 kodlandı ve otomatik doğrulamadan geçti;
Windows 11 ve gerçek Claude Code kabulü henüz yapılmadı.

## Sonuçlar

| Kontrol | Sonuç |
| --- | --- |
| Tüm pytest paketi, Linux/offscreen | 807 geçti, 1 test seçilmedi; 27.90 sn |
| F5, platform süreç ve CLI hedefli testler | 41 geçti; 4.13 sn |
| Ruff | `All checks passed!` |
| Mypy, Linux | 71 kaynak dosyası geçti |
| Mypy, win32 hedefi | 71 kaynak dosyası geçti; Windows çalışma zamanı testi değildir |
| Ubuntu/Windows CI matrix'i | Dört CI işi bu yerel doğrulamada çalıştırılmadı |
| F5 güvenlik mutasyonu | Onay riski SIDE_EFFECT'e indirilince hedef test kırıldı |
| Tüm güvenlik mutasyonları | 4/4 yakalandı; geçici kopyalar dışında kaynak özeti değişmedi |
| Mevcut genel sahte kabul bench'i | 39/39 geçti; tehlikeli hata 0, onaysız yan etki 0 |
| Sahte bench kullanım/maliyet | 0 giriş/çıkış tokenı, $0 |
| Gerçek Claude, ücretli çağrı, gerçek kullanıcı deposu | Çalıştırılmadı |

Genel `bench.run --sahte` mevcut SafetyGate ve dosya senaryolarını ölçer; Faz 5
işlerini senaryo listesine almaz. Faz 5 taslak, onay, soru/cevap, iptal, zaman aşımı,
şema doğrulama ve süreç sahipliği sahte CLI süreçleriyle pytest hedeflerinde sınandı.
Bir sahte LLM diyaloğu taslak hazırlar, başlatma reddini ve sonraki onayı işler,
CLI sorusunu alır ve aynı CLI session UUID'si ile yanıtı sürdürür. Sahte CLI,
gerçek abonelik erişimini veya Claude model davranışını kanıtlamaz.

Sahte bench'in makinece okunabilir çıktısı yok sayılan
`reports/f5-workflow-bench.json` dosyasındadır. Projects paneli ekran görüntüsü
sentetik taslak ve sahte durum verisiyle üretildi; gerçek iş veya CLI çağrısı
göstermez: [ekran görüntüsü](assets/desktop/projects.png).

## Bilinçli sınırlamalar

Yerel kod ajanı OS sandbox değildir. Claude Code Bash ve alt ajan çağrıları
Jarvis SafetyGate tarafından tek tek denetlenmez. Profil ayarları model seçimine
yön verir; gerçek model dağılımı ya da harcama üst sınırı değildir. Windows Job
Object ve süreç kapanışı yalnız tip/sahte adaptör kontrollerinden geçti; gerçek
Windows kabulü bekliyor. Claude Code oturum açma, gerçek kullanıcı aboneliği ve
uçtan uca gerçek proje işi denenmedi. Yeniden başlatmada açık işler otomatik
çalışmaz; kesinti olarak kalır. Temizliği doğrulanamayan iş başarı sayılmaz ve
depo sahiplik kilidi korunur.
