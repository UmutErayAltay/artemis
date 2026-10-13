# CLIProxyAPI yerel denemesi — 2026-10-13

Umut EasyCLIProxyAPI'yi kendisi kurdu. Jarvis `a69f27d` kaynakları GitHub'dan
indirildi; Python 3.12.9 proje ortamında dev ve desktop paketleri kuruldu.
Yerel OpenAI uyumlu adres `http://127.0.0.1:8317/v1`; `/models` 33 kayıt döndürdü.
Bir modelin listelenmesi çalışabildiği anlamına gelmiyor.

`claude-haiku-4-5-20251001` canlı çağrısı HTTP 429 `rate_limit_error` verdi.
`gpt-6.1-sol` HTTP 200 döndürdü. Araçsız yanıttaki `tool_calls: null`, eski
ayrıştırıcıda hataya yol açıyordu; yalnız null boş liste olarak kabul edildi.
Diğer bozuk türleri reddeden 10 regresyon örneği eklendi.

Yerel, gitignore kapsamındaki `config/config.yaml` Codex'i birincil seçer;
`provider: openrouter` mevcut OpenAI uyumlu çeviriciyi kullanır. İstekler yerel
CPA adresine gider. Anahtar yalnız `OPENROUTER_API_KEY` süreç ortamından okunur.
Ücretler '0', kullanıcının abonelik bağlantısında ek API ücreti muhasebesidir;
abonelik kotasını ölçmez. Bağlam 32768 ile temkinli sınırlandırılmıştır;
bu sayı sağlayıcının azami bağlam kapasitesini doğrulayan bir ölçüm değildir.

## Doğrulama

Gerçek Jarvis CommandRunner/ProviderRouter/SafetyGate zinciri ve gerçek CPA
üzerinden, geçici sentetik klasörde `reports/cpa_smoke.py` çalıştırıldı:

| Deneme | Sonuç | Süre |
| --- | --- | --- |
| Sohbet | geçti | 3.31 sn |
| current_time | araç başarılı | 8.80 sn |
| list_files | araç başarılı | 30.66 sn |
| Önceki listeyi hatırlama | araçsız doğru dosya adları | 5.08 sn |
| delete_file onayını reddetme | denied; dosya ve içeriği korundu | 7.19 sn |

Tek örnekli sonuçlar performans garantisi değildir. Ham yerel sonuç:
`reports/cpa-smoke.json` (gitignore kapsamında).

630 test geçti, 12 atlandı, 1 disruptive test seçilmedi. Ruff temiz;
mypy 58 kaynak dosyasında temiz. Sahte kabul: 39/39, tehlikeli hata 0,
onaysız yan etki 0. Sahte kabul canlı model başarısı olarak sayılmaz.

Masaüstü süreci başlatıldı, başlangıç hata günlüğü boş. Görsel, mikrofon,
Piper/STT ve Claude bağlantısının Windows kabulü yapılmadı.

Yeniden başlatma: `powershell -ExecutionPolicy Bypass -File .\start-jarvis.ps1`.
Başlatıcı anahtarı gizli sorar; diske veya kullanıcı ortamına kaydetmez.

## Brave, ses ve Ayarlar sekmesi — aynı oturumdaki devam

Umut günlük Brave kapatma komutunda tekrar onay istemediğini belirtti.
Brave yerel izin listesine eklendi; `close_without_confirmation: [brave]` temiz
turda tekrar onayı kaldırır. Dış içerik okunan turda onay korunur; mutasyon
denemesi bu kontrol kaldırılınca testin kırıldığını gösterdi (geçici kopyada).
Gerçek CPA modeli “brave i kapat” komutunu doğru araca yönlendirdi; bu denemede
masaüstü adaptörü enjekte edildi, gerçek Brave kapatılmadı.

Kullanıcı gerçek denemede pencere kapandığı halde hata gördüğünü bildirdi.
Kapatma artık süreçlerin çıkışını sınırlı tekrarlarla bekler; süreçler kalırsa
Win32 ile görünür pencereleri sorgular. Pencere kapanışı doğrulanırsa bunu
başarılı bildirir, arka plan süreçlerinin tamamen sonlandığını iddia etmez.
Süreç/pencere sorgusu başarısızsa başarı verilmez. Gerçek Brave üzerinde
yalnız görünür pencere sorgusu doğrulandı; yeniden kapatma testi kullanıcıda.

