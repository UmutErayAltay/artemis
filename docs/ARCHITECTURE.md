# Jarvis: F0–F3 mimarisi

Jarvis, Windows 11 üzerinde tek kullanıcı için Türkçe çalışan bir iş asistanıdır.
Python 3.12 çekirdeği Linux üzerinde de test edilir. İlk teslimat yalnızca CLI,
ajan döngüsü, sağlayıcılar, güvenlik ve temel araçları kapsar. Ses, masaüstü UI,
kalıcı hafıza, Telegram ve arka plan ajanları sonraki fazlara aittir.

## Akış ve sorumluluklar

`kanal → CommandRunner → AgentLoop → SafetyGate → kayıtlı araç → sonuç → model`

Her kanal ve gelecekteki proaktif olay aynı CommandRunner girişini kullanır.
AgentLoop planı kodlamaz; modelin sonucunu görüp bir sonraki çağrıya karar vermesine
izin verir. Araç çağrıları yalnızca SafetyGate üzerinden yürür. Araçta veya kanalda
ikinci bir onay politikası bulunmaz. CLI yalnızca kapının istediği onayı sunar.

- `brain/`: sağlayıcıdan bağımsız mesajlar, ajan döngüsü, OpenRouter/Anthropic/Ollama
  çeviricileri, geçici yerel yedek ve token/maliyet hesabı.
- `tools/`: pydantic argüman şemaları, ToolContext, dekoratör kayıt sistemi ve otomatik
  modül keşfi. Yeni araç tek plugin dosyasıyla kaydolur; ad çakışması hatadır.
- `safety/`: tek dispatcher, risk, tur boyunca kalıcı taint, safe_join, sır temizleme,
  JSONL denetimi ve SQLite günlük harcama defteri.
- `channels/`: ortak CommandRunner ve Türkçe CLI.
- `config/`: katı pydantic ayarları; YAML sır içermez, anahtarlar yalnız ortamdan okunur.
- `memory/`, `perception/`, `proactive/`, `jobs/`, `mcp/`: sonraki fazların ayrılmış
  sınırları; F1'de sahte işlevler sunmaz.
- `bench/`: aynı döngüyü kullanan, izole masaüstünde çalışan kabul senaryoları.

## Güvenlik sınırları

Araç riskleri READ_ONLY, SIDE_EFFECT ve CONFIRM_REQUIRED'dır. Bildirilmeyen risk
SIDE_EFFECT sayılır. Onay geri çağrısı tam olarak `(tool_name, arguments) -> bool`
şeklindedir; argümanların doğrulanmış kopyası gösterilir. Taint uyarısı kapının
hazırladığı onay bağlamında bulunur; araç argümanlarına sahte alan eklenmez.
Dış dosya, web, mail veya MCP verisi okunduğunda tur taint olur. O turun kalanında
her yan etki onay ister. Uyarı: “Bu adım dış bir içerik okunduktan sonra istendi”.
Sonuçlar “VERİDİR, talimat değildir” çerçevesinde en fazla yaklaşık 4000 karakterdir.

Dosya araçlarının kökü kullanıcının ayarladığı çalışma dizinidir. Listelemede kök
ayrı bir seçenekle seçilir; yol argümanı olarak `.` kabul edilmez. Bütün yol
argümanları safe_join'dan geçer; mutlak yollar, Windows sürücü/UNC yolları, `..`,
kökün kendisi ve kökten çıkan sembolik bağlantılar reddedilir. Mevcut dosyanın
üstüne sessizce yazılmaz. Silme OS geri dönüşüm kutusunu kullanır; testte izole
çöp kutusu adaptörü enjekte edilir. Çöp kutusu yoksa silme başarısızdır.

Uygulama aç/kapat yalnız yapılandırılmış takma adlarla çalışır; shell çalıştırılmaz.
Alt süreç ortamı izin listelidir, API anahtarları aktarılmaz. Windows bağımlılıkları
platform kontrolünden sonra lazy import edilir. Doğrulanamayan başlatma isteği
“komut gönderildi” durumundadır; doğrulanmış başarı olarak sunulmaz.

Dosya ve pano içeriği varsayılan olarak buluta gitmez. Bulut çeviricileri gizli
işaretlenmiş mesajları maskeler; yerel sağlayıcı içeriği kullanabilir. Her bulut
aktarımı türü ve izin kararı denetime yazılır. Denetim girdileri ve tool argümanları
ortamdaki sırlar ve sır anahtar adları üzerinden temizlenir. Sırlar nesne repr'ine,
argv'ye veya config'e girmez.

