# ADR 0072 — Oyun modu: tam ekranda büyük STT modelini bırakma

**Bağlam:** Yerel tanıma `large-v3` ile çalışır (int8_float16 ~2,5 GB, float16 ~4,3 GB VRAM). Tam ekran
oyun öndeyken bu bellek oyundan çalınır; Artemis'i kapatmak ise sesli komutu da kaybettirir. Fikir
Umut'un tanıma notlarındaydı ("oyun modu", Fikir 2); 2026-11-15'te istendi.

## Karar

1. `voice.game_mode` (varsayılan kapalı). Açıkken ses hattı tam ekran durumunu birkaç saniyede bir okur.
2. Algılama Windows'un kendi sinyaliyle yapılır: `SHQueryUserNotificationState` →
   `QUNS_BUSY` (tam ekran uygulama, kenarlıksız oyun dahil) ya da `QUNS_RUNNING_D3D_FULL_SCREEN`
   (özel tam ekran D3D). Pencere sayımı ya da süreç adı listesi yoktur; ucuz ve oyun listesine bağlı
   değildir. Tam ekran video da "tam ekran uygulama" sayılır; bu kabul edilen bir yan etkidir (büyük
   model yine geri gelir).
3. Durum değişimi ardışık iki okumayla kesinleşir (alt-tab titremesi model yüklemesini tekrarlamasın).
4. Oyuna girişte ÖNCE hafif model yüklenir, sonra etkin tanıyıcı değişir, EN SON büyük model bırakılır;
   çıkışta tersi. Hiçbir an tanıyıcısız kalınmaz; yükleme hatasında eski tanıyıcı sürer
   (`game:error`), iş parçacığı düşmez.
5. Hafif model `voice.game_mode_stt_model`, boşsa yerelde kurulu faster-whisper `small`. İkisi de
   yoksa oyun modu kurulmaz (`game:unavailable`), ses hattı her zamanki gibi çalışır. İndirme yapılmaz.
6. Güvenlik/kapı değişmez: yalnız aynı `SpeechRecognizer` arayüzünün arkasındaki model değişir.
   `stt_mode: hybrid` ise komut sesi yine buluta gider; hafif model yalnız uyandırmaya karar verir.

## Sonuçlar

- Oyundayken yerel tanıma doğruluğu düşer (small, ADR 0045 ölçümleri); hybrid kip bunu telafi eder.
- Oyundan çıkışta büyük modelin yüklenmesi birkaç saniye sürer; bu sürede hafif model hizmet verir.
- Test: `tests/test_voice_game_mode.py` (sahte algılayıcı ve tanıyıcılar; gerçek GPU/Windows yok).
  Windows kabulü (gerçek oyunda VRAM düşüşü) Umut'ta.
