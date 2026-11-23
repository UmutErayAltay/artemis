# ADR 0088: Ses modeli sağlık yöneticisi ve Windows dağıtım hattı

- Tarih: 2026-11-23
- Durum: Kabul edildi

**Bağlam:** Artemis yerel ses hattı dört farklı model ailesine dayanır: Whisper (faster-whisper STT), Piper (yerel TTS `.onnx`/`.onnx.json`), Silero VAD (`silero_vad.onnx`) ve DeepFilterNet3 (`dfn3_step.onnx`). Kullanıcılar bu modellerin hangilerinin kurulu olduğunu, diskte ne kadar yer kapladığını veya SHA-256 / dosya bütünlüğünün bozulup bozulmadığını tek bir yerden göremiyordu; ayrıca paket içi Silero VAD ve DFN3 dosyalarının ya da kullanıcının özel model klasörlerinin yanlışlıkla silinmesini önleyen merkezi bir koruma yoktu. Öte yandan Windows kurucu derlemesi yalnız yerel betik (`scripts/build_windows.ps1`) olarak duruyordu; SignPath / Authenticode imzalama kancası, imzasız yedek derleme ve `SHA256SUMS.txt` doğrulama manifesti içeren tekrarlanabilir bir CI dağıtım hattı gerekiyordu.

**Karar:**

1. **Qt'siz ses modeli envanter ve bakım çekirdeği (`artemis/channels/voice/model_health.py`):**
   - `inventory_voice_models`, `verify_voice_model`, `download_voice_model`, `clean_voice_model` ve `summarize_health` dört aileyi tek `VoiceModelEntry` yapısında birleştirir:
     - **Whisper (`stt_catalog`):** Katalogdaki modellerin yanında yapılandırılmış özel `stt_model` ve `game_mode_stt_model` yollarını da tarar; `model.bin` + `config.json` varlığını, boş olmamasını ve isteğe bağlı SHA-256 eşleşmesini denetler. Temizleme yalnız tanınan HuggingFace önbellek klasörlerine (`can_clean=True`) uygulanır; özel kullanıcı klasörleri silinmez.
     - **Piper:** Küratörlü sesler (`tr_TR-dfki-medium`, `en_US-lessac-medium`, `en_GB-alba-medium`) ve diskte keşfedilen `.onnx` / `.onnx.json` çiftlerinin boyutunu, JSON geçerliliğini ve sabitlenmiş SHA-256 değerlerini doğrular; `.part` + `os.replace` ile atomik tek tık indirme ve yalnız yönetilen model klasöründeki çiftleri silme sunar.
     - **Silero VAD (`silero.py`) & DeepFilterNet3 (`denoise.py`, `model_paths.py`):** Sırasıyla `verify_silero_model()` ve `verify_step_model()` ile SHA-256 doğrulaması (`ok` / `corrupt` / `missing`) yapar. Paket içi modeller (`bundled=True`, `can_clean=False`) ve özel `noise_model_dir` konumları temizlemeden kesin olarak korunur.
   - Tüm indirme ve doğrulama işlevleri enjekte edilebilir getirici kancaları (`whisper_installed`, `whisper_downloader`, `url_fetcher`) kabul ederek testlerin tamamen çevrimdışı çalışmasını sağlar.

2. **Masaüstü Ses Modeli Yöneticisi (`artemis/channels/desktop/voice_model_manager.py`):**
   - `VoiceModelManagerWidget` envanter yenileme, doğrulama, tek tık indirme ve temizleme işlemlerini arayüz iş parçacığını kilitlemeden arka plan `QThread` işçileriyle yürütür; panel kapanırken `shutdown_thread` ile güvenli sonlandırma yapar.
   - Paket içi modellerde `Temizle` düğmesi devre dışıdır; `demo=True` kipinde indirme ve temizleme devre dışı kalır.
   - Bileşen tekrar oluşturmadan *Ayarlar > Ses > Ses modelleri* 3. alt sekmesi (`panel.voice_model_manager`) altında tek merkezde bağlanır ve model değişikliğinde `models_changed` sinyaliyle *Dinleme* / *Yanıt* ses seçicilerini günceller; *Hakkında ve Güncelleme* yalnızca uygulama sürümü, lisans ve güncelleyiciye odaklanır. *Ses* sayfasının `6/6` yardım baloncuğu sınırı yeni `HelpTip` eklenmeden korunur.

3. **Windows sürüm CI ve kurucu hizalaması (`.github/workflows/release-windows.yml`, `packaging/artemis.iss`):**
   - `windows-latest` ve Python `3.12` üzerinde `workflow_dispatch` ile `v*` etiketlerinde çalışan yayın akışı PyInstaller paketini (`packaging/artemis.spec`) ve Inno Setup kurucusunu (`packaging/artemis.iss /DMyAppVersion=...`) derler.
   - SignPath (`SIGNPATH_*`) veya Authenticode (`WINDOWS_CERT_PFX_BASE64`) gizli anahtarları tanımlıysa imzalama adımını çalıştırır; anahtarlar yoksa açık bir özet bildirimiyle imzasız derlemeye devam edip `SHA256SUMS.txt` özet manifesti ve sürüm varlıklarını üretir.
   - `packaging/artemis.iss` yayın/sürüm URL'leri, `VersionInfo*` alanları, 64-bit mimari meta verisi ve isteğe bağlı `#ifdef SignToolCmd` kancasıyla hizalanır; `DisableDirPage=yes`, `PrivilegesRequired=lowest` ve `{userappdata}` dokunmayan `[UninstallDelete]` değişmezleri korunur.

**Sonuç:** Kullanıcılar tüm ses modellerinin sağlık, boyut ve SHA-256 durumunu *Ayarlar > Ses > Ses modelleri* alt sekmesinden tek yerde güvenle yönetebilir; Windows kurucuları da imzalama gizli anahtarları olsun veya olmasın tekrarlanabilir bir CI hattıyla `SHA256SUMS.txt` eşliğinde üretilir.