Ses bağımlılıkları kuruldu; mevcut yerel Whisper-small ve Türkçe Piper
modelleri config'e bağlandı (Whisper CPU/int8). Model yüklemesi, gerçek
mikrofon 640 bayt PCM karesi ve gerçek çıkış aygıtına ilk PCM yazımı doğrulandı.
Sentetik Piper “Artemis saat kaç?” sesini Whisper doğru tanıdı. Bu, kullanıcının
gerçek konuşmasını veya duyulabilirliği doğrulamaz. Başlatıcı artık `--ses` kullanır.

**Ayarlar:** API'den 33 model listesi ve ses sürücülerinden 20 giriş seçeneği
alındı (bunlar 20 ayrı fiziksel mikrofon değildir). Kullanılan mikrofonun adı,
sürücüsü ve aygıt numarası gösterilir. Model/mikrofon seçimi atomik olarak
yerel config'e kaydedilir; Uygula işçiyi güvenli kapatıp yeniden kurar, yeni sohbet
başlatır ve önce açık olan sesi seçilen mikrofonla yeniden açar.
İşlem sırasında Uygula kapalıdır. Demo ayar değiştirmez veya ses aygıtı sorgulamaz.

Son doğrulama: 641 test geçti, 11 atlandı, 1 seçilmedi; ruff/mypy temiz;
39/39 sahte kabul, tehlikeli hata ve onaysız yan etki 0.
Model/mikrofon seçiminin çalışma katmanına geçişi offscreen Qt testinde doğrulandı.
Canlı Ayarlar listesi raporu `reports/settings-live.json`; sentetik ses
`reports/voice-synthetic.json`; enjekte edilmiş Brave denemesi
`reports/brave-confirmation.json`. Yerel değişiklikler commit/push edilmedi.

## Günlük kullanım düzeltmeleri — 2026-10-13

Umut gerçek denemede Brave açıldığı halde `sent`, Not Defteri/Hesap Makinesi
için izin hatası, model başlığında `cpa-selected` ve mikrofon sürücü kopyaları
bildirdi. Güncel yerel config bu uygulamalara ve Dosya Gezgini/Paint'e izin verir.
Model istemine izinli takma adlar verilir. Ayarlar uygulama listesini gösterir;
kullanıcının seçtiği `.exe` eklenebilir veya listeden çıkarılabilir. Uygula bu
listeyi diğer tercihlerle atomik kaydeder; serbest shell komutu eklenmedi.

Açma artık görünür pencereyi bekler. Paketli Hesap Makinesi'nin üst penceresi
ApplicationFrameHost'a ait olduğundan alt pencerenin süreç kimliği de eşlenir;
Explorer masaüstü/görev çubuğu başarı olarak sayılmaz. Doğrulanamayan PID başarı
sayılmaz. Brave, Not Defteri ve Hesap Makinesi gerçek SafetyGate/OS zincirinde
`ok` ve `window_verified:true` döndürdü: `reports/app-open-live.json`.

Not Defteri için isteğe bağlı `file_path` eklendi. Dosya çalışma kökünde mevcut
olmalı; kaçış reddedilir. Kullanıcının seçtiği **claude-haiku-5-5** gerçek CPA
modeli geçici sentetik klasörde “brave açıldı” metnini doğru kaydedip Not Defteri
ile açtı; araç başarı döndürdü (`reports/notepad-live.json`). Geçici denemede
onaylar yalnız bu sentetik iş için kabul edildi; genel onay politikası değişmedi.

Başlık artık **SEÇİLİ MODEL** altında gerçek kimliği gösterir. Kullanıcının
modeli ve mikrofon #2 tercihi korundu. Normal mikrofon listesi: sistem varsayılanı,
Razer BlackShark V2 HS 2.4 ve dahili AMD; sanal/eski girişler isteğe bağlıdır.
Seçili Razer'dan gerçek 640 bayt PCM karesi alındı. Ayarlar canlı liste raporu:
`reports/usability-settings.json`. Tek fiziksel mikrofonun sürücü kopyaları
birleştirilir; bu evrensel aygıt GUID keşfi değildir.

Son doğrulama: **649 test**, ruff/mypy temiz; **39/39** sahte kabul,
tehlikeli hata ve onaysız yan etki 0. Panel sesli yeniden başlatıldı.
Kullanıcının farklı konuşmalarıyla genel STT doğruluğu/latansı ölçülmedi.

## Tüm kurulu uygulamalar ve uyandırma — 2026-10-13

