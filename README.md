# Jarvis

Windows 11 için Türkçe kişisel masaüstü asistanı. CLI ve isteğe bağlı masaüstü
paneli, sağlayıcıdan bağımsız ajan döngüsünü, tek güvenlik kapısını ve araçları
paylaşır. F3 ses kanalı kodu hazırdır; gerçek Windows ses ölçümü bekler.
Vault hafızası ile Telegram ve bildirimler isteğe bağlıdır, varsayılan kapalıdır; arka plan
ajanları bu sürüme dahil değildir.

CLI ve masaüstünde konuşma bağlamı `CommandRunner`'da tutulur (ADR 0012); kalıcı hafıza
yalnız aşağıdaki isteğe bağlı vault araçlarıyla sağlanır.

Mimari [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), kararların gerekçeleri
[docs/adr/](docs/adr/), geliştirme kuralları [CLAUDE.md](CLAUDE.md) içindedir.
Son sağlamlık doğrulaması [F2 raporunda](docs/F2_REPORT.md) bulunur.
Ses katmanı doğrulaması ve bekleyen Windows kabulü [F3 raporunda](docs/F3_REPORT.md)
açıklanır.

## Masaüstü arayüzleri

Tek uygulamada sohbet paneli, işlem adımları, hızlı komut penceresi ve sistem
tepsisi bulunur. Onay penceresi araç adını, tam doğrulanmış argümanları ve dış
içerik uyarısını gösterir; varsayılan seçim **Hayır**'dır.

Görüntüler Linux'ta gerçek Qt pencerelerinden **demo modunda** alınmıştır.
Demo, sahte sağlayıcı ve geçici bellek verisi kullanır; ağ çağrısı yapmaz,
kullanıcının dosyalarına veya uygulamalarına erişmez.

### Sohbet ve işlem adımları

![Jarvis sohbet paneli ve gerçek demo işlem adımları](docs/assets/desktop/panel.png)

[Karşılama ekranını da görüntüleyin.](docs/assets/desktop/welcome.png)

### İşlem onayı

![Araç adı, argümanlar ve dış içerik uyarısıyla işlem onayı](docs/assets/desktop/approval.png)

### Hızlı komut penceresi

![Ana panelle aynı çalışma katmanını kullanan hızlı komut penceresi](docs/assets/desktop/overlay.png)

Python 3.12 ortamında arayüzü kurup ücretsiz, ağsız demoyu açın:

```powershell
python -m pip install -e ".[desktop]"
python -m jarvis desktop --demo
```

Ubuntu'da PySide6 için gereken Qt grafik kitaplıklarını önce kurun:

```bash
sudo apt-get update
sudo apt-get install -y libegl1 libopengl0
```

Demoda `örnek akış` yazıp Gönder'e basınca onay sürecini deneyebilirsiniz.
Gerçek kullanım için aşağıdaki kurulum ve sağlayıcı ayarlarını tamamlayın:

```powershell
python -m jarvis desktop --config config/config.yaml
```

Tepsi varsa pencereyi kapatmak paneli gizler; tepsi menüsünden **Çıkış** tüm
uygulamayı kapatır. Tepsi yoksa pencere kapanışı temiz çıkış başlatır. Ses panelin
içindedir: başlıktaki **Ses** düğmesi mikrofonu açar, `--ses` ile panel ses açık
başlar (aşağıdaki ses modu bölümü).
**Ayarlar** sekmesinde model, mikrofon ve izinli uygulama listesi bulunur. Uygulama
eklemek için `.exe` dosyasını seçip **Uygula**'ya basın. Model başlığında gerçek
model adı gösterilir. Mikrofonun farklı sürücü kopyaları tek seçenek altında
toplanır; sanal girişler ayrıca açılabilir. Uygula yeni sohbet başlatır.
Not Defteri'ne metin yazma, çalışma klasöründe metin dosyası oluşturup Not Defteri
ile açma yoluyla yapılır. Açma başarısı görünür pencereyle doğrulanır.
Uygulama seçmek istemiyorsanız **Bütün kurulu uygulamalara erişim** seçeneği
Windows Başlat menüsündeki kayıtları otomatik kullanır. Normal işlemler tekrar
onay istemez; silme, kaldırma ve belge/kod kaybı yaratabilecek kapatmalar onaylıdır.
Tek başına **Artemis** dediğinizde yerel **Dinliyorum** yanıtı ve komut bekleme
durumu gelir. Ayarlar'daki mikrofon seviyesi ve **Son duyulan** göstergesi, sesin
girdiğini ve nasıl tanındığını gösterir; ayrı ses kaydı tutulmaz.
Windows tepsi, odak ve DPI kontrolleri henüz elle doğrulanmadı.
Testler ve bilinçli sınırlar [masaüstü raporunda](docs/F5_REPORT.md) bulunur.

