# Artemis konuşma kalitesi planı

## Amaç ve mevcut durum

Artemis'in `artemis` uyandırma sözcüğünden sonra Türkçe/İngilizce karışık gündelik
konuşmayı genel kullanıcılar için doğru anlamasını geliştirmek. Hedef makine RTX
4050 ve 32 GB RAM'dir. Bu belge uygulama planı ve ilerleme kaydıdır. İlk ölçüm alt adımı aşağıda
belgelenmiştir; sonraki aşamalar tamamlanmış veya kabul edilmiş karar sayılmaz.

Bugün [ses doğruluğu planındaki](SPEECH_ACCURACY_PLAN.md) 15 satır yer tutucu WAV
planı gerçek sesle genel WER sağlamaz. `bench/voice_latency.py` canlı raporu artık gerçek uygulamanın
[factory](../artemis/channels/voice/factory.py) ve Qt'siz runtime'ını kullanır;
EMA/Edge/Piper seçimi ve AEC/DFN ayarları ortak zincirden geçer. Factory'nin
isteğe bağlı yapılandırılmış olay hook'u ile eski `on_timing` hook'u dışarıdan
bağlanabilir. [core.py](../artemis/channels/voice/core.py) `_run_and_speak` içinde
`run_command` tamamlanana kadar bekler; sağlayıcı çağrıları streaming kapalıdır.
Dolayısıyla ilk ses, bugün model/araç işi bittikten sonra başlar.

[ADR 0037](adr/0037-noise-suppression.md) sentetik/teknik DFN ölçümlerini
belgeliyor: akış-parite, gecikme, VAD ve gürültü koşullarında segment kapanışı;
Whisper WER'i bu testte kötüleşebildiğinden temizlenmiş ses varsayılan olarak
STT'ye verilmez. [ADR 0038](adr/0038-echo-cancellation.md) sentetik yankı
azaltımı ve işlem maliyetini ölçüyor. Bu kanıtlar korunmalı, fakat genel
konuşmacı başarısı ya da Windows gerçek cihaz kabulü olarak yorumlanmamalıdır.

## Aşamalar

2026-10-24 öncelikli kullanıcı hatası: yalnız VAD ile sesin kesilmesi kaldırıldı;
söz kesme nihai STT ve mevcut dinleme kurallarını bekliyor ([ADR 0040](adr/0040-validated-playback-interruption.md)).
Bu düzeltme aşağıdaki gerçek doğruluk ve gecikme ölçümlerinin yerini tutmaz.

### 1. Ortak canlı çalışma zamanı ölçümü

**2026-10-25 — ilk alt adım uygulandı; aşamanın tamamı bitmedi.**
[ADR 0041](adr/0041-common-voice-telemetry.md) ortak factory/runtime ve kimlikli
olay kararını kaydeder. Rapor; VAD başlangıcı/son voiced frame, endpoint, nihai STT,
komut başlangıcı, bütün runner tamamlanması, TTS ilk parça ve player ilk PCM'ini
ölçer. Birbirine giren reddedilmiş sözler eski yanıtın olaylarını sahiplenmez.
Wake/kip bildirimi ayrı `ack` sonucudur. Tamamlandı/reddedildi/iptal/hata/sessiz/
yarım sözler, geçersiz zamanlar ve sınırlı tampon taşması açık sayılır. `completed`
ses oynatma sonucudur; model/araç başarısı değildir. Aşama dağılımları, mevcut
geçerli olay çiftlerini iptal/hata örneklerinde de kullanabilir; her metriğin
örnek sayısı ve dışlama paydası vardır. p50 medyandır; p90 sıralanmış örneklerin
`round((n-1)*0.9)` indeksidir. Ham ses ve transcript rapora yazılmaz.

Canlı rapor onay/pause/cancel/kapanış ve timer/sinyal temizliğini aynı runtime
üzerinden yapar; ücretli çağrı izni açık config reddedilir. `--rapor PATH` JSON
artifaktı isteğe bağlıdır; stdout JSON, durum/onay çıktısı stderr'dir. Sahte yol
izole adaptörler kullanır ve aynı şemayı sınar; gerçek gecikme iddiası değildir.
Native model yüklemesi `to_thread` iptaliyle durmaz: döngü çalışıyorsa yükleme
bitince kaynak temizlenir; süreç kapanışının native yüklemeyi anında kestiği veya
kesin sürede bittiği iddia edilmez.

