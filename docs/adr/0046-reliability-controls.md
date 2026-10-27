# ADR 0046 — Onay, süre, ses ve veri sürekliliği

Tarih: 2026-10-27. Durum: uygulandı; gerçek Windows kabulü bekliyor.

## Karar

- Host güveni zorunlu `CONFIRM_REQUIRED` riskini düşürmez; bu çağrılarda host hatırlama
  seçeneği sunulmaz. ADR 0039'un açık auto istisnaları ve gözetimsiz ret korunur.
- ComputerSession tek mutlak deadline kullanır. Capture/model/retry/onay/target sonrasında
  kalan süre denetlenir; executor thread'i OS girdisinden hemen önce süre ve Esc'yi kontrol
  eder. Başlamış OS işlemi geri alınmış sayılmaz.
- Mikrofon/VAD tüketimi final STT'den ayrıdır: tek decoder, iki bekleyen segment. Fazlası
  görünür uyarı ve `stt_queue_full` terminal olayıyla reddedilir; sınırsız PCM birikmez.
  Onay/yeniden başlatma kuyruk neslini geçersiz kılar; çözülmekte olan eski sonuç da
  komut olamaz. Kısmi STT final beklerken başlatılmaz; executor'da bekleyen partial final
  gelince atlanır. Çalışan native inference zorla kesilemez, fakat yakalama devam eder.
- `stt_queued`/`stt_start`/`stt_final` ile kuyruk ve çözüm süresi ayrılır. Mikrofon düşme
  sayacı echo/denoise sarmalayıcılarından geçer, kullanıcıya uyarılır ve raporda
  `capture_health` olayları görünür. Ham ses saklanmaz.
- Geçmiş yönetimi panel ve `/hafıza` kontrollerinden runner'ın tipli araç yoluna gider.
  ToolContext.control_request yalnız tipli istek süresince açılır; proaktif bağlamlara
  taşınmaz. Model bu yönetimi kendiliğinden uygulayamaz. Silme/saklama değişikliği SafetyGate'te
  zorunlu onaylıdır. Notlar ayrı kalır; `/yeni` arşivi silmez.
- `history_policy` SQLite tablosu geriye uyumlu eklenir; varsayılan 0 gün = otomatik silme
  kapalı. Pozitif politika değişikliği ile ilk budama aynı transaction'dadır. Uygulama
  açılışında, geçmiş render'ında ve tur kaydında politika uygulanır; uygulama kapalıyken
  zamanlayıcı çalışmaz. Turlar/reset işaretleri birlikte silinir; notlar korunur.
  Bu bir SQL veri silmedir; audit, WAL, yedek veya diskten güvenli silme garantisi değildir.
- Teslim geçmişi tüm durumları gösterir. `uncertain` teslimin doğrulanamamasıdır, kesin
  başarısızlık değildir. `reminder_retry` zorunlu onayla yeni kimlik oluşturur; eski sonuç
  korunur. Kaynak hatırlatma başına tek tekrar kaydı, transaction ve UNIQUE ile korunur;
  yeni girişim de başarısız olursa onun kimliği ayrıca yinelenebilir. Taint/private korunur.
  Otomatik tekrar yoktur, olası çift bildirim onay açıklamasında görünür.

## Servis sahipliği

| Kaynak | Başlatan/sahip | Kapanış |
|---|---|---|
| CodeAgentJobs ve sahiplik kilidi | Runtime | shutdown; başarısız kurulumda ExitStack |
| Sağlayıcı HTTP/CLI isteği | Sağlayıcının çağrısı / AgentLoop | context manager / iptal ve süreç temizliği |
| MemoryStore / ProactiveStore | Runtime servis referansı | Her işlem kısa SQLite bağlantısını kapatır |
| MCP süreci | Her StdioMCPClient çağrısı | Çağrı sonunda süreç ağacı temizliği |
| Ses controller, decoder ve ses adaptörleri | DesktopBridge | stop/close, decoder iptali ve kuyruk geçersizliği |
| ProactiveScheduler | DesktopBridge / Telegram kanalı | stop, cancel ve await |
| Telegram istemcisi | İlgili kanal | Kanalın finally/temiz kapanışı |

Tekrarlı shutdown ve sağlayıcı kurulumu başarısızlığı için kilit bırakmama testleri vardır.
Kanıtlanmış ilave sızıntı olmadığından servis mimarisi yeniden yazılmamıştır.
Kod ajanının Bash/MCP/alt ajan yetkisi Artemis kapısının dışındadır; Projeler ekranındaki
`AUTHORITY_BOUNDARY` açıklaması korunur. OS sandbox eklenmemiştir.

## Kabul

Kanonik durum ve Windows senaryoları: [Güvenilirlik durumu](../RELIABILITY_STATUS.md).
Onay/deadline mutantlarının regresyonları bozması ve tüm yerel kontroller şarttır.
Windows gerçek mikrofon/model/masaüstü kabulü otomatik veya offscreen testle geçmez.
