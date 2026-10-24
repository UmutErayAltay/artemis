# ADR 0041: Ortak ses hattında ilişkilendirilmiş ölçüm

- Tarih: 2026-10-25
- Durum: Kabul edildi (ilk ölçüm adımı)

Canlı gecikme betiğinin ayrı STT/TTS zinciri üretim factory'sinden sapıyordu.
Canlı ölçüm `create_runtime` ve `build_voice_controller` kullanır; araçlar aynı
CommandRunner/SafetyGate üzerinden geçer. Ücretli çağrı izni olan ayar reddedilir.

Factory isteğe bağlı yapılandırılmış zaman callback'ini controller'a taşır.
Eski `(stage, timestamp)` callback'i uyumluluk için korunur. Her VAD sözüne yerel
`utterance_id`, kabul edilen her ses üretimine `generation_id` atanır. Bunlar AgentLoop
tur kimliği değildir. Yanıt callback'leri kimliği kapatır; araya giren reddedilmiş
ses eski yanıtın ilk PCM olayını üzerine alamaz. Ham ses ve transcript ölçüme girmez.

Monotonik saatle son voiced frame, endpoint kararı, nihai STT, komut başlangıcı,
runner tamamlanması, TTS ilk parçası ve player ilk PCM olayı kaydedilir. Mevcut
`model_complete` adı bütün runner süresini belirtir; saf LLM süresi değildir.
İlk PCM'in model tamamlanmasından önce gelmesine rapor şeması izin verir; bu karar
streaming özelliği eklemez. Wake/kip bildirimi görev yanıtı olarak sayılmaz.
Terminal sonuçlar ve eksik/geçersiz örnekler raporda korunur, her metriğin geçerli
örnek sayısı ayrıdır. Stop süresi gerçek olay çifti yokken null'dır. Player callback'i
fiziksel duyulabilirlik kanıtı değildir.

İlk PR model/token/araç içi olayları, masaüstü telemetri dışa aktarımını, gerçek
Windows donanım kabulünü ve streaming uygulamasını erteler. Bilinmeyen donanım,
bellek ve gerçek işleme durumları null kalır. Sahte adaptör ölçümü aynı rapor
şemasını sınar; gerçek gecikme iddiası üretmez.

`completed` ses oynatma sonucudur; model veya araç başarısını kanıtlamaz. Dağılımlar
ack hariç geçerli aşama çiftlerini hata/iptal örneklerinde de kullanabilir; her
metriğin kendi paydası vardır. Stop olayları bu alt adımda bağlanmaz, değer null'dır.
Native model yüklemesi thread iptaliyle durdurulamaz. Döngü yaşarken yükleme bitişinde
gecikmiş cleanup yapılır; süreç kapanışında native yüklemenin anında kesildiği veya
kapanışın kesin süreyle sınırlı olduğu iddia edilmez.