**Ertelenen ölçümler:** model isteği/ilk token ve tool içi aşamalar, gerçek streaming,
sohbet/araç sınıfları, stop isteği/oynatma durması olaylarını bağlamak, masaüstü
telemetri dışa aktarımı, soğuk/ısınmış profil ayrımı, gerçek aygıt gecikmesi ve
GPU/CPU/bellek/contention profili. Bunlar bilinmiyorsa null'dır; ayar profili
`requested_*` ile etiketlidir (TTS fallback'in fiilî backend'i doğrulanmaz).
Stop metriği gerçek olay çifti yokken null'dır. İlk PCM player yazım olayıdır,
fiziksel duyulabilirlik ve Windows gerçek cihaz kabulü henüz ölçülmedi.
`collection_complete` yalnız sınırlı olay tamponunun taşmadığını bildirir;
setup/run hataları ile yarım/geçersiz örnekler ayrı alanlarda görünür.

Doğrulama: donanımsız hedefli core/factory/rapor/bench kümesi 100 geçti,
2 isteğe bağlı bağımlılık testi atlandı. Genel kontrol çıktıları:

```text
2 failed, 1673 passed, 6 skipped, 4 deselected in 72.55s (0:01:12)
All checks passed!
Success: no issues found in 104 source files
```

İki başarısız test `tests/test_mcp_client.py` içindeki alt süreç kapanış
kontrolleridir. Bunlar önceki oturumda değişmemiş `765c5cb` ana dalında da
yeniden üretildi: container'da kapanmış süreçlerin PID 1 altında zombi kalması
([ADR 0040 doğrulaması](adr/0040-validated-playback-interruption.md)). MCP kodu
bu adımda değişmedi. Sahte kabul senaryoları 39/39, tehlikeli hata ve onaysız
yan etki 0; sahte ses ölçümü 3/3 tamamlandı, geçersiz zaman 0 ve dosya/stdout
JSON çıktısı eşit. Sahte sonuçlar gerçek ses performansı değildir.
Canlı mikrofon/hoparlör, model indirme ve ücretli çağrı yapılmadı. Ortak hattın
kurulması 1.5 s veya stop hedefinin tuttuğunu göstermez.

Aşamanın kalan hedef kapsamı:

Tek rapor yolu gerçek `build_voice_controller`/uygulama runtime'ını kullanmalı;
ayrı ve güncelliğini yitirebilecek bir zincir kurmamalı. `on_timing` factory ve
desktop worker üzerinden enjekte edilerek `turn_id`/`generation_id` ile eşlenen
olaylar ölçülmeli: speech start, son voiced frame (`speech_end`), VAD endpoint
kararı, son STT, model isteği/ilk token/tamamlanma, tool başlangıç/bitiş, TTS ilk
PCM, player callback'i, stop isteği ve oynatmanın kesilmesi. VAD sessizlik
beklemesi uçtan uca gecikmeye dahildir; endpoint zamanı ayrıca raporlanır.
Hedef speech_end → ilk PCM'dir. Player callback'i fiziksel olarak duyulduğunu
kanıtlamaz; aygıt çıkış gecikmesi kaydedilmeli, Windows loopback veya manuel
cihaz kabulü ayrı raporlanmalıdır. “Dinliyorum” wake ack'i yanıtın ilk PCM'i
sayılmaz. Streamingde first PCM, model_complete'ten önce gelebileceğinden olay
sıralaması buna izin vermeli. Gürültü, aygıt/ASR hatası ve iptal örnekleri
metrikten sessizce düşürülmemeli; sayıları ve dışlama nedenlerini yazın.
Factory kurulumunun kapanış/cleanup ve sinyal/iptal davranışını da bu PR'da
doğrulayın.

Komut sınıfını sohbet ve araçlı tur diye ayırın; ikisini tek ortalamada
birleştirmeyin. Her aşamada p50/p90, örnek adedi ve başarısız/iptal tur sayısı
verin. `voice_latency` sahte raporu ile canlı raporu açıkça ayırın; sahte değerler
canlı performans iddiası üretmesin. Canlı test yerel STT/TTS ile sınırlı olsun;
gerçek model testi için ücretli API çağrısı yapmayın. Kişisel sağlayıcı kotası
varsa test onu tüketebilir; fiyatın `0` olması sınırsız kullanım demek değildir.

Sohbette mevcut hedef speech_end → ilk ses p50 < 1.5 s; önerilen ek hedefler
p90 < 3 s ve stop isteğinden oynatmanın durmasına p90 < 300 ms'dir. Araçlı turda
ilk dürüst bilgilendirme ile işin tamamlanması ayrı ölçülür; uzun bir işin
tamamlanmasına 1.5 s sınırı konmaz. İlk ölçüm alınmadan hedef tuttu denmez.
Profil başına en az 30 geçerli tur önerilir; soğuk başlangıç ve ısınmış çalışma
ayrı raporlanır. Söz kesmede kullanıcı sesinin başlangıcından durmaya kadar olan
süre de ölçülür; yalnız stop sinyali sonrası süre algılama gecikmesini gizlememeli.
Rapor ayrıca GPU/CPU bellek tepe ve boş VRAM'i, eşzamanlı kullanım (contention),
seçili model/quantization/beam, STT/TTS yolu ve aygıt örnekleme oranını içermeli.

