# ADR 0006 — Tur iptali ve masaüstü işçisi

**Bağlam:** F1'de iptal işareti yalnız adımlar arasında okunuyordu; to_thread ile
başlatılmış masaüstü çağrısı süre aşımından sonra çalışmaya devam edebiliyordu.

**Karar:** CommandRunner aktif turun iptal sahibidir; yeni tur temiz bir sinyalle
başlar. AgentLoop sağlayıcı/araç beklemesini iptal sinyali ve kalan süreyle yarıştırır;
iptal edilen görevleri temizler, yeni adım çalıştırmaz. CLI'da işlem sırasındaki
Ctrl+C aktif turu iptal eder ve sohbet kullanılabilir kalır. Senkron onay geri
çağrısının `(tool_name, arguments) -> bool` sözleşmesi korunur; onaydan hemen sonra
süre/iptal tekrar denetlenir.

Varsayılan masaüstü adaptörü izin listeli eylemi ayrı Python işçisinde yürütür.
Argümanlar argv yerine stdin JSON ile taşınır. Ortam yalnız OS/profil/GUI izin
listesidir. Eylem seçimi sabittir; genel shell/exec/arbitrary module arayüzü yoktur.
İptal ve süre aşımı işçiyi sonlandırıp bekler. Enjekte test adaptörleri ağsız kalır.

**Sonuçlar:** Asistanın kendi devam eden masaüstü işçisi temizlenebilir. İşletim
sistemine zaten gönderilmiş pencere/pano/uygulama isteğinin geri alındığı iddia
edilmez. Başlatılan kullanıcı uygulaması iptal sırasında körlemesine öldürülmez.
Yerel kısa dosya işlemleri ve senkron terminal onayı kesilemeyen bölgelerdir;
süre aşımında yeni yan etki başlamaz. Bu süreç sınırı sandbox değildir.