## Artemis ses modu

Uyandırma sözcüğü **artemis**. Ses kurulumu isteğe bağlıdır; varsayılan testler
mikrofonu veya hoparlörü açmaz. Yerel Türkçe STT ve Piper modelleri önceden
hazırlanır. RTX 4050 için CUDA/float16 başlangıç profili ve CPU alternatifi
[Windows ses rehberinde](docs/VOICE_SETUP.md) açıklanır.

```powershell
python -m pip install -e ".[dev,desktop,voice]"
python -m jarvis desktop --ses --config config/config.yaml
# kısayol: python -m jarvis voice --config config/config.yaml (aynı komut)
```

Önce model dosyalarını, beyin modelini ve ses aygıtını rehbere göre hazırlayın.
Ham mikrofon kaydı tutulmaz. Edge TTS yalnız ayrı gizlilik ayarıyla açılır;
varsayılan yerel sestir. Ses masaüstü uygulamasının içinde çalışır; mikrofonu
yalnız **Ses** düğmesi ya da `--ses` açar. Onaylar tam tool/argümanlarla ekrandaki
pencereden alınır ([ADR 0014](docs/adr/0014-voice-in-desktop.md)); sesli “evet”
onay sayılmaz. İlk Windows denemesini kulaklıkla yapın.
Sahte gecikme sonucu, medyan < 1.5 sn hedefinin gerçekleştiğini göstermez.

Tanıma hedefi farklı kullanıcıların günlük Türkçe–İngilizce karışık konuşmasıdır;
birkaç uygulama adına bağlı komut kalıpları kullanılmaz. Özel kelimeler isteğe
bağlı ipucudur. Gerçek ses kaydıyla kelime/karakter ve uygulama adı hatalarını
ölçme adımları [ses rehberinde](docs/VOICE_SETUP.md) bulunur. Konuşmayı doğru
yazıya çevirmek, niyeti ve doğru araç hedefini anlamakla aynı ölçüm değildir.
Genel konuşma doğruluğu gerçek, farklı konuşmacı kayıtlarıyla henüz doğrulanmadı.
Son değişiklik ve doğrulamalar [konuşma raporunda](docs/SPEECH_ACCURACY_REPORT.md).

## Kurulum

Python 3.12 kullanın. Windows PowerShell:

```powershell
$env:PYTHONUTF8 = '1'
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item config/config.example.yaml config/config.yaml
New-Item -ItemType Directory -Force desktop
```

`PYTHONUTF8=1`, Windows'ın bölgesel kod sayfası yerine UTF-8 kullanır. Sahte
kabul raporları Türkçe JSON yazdığından bu ayar, yönlendirilmiş çıktının Windows'ta
UnicodeEncodeError ile yarıda kesilmesini önler.