### 2. Gerçek konuşmacı değerlendirmesi ve ASR/VAD deneyleri

İlk veri paketi en az 200 kısa örnek ve en az 8 gönüllü içersin; bunlar önerilen
planlama tabanıdır, temsili demografik örnek iddiası değildir. Örnekler gündelik
soru, Türkçe içinde İngilizce söz/özel ad, tereddüt ve kendini düzeltme, uygulama
dışı istek, olumsuzluk ve uyandırma sözcüğü/sessizlik koşullarını kapsasın.
Geliştirme ve test konuşmacılarını ayırın; testte geliştirmede görülmemiş
uygulama adları kullanın. Yanlış uyanma ölçümü için en az bir saat etiketli
komut içermeyen ses önerilir; kısa sessizlik örnekleri saatlik oranı kanıtlamaz.

Ham ses yalnız gönüllülerin açık opt-in'iyle yerel değerlendirmede tutulmalı;
otomatik kayıt, otomatik yükleme ve depoya WAV ekleme yok. Katılımcı onayı,
silme yöntemi ve anonim kimlikler veri paketi notunda belirtilsin. Gündelik
çalışma modunda ham ses saklanmaz.

Ayrı koşullarda Whisper `large-v3-turbo` ve `large-v3` katalogda sunulan
seçeneklerini, mevcut kullanıcı profilini başlangıç karşılaştırması olarak ve STT dil ayarı/beam
seçeneklerini karşılaştırın. Konfigürasyonda bulunan modeli/yolu kullanın;
ölçmeden kazanan ilan etmeyin. Model, beam ve dilin tam Kartezyen çarpımı yerine
önce sabit baseline, sonra en umut verici bir ayar değişkeni, en son etkileşim
kontrolü şeklinde kademeli deney yapın. RTX 4050/32 GB'de süre, VRAM ve bellek
baskısını doğrulukla birlikte değerlendirin.

Her örnek için referans metin ve etiketli uyandırma, niyet, hedef uygulama,
olumsuzluk/düzeltme, kategori, konuşmacı split'i ve koşul bulunsun. WER/CER,
uyandırma kaçırma ve yanlış uyanma (saat başına), VAD sessizlikte yanlış metin,
niyet/hedef doğruluğu ve tehlikeli yanlış anlama ayrı raporlansın. Aşama 1'in
p50/p90 zamanları, VRAM boşluğu ve contention'ı model/koşul bazında rapora katın.

DFN/AEC için ADR 0037/0038'deki sentetik teknik metrikleri teknik regresyon
göstergesi olarak rapora ekleyin; gerçek konuşmacı sonuçlarıyla ayrı bölümde
tutun. Ham/işlenmiş STT karşılaştırması, DFN düzeyleri, AEC açık/kapalı ve VAD
eşikleri ölçülebilir; sentetik bulgulara dayanarak gerçek ses WER'i varsayılmaz.

### 3. Güvenli streaming yanıtı ve söz kesme

Streaming mimarisi için önce ayrı ADR önerisi hazırlanmalı; bu plan kararı kabul
etmez. Sağlayıcı token'ları cümle/parça sınırlarında TTS'ye aktarılabilir, ancak
tool-call argümanları tamamlanıp şema doğrulaması ve `SafetyGate` onayı bitmeden
araç çalıştırılmaz. Kısmi argümanlarla çalışma yoktur. Araç sonucu başarıyla
doğrulanmadan sesli yanıt “yaptım” diyemez; parça parça yanıt belirsizliği dürüstçe
ifade etmelidir.

Streaming desteklemeyen sağlayıcı için açıkça raporlanan tam yanıt fallback'i
tanımlanabilir; her sağlayıcıda streaming şartı konmaz.

Söz kesmede generation iptali, sağlayıcı işi, TTS üretimi ve player kuyruğu tek
iptal akışıyla durmalı. Oynatma kuyruğu sınırlı olmalı; eski tur PCM'i sonraki
tura sızmamalı. Mevcut wake/taint/onay davranışları ve AEC korumaları korunmalı.
Yanıt cümleleri araç sonucu beklenmeden söyleniyorsa eylem başarısı iddiası
içermemeli. Sahte sağlayıcıyla iptal/sıra davranışı izole doğrulanmalı; canlı
gecikme ve barge-in ölçümü aşama 1 ile aynı runtime'dan alınmalı.

### 4. Niyet, hedef ve uygulama kataloğu

