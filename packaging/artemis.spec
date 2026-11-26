# -*- mode: python ; coding: utf-8 -*-
"""Artemis Windows onedir PyInstaller reçetesi (ADR 0075)."""

import tomllib
from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_all,
    collect_data_files,
    collect_submodules,
    copy_metadata,
)

# SPECPATH `packaging/` klasörünü gösterir; depo kökü bir üst dizindir.
ROOT = Path(SPECPATH).resolve().parent

# Sürüm kaynağı tek yerdedir: pyproject.toml. Exe sürüm kaynağı (Dosya özellikleri) buradan üretilir.
APP_VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]


def _version_info(original_name):
    from PyInstaller.utils.win32.versioninfo import (
        FixedFileInfo,
        StringFileInfo,
        StringStruct,
        StringTable,
        VarFileInfo,
        VarStruct,
        VSVersionInfo,
    )

    numbers = tuple((int(p) for p in APP_VERSION.split(".")[:3])) + (0,)
    text = APP_VERSION
    return VSVersionInfo(
        ffi=FixedFileInfo(filevers=numbers, prodvers=numbers),
        kids=[
            StringFileInfo(
                [
                    StringTable(
                        "040904B0",
                        [
                            StringStruct("ProductName", "Artemis"),
                            StringStruct("FileDescription", "Artemis"),
                            StringStruct("FileVersion", text),
                            StringStruct("ProductVersion", text),
                            StringStruct("OriginalFilename", original_name),
                        ],
                    )
                ]
            ),
            VarFileInfo([VarStruct("Translation", [1033, 1200])]),
        ],
    )

# Paket içi varlıklar (ikon, SVG'ler, gömülü ses modelleri) ve örnek ayar dosyası.
datas = collect_data_files("artemis") + [
    (str(ROOT / "config" / "config.example.yaml"), "config"),
]

# Çalışma zamanı veri dosyası taşıyan isteğe bağlı/ses paketleri kuruluysa eklenir.
for _pkg in (
    "faster_whisper",
    "piper",
    "onnxruntime",
    "_sounddevice_data",
    "webrtcvad",
    "livekit",
    "edge_tts",
):
    try:
        datas += collect_data_files(_pkg)
    except ImportError:
        # Eksik paketin veri dosyası kurucuya girmez; derleme bunu uyarmaz.
        pass

# Araç eklentileri `@register_tool` ile çalışma anında keşfedildiği için tüm alt modüller eklenir.
hiddenimports = collect_submodules("artemis")
# Ayarlar > Model testi `-m bench.model_testi` ile alt süreçte koşar: modül ve senaryo dosyaları
# pakete girer (ağır bench araçları — ses korpusu, değerlendirme — yine de çağrılmadıkça yüklenmez).
hiddenimports += ["bench", "bench.model_testi", "bench.run", "bench.bilgisayar_testi"]
# Lisans ve üçüncü taraf bildirimleri kurucuya girer (GPL/LGPL/MPL şartı).
datas += [(str(ROOT / name), ".") for name in ("LICENSE", "THIRD_PARTY_NOTICES.md")]
datas += [(str(ROOT / "LICENSES"), "LICENSES")]
datas += [
    (str(ROOT / "bench" / name), "bench")
    for name in ("model_testi.yaml", "senaryolar.yaml")
]
# Yerel STT: faster_whisper açılışta huggingface_hub'ı içe aktarır ve ctranslate2'nin DLL'lerine
# ihtiyaç duyar; analiz bunları tembel içe aktarımdan bulamadığı için v0.2.0 paketinde yerel tanıma
# kırıktı (2026-11-18 derin tarama). Kuruluysa ikili dosyaları ve verileriyle birlikte toplanır.
binaries = []
for _pkg in ("ctranslate2", "tokenizers", "huggingface_hub", "faster_whisper"):
    try:
        _datas, _binaries, _hidden = collect_all(_pkg)
    except ImportError:
        continue
    datas += _datas
    binaries += _binaries
    hiddenimports += _hidden

# Dondurulmuş pakette importlib.metadata.version("artemis-tr") çalışsın diye dist-info eklenir.
datas += copy_metadata("artemis-tr")

# Ağır isteğe bağlı özellikler ve geliştirme araçları temel paketin dışında tutulur (ADR 0075).
excludes = [
    "torch",
    "torchaudio",
    "ema_lightning",
    "playwright",
    "pyarrow",
    "fastembed",
    "sherpa_onnx",
    "tests",
    "pytest",
    "mypy",
    "ruff",
]

a = Analysis(
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Artemis",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    # Masaüstü uygulaması konsol penceresi açmadan başlar.
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / "artemis" / "channels" / "desktop" / "artemis.ico"),
    version=_version_info("Artemis.exe"),
)

# Pencereli exe'de sys.stdout yok; stdio ile JSON konuşan alt süreçler (masaüstü işçisi, MCP,
# model testi) bu konsol kardeşiyle başlar (entry.py sys.executable'ı buna çevirir). Aynı
# _internal klasörünü paylaşır, ek boyut yalnız önyükleyicidir.
cli_exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="artemis-cli",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / "artemis" / "channels" / "desktop" / "artemis.ico"),
    version=_version_info("artemis-cli.exe"),
)

coll = COLLECT(
    exe,
    cli_exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="Artemis",
)
