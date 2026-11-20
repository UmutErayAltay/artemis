"""Ekran görüntüsü yakalama betiğinin CLI ayrıştırma ve 2x EN uçtan uca testleri."""

from __future__ import annotations

import importlib.util
import struct
import subprocess
import sys
from pathlib import Path
from types import ModuleType

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "capture_screenshots.py"

CORE_PNGS = {
    "welcome.png",
    "approval.png",
    "panel.png",
    "overlay.png",
    "settings-ses.png",
    "settings-telegram.png",
    "auto-warning.png",
    "pet.png",
    "projects.png",
}

EXTRA_PNGS = {
    "settings-model.png",
    "settings-routines.png",
    "settings-calendar.png",
    "settings-look.png",
    "settings-about.png",
    "connect-wizard.png",
}


def _load_script_module() -> ModuleType:
    """Betiği modül olarak yükler; sys.path'i kirletmez ve Qt'yi içe aktarmaz."""
    spec = importlib.util.spec_from_file_location("capture_screenshots", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _read_png_dimensions(path: Path) -> tuple[int, int]:
    """Saf Python ile PNG IHDR başlığından piksel genişlik ve yüksekliğini okur."""
    header = path.read_bytes()[:24]
    assert header[:8] == b"\x89PNG\r\n\x1a\n", f"Geçersiz PNG imzası: {path}"
    assert header[12:16] == b"IHDR", f"IHDR bloğu eksik: {path}"
    width, height = struct.unpack(">II", header[16:24])
    return width, height


def test_parse_args_defaults() -> None:
    """Hiç bayrak verilmediğinde varsayılan değerler TR 1x ve docs/assets/desktop olur."""
    cs = _load_script_module()
    args = cs.parse_args([])
    assert args.scale == 1
    assert args.lang == "tr"
    assert args.extra is False
    assert args.out == REPO / "docs" / "assets" / "desktop"


def test_parse_args_explicit(tmp_path: Path) -> None:
    """Açıkça verilen --scale, --lang, --out ve --extra bayrakları doğru ayrıştırılır."""
    cs = _load_script_module()
    args = cs.parse_args(
        ["--scale", "2", "--lang", "en", "--out", str(tmp_path), "--extra"]
    )
    assert args.scale == 2
    assert args.lang == "en"
    assert args.out == tmp_path
    assert args.extra is True


def test_capture_screenshots_2x_en_extra(tmp_path: Path) -> None:
    """Alt süreçte --scale 2 --lang en --extra tüm 15 PNG'yi ve 2560x1560 welcome.png'yi üretir."""
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--scale",
            "2",
            "--lang",
            "en",
            "--out",
            str(tmp_path),
            "--extra",
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, (
        f"Betik hata ile çıktı ({result.returncode}):\n"
        f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )

    produced = {path.name for path in tmp_path.glob("*.png")}
    assert produced == CORE_PNGS | EXTRA_PNGS
    assert _read_png_dimensions(tmp_path / "welcome.png") == (2560, 1560)