Linux:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
cp config/config.example.yaml config/config.yaml
mkdir -p desktop
```

YAML içindeki göreli yollar config dosyasının dizinine göre çözülür. Örnek
`workspace: ../desktop` değerini dosya araçlarının erişebileceği köke ayarlayın.
Gerçek Windows Masaüstü klasörü kendiliğinden seçilmez.

## Sağlayıcı ve sırlar

Örnek config bilerek model içermez: model kimlikleri ve fiyatlar değişir.
[Resmi kaynaklardan](docs/PROVIDERS.md) güncel kimlik, araç desteği, bağlam sınırı ve
girdi/çıktı fiyatını doğrulayın. `models` içine bir takma ad altında şu alanları
ekleyip `primary_model` değerini bu takma ada ayarlayın:

| Alan | Değer |
| --- | --- |
| `provider` | `openrouter`, `anthropic` veya `ollama` |
| `model` | Sağlayıcının güncel model kimliği |
| `input_per_million` | Milyon girdi token'ı başına USD; ondalık YAML metni |
| `output_per_million` | Milyon çıktı token'ı başına USD; ondalık YAML metni |
| `context_window` | Modelin doğrulanmış bağlam sınırı |

Ücretsizliği doğrulanan modelin iki fiyat alanı `'0'` olabilir; bilinmeyen fiyatı
sıfır yazarak geçmeyin. `allow_paid: false` varsayılanını koruyun. Ücretli model
kullanımı ayrıca açık kullanıcı izni ve `allow_paid: true` gerektirir.

| Ortam değişkeni | Ne zaman gerekli? |
| --- | --- |
| `OPENROUTER_API_KEY` | OpenRouter çağrısı |
| `ANTHROPIC_API_KEY` | Anthropic çağrısı |

Anahtarları yalnız çalıştırma ortamından sağlayın. YAML, komut satırı argümanları,
test fixture'ları veya depoya anahtar yazmayın. Proje `.env` dosyasını otomatik
yüklemez. Sağlayıcı anahtarları log ve nesne gösterimlerinden temizlenir; uygulama
alt süreçlerine ortamın yalnız izin verilen kısmı aktarılır.

Yerel yedek için Ollama'yı ayrıca kurup araç çağırmayı destekleyen bir modeli
indirin. `models` içinde `provider: ollama` kaydı oluşturup `fallback_model` alanına
bu kaydın takma adını yazın. Varsayılan adres `http://127.0.0.1:11434`'tür. Bulut
bağlantısı veya sunucusu başarısız olduğunda yönlendirici `fallback_seconds` boyunca
yerele geçer ve olayı kaydeder. Yerel yedek basit, en fazla bir araçlı işler içindir.
Kimlik doğrulama ve geçersiz istek hataları bağlantı kesintisi diye gizlenmez.

## İlk çalıştırma

```bash
python -m jarvis chat --config config/config.yaml
```

Çıkmak için `/çık`, `/cik` veya `/exit` yazın. `/yeni` konuşma geçmişini sıfırlar
(panelde "Yeni sohbet"); modele son `context_turns` tur verilir ([ADR 0012](docs/adr/0012-conversation-context.md)).

İşlem sürerken Ctrl+C aktif turu iptal eder; sohbet sonraki komut için açık kalır.
Onay sorusundaki Ctrl+C hem onayı reddeder hem turu iptal eder. Önceki görevin
temizliği tamamlanmadıysa yeni komut yürütülmez; bu durum açıkça bildirilir.

Örnek komutlar: “Masaüstündeki dosyaları listele”, “Saat kaç?” ve
“not.txt dosyasının ilk satırını söyle”. Ajan sonuçları görüp sonraki araca karar
verir. Eksik hedef içeren isteklerde açıklama istemesi beklenir.

Onay sorusu araç adını ve doğrulanmış argümanları gösterir; boş cevap hayırdır.
Silme ve uygulama kapatma onay ister. Dosya, web veya pano gibi dış içerik
okunduktan sonra o turdaki her yan etki de onay ister. Bu durumda onayda
“Bu adım dış bir içerik okunduktan sonra istendi” uyarısı gösterilir.

## Gizlilik ve bütçe

`allow_cloud_file_content` ve `allow_cloud_clipboard` varsayılan olarak kapalıdır.
Kapalıyken bu araçların içeriği bulut sağlayıcısına maskelenerek gönderilir; bu
durumda bulut modelinden gerçek dosya/pano özeti beklenmez. Yerel sağlayıcı bu
içeriği işleyebilir. Ekran görüntüsü bu sürümün kapsamında değildir.

Komutunuzun metni, araç şemaları ve izin verilen sonuçlar seçilen sağlayıcıya
gider. Buluta gönderim kararları denetlenir. `audit_path` UTF-8 JSONL denetim
kaydını, `state_dir` günlük bütçe durumunu belirler. Denetim logu sırlar
temizlense de kişisel komutlar ve yerel içerik barındırabilir; yerelde tutun.
Bu yollar ve yerel config `.gitignore` kapsamındadır.