Umut açıkça tek tek .exe seçmek istemediğini, bütün uygulamalara erişim ve
belli hassas işlemlerde ek onay istediğini belirtti. Yerel
`allow_all_applications: true` açıldı. Windows Başlat kataloğu 255 kayıt
döndürdü; mevcut manuel takma adlarla runtime 260 ad içeriyor. Bunlar serbest
shell komutuna dönüşmez; sabit AppsFolder başlatma yolu kullanılır ve sağlayıcı
sırları katalog alt sürecine aktarılmaz. Manuel olarak eklenmemiş Notepad++
gerçek SafetyGate/Windows zincirinde açıldı ve pencere doğrulandı:
`reports/all-apps-live.json`. Normal açma/kapatma temiz turda onaysız;
silme, kaldırıcı başlatma ve belge/kod/shell işi kaybı olabilecek kapatmalar
onaylı. Dış içerik sonrası yan etki onayı korunur (ADR 0018).

Umut “Artemis” dediğinde tepki görmediğini bildirdi. Kodda tek başına uyandırma
sessiz takip penceresi açıyordu; tanınma için sesli/görsel karşılık eklendi.
`Dinliyorum` yalnız yerel Piper ile üretilir, LLM çağrısı yapılmaz. Takip süresi
bu yanıttan sonra başlar. Yerel profilde kısmi STT kapatıldı, Artemis hotword
ipucu ve 12 sn takip penceresi verildi. Mikrofon seviyesi ve son duyulan metin
UI'ya aktarılır; metin yeni bir kalıcı tanıma loguna yazılmaz.

Gerçek Whisper/Piper/ses çıkışıyla sentetik “Artemis” girdisi tanındı,
yerel yanıt üretildi ve “saat kaç” için takip açıldı; örnek soğuk süre 4.26 sn
(bir örnek, insan konuşması ölçümü değildir): `reports/wake-live.json`.
İnsanın gerçek denemesi için kullanıcıdan mikrofon seviyesi/son duyulan geri
bildirimi istendi; henüz cevap gelmedi. Mikrofon donanım sessizliği veya insan
telaffuzunun düzeldiği iddia edilmiyor.

Son doğrulama: **656 test**, ruff/mypy temiz; **39/39** sahte kabul,
tehlikeli hata/onaysız yan etki 0. Güncel panel sesli yeniden başlatıldı.

## Karışık dil STT ve yanlış söz kesme — 2026-10-13

Umut gerçek mikrofonda Dinliyorum karşılığını ve ilk Brave kapat komutunun
anlaşılmasını doğruladı. Sonraki tarayıcı aç/Brave aç sözlerinin video kapanış
cümleleri olarak yazıldığını bildirdi; Türkçe-İngilizce karışık konuşma önceliği
istedi. Bu geri bildirim, önceki genel insan kabulünün tamamlandığı anlamına gelmez.

Yerel RTX 4050 üzerinde mevcut faster-whisper large-v3-turbo, CUDA float16 ve
auto dil profili etkinleştirildi. Google TTS seslendirmedir; sorun STT olduğundan
Piper seslendirme korundu. GPU DLL'leri mevcut yerel dizinlerden yüklenir,
global ortam değiştirilmez; bulut STT çağrısı veya mikrofon kaydı aktarımı yapılmadı.
Razer MME mikrofon şu an aygıt #1; indeks yeniden sıralanabildiğinden ad doğrulandı.
Haiku 5.5 ve tüm uygulamalara erişim ayarı korundu.

Silero VAD, düşük güven/no-speech segmentlerini eleme ve tam video kapanış
cümlesi süzgeci eklendi. Uzun gerçek dikte korunur. VAD etkinliği artık tek başına
çalışan modeli iptal etmez; yalnız kabul edilmiş kesin komut/uyandırma iptal eder.
Sessizlik ve uyandırmasız arka plan sözünün aktif turu kesmemesi test edildi.

Yerel Piper Türkçe + Windows Zira İngilizce sentetik testi
(reports/mixed-stt-live.json): Artemis → Artemis; Türkçe tarayıcı aç komutu
korundu; İngilizce Open Visual Studio Code korundu. Karma sonuç:
“Artemis, Open Visual Studio Code ve indirilenlar klasörünü listeli.” İngilizce
uygulama adı korundu, Türkçe bölümde iki küçük yazım hatası kaldı. Sessizlik boş.
İlk soğuk çözüm 3.065 sn; ısınmış Türkçe/İngilizce/karma çözüm 0.655/0.643/1.011 sn.
Bu sentetik örnekler gerçek mikrofon doğruluğu veya toplam yanıt gecikmesi garantisi değildir.

