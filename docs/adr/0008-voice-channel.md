# ADR 0008 — Yerel ses algılama ve ortak komut yolu

**Bağlam:** Kullanıcı 32 GB RAM ve RTX 4050 kullanıyor; uyandırma sözcüğü
“artemis”. Türkçe için hazır ve doğrulanmış özel uyandırma modeli bulunmadan
ayrı model seçmek ölçülemeyen bir varsayım olur. Ses onayı tercihi bildirilmedi.

**Karar:** İlk sürüm VAD ile bölünen ses üzerinde yerel faster-whisper kullanır;
“artemis” yalnız sözcük sınırında algılanır. STT modeli/aygıtı/hesap türü ayarlıdır;
bu donanım için başlangıç profili CUDA + float16 olur, CUDA yoksa dürüst hata ve
belgeli CPU profili vardır. Model önceden indirilir; çalışma sırasında gizli
indirme yapılmaz. Kesinleşmemiş STT metni araç çalıştırmaz.

Ses kanalı mevcut CommandRunner ve SafetyGate'i kullanır. Onayda tam tool,
argümanlar ve taint uyarısı terminalde gösterilir; klavyeden açık “evet” dışında
ret verilir. Sesli onay bu sürüme eklenmez. Senkron onay sırasında ses yakalama
durdurulur/atılır; bu aralık sonraki komuta taşınmaz.

**Sonuçlar:** Özel sözcük Türkçe yerel STT ile yapılandırılabilir; ayrı düşük
güçlü uyandırma modelinden daha fazla GPU/CPU tüketebilir. Yanlış uyanma ve
gecikme Windows'ta ölçülmeden eşik/model başarısı iddia edilmez. Terminal onayı
tamamen eller serbest kullanım sağlamaz, fakat STT hatası işlem onaylayamaz.