Her model çağrısında token ve USD maliyeti kaydedilir. `daily_budget_usd` UTC gününe
göre SQLite'ta tutulur. Çağrıdan önce bağlam ve çıktı sınırları üzerinden üst
maliyet ayrılır; gerçekleşen kullanım ile hesap kapatılır. Bu temkinli sınır,
kalan bütçe gerçek kısa çağrıya yetse bile isteği reddedebilir. Fiyatlar config'den
gelir; sağlayıcı tarifeleri değiştiğinde güncellemek gerekir.

`provider_timeout_seconds` her sağlayıcı denemesini sınırlar (varsayılan 15 sn).
Bulut yanıt vermezse toplam tur süresinin kalan kısmında yerel yedek denenir.
Kullanıcı iptali, kimlik doğrulama hatası ve bütçe reddi yedeği tetiklemez.
Bozuk/kilitli bütçe kaydı çağrıyı engeller; belirsiz tüketim rezervasyonu korunur.

## Araçlar ve platform sınırları

| Alan | Araçlar |
| --- | --- |
| Dosyalar | `list_files`, `read_file`, `create_file`, `move_file`, `rename_file`, `delete_file`, `create_directory` |
| Uygulamalar | `open_application`, `close_application` |
| Web | `web_search`, `open_url` |
| Pano | `read_clipboard`, `write_clipboard` |
| Sistem | `system_info`, `current_time` |
| Geri alma kaydı | `list_undo_records` |

Dosya yolları çalışma kökü içinde kalmalıdır. Mutlak yollar, `..`, `.` ve kökün
dışına çıkan sembolik bağlantılar reddedilir. Kökü listelemek için `list_files`
argümansız çağrılır. Silme geri dönüşüm kutusunu kullanır; geri dönüşüm desteği
başarısızsa kalıcı silmeye geçilmez. Var olan dosyanın üstüne sessizce yazılmaz.

Uygulamalar `applications` içindeki izinli takma adlarla açılır/kapatılır; serbest
shell komutu aracı yoktur. Kapatma, izinli `.exe` adına uyan bütün süreçlere normal
kapatma isteği gönderir; kaydedilmemiş işler kaybolabilir. Windows'a bağlı uygulama
ve pano işlevleri Linux'ta desteklenmediğini bildirir. Linux üzerinde çekirdek,
dosyalar ve sahte platform adaptörleri test edilir.

Günlük kullanımda seçili uygulamalar için tekrar kapatma onayını kaldırabilirsiniz:
`close_without_confirmation: [brave]`. İsim `applications` içinde tanımlanmış
olmalıdır. Varsayılan liste boştur; dış içerik okunan turda onay yine istenir
([ADR 0017](docs/adr/0017-application-close-confirmation.md)).

Uygulama/tarayıcı başlatma isteğinin gönderilmesi, ekranda
açıldığının kanıtı sayılmaz. Windows'ta elle doğrulama ayrı bir teslimat kontrolüdür.
Varsayılan masaüstü işlemleri ayrı Python işçisinde çalışır. İptal ve zaman aşımı
işçiyi sonlandırıp çıkışını bekler; stdin/stdout iletişimi boyutla sınırlandırılır.
OS'ye daha önce gönderilmiş bir isteğin geri alındığı iddia edilmez. Enjekte edilen
senkron adaptörler ve kısa dosya işlemleri kesilemeyen bölgelerdir.

Silmeden önce `state_dir/undo.sqlite3` günlüğüne kayıt yazılır; kayıt yazılamıyorsa
silme başlamaz. `list_undo_records` hazırlık, tamamlandı, başarısız ve belirsiz
durumları gösterir. Geri yükleme OS Geri Dönüşüm Kutusu üzerinden elle yapılır.
Kayıt, dosyanın kutuda hâlâ bulunduğunun garantisi değildir; içerik yedeği tutulmaz.