STT metninin doğruluğunu, niyet/hedef seçimini ayrı test katmanlarında ölçün.
Olumsuzluk (“açma”), düzeltme (“onu değil”), belirsiz hedef ve soru sorma
davranışını örnek bazında etiketleyin. Yanlış transcript sessizce başka bir
komuta dönüştürülmemeli; model belirsizliğinde netleştirme istenmeli. Uygulama
adları canlı kurulu uygulama kataloğundan bulunmalı; depo içine kullanıcıya özel
ad listesi veya ikinci bir keşif mekanizması eklenmemeli. Testte görülmemiş
uygulama adları genelleme kontrolü sağlar.

Araç çalıştırma başarısı ayrı oran olarak raporlanmalı. Tehlikeli yanlış hedefe
işlem ve onaysız yan etki için kabul sayısı sıfırdır; toplam WER/skor bunları maskelememeli.
Türkçe/İngilizce transcript'in bir uygulama adını içermesi tek başına o
uygulamayı açma niyeti sayılmaz. Bütün araç çağrıları mevcut `CommandRunner` ve
`SafetyGate` üzerinden kalmalı.

## Rapor sözleşmesi ve kabul

Her deney raporu JSON ve okunur özet üretmeli; asgari şema:

```json
{
  "mode": "fake|live",
  "runtime": "factory|isolated",
  "date_utc": "...",
  "hardware": {"gpu": "...", "ram_gb": 32, "vram_free_gb": null},
  "config": {"stt_model": "...", "language": "...", "beam": null},
  "corpus": {"id": "...", "split": "test", "items": 0, "speakers": 0},
  "metrics": {"wer": null, "cer": null, "intent_accuracy": null,
    "target_accuracy": null, "negation_accuracy": null,
    "false_wake_per_hour": null, "missed_wake_rate": null,
    "false_interrupt_rate": null, "latency_ms": null,
    "endpoint_delay_ms": null, "device_output_latency_ms": null,
    "failed_or_cancelled": 0, "excluded_with_reason": 0},
  "notes": []
}
```

Ölçülmemiş alan `null` olur; tahminle doldurulmaz. Fake ve live sonuçlar ayrı
etiketlenir; sentetik, gerçek ses ve Windows cihaz sonuçları kendi protokolüyle
ayırt edilir. Komut satırı en az sahte corpus ve açıkça seçilmiş yerel canlı
corpus çalıştırmasını göstermeli; hiçbir canlı kip ücretli çağrı başlatmamalı.

İlk küçük PR yalnız ortak runtime zamanlama callback'i ve schema'lı rapor
çıktısını kapsasın. Takip PR'ları gönüllü corpus/etiket şeması ve WER/CER;
ayrılmış konuşmacı değerlendirmesi ile staged ASR/VAD deneyleri; ayrı ADR ve
streaming/iptal; en son niyet/hedef kabul seti olabilir. Installer, yeni kullanıcı
özellikleri ve varsayılan model değişikliği kapsam dışıdır.

İlk PR'nin beklenen dosyaları `artemis/channels/voice/factory.py`,
`artemis/channels/voice/core.py`, `bench/voice_latency.py` ve bunların testleridir;
masaüstü olay aktarımı gerekiyorsa `artemis/channels/desktop/bridge.py` eklenir.
Bitiş şartı: ölçüm EMA/Edge/Piper, AEC ve DFN seçimini aynı factory'den alır;
eksik Piper, Edge'yi engellemez; hata/iptalde kaynaklar kapanır; ölçüm callback'i
eklenmesi onay, taint ve eski sesin atılması testlerini değiştirmeden geçirir.
İlk PR gerçek WAV kayıtları olmadan sahte adaptörlerle tamamlanabilir. Gerçek
doğruluk aşaması için kayıtlar; cihaz gecikmesi kabulü için Windows makinesi gerekir.

Başlangıç kontrolleri: `python -m bench.voice_latency --sahte`,
`python -m bench.speech_accuracy --sahte --corpus bench/speech_accuracy_corpus.yaml`,
ardından ilgili izole testler ve depo değişiklik kontrolleri (`ruff check .`,
`mypy`, uygun test seçimi). Canlı yerel ses yalnız açıkça seçilen veriyle ve
kullanıcının mikrofonu açtığı oturumda denenir; ücretli sağlayıcı çağrısı yoktur.
Mevcut sözleşme komutları [CLAUDE.md](../CLAUDE.md) içindedir.

Planın tamamlanma ölçütü: ortak runtime raporu ve şema çalışır; en az 200 örnek,
8 konuşmacı ve sızıntısız split ile gerçek WER/CER ile metrikler raporlanır;
her konuşma-model koşulu için gecikme ve bellek sonuçları vardır; güvenlik
hataları ayrı gösterilir; ölçüm bulunmayan iddia yoktur. Windows donanım kabulü
yapılmadıysa Windows'ta doğrulandı denmez.
