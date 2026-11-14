# F3 — Artemis ses katmanı teslim raporu

## Kapsam ve durum

Uyandırma sözcüğü kullanıcı tercihiyle **artemis** seçildi. Ses kanalı yerel VAD
ve STT üzerinden aynı CommandRunner/SafetyGate akışına bağlanır. Yerel Piper
varsayılandır; Edge TTS yalnız ayrı metin aktarımı izniyle kullanılır. Takip
penceresi ve konuşmayı kesme ses kanalının sorumluluğudur; tool onayı hâlâ tam
argümanlarla terminalden alınır. Sesli “evet” onay değildir.

Uygulama ve yerel izole test doğrulaması tamamlandı.
**F3 kabulü Windows elle doğrulama ve medyan < 1.5 sn ölçümü bekler.** Bu Linux
ortamında mikrofon/hoparlör açılmadı; sahte ölçüm gerçek gecikme iddiası değildir.

## Doğrulama

Yerel Linux komut çıktılarının son satırları birebir:

```text
pytest -q: 323 passed in 5.34s
ruff check .: All checks passed!
mypy: Success: no issues found in 41 source files
```

| Ölçüm | Sonuç |
| --- | --- |
| Ses sözleşmesi testleri | 19 geçti; gerçek aygıt/model yüklemesi yapılmadı |
| Sahte ses koşusu | 20 örnek; iptal 0, ses üretmeyen 0 |
| Sahte ilk PCM medyan / p90 | 103.167 / 136.353 ms |
| F0–F2 kabul regresyonu | 29/29; üç tekrar, 87 koşu |
| Tehlikeli hata / onaysız yan etki | 0 / 0; mevcut sahte kabul setinde |
| Güvenlik mutasyonu | 3/3 yakalandı; kaynak hash'leri değişmedi |
| Dosya güdümlü gerçek donanım ilk ses medyan / p90 (2026-11-14, aşağıda) | 2938 / 3359 ms (WebRTC, `end_silence_ms` 1000) |
| Gerçek mikrofonla ilk ses medyan / p90 | Kullanıcıda (ölçülmedi) |
| Uyandırma ön-kapısı, gerçek kullanıcı sesi (ADR 0069) | 26/27 isabet, 0/20 yanlış alarm |
| Canlı yanlış onay, söz kesme gecikmesi | Ölçülmedi |

Sahte ses düzeneği 100 ms sentetik VAD sessizlik aralığı ile sahte STT/model/TTS
ve PCM sink kullanır. Bu profil varsayılan 600 ms gerçek VAD ayarı değildir ve
CUDA/Edge/Piper performansını ölçmez. Canlı betik başarısız, ses üretmeyen ve
geçersiz zaman sıralı örnekleri ayrı raporlar; ölçülmeyen oranlar `null` kalır.
Yerel ham raporlar gitignore kapsamındaki `reports/f3-*` dosyalarındadır.

Denetlenen davranışlar: kesin metin dışında dispatch olmaması,
sözcük sınırı, takip penceresi, model/TTS sırasında söz kesme, onay sırasında
ses atma, kuyruk sınırları, Edge gizlilik izni, iptalde fallback olmaması ve
gerçek PCM oynatıcı olayına dayalı ölçüm.

Konuşma üst sınırı aşılırsa kesit komut sayılmaz; tüm konuşma atılır ve sessizlik
sonrası yeniden uyanma gerekir. Onay sırasında kuyruğa alınmış veya bekleyen
okumada tutulmuş eski mikrofon karesi zaman sınırıyla reddedilir. Edge'in kendi
hata ailesi ve ağ hataları normalize edilir; ilk PCM'den sonra oluşan hata eski
yanıtı baştan yerel sesle tekrar oynatmaz.

## Bilinçli sınırlar

- 32 GB RAM ve RTX 4050 için CUDA/float16 başlangıç profili seçildi; VRAM,
  sürücü, mikrofon ve gerçek gecikme burada ölçülmedi.