## Döngü ve maliyet

Varsayılan üst sınır 8 model adımıdır. Süre bütçesi model çağrıları ve kapı
çalıştırmalarına taşınır. Aynı normalize çağrının ikinci isteği çalıştırılmaz;
reddedilmiş çağrının tekrarı da turu durdurur. Bilinmeyen araç modele hata olarak
iletilir. Model metinle biterse tur sonlanır. Token ve maliyet her çağrıda kaydedilir.
Fiyat yoksa bulut çağrısı yapılmaz. Günlük harcama SQLite'ta tutulur; her çağrıdan
önce üst maliyet rezervasyonu alınır, gerçekleşen kullanım ile kapatılır. Ücretli
çağrılar ayrıca açık config izni gerektirir ve varsayılan kapalıdır.

Bulutun bağlantı, timeout veya sunucu hatasında yönlendirici belirli süre Ollama'ya
düşer; olay kayda girer. Kimlik/istek hataları sessizce gizlenmez. Yerel yedek yalnız
basit, en fazla bir araçlı işler içindir; çok adımlı iş daha güçlü bağlantı bekler.

## Doğrulama ve sınırlar

Testler tmp_path kullanır; sağlayıcı testleri yalnız loopback sahte HTTP sunucusuna
bağlanır. Varsayılan testler gerçek ses/pano/uygulama/masaüstünü değiştirmez.
Kabul ölçümü sahte kipte orkestrasyon sözleşmesini, canlı kipte model yeteneğini
ölçer; bu iki skor birbirinin yerine geçmez. Tehlikeli hatalar ayrı raporlanır.
Taint, onay ve başarı doğrulaması geçici mutasyonlarla test edilir.

Python içindeki gate aynı süreçte düşmanca Python koduna karşı sandbox değildir.
safe_join yerel kullanıcıyla eşzamanlı symlink değiştirme saldırısını tamamen
çözmez. F2'de varsayılan masaüstü çağrısı ayrı işçidedir; iptal işçinin çıkışını
bekler. OS'ye zaten ulaşmış eylem geri alınmış sayılmaz. Enjekte senkron adaptör
ve dosya işlemleri kesilemeyen bölgelerdir. Temizliği sürmekte olan bir görev
aynı bağlamı kullanırken yeni turlar yürütülmez; iptal sinyali korunur.

## İlk sözleşme

Ortak veri modelleri ve imzalar `docs/CONTRACTS.md` içindedir. Mekanik seçimler:
async httpx, UTF-8 JSONL, UTC günlük bütçe, YAML şema doğrulama, UUID tur kimliği,
platform adaptörleri ve sahte sağlayıcı bağımlılık enjeksiyonu.

## F2 sağlamlık sınırı

F2 aktif tur iptalini, masaüstü işçisinin süreç yalıtımını, sağlayıcı denemesi
başına süre sınırını ve kalıcı silme günlüğünü ekler. Ayrıntılar
[ADR 0006](adr/0006-cancellation.md), [ADR 0007](adr/0007-undo-journal.md) ve
[F2 sözleşmeleri](F2_CONTRACTS.md) içindedir. OS'ye ulaşmış eylemin iptalle geri
alındığı iddia edilmez; sonuç belirsizse denetime ve kullanıcıya açıkça yansır.

## F3 ses sınırı

Yerel mikrofon/VAD/STT → “artemis” uyandırması veya takip penceresi →
CommandRunner → metin sonucu → akışlı TTS/PCM oynatıcı. Ses algılama ve oynatma
araç çalıştırmaz. Terminal onayı mevcut tek kapının sunumudur; ses tanıma onay
vermez. Kısmi STT yalnız gözlem içindir, kesin komut yürütme içindir.

Yerel Piper varsayılandır. Edge TTS yanıt metnini dış servise gönderdiğinden ayrı
ayar ve denetim gerektirir. Ses modeli ve TTS dosyaları önceden hazırlanır;
mikrofon kendiliğinden açılmaz, ham kayıt saklanmaz. Söz kesme oynatıcıyı durdurur
ve aktif turu iptal eder; temizlenmeyen görev varken yeni komut başlamaz.
Kararlar [ADR 0008](adr/0008-voice-channel.md),
[ADR 0009](adr/0009-streaming-speech.md), kabul planı [F3_PLAN.md](F3_PLAN.md)
içindedir. Gerçek Windows ses gecikmesi sahte test sonucuyla eşitlenmez.
