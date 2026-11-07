# Test çalıştırma ve atlanan testler

Üç durum ayrı yazılır: **kodlandı** (değişiklik var), **otomatik test geçti** (bu dosyadaki
komutlar) ve **gerçek cihazda doğrulandı** ([kontrol listesi](WINDOWS_CHECKLIST.md),
sonuçlar [durum tablosunda](RELIABILITY_STATUS.md)). Biri diğerinin yerine geçmez.

## Varsayılan koşu

```powershell
.\.venv\Scripts\python.exe -m pytest -q        # -m "not disruptive" pyproject'te varsayılan
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\mypy.exe
.\.venv\Scripts\python.exe -m bench.run --sahte
```

Varsayılan koşu gerçek makineye dokunmaz (CLAUDE.md kural 8): dosya `tmp_path`, LLM sahte
sağlayıcı, HTTP loopback sahte sunucu, Telegram sahte istemci, ekran/fare sahte arka uç.
Sahte bench'in 53/53 geçmesi canlı model başarısı **değildir**; yalnız kapı, onay ve
beklenti şemasını sınar.

## Neden atlanır: sınıflar

`-rs` ile her atlamanın nedeni görünür: `pytest -q -rs`.

| Sınıf | Neden | Örnek dosyalar | Nasıl çalıştırılır |
|---|---|---|---|
| Gerçek cihaz (`disruptive`) | Görünür yan etki: toast, Chromium, ekran OCR, giriş arka ucu | `test_notify.py`, `test_browser_server.py`, `test_screen_ocr.py`, `test_computer_windows.py` | `scripts/run-real-tests.ps1` |
| Platform | Yalnız Windows (ya da yalnız POSIX) API'si | `test_computer_windows.py`, `test_credentials.py`, `test_undo.py`, `test_mcp_client.py`, `test_voice_resources.py`, `test_code_agent*.py` (görünür pencere) | Uygun işletim sisteminde varsayılan koşu |
| Ortam izni | Symlink oluşturma izni, FIFO, sistem tepsisi, Segoe UI yazı tipi | `test_memory.py`, `test_paths.py`, `test_tools.py`, `test_pet_sprite.py`, `test_desktop_gui.py`, `test_shell_chat.py` | Windows'ta Geliştirici Modu (symlink); masaüstü oturumu (tepsi) |
| İsteğe bağlı paket | `importorskip`: PySide6, numpy, pyarrow, playwright | GUI ve ses testlerinin çoğu | `pip install -e ".[dev,desktop,voice,browser,eval]"` |
| Dış varlık | DeepFilterNet model/ses dosyaları | `test_voice_denoise.py` | `run-real-tests.ps1 -DfnModelDir … -DfnAssets … -DfnGolden …` |

### Bu makinedeki gerçek atlamalar (2026-11-03, Windows 11, `b945876`)

`pytest -q -rs`: **2466 geçti, 22 atlandı, 4 disruptive seçilmedi.** 22 atlamanın dağılımı:

| Adet | Neden | Sınıf | Risk |
|---|---|---|---|
| 8 | Symlink yetkisi yok (WinError 1314): `test_paths.py` ×3, `test_memory.py` ×3, `test_tools.py`, `test_pet_sprite.py` | Ortam izni | **Doğrulama eksikliği:** `safe_join`/vault/dosya araçlarının symlink reddi bu Windows'ta otomatik testle koşmadı. Geliştirici Modu açıp tekrar koşun. |
| 5 | DeepFilterNet varlıkları yok | Dış varlık | Gürültü modeli altın çıktı karşılaştırması koşmadı |
| 7 | Linux/POSIX'e özgü yol (symlink kökü, POSIX grup, FIFO, getrusage, Linux toast, Windows dışı, chmod) | Platform | Windows için anlamsız; Linux CI'da koşar |
| 1 | `taskkill /T` lider çıkınca ağacı bulamaz (`test_mcp_client.py:186`) | Platform | Bilinen sınır: MCP torun süreci yetim kalabilir |
| 1 | Playwright kurulu olduğu için "kurulu değil" yolu atlandı | İsteğe bağlı paket | Yok |

Ağ işaretli ya da `xfail` test yoktur; kararsız (flaky) diye atlanan test yoktur. Bir test
kararsızsa atlanmaz, kök nedeni düzeltilir (örn. `ed36c80`, PR #12).

## Gerçek bağımlılıklı koşu

`scripts/run-real-tests.ps1` çıktıyı test edilen commit ile `reports/real-tests-<commit>.txt`
dosyasına yazar (`-dirty` eki: commit'lenmemiş değişiklik vardı). Durum tablosuna sonuç bu
dosyaya bağlanarak işlenir. Rapor eski bir commit'e aitse "güncel sürüm sonucu" diye
sunulmaz.

Ses doğruluğu ve gecikme ölçümleri ayrı araçlardır (`bench.speech_accuracy`,
`bench.voice_latency --canli`); sentetik TTS corpus'u gerçek mikrofon sonucu değildir.