Web araması DuckDuckGo'nun HTML sonuçlarından başlık ve URL okur. Bu resmi bir
arama API'si değildir; sayfa biçimi değişirse veya erişim engellenirse araç hata
verir. Bu sürüm sonuç sayfalarının içeriğini ayrıca açıp indiren bir araç içermez.
Arama sorgusu dış servise gönderildiğinden araç yan etkili sayılır: temiz turda
onaysız çalışır, dış içerik okunduktan sonraki turda kullanıcı onayı ister.

## Hafıza (Obsidian vault)

Kapalıdır. Açmak için config'e `memory.vault_path` ekleyin (örnek:
[config.example.yaml](config/config.example.yaml)). Üç araç gelir: `vault_search` (arama),
`vault_read_note` (okuma) ve `vault_append` (ekleme). Okuma araçları dosya içeriği
sayılır: o tur dış içerikle lekelenir, sonraki yan etkiler onay ister ve içerik buluta
yalnız `allow_cloud_file_content: true` ise gider. `vault_append` her seferinde onay
ister, mevcut notu yeniden yazmaz (yalnız sonuna ekler), klasör oluşturmaz. `.git`,
`.obsidian`, `daily/`, `receipts/` ve companion klasörü aranmaz, okunmaz, yazılmaz.
Not içeriği turlara kendiliğinden eklenmez; model gerektiğinde aracı çağırır.

Arama varsayılan olarak yerleşik sözcük aramasıdır. Anlamsal arama için
`python -m pip install -e ".[memory]"` kurun ve `memory.embedding_model` verin; ilk
aramada model indirilir, indeks vault dışında `state_dir/embedding` altında tutulur.
Paket veya model yoksa sözcük aramasına dönülür ve sonuçta bu yazılır. Karar:
[ADR 0015](docs/adr/0015-vault-memory.md).

## Telegram ve bildirimler

Varsayılan kapalıdır; config'te `telegram.chat_id` verilmedikçe hiçbir şey açılmaz.
Gerekçe [ADR 0016](docs/adr/0016-telegram-and-notifications.md) içindedir.

Kurulum:

1. Telegram'da **@BotFather**'a `/newbot` yazın ve size verilen token'ı alın.
2. Token'ı kullanıcı ortam değişkenine yazın, sonra terminali kapatıp yeniden açın (değer
   yalnız yeni açılan terminallerde görünür). Token'ı komutun içine yazmayın: komut satırı
   PowerShell geçmiş dosyasına kaydedilir. `Read-Host` ile sorulan değer kaydedilmez:

   ```powershell
   $t = Read-Host "Telegram bot token"
   [Environment]::SetEnvironmentVariable("JARVIS_TELEGRAM_BOT_TOKEN", $t, "User")
   Remove-Variable t
   ```

   Token'ı config dosyasına, komut satırına veya belgeye yazmayın. Ortam değişkeni adı
   `telegram.token_env` ile değiştirilebilir, ama `_TOKEN` ile bitmek zorundadır.
3. Bota kendi hesabınızdan bir mesaj yazın ve chat id'nizi öğrenin. En kolayı
   **@userinfobot**'tur. `getUpdates` çıktısındaki `message.chat.id` de kullanılabilir; ancak
   o adres token içerir, çıktıyı paylaşmayın ve tarayıcı geçmişinde bırakmayın.
4. `config/config.yaml` dosyasına ekleyin (diğer alanların varsayılanları örnek dosyadadır):

   ```yaml
   telegram:
     chat_id: 123456789
   ```

5. Kanalı başlatın:

   ```powershell
   python -m jarvis telegram --config config/config.yaml
   ```

   Açılınca bota "Jarvis Telegram kanalı açık" mesajı gider. Kanal kapalıyken birikmiş
   mesajlar açılışta çalıştırılmaz. Durdurmak için Ctrl+C.

Davranış:

- Yalnız sizin özel sohbetinizden gelen düz metin mesajlar çalıştırılır; başkalarının
  mesajları yanıtlanmaz. İletilmiş, metin dışı veya `max_message_age_seconds` (varsayılan 120
  sn) değerinden eski mesajlar çalıştırılmaz; sohbetinizde kısa bir uyarı çıkar.
