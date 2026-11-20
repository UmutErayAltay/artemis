"""Ekran görüntüsü yakalama betiğinin CLI ayrıştırma ve 2x EN uçtan uca testleri."""

from __future__ import annotations

import importlib.util
import os
import struct
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

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
    pytest.importorskip("PySide6")  # betik alt süreçte Qt ister; Qt'siz CI işinde atlanır
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

    # Her üretilen PNG çift boyutlu ve pozitif olmalı; pencere/diyalog çekimleri tam 2x boyuttadır.
    for png_name in produced:
        width, height = _read_png_dimensions(tmp_path / png_name)
        assert width > 0 and height > 0, png_name
        assert width % 2 == 0 and height % 2 == 0, (png_name, width, height)

    expected_2x = {
        "welcome.png": (2560, 1560),
        "panel.png": (2560, 1560),
        "approval.png": (1120, 760),
        "overlay.png": (1040, 520),
        "projects.png": (2160, 1840),
        "connect-wizard.png": (1520, 1280),
        "settings-ses.png": (2560, 1560),
        "settings-telegram.png": (2560, 1560),
        "settings-model.png": (2560, 1560),
        "settings-routines.png": (2560, 1560),
        "settings-calendar.png": (2560, 1560),
        "settings-look.png": (2560, 1560),
        "settings-about.png": (2560, 1560),
    }
    for png_name, size in expected_2x.items():
        assert _read_png_dimensions(tmp_path / png_name) == size, png_name

    auto_w, auto_h = _read_png_dimensions(tmp_path / "auto-warning.png")
    assert auto_w > 400 and auto_h > 300, (auto_w, auto_h)


def test_about_screenshot_label_has_no_real_user_path(tmp_path: Path) -> None:
    """Hakkında ekran görüntüsü için ayarlanan klasör metni gerçek kullanıcı yolunu içermez."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")

    from PySide6.QtWidgets import QApplication

    from artemis.channels.desktop.settings_panel import SettingsPanel
    from artemis.i18n import set_language

    cs = _load_script_module()
    app = QApplication.instance() or QApplication([])
    del app
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    demo_cfg = cfg_dir / "demo.yaml"
    demo_cfg.write_bytes((REPO / "config" / "config.example.yaml").read_bytes())

    panel = SettingsPanel(demo_cfg, demo=True)
    try:
        for lang in ("tr", "en"):
            set_language(lang)
            cs.neutralize_captured_paths(panel)
            text = panel.about_section.config_label.text()
            assert cs.DEMO_CONFIG_DIR in text
            assert str(tmp_path) not in text
            assert str(Path.home()) not in text
            assert panel.repos_dir.placeholderText() == cs.DEMO_REPOS_DIR
    finally:
        set_language("tr")
        panel.deleteLater()
