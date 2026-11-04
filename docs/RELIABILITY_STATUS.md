# Artemis kabul ve güvenilirlik durumu

Bu dosya güncel durumun kanonik kaynağıdır. Her satır üç durumu ayırır: **Kodlandı**,
**Otomatik test** (sahte arka uçla; gerçek cihaz değildir) ve **Gerçek cihaz** (Windows,
tarih + commit + kanıt yoluyla). Çalıştırma yolları: [TESTING.md](TESTING.md).

## Güvenilirlik turu — 2026-11-03 (dal `fix/guvenilirlik-turu`, base `aa70baf`)

Ürünleştirme öncesi inceleme; yeni özellik yok. Bulgular güncel kodda yeniden doğrulandı.

| Bulgu (sınıf) | Kodlandı | Otomatik test | Gerçek cihaz |
|---|---|---|---|
| Çökme sonrası kod ajanı depo kilidi hiç kalkmıyordu (doğrulanmış hata) | `13b994d` | geçti | geçti 2026-11-04, betik¹: servis süreci zorla öldürüldü → ajan süreci de kapandı (Job Object); yeniden açılışta `interrupted` + kilitli, yeni iş reddedildi; `release` sonrası yeni iş `done`. Panel düğmesi elle denenmedi |
| Bozuk `code_agent.sqlite3` tüm asistanı açtırmıyordu (doğrulanmış hata) | `13b994d` | geçti | bekliyor |
| İş başlarken audit hatası işi sonsuza dek `queued` bırakıyordu (doğrulanmış hata) | `13b994d` | geçti | — |
| Sonuç audit'i yazılamazsa gerçekleşmiş işlem "tamamlanamadı" görünüyordu (doğrulanmış hata) | `9975083` | geçti | — |
| İptalde thread'deki PowerShell/OS işi sürerken yeni tur başlıyordu (doğrulanmış hata) | `9975083` | geçti | geçti 2026-11-04. Umut (turn `367cfe`): İptal düğmesi model çağrısını 0,56 sn'de kesti. Betik¹ (gerçek model + gerçek PowerShell `window_list`, iptal `tool_dispatch` anında): tur `3b5915cb` `cancelled` + "temizleme süresi içinde sonlanmadı" (+0,52 sn); hemen gelen tur `671c629b` `cleanup_pending` ile modelsiz reddedildi; PowerShell +0,55 sn'de bitti (`tool_interrupted`, `completion: unknown`); sonraki tur `a8d72836` normal `completed`. Panel "temizleniyor" görünümü elle bakılmadı. (İlk üç Esc denemesi geçersizdi.) |
| Telegram kapanışı süren turu iptal etmiyordu; GUI 10 sn donuyordu (doğrulanmış hata) | `52d793b` | geçti | geçti 2026-11-04 (Umut): onay beklerken Çık → kapandı, onay reddedildi; audit `edbc01` `close_application denied`, `stop=cancelled` |
| Masaüstü ayar değişikliği gömülü Telegram'a ulaşmıyordu (doğrulanmış hata) | `52d793b` | geçti | geçti 2026-11-04 (Umut): tüm uygulama erişimi kapalıyken Telegram'dan "Operayı aç" reddedildi (`ee6a1f`, araç çağrılmadı) |
| Telegram yanıtı "gönderildi ama doğrulanmadı"yı gizliyordu (doğrulanmış hata) | `52d793b` | geçti | geçti 2026-11-04 (Umut): "Ekranı kilitle" → `power sent` (`71dacf`), yanıtta durum satırı |
| Bilgisayar kullanımı eski koordinat/odakla girdi gönderebiliyordu (doğrulanmış hata) | `d05cc5d` | geçti | kısmi 2026-11-04 (Umut, turn `61bbdb`): Not Defteri ikinci ekrana sürüklenince yazma adımı engellendi, girdi gönderilmedi. Oturum 3. engelde bitti (önceki ikisi Artemis'in kendi penceresine tıklama). Bulgu: model "bu pencereye girdi gönderilemez" notunu alıyordu → `ATLANDI` notu ile düzeltildi (bu commit). Ölçek/monitör çıkarma ve farklı DPI bekliyor |
| Kod ajanı "Doğrulandı" = çıkış kodu/öz beyan (doğrulanmış hata) | `01df2f5`, `7732ba5` | geçti | geçti 2026-11-04, betik¹ (generic ajan, gerçek git): kontrol geçti → `accepted`, çıkış 3 → `failed`, kontrolsüz → `unverified`. Claude/Codex ile ve panelde bekliyor |
| Audit dosyası sınırsız büyüyordu (bakım) | `31f3887` | geçti | geçti 2026-11-04: gerçek 21,25 MB `audit.jsonl` ilk yazımda `audit-20261103T213402271373-5224.jsonl` oldu, boyutu aynı, silinmedi |
| Geçmiş silme WAL/boş sayfada metin bırakıyordu; kapsam belirsizdi (doğrulanmış hata) | `df090a0` | geçti | geçti 2026-11-04, gerçek `memory.sqlite3`'ün **kopyasında**¹: 23 tur silindi, 3 not kaldı, 130 karakterlik silinen cümle dosya baytlarında yok. Panel düğmesi elle denenmedi |
| Kapanış temizliği süresiz bekleyebiliyordu (doğrulanmış hata) | `c274f5a` | geçti | bekliyor |
| Karma olumsuzluk senaryosu yoktu (doğrulama eksikliği) | `fc42c95` | sahte bench 53/53 | canlı model bekliyor |
| Konuşma sonu → eylem bitişi gecikmesi ölçülmüyordu (doğrulama eksikliği) | `831dfad` | geçti | canlı ölçüm bekliyor |
| auto_mode_ok kapsamı kayıt geneli testsizdi (doğrulama eksikliği) | `9975083` | geçti | — |
| app.py / voice/core.py sorumluluk ayrımı, salt taşıma (bakım) | `b945876` | geçti | — |

¹ Windows 11, kod `b945876`, `.venv` Python 3.12.9. Betikler geçici klasörde gerçek süreç, git
ve SQLite kullandı; GUI'den geçmedi, kullanıcının depo ve kayıtlarına yazmadı.

Zaten giderilmiş bulunanlar (değişiklik yapılmadı): geç model yanıtı iptalden sonra araç
çalıştırmıyor; iptal/süre aşımında bekleyen onaylar geçersiz; iptal edilen iş "done"
görünmüyor; kod ajanı süreç ağacı Windows Job Object ile kapanıyor; hatırlatma talepleri
çökme sonrası `uncertain` oluyor, iki kez çalışmıyor; SafetyGate dışında araç yürütme yolu yok.

Bilerek değiştirilmeyenler / açık sınırlar:
- `open_url` yol/alt alan adı sızıntısı (ADR 0036): Umut kararıyla aynı kaldı.
- `read_only_tools` listesindeki MCP aracı, outbound olsa da geçmiş leke onayını atlar
  (kullanıcının açık seçimi; `test_gate.py` bunu kilitler).
- Claude kod ajanı Bash'i OS sandbox değildir; `Bash(git push:*)` reddi aşılabilir önek
  kuralıdır. Bağımsız kontroller ajanın değiştirdiği depoda çalışır.
- `computer_use` "ok" sonucu hâlâ ikinci model yargısıdır; UIA/OS durum doğrulaması yok.
- MCP ve `claude_cli` süreçleri `taskkill /T` ile kapanır; ebeveyn önce ölürse torun yetim
  kalabilir (Job Object yok).
- Silme günlüğünde `prepared` satırları açılışta uzlaştırılmıyor (dürüstçe "çözülmemiş" görünür).
- Hafıza varsayılan açık, SQLite şifresiz, geçmiş saklama varsayılan sınırsız (0 gün);
  değiştirilmedi, README'de yazılı. Şema sürümü (`user_version`) ve yedek/geri yükleme yok.

Bu turun yerel doğrulaması (Windows 11, Python 3.12.9, gerçek cihaza dokunulmadı):

| | Commit | pytest | ruff | mypy | Sahte bench |
|---|---|---|---|---|---|
| Başlangıç | `aa70baf` | 2421 geçti / 22 atlandı / 4 seçilmedi | temiz | temiz | tehlikeli/onaysız/yanlış hedef 0 |
| Tur sonu | `b945876` | 2466 geçti / 22 atlandı / 4 seçilmedi | temiz | temiz (135 dosya) | 53/53; tehlikeli/onaysız/yanlış hedef 0 |

Her düzeltmenin regresyon testi, düzeltme geçici kaldırılınca kırıldı (mutasyon kontrolü).
Atlamaların dökümü [TESTING.md](TESTING.md#bu-makinedeki-gerçek-atlamalar-2026-11-03-windows-11-b945876):
8 symlink güvenlik testi bu Windows'ta yetki yüzünden koşmadı (doğrulama eksikliği).

## Önceki tur (2026-10-27)

Önceki Windows
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
| Streaming | Sağlayıcılarda kod ve test mevcut; akışsız kipte first token boş | claude_cli `stream-json` biçimi 2026-10-31 canlı doğrulandı ([A3](WINDOWS_CHECKLIST.md)); diğer profiller bekliyor |

Karar ve sınırlar: [ADR 0046](adr/0046-reliability-controls.md).

## Önceden yapılmış Windows F6/F7 kabulü

Senaryo tanımları ve ham gözlemler: [F6/F7 kabul dosyası](F6_F7_WINDOWS_ACCEPTANCE.md).
Tarih: eski kayıtlarda 2026-10-15–19; tek tek commit her kayıtta yok, uydurulmadı.

| # | Son doğrulanmış durum | Açık kalan |
|---|---|---|
| 1 | Kısmi geçti | Toast kullanıcı ayarında kapalı |
| 2 | Yapılmadı | Yeniden açılışta kaçan hatırlatma |
| 3 | Geçti (2026-11-04, `b945876`, Windows 11, `claude-haiku-5-5`) | İki parça, kanıt `.jarvis/audit.jsonl`. (a) Zamanlayıcı yolu, turn `d6465dc3`: 00:04 «kabul» brifingi gözetimsiz koştu, yalnız `reminder_list` çağrıldı, teslim `panel: ok`; model yazma aracını kendisi çağırmadı. (b) Kapı reddi, turn `ccd2c303`: `_run_briefing` ile aynı kurulumu yapan elle başlatılmış betik (önek + `unattended=True` + `_refuse`; hafıza/MCP/kod ajanı kapalı). Model `create_file` çağırdı; `approval` `approved: false, unattended: true`, sonuç `denied`, dosya oluşmadı. Zamanlayıcı saatiyle tetiklenen turda kapı reddi ayrıca görülmedi. Brifing config'ten kaldırıldı. |
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

## 2026-10-27 yerel doğrulaması (geçmiş kayıt; güncel sürümün sonucu değildir)

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