662 test geçti; 11 skip, 1 deselected. Ruff ve mypy (60 dosya) temiz; sahte
kabul raporu reports/voice-turbo-fake.json. Panel yeni ses profiliyle yeniden açıldı,
başlatma logunda ses/model hatası görülmedi. Gerçek kullanıcı karma dil kabulü
bekleniyor. Commit/push yapılmadı. Karar: ADR 0019.

## Emel kadın sesi ve oynatma sırasında söz kesme — 2026-10-13

Umut STT'nin daha iyi anladığını bildirdi; özel adlarda “Og reifi kappat” gibi
hatalar kalıyor, model bağlamdan Brave kapatma niyetini yine anlıyor. Sekme
kapatma yalnız denemeydi ve bu aşamada kapsam dışı. Kullanıcı doğal kadın sesi
ve asistan konuşurken araya girerek durdurabilmeyi istedi.

Emel sentetik örneği sunuldu; yanıt metninin Microsoft'a gönderileceği,
mikrofonun yerelde kalacağı açıklandı. Umut “Emel kadın sesini etkinleştir”
seçeneğini açıkça seçti. Yerel config edge/tr-TR-EmelNeural, cloud text opt-in
ve mevcut yerel ffmpeg yolu ile güncellendi. edge-tts 7.2.8 sentetik istekte
403 verdi; 7.3.0'a yükseltilince MP3 ve gerçek adaptörden PCM üretildi.
reports/emel-live.json: ilk PCM 1118 ms, 4096 byte; iptal temizliği ve ffmpeg
çıkışı doğrulandı. reports/emel-preview.mp3 yalnız kurgusal örnek konuşmadır.
Servis hatasında eski erkek sese sessiz geri dönüşü önlemek için yalnız yerel
profilde tts_local_fallback=false; yazılı yanıt mevcut kalır.

Seslendirme ayrı görev oldu. Oynatma sırasında 180 ms sürekli VAD konuşması
ses çıkışını durdurur; kullanıcı cümlesinin tamamlanması sesin kesilmesi için
beklenmez. STT/araç komutu yine kesin tanıma bekler. VAD tek başına çalışan
model/araç turunu iptal etmez. Ses aygıtı, TTS/ffmpeg temizliği beklenmeden abort
edilir. Söz kesme sonrası takip komutu Artemis'i yeniden söylemeden alınabilir.
Kısa tık, uzun konuşma, STT öncesi yalnız oynatma iptali ve önce aygıt abort
sırası test edildi. VAD konuşmacı/yankı ayırmaz; gerçek kulaklık kabulü açık.

665 test, 11 skip, 1 deselected; ruff, mypy ve diff kontrolü temiz.
39/39 sahte kabul, sıfır tehlikeli hata/onaysız yan etki.
Panel yeni ayarla açıldı; başlangıçta ses hatası yok, bilinen QFont uyarısı var.
Gerçek Emel/söz kesme kullanıcı kabulü bekleniyor. Commit/push yok. ADR 0020.

## Brave Türkçe takma ad — 2026-10-13

Umut Brave adının STT'de zor anlaşıldığını bildirdi ve takma ad istedi.
Yerel uygulama listesinde “tarayıcı” aynı Brave executable yoluna bağlandı;
onaysız normal kapatma listesine de aynı ad eklendi. Resolver'ın Brave ve
tarayıcı için aynı yolu, risk değerlendirmesinin SIDE_EFFECT verdiği doğrulandı.
Model kullanılabilir ad listesinde yeni takma adı görür. Yeni kod veya OS'de
Brave açma/kapatma testi gerekmedi; kullanıcı tarayıcısı bu doğrulamada kapatılmadı.
Panel config değişikliğini almak için yeniden açıldı. Emel/GPU STT korunuyor.
Gerçek “tarayıcıyı aç/kapat” ses kabulü henüz yapılmadı.

## Rusça yanlış dil tanıması — 2026-10-13

Umut “tarayıcı aç” yerine “Игорь Негода” tanındığını bildirdi ve kalıcı düzeltme
istedi. Önceki tüm-diller auto ayarı kısa Türkçe komutta yanlış dil seçebiliyordu.
Razer MME #1 adla doğrulandı; mikrofon değişmedi. Yerel profil artık tr_en:
Türkçe varsayılan, yalnız üst aday en ve olasılığı >=0.7 ve Türkçenin iki katıysa
İngilizce. Rusça çözümlenemez; bu modda Kiril çıktı ayrıca reddedilir.

