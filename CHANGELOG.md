# Değişiklik günlüğü

Kullanıcıya dönük özet. Kurucular henüz imzasızdır (SignPath başvurusu beklemede);
her sürümün `SHA256SUMS.txt` dosyasıyla doğrulayın. Ayrıntılı kayıtlar
[GitHub Releases](https://github.com/UmutErayAltay/artemis/releases) sayfasındadır.

## 0.2.6 (2026-11-26)

- Düzeltme: 0.2.5 kurucusunda Artemis açılmıyordu (pakete NumPy alt modülü girmemişti). Paket artık yayından önce CI'da açılış denemesinden geçiyor; eksik modül olursa pencere sessizce kapanmak yerine hangi modülün yüklenemediğini söylüyor.
- Güvenlik: uygulama açıldı tetikleyicisi ve öneri zincirlerinde `{app}` (ön plandaki program adı) içeren öneri dış içerik sayılıyor; o turda yan etkiler onay istiyor.
- Ses: Ayarlar açılırken model taraması ve doğrulama arka planda; panel donmuyor.
- Ses: mikrofon sinyali 2 sn kesilirse "Mikrofon sinyali kesildi" hatası; beklenmeyen hatada ses sessizce kapanmıyor, hata gösteriliyor; model indirme "İndiriliyor…"da takılı kalmıyor.
- Yayın: imza yalnız SignPath üzerinden (eski sertifika yolu kaldırıldı); release notunda imzasız uyarısı ve `Get-FileHash` doğrulaması; SECURITY.md eklendi.

## 0.2.5 (2026-11-26)

- Ses: ekran bağlamı ve Whisper indirme hataları daha dürüst bildiriliyor; kurulu model bilgisi önbelleğe alınıyor.
- Ses: model indirmelerine boyut sınırı (Piper) ve GPU sorgusu arka plana alındı, ayarlar paneli takılmıyor.
- Güvenlik: uygulama başlatma yer tutucuları ve sesli ekran bağlamında engelli süreçler sıkılaştırıldı.
- Kod ajanı iş deposu kilidi ve kör hata yakalamalar daraltıldı; dahili dosyalar bölündü.
- Dokümanlar ve README güncellendi.

## 0.2.4 (2026-11-26)

- Kurucu GitHub Actions ile otomatik derleniyor; `SHA256SUMS.txt` yayımlanıyor.
- Hesap takvim/posta, makro kaydedici, rutin ve tetikleyici hatalarında düzeltmeler (#37–#48): dış içerik varken onay, sınırlı yeniden deneme, pencere filtreleri.
- Pano ve ekran okuma: metin dışı pano içeriği korunuyor; model indirme ağ hataları kullanıcıya düzgün bildiriliyor.
- SignPath ile imzalama hazırlığı; imza başvurusu onaylanana dek kurucu imzasız.

## 0.2.3 (2026-11-20)

- Windows kurulum paketi (PyInstaller + Inno Setup) ve uygulama içi güncelleme denetimi.
- CLI İngilizce arayüzde `yes` yanıtını da onay sayar.
- Masaüstü kabul turları ve sürüm/paket kaydı uyuşmazlığı denetimi.

## 0.2.2 ve öncesi

- 0.2.2 (2026-11-19), 0.2.1 (2026-11-18), 0.2.0 (2026-11-17), 0.1.0 (2026-11-16): ilk masaüstü, ses, hafıza ve araç sürümleri; ayrıntı için etiketlere bakın.
