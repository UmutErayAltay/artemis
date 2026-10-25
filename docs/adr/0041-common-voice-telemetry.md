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

## Ek (2026-10-25): aşamanın kalan ölçümleri

- Model ve araç aşamaları AgentLoop'ta içeriksiz `PhaseTiming` (model_request/response,
  tool_start/end, `time.monotonic`) olarak `RunResult.phases`'e yazılır. Turn_end denetimine
  girmez (`exclude=True`): monotonik değer denetimde anlamsızdır, süre zaten oradadır. Ayrı
  bir gözlemci/global durum eklenmedi; masaüstü, CLI ve bench aynı süreçte `RunResult`'ı
  zaten alıyor. Araç süresi `SafetyGate.dispatch`'tir, onay bekleme dahildir.
- Komut sınıfı: araç çağrısı varsa `tool`, yoksa `chat`; runner'a ulaşmayan tur
  (boş `turn_id`: meşgul/iptal) sınıflandırılmaz. İlk görev turu `cold`, sonrakiler `warm`.
- Stop çifti kesen sözün kimliğinde ve generation'sız yazılır; yalnız hoparlöre yazım
  sürerken (`_output_active`) üretilir. Düşünme sırasındaki iptal söz kesme sayılmaz.
  `playback_stopped` sink yazımının bittiği andır; aygıt tamponu ölçülmez.
- Hedefler en az 30 geçerli örnek ister, aksi halde `met: null`; sahte kip hedef
  değerlendirmez. İlk token, sağlayıcılar streaming kullanmadığı için null'dır.
- Canlı kip PortAudio'nun bildirdiği çıkış gecikmesini (sürücü tahmini; loopback/manuel
  kabul değildir), süreç bellek tepe değerini ve `nvidia-smi` varsa aygıt geneli VRAM
  tepe/boş değerleri ile eşzamanlı GPU süreç sayısını yazar. Okunamayan değer null'dır.
- Masaüstü `voice.telemetry_report` (varsayılan kapalı) ile ses kapanınca aynı rapor
  `state_dir/voice-telemetry/` altına `"x"` kipiyle yazılır; sonuç `voice_telemetry`
  denetim olayıdır. Rapor `artemis/channels/voice/metrics.py`'dedir, bench yeniden kullanır.

Bu ek gerçek Windows cihaz kabulünü, canlı 30 turluk ölçümü ve streaming'i sağlamaz.