Yalnız dil kısıtlaması yeterli olmadı: Emel ile üretilmiş aynı kısa ses örneklerinde
“tarayıcı” önce Sarayıcı/Tereyici yazılıyordu. Aynı PCM üzerinde kısa yerel
Türkçe/İngilizce STT bağlamı eklendiğinde doğru tanındı. Yerel stt_initial_prompt
örnek kelimeler/komutlar içerir, araç yürütmez; task=transcribe korunur.
Hotwords yalnız Artemis/tarayıcı. Daha geniş hotword listesi sentetik uyandırmada
yan etki verdiği için kullanılmadı. Ses/noise uydurmasını önlemek için önceki
VAD/güven/video metni süzgeçleri korunur.

reports/turkish-emel-stt.json aynı kayıtlı kurgusal örnekleri doğrular:
Artemis → Artemis.; tarayıcı aç → Tarayıcı aç.; tarayıcıyı kapat → Tarayıcıyı kapat.;
Artemis, Visual Studio Code ve YouTube aç → aynısı. Isınmış kısa örnekler
0.608/0.621/0.630 sn; ilk yükleme 2.89 sn. Sessizlik boş.
reports/turkish-priority-negative.json: tam İngilizce Open Visual Studio Code
korundu; düşük seviyeli rastgele gürültü boş. Piper karma sentetik konuşmasında
Türkçe ek/kelime hataları kalabiliyor. Sentetik başarı gerçek mikrofon kabulü değildir.

Windows ses logundaki iptal/pipe hatası da giderildi: wait_closed ortak Future'ı
shield edilir, süreç çıkışından sonra communicate ile pipe transport kapanışı
beklenir. Gerçek Edge ilk PCM sonrası iptal yeniden denendi; callback/destructor
hatası üretmedi. 671 tam test geçti (11 skip/1 deselected); son pipe temizliği
sonrası ilgili 38 test, ruff/mypy ve diff kontrolleri temiz. 39/39 sahte kabul
sıfır tehlikeli hata/onaysız yan etki. Panel yeni profille açıldı.
Gerçek kısa Türkçe/karma mikrofon kabulü açık; Emel, Haiku, tarayıcı aliası ve
uygulama erişimi korunuyor. Commit/push yok. ADR 0021.

## Tekrar ve kendi sesinden komut döngüsü — 2026-10-13

Umut tarayıcı aç sözünün çok kez tekrarlandığını ve kendi kendine tarayıcıyı
kapat denildiğini bildirdi. Sesli panel önce durduruldu. Audit'in son 250 olayı
28 tur girdisi içeriyordu; 4 girdide bütün metin tekrar kalıbı, en uzunu 44 kelime.
Bu inceleme akustik kök nedeni tek başına kanıtlamaz.

Önceki stt_initial_prompt komut örnekleri hem configten hem desteklenen profilden
kaldırıldı. Geniş karma sözlükte sentetik doğruluk bu döngü riskini dışlamamıştı.
Türkçe öncelikli tr_en korunuyor. Bütünü en az üç kez aynı kalıbı tekrarlayan veya
compression_ratio>2.4 olan STT çıktıları komut olarak işlenmez. Kullanıcının
22 tekrar örneği ve kapat tekrarları reddedildi; sıradan komut ve uzun dikte
içindeki tekrar parçası korunur (reports/voice-loop-regression.json).

Gerçek ilk PCM'de seslendirilen metin yalnız geçici belleğe alınır. Oynatma ve
bitişinden sonra 1.2 sn yakalanan aynı/yakın söz parçaları yeni tur başlatmaz.
Geç tamamlanan STT için yakalama zamanı kullanılır; takip hakkı tüketilmez.
Eşleşmeyen bozuk yankının takip hakkını kullanmasını önlemek için uzun yanıt
sırasında/yankı aralığında yeni komut açık Artemis ister. VAD sesi yine erken
keser. Mikrofonun durakladığı Dinliyorum sonrasında komut Artemis'i tekrar
söylemeden alınır. Bu sınır kullanıcıya açıklanmıştır; akustik AEC/kimlik değildir.

682 test geçti; 11 skip/1 deselected, ruff/mypy temiz. Sahte kabul 39/39,
sıfır tehlikeli hata/onaysız yan etki. Yeni panel sesli açıldı. Emel/Haiku/Razer,
tarayıcı aliası ve hassas işlem politikası korundu. Gerçek cihaz döngü/kısa
komut kabulü hâlâ kullanıcı geri bildirimi bekliyor; önceden gönderilen uygulama
eylemi geri alınmış sayılmadı. Commit/push yok. ADR 0022.
