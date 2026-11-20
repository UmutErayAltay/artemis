"""Masaüstü kabul betiği CLI ayrıştırma ve yol maskeleme birim testleri (ekransız)."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import ModuleType

import pytest

from scripts.kabul_masaustu_checks.common import (
    DEMO_DOCS_DIR,
    REPO,
    GlobalTimeoutError,
    check_deadline,
    sanitize_text_paths,
)

SCRIPT = REPO / "scripts" / "kabul_masaustu.py"


def _load_driver() -> ModuleType:
    spec = importlib.util.spec_from_file_location("kabul_masaustu", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_help_exits_zero() -> None:
    """`--help` çağrısı Qt başlatmadan sıfır çıkış koduyla seçenekleri yazdırır."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        cwd=REPO,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    for flag in ("--out", "--offscreen", "--timeout"):
        assert flag in result.stdout


def test_parse_args_defaults_and_flags(tmp_path: Path) -> None:
    """Varsayılanlar (`offscreen=False`, `timeout=300`) ve açık bayraklar ayrıştırılır."""
    mod = _load_driver()
    defaults = mod.parse_args([])
    assert defaults.out is None
    assert defaults.offscreen is False
    assert defaults.timeout == 300.0

    parsed = mod.parse_args(["--out", str(tmp_path), "--offscreen", "--timeout", "45"])
    assert parsed.out == tmp_path
    assert parsed.offscreen is True
    assert parsed.timeout == 45.0


@pytest.mark.parametrize("raw_timeout", ["0", "-5", "nan", "inf"])
def test_parse_args_rejects_invalid_timeout(raw_timeout: str) -> None:
    """Geçersiz `--timeout` değerleri argparse tarafından reddedilir."""
    mod = _load_driver()
    with pytest.raises(SystemExit):
        mod.parse_args(["--timeout", raw_timeout])


def test_check_deadline_raises_when_expired() -> None:
    """Geçmiş bir bitiş zamanı GlobalTimeoutError yükseltir; gelecekteki geçer."""
    check_deadline(None, "sınırsız")
    check_deadline(time.monotonic() + 60.0, "gelecek")
    with pytest.raises(GlobalTimeoutError):
        check_deadline(time.monotonic() - 1.0, "süre doldu")


def test_sanitize_text_paths_removes_real_user_and_temp_paths(tmp_path: Path) -> None:
    """Gerçek ev/geçici/depo yolları görüntü maskelemesinde kurmaca demo yoluna döner."""
    docs_dir = tmp_path / "belgeler"
    docs_dir.mkdir()
    sample = (
        f"Klasörler: {docs_dir} | {docs_dir.as_posix()} | "
        f"{Path.home()} | {tempfile.gettempdir()} | {REPO}"
    )
    cleaned = sanitize_text_paths(sample, real_roots={docs_dir: DEMO_DOCS_DIR})
    assert DEMO_DOCS_DIR in cleaned
    for forbidden in (str(docs_dir), str(Path.home()), tempfile.gettempdir(), str(REPO)):
        assert forbidden.casefold() not in cleaned.casefold()