- STT özel uyandırma modelinin yerine kullanılır. Sürekli yerel STT'nin kaynak
  tüketimi ve yanlış/kaçırılan uyanma oranı Windows'ta ölçülmelidir.
- STT pencere sonuçları kesin komut değildir. Model yanıtı mevcut AgentLoop'ta
  tamamlandıktan sonra seslendirilir; LLM token akışı bu fazda değiştirilmedi.
- Senkron terminal onayı tamamen eller serbest kullanım sağlamaz. Onay
  aralığındaki ses sonraki komuta taşınmaz.
- Akustik yankı giderme yoktur. İlk söz kesme doğrulaması kulaklıkla yapılır;
  hoparlör kaynaklı yanlış kesmeler ayrıca sayılmalıdır.
- Native STT/TTS inference hesabı Python task iptaliyle anında durdurulamaz.
  Model erişimi tekilleştirilir; iptal edilmiş sonuç komut/oynatma üretmez.
  Native çağrı takılırsa süreç kapanışı da bekleyebilir; zorla kesilebilir
  inference işçisi bu sürümde yoktur.
- Edge yolu FFmpeg kurulumu ve metin aktarımı izni gerektirir. Yerel modeller
  önceden hazırlanır; uygulama kendiliğinden model indirmez.
- Windows ve uzak CI çalışması henüz yapılmadı. API anahtarları yok; canlı veya
  ücretli beyin çağrısı yapılmadı. Ölçülmemiş tehlike/gecikme oranı sıfır yazılmaz.

Kurulum ve elle kabul adımları [VOICE_SETUP.md](VOICE_SETUP.md), mimari gerekçesi
[ADR 0008](adr/0008-voice-channel.md) ve [ADR 0009](adr/0009-streaming-speech.md)
içindedir. F4 hafızaya geçmeden F3'ün Windows kabulü ve gecikme ölçümü önerilir.

## Dosya güdümlü gerçek donanım ölçümü (2026-11-14)

`python -m bench.voice_latency --canli --config <kopya> --wav saat.wav tarih.wav selam.wav --tekrar 7`
(21 tur). WAV'lar projenin Piper sesiyle sentezlendi (kişisel ses yok) ve gerçek zamanlı mikrofon
gibi beslendi; STT (faster-whisper large-v3, CUDA), model (Haiku 5.5, `proxy_cli`) ve TTS (EMA) gerçektir,
hoparlöre ses çıkmaz. Kullanıcı hafızası kirlenmesin diye ayrı `state_dir` kullanıldı. Bu ölçüm gerçek
mikrofon kabulünün yerini tutmaz.

| Koşu | Uçtan uca medyan / p90 | Konuşma sonu bekleme | STT | Model ilk token | Yerel hat* |
| --- | --- | --- | --- | --- | --- |
| WebRTC VAD, sessizlik 1000 ms | 2938 / 3359 ms | 1047 | 781 | 797 | 2156 ms |
| Silero VAD (ADR 0067), 1000 ms | 2875 / 3610 ms | 1032 | 781 | 766 | 2128 ms |
| Silero + Smart Turn (ADR 0068), 600 ms | 5062 / 8469 ms | 672 | 750 | 3438 | 1609 ms |

\* Yerel hat = konuşma sonu bekleme + STT + (TTS ilk parça − model ilk token): modelin dışındaki,
Artemis'in kendi kontrolündeki süre. Son koşuda sağlayıcının ilk token süresi 0,8 sn'den 3,4 sn'ye
çıktı (aynı akşam iki tekrar: 4,2 ve 3,4 sn); uçtan uca fark bu dış gecikmedendir. Yerel hat %25
kısaldı. Uçtan uca medyan < 1,5 sn hedefi bu model/sağlayıcı ile tutmuyor: modelin ilk token süresi tek
başına ~0,8 sn, yerel hat ~1,6 sn. Kalan büyük yerel kalem STT'dir (large-v3 + beam 5; `large-v3-turbo`
veya beam 1 ile kısalabilir, doğruluk ölçülerek seçilmeli — kural 11).