- Yan etkili bir işlem onay ister. Telegram'da araç adı ve tüm argümanlar görünür; **Onayla**
  veya **Reddet** düğmesine basın. Cevap verilmezse süre dolunca reddedilir ve mesaj kalan
  süreyi yazar. Onay beklerken gelen yeni komutlar çalıştırılmaz.
- Turda ya da konuşma geçmişinde dosya veya pano içeriği varsa onay Telegram'a gönderilmez;
  işlemi masaüstünden onaylayın ya da `/yeni` ile yeni sohbet başlatın. Bu turların yanıtı da
  Telegram'a gelmez.
- `/yeni` konuşma geçmişini sıfırlar; `/yardım` kısa komut özetini gösterir.
- Telegram geçmişi masaüstü ve CLI geçmişinden ayrıdır.

Windows bildirimleri (`notify_desktop`):

- Toast, Windows PowerShell 5.1 (`powershell.exe`) ile gösterilir; PowerShell 7 ile çalışmaz.
- Ayarlar > Sistem > Bildirimler altında bildirimler açık olmalı ve "Windows PowerShell"e
  bildirim izni verilmelidir. Kapalıysa araç bunu söyler ve başarılı saymaz.
- Windows dışında araç "yalnız Windows'ta çalışır" hatası döner.

Bu kanal Linux'ta test edildi; gerçek bot ve Windows toast'ı henüz denenmedi
([yol haritası](docs/ROADMAP.md), Faz 4).

## Doğrulama ve ölçüm

```bash
pytest -q
ruff check .
mypy
python -m bench.run --sahte
```

Testler ağ servisi kullanmaz; sağlayıcı protokolleri yalnız loopback üzerindeki
sahte HTTP sunucusuyla denenir. Dosya testleri geçici masaüstünde gerçek dosya
işlemleri yapar. `disruptive` işaretli testler varsayılan olarak atlanır; bunları
yalnız elle hazırlanmış bir makinede `pytest -m disruptive` ile seçin.

Kabul senaryoları [bench/senaryolar.yaml](bench/senaryolar.yaml) içinde; senaryo
başına bitiş durumu, onaylar, tehlikeli hatalar, adım, süre, token ve maliyet ölçülür.
Sahte kip güvenlik ve orkestrasyon sözleşmesini ölçer. Sahte cevaplarla alınan skor
gerçek modelin Türkçe anlama veya planlama başarısına kanıt değildir.

Canlı ölçüm, ortam anahtarı sağlandıktan sonra yalnız güncel katalogda ücretsiz
olduğu doğrulanan OpenRouter modeliyle elle çalıştırılır:

```bash
python -m bench.run --canli --model MODEL_KIMLIGI
```

`--tekrar 3` her senaryoyu üç kez ölçer; medyan/p90 için tek örnekten daha anlamlıdır.
`--rapor reports/sonuc.json` JSON raporunu dosyaya yazar. `--senaryolar DOSYA` özel
senaryo seti seçer. Canlı kipte `--config config/config.yaml --model TAKMA_AD`
yazılabilir; takma adın OpenRouter kimliği yine güncel katalogla doğrulanır.

Canlı senaryolar da geçici masaüstü ve sahte platform servisleri kullanır; gerçek
model senaryolardaki sentetik içeriği görür. Aynı senaryo setini en az iki modelle
çalıştırın; yalnız toplam başarıya göre seçim yapmayın, tehlikeli hata sayısını
ayrı değerlendirin. API kotası ve ücretsiz model erişilebilirliği değişebilir.

GitHub Actions Ubuntu ve Windows üzerinde test → ruff → mypy ve sahte kabul
koşucusunu çalıştıracak biçimde tanımlıdır. Yerel Linux sonucu, Windows CI sonucu
yerine geçmez. Bu oturumda push yapılmadığından uzak CI çalıştırıldığı iddia edilmez.

## Geliştirme

Yeni araç tek bir `jarvis/tools/plugins/` dosyasına argüman modeli ve
`@register_tool` sınıfı ekleyerek keşfedilir. Şema ve sağlayıcı biçimleri otomatik
üretilir. Onay mantığını araca eklemeyin; örnek ve kurallar
[CLAUDE.md](CLAUDE.md) içindedir. Yeni davranış için izole test ve kabul senaryosu
ekleyin.
