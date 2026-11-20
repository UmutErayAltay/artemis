"""Masaüstü kabul betiği CLI ayrıştırma ve yol maskeleme birim testleri (ekransız)."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType

import pytest

from scripts.kabul_masaustu_checks.common import (
    DEMO_DOCS_DIR,
    REPO,
    GlobalTimeoutError,
    MutableClock,
    ToastCapture,
    _intercept_toast,
    check_deadline,
    fake_global_hotkeys,
    intercept_toasts,
    read_audit_events,
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


def test_mutable_clock_requires_aware_and_advances() -> None:
    """MutableClock yalnız saat dilimli zaman kabul eder ve UTC olarak ilerler."""
    with pytest.raises(ValueError):
        MutableClock(datetime(2026, 10, 15, 10, 0))

    tz_plus_3 = timezone(timedelta(hours=3))
    clock = MutableClock(datetime(2026, 10, 15, 10, 0, tzinfo=tz_plus_3))
    assert clock() == datetime(2026, 10, 15, 7, 0, tzinfo=UTC)

    advanced = clock.advance(minutes=2, seconds=30)
    assert advanced == datetime(2026, 10, 15, 7, 2, 30, tzinfo=UTC)
    assert clock() == advanced

    with pytest.raises(ValueError):
        clock.set(datetime(2026, 10, 15, 12, 0))

    clock.set(datetime(2026, 10, 15, 11, 0, tzinfo=tz_plus_3))
    assert clock() == datetime(2026, 10, 15, 8, 0, tzinfo=UTC)


def test_intercept_toasts_captures_and_restores() -> None:
    """intercept_toasts yakalama sırasında yükleri toplar ve çıkışta eski duruma döner."""
    outside_before = _intercept_toast("Başlık", "Gövde")
    assert outside_before.status == "skipped"

    with intercept_toasts("ok") as captured:
        inside = _intercept_toast("Hatırlatma", "Su iç")
        assert inside.status == "ok"
        assert captured == [ToastCapture(title="Hatırlatma", body="Su iç")]

    outside_after = _intercept_toast("Başlık", "Gövde")
    assert outside_after.status == "skipped"


def test_read_audit_events_filters_and_skips_invalid(tmp_path: Path) -> None:
    """read_audit_events eksik dosyada boş döner, bozuk satırları atlar ve olaya göre süzer."""
    audit_path = tmp_path / "audit.jsonl"
    assert read_audit_events(audit_path) == []

    lines = [
        json.dumps({"event": "turn_start", "turn_id": "t1"}),
        "{bozuk json",
        json.dumps(["liste", "olay", "degil"]),
        "",
        json.dumps({"event": "proactive_delivery", "delivered": True}),
    ]
    audit_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    all_events = read_audit_events(audit_path)
    assert len(all_events) == 2
    assert read_audit_events(audit_path, "proactive_delivery") == [
        {"event": "proactive_delivery", "delivered": True}
    ]


def test_fake_global_hotkeys_validates_and_records() -> None:
    """fake_global_hotkeys Win32 kaydı yapmadan parse_hotkey doğrulamasını ve start/stop'u izler."""
    pytest.importorskip("PySide6")
    import artemis.channels.desktop.app as app_mod

    orig_cls = app_mod.GlobalHotkey
    with fake_global_hotkeys() as recorder:
        with pytest.raises(ValueError):
            app_mod.GlobalHotkey("invalid_key_xyz", lambda: None)

        hk = app_mod.GlobalHotkey("ctrl+alt+k", lambda: None)
        assert hk.start() is True
        hk.stop()
        hk.stop()
        assert recorder.started == ["ctrl+alt+k"]
        assert recorder.stopped == ["ctrl+alt+k"]

    assert app_mod.GlobalHotkey is orig_cls
