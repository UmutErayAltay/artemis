# -*- mode: python ; coding: utf-8 -*-
"""Artemis Windows onedir PyInstaller reçetesi (ADR 0075)."""

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# SPECPATH `packaging/` klasörünü gösterir; depo kökü bir üst dizindir.
ROOT = Path(SPECPATH).resolve().parent

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
        pass

# Araç eklentileri `@register_tool` ile çalışma anında keşfedildiği için tüm alt modüller eklenir.
hiddenimports = collect_submodules("artemis")

# Ağır isteğe bağlı özellikler ve geliştirme araçları temel paketin dışında tutulur (ADR 0075).
excludes = [
    "torch",
    "torchaudio",
    "ema_lightning",
    "playwright",
    "pyarrow",
    "fastembed",
    "sherpa_onnx",
    "huggingface_hub",
    "tests",
    "bench",
    "pytest",
    "mypy",
    "ruff",
]

a = Analysis(
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(ROOT)],
    binaries=[],
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
