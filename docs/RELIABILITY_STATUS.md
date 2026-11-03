# Artemis kabul ve güvenilirlik durumu

Güncelleme: 2026-10-27. Bu tablo güncel durumun kanonik kaynağıdır. Önceki Windows
sonuçları base `737a661` içindeki kabul notlarından aktarılmıştır; bu değişikliğin
Windows'ta tekrar doğrulandığı anlamına gelmez. Eski notlar gözlem kanıtı olarak korunur.

Kod commitleri: `1de710a` (onay/deadline), `c3d15a8` (ses), `8158e59` (hafıza/bildirim/panel).

## Güvenilirlik düzeltmeleri

| Senaryo | Kod / yerel kanıt | Windows durumu |
|---|---|---|
| Güvenilen host + destructive MCP | Risk/site çapraz regresyonları | Gerçek MCP ile yeniden deneme bekliyor |
| Gecikmiş capture/model/target/onay sonrası girdi | Sahte saat ve backend; OS girdisi yok | Gerçek yavaş model + Esc bekliyor |
| STT beklerken ses yakalama | Yavaş STT, iki segment sınırı, onayda eski ses iptali | Gerçek mikrofon/GPU bekliyor |
| Ses kaybı ve kuyruk ölçümleri | capture_health + STT kuyruk/çözüm metrikleri | Profilli canlı rapor bekliyor |
| Geçmiş listesi/silme/saklama | SQLite ve tipli onay testleri; 0 gün varsayılan | Panel/CLI kabulü bekliyor |
| failed/uncertain listeleme ve tekrar | Eski kayıt ve gizlilik korunur; tek atomik tekrar | Bildirim teslim denemesi bekliyor |
| Kanal/Runtime kapanış sahipliği | Tekrarlı shutdown, kurulum hatası, mevcut bridge testleri | Aç/kapat ve süreç kontrolü bekliyor |
| Streaming | Sağlayıcılarda kod ve test mevcut; akışsız kipte first token boş | claude_cli canlı biçim doğrulaması bekliyor |

Karar ve sınırlar: [ADR 0046](adr/0046-reliability-controls.md).

## Önceden yapılmış Windows F6/F7 kabulü

Senaryo tanımları ve ham gözlemler: [F6/F7 kabul dosyası](F6_F7_WINDOWS_ACCEPTANCE.md).
Tarih: eski kayıtlarda 2026-10-15–19; tek tek commit her kayıtta yok, uydurulmadı.

| # | Son doğrulanmış durum | Açık kalan |
|---|---|---|
| 1 | Kısmi geçti | Toast kullanıcı ayarında kapalı |
| 2 | Yapılmadı | Yeniden açılışta kaçan hatırlatma |
| 3 | Kısmi geçti | Gerçek gözetimsiz yazma reddi tetiklenmedi |
| 4–6 | Geçti | MCP okuma/onay/süreç temizliği; bu sürümde tekrar bekliyor |
| 7 | Odak düzeltmesi canlı denendi | Türkçe OCR harf hataları sürüyor |
| 8 | Yapılmadı | Farklı ekran ölçeği |
| 9 | Uygulanamadı | OCR dil paketi yokluğu senaryosu |
| 10 | Geçti | Açılış kaydı; kimlik bilgisi okuma sonradan düzeltildi |
| 11 | Yapılmadı | Windows oturum kapatma/açma |
| 12 | Kısmi geçti | Tepside ikinci örnek |
| 13 | Yapılmadı | Tepsi menüsünden açılışı kapatma |
| 14 | Düzeltme canlı denendi | Pencere listesinden masaüstü elendi |
| 15 | Düzeltme tek ekranda canlı denendi | Çoklu monitör |
| 16 | Düzeltme ses düzeyinde canlı denendi | Tam sessize al/aç matrisi yeniden doğrulanmalı |
| 17 | Geçti | Parlaklık geri okundu |
| 18 | Bekliyor | Kilit kabulü |
| 19 | İlk deneme kaldı; ortam düzeltildi | 60 sn kapatma uyarısı ve iptal tekrar denenecek |
| 20 | Bekliyor | Uyku kabulü |
| 21 | Geçti | Browser MCP keşfi |
| 22 | Önceki beklenti testi kaldı | Yeni site onay politikasıyla tekrar denenecek |
| 23 | Kısmi geçti | file:// sunucu reddi doğrudan tetiklenmedi |
| 24 | Geçti | Chromium süreç temizliği |

## Güncel cihaz kabulü

[Windows kontrol listesi](WINDOWS_CHECKLIST.md) uygulama adımlarını içerir. Sonuç eklendiğinde
bu tabloya tarih, test edilen commit, cihaz/ortam, geçti/kısmi/kaldı ve kanıt yolu yazılır.
Gerçek cihaz kabulü tamamlanmadan güvenilir günlük ürün veya Windows kabulü tamamlandı denmez.

## Bu sürümün yerel doğrulaması

2026-10-27, Linux/Python 3.12, kod başı `8158e59`:

- Tam paket: **1904 geçti, 7 atlandı, 4 disruptive test seçilmedi**.
- `ruff check .`, `mypy` ve masaüstü GUI için ayrı tip kontrolü geçti.
- Sahte kabul: **52/52**, tehlikeli hata/onaysız yan etki/yanlış hedef **0**.
  Altı senaryoda sonuç bilinçli `sent`; doğrulanmış OS başarısı sayılmadı.
- Sahte ses bench: 3 örnek, geçersiz zamanlama ve başarısız iterasyon 0; canlı hedef kanıtı değildir.
- Geçici kopyada zorunlu risk kontrolü, executor deadline kontrolü ve eski ses nesli kontrolü
  ayrı ayrı kaldırılınca ilgili regresyonlar başarısız oldu; gerçek kaynaklar değiştirilmedi.
- Yönetilen konteynerin PID 1'i ölen torunları toplamıyor. MCP testlerindeki `kill(pid, 0)`
  zombi kaydını canlı saydığı için tam paket init benzeri subreaper test başlatıcısında
  çalıştırıldı; süreç testleri gevşetilmedi. Gerçek fare/klavye/ses cihazına dokunulmadı.
- Masaüstü offscreen kontrolleri geçti; Windows kabulü yerine geçmez. Ücretli/uzak model çağrılmadı.
