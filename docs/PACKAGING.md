# Windows Paketleme Kılavuzu

Bu belge Artemis'in Windows `onedir` paketi ve Inno Setup kurucusunun nasıl derlendiğini özetler (bkz. [ADR 0075](adr/0075-urunlestirme-plani.md)).

## Derleme komutu

Depo kökünden PowerShell ile çalıştırın:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1
```

Betik sürümü `pyproject.toml` dosyasından okur, `.venv` içine `pyinstaller==6.11.1` kurar, `dist\Artemis\` çıktısını üretir ve `ISCC.exe` kuruluysa `dist\installer\Artemis-Setup-<sürüm>.exe` kurucusunu oluşturur.

Ön koşullar: `uv` (betik PyInstaller'ı `uv pip install` ile kurar) ve depo kökünde proje bağımlılıklarıyla hazırlanmış bir `.venv`; betik sanal ortam oluşturmaz.

## Paket içeriği ve hariç tutulanlar

- **Dahil edilenler:** `artemis` alt modülleri ve veri dosyaları (ikon, SVG'ler, gömülü ses modelleri), `config\config.example.yaml` ve kurulu olan ses/runtime paketlerinin (`faster_whisper`, `ctranslate2`, `tokenizers`, `huggingface_hub`, `piper`, `onnxruntime`, `_sounddevice_data`, `webrtcvad`, `livekit`, `edge_tts`) veri dosyaları. `bench` paketinden yalnız `bench.model_testi`, `bench.run` ve `bench.bilgisayar_testi` modülleri ile `model_testi.yaml` ve `senaryolar.yaml` girer (Ayarlar > Model testi).
- **Hariç tutulanlar:** Ağır isteğe bağlı bileşenler (`torch`, `torchaudio`, `ema_lightning`, `playwright`, `pyarrow`, `fastembed`, `sherpa_onnx`) ile test/geliştirme araçları (`tests`, `pytest`, `mypy`, `ruff`). (`packaging\artemis.spec` kuruluysa `faster_whisper`, `ctranslate2`, `tokenizers` ve `huggingface_hub` dosyalarını `collect_all` ile toplar.)

## Kullanıcı verileri ve kaldırma

- Paketli kurulumda yapılandırma ve durum dosyaları `%APPDATA%\Artemis` klasöründe tutulur; ilk açılışta `config.yaml` yoksa paket içindeki örnek dosyadan kopyalanır.
- Kaldırma işlemi yalnız kurulum klasörünü (`{localappdata}\Programs\Artemis`) siler; `%APPDATA%\Artemis` altındaki kullanıcı verilerine dokunmaz.

## İmzasız kurucu uyarısı

Kurucu şu aşamada kod imzalama sertifikasıyla imzalanmamıştır (bkz. [kod imzalama politikası](CODE_SIGNING_POLICY.md)). Windows SmartScreen "Bilinmeyen yayıncı" uyarısı gösterebilir.

## `-m` öykünmesi neden var?

Uygulama içindeki alt süreçler (`artemis\tools\processes.py`, `artemis\channels\desktop\provider_section.py` ve MCP `{python}` köprüsü) çocuk süreçleri `sys.executable -m <modül>` ile başlatır. Dondurulmuş pakette `sys.executable` doğrudan `Artemis.exe` olduğu ve `-m` bayrağını bilmediği için `packaging\entry.py` yalnızca `artemis.*` önekli modüller ile `_IZINLI_MODULLER` listesindeki `bench.model_testi` için `-m` davranışını öykünür; diğer modül adlarını reddeder.
