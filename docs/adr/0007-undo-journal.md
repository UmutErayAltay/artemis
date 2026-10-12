# ADR 0007 — Silmeden önce kalıcı geri alma kaydı

**Bağlam:** OS geri dönüşüm kutusuna taşıma F1'de vardır, ancak yeniden başlatma
sonrasında hangi dosyanın ne zaman kaldırıldığını gösteren kayıt yoktur.
Send2Trash platformlar arasında güvenilir bir geri yükleme kimliği sağlamaz.

**Karar:** SQLite geri alma günlüğüne silme başlamadan önce hazırlık kaydı yazılır.
Kaynak göreli yol, çalışma kökü, zaman, dosya boyutu ve işlem kimliği tutulur;
dosya içeriği kopyalanmaz. Gerçek taşıma doğrulanınca kayıt tamamlanır; hata veya
belirsizlik ayrı durumdur. Günlük yazılamıyorsa silme başlamaz. Listeleme aracı
aynı SafetyGate'ten geçer ve kayıtları dış/özel veri olarak döndürür.

**Sonuçlar:** Kullanıcı tamamlanan silme kayıtlarından OS Geri Dönüşüm Kutusu ile
geri yükleme yolunu görebilir. Otomatik geri yükleme veya ikinci bir gizli dosya
kopyası üretilmez. Tamamlanmamış kayıt başarı iddiası taşımaz; çöküş sonrası
belirsiz durumu açıkça gösterir. Geri dönüşüm kutusu elle boşaltılmışsa dosyanın
hâlâ kurtarılabilir olduğu iddia edilmez.
