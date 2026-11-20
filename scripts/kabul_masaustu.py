"""Artemis masaüstü panel ve süreç kabul sürücüsü (gerçek ekran veya --offscreen).

Kullanım:
    python scripts/kabul_masaustu.py [--offscreen] [--timeout SANIYE] [--out DİZİN]

Varsayılan olarak gerçek Windows ekranında çalışır; `--offscreen` verilirse PySide6 içe
aktarılmadan önce `QT_QPA_PLATFORM=offscreen` ayarlanır. Geçici ayar ve durum klasörü,
sahte sağlayıcı ve izole QSettings kullanır; gerçek kullanıcı yapılandırması, kayıt defteri,
kasa, ağ, mikrofon veya ücretli modellere dokunmaz.
"""

from __future__ import annotations

import argparse
import io
import math
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _positive_timeout(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"Geçersiz zaman aşımı değeri: {raw!r}") from error
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError(f"Zaman aşımı sıfırdan büyük olmalı: {raw!r}")
    return value


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Komut satırı seçeneklerini ayrıştırır."""
    parser = argparse.ArgumentParser(
        description="Artemis masaüstü UI ve tek-örnek/kapanış kabul betiği.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Ekran görüntülerinin yazılacağı dizin (varsayılan: kalıcı geçici dizin).",
    )
    parser.add_argument(
        "--offscreen",
        action="store_true",
        help="PySide6'yı offscreen Qt platformunda çalıştırır (QT_QPA_PLATFORM=offscreen).",
    )
    parser.add_argument(
        "--timeout",
        type=_positive_timeout,
        default=300.0,
        help="Tüm kabul çalışması için küresel zaman aşımı (saniye, varsayılan: 300).",
    )
    return parser.parse_args(argv)


def _configure_stdio() -> None:
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if isinstance(sys.stderr, io.TextIOWrapper):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def _print_table(results: list[object], out_dir: Path, *, offscreen: bool) -> tuple[int, int, int]:
    from scripts.kabul_masaustu_checks.common import RowResult

    rows: list[RowResult] = [r for r in results if isinstance(r, RowResult)]
    status_w = max(5, *(len(r.status) for r in rows)) if rows else 5
    id_w = max(5, *(len(r.row_id) for r in rows)) if rows else 5
    shot_texts = [", ".join(r.screenshots) if r.screenshots else "-" for r in rows]
    shot_w = max(14, *(len(s) for s in shot_texts)) if rows else 14

    header = (
        f"{'DURUM':<{status_w}} | {'SATIR':<{id_w}} | "
        f"{'EKRAN GÖRÜNTÜSÜ':<{shot_w}} | GÖZLEM"
    )
    print(header)
    print("-" * len(header))

    passed = failed = skipped = 0
    for row, shots in zip(rows, shot_texts, strict=True):
        clean_obs = " ".join(row.observation.splitlines()).strip()
        print(f"{row.status:<{status_w}} | {row.row_id:<{id_w}} | {shots:<{shot_w}} | {clean_obs}")
        if row.status == "PASS":
            passed += 1
        elif row.status == "FAIL":
            failed += 1
        else:
            skipped += 1

    mode_label = "OFFSCREEN" if offscreen else "GERCEK_EKRAN"
    print(f"\nMOD {mode_label}")
    print(f"OZET pass={passed} fail={failed} skip={skipped}")
    print(f"GORUNTULER {out_dir}")
    return passed, failed, skipped


def main(argv: list[str] | None = None) -> int:
    """Masaüstü kabul denetimlerini gerçek ekranda veya --offscreen ile çalıştırır."""
    _configure_stdio()
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))

    args = parse_args(argv)
    from scripts.kabul_masaustu_checks.common import (
        GlobalTimeoutError,
        RowResult,
        assert_no_leaked_windows,
        configure_qt_platform,
        force_close_all_windows,
        install_safety_guards,
        remove_tree,
    )

    # PySide6 içe aktarılmadan önce platform ayarı uygulanır.
    configure_qt_platform(offscreen=args.offscreen)

    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication, QDialog

    from artemis.channels.desktop.app import _STYLE
    from artemis.i18n import set_language
    from scripts.kabul_masaustu_checks.daily_summary_offline import (
        run_daily_summary_offline_checks,
    )
    from scripts.kabul_masaustu_checks.dictation_panel import run_dictation_panel_checks
    from scripts.kabul_masaustu_checks.panels import run_panel_checks
    from scripts.kabul_masaustu_checks.processes import (
        run_process_checks,
        terminate_tracked_processes,
    )
    from scripts.kabul_masaustu_checks.reminders_live import run_reminders_live_checks
    from scripts.kabul_masaustu_checks.routines_triggers_live import (
        run_routines_triggers_live_checks,
    )
    from scripts.kabul_masaustu_checks.search_research_memory import (
        run_search_research_memory_checks,
    )
    from scripts.kabul_masaustu_checks.triggers_files import run_triggers_and_files_checks

    out_dir = (
        args.out.resolve()
        if args.out is not None
        else Path(tempfile.mkdtemp(prefix="artemis-kabul-masaustu-"))
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp_root = Path(tempfile.mkdtemp(prefix="artemis-kabul-calisma-"))

    app = QApplication.instance() or QApplication([])
    assert isinstance(app, QApplication)
    if args.offscreen and app.platformName() != "offscreen":
        raise RuntimeError(
            f"--offscreen verildi ancak Qt platformu {app.platformName()!r} açıldı."
        )
    if not args.offscreen and app.platformName() == "offscreen":
        raise RuntimeError("Kabul betiği --offscreen verilmeden offscreen platformda çalışamaz.")

    app.setQuitOnLastWindowClosed(False)
    app.setFont(QFont("Segoe UI", 10))
    app.setStyleSheet(_STYLE)
    set_language("tr")
    install_safety_guards(tmp_root / "qsettings")

    deadline = time.monotonic() + args.timeout
    timed_out = False

    def _on_qt_timeout() -> None:
        modal = app.activeModalWidget()
        if isinstance(modal, QDialog):
            modal.reject()
        terminate_tracked_processes(timeout=2.0)

    def _on_hard_timeout() -> None:
        terminate_tracked_processes(timeout=2.0)
        print(
            f"ZAMAN ASIMI: kabul betiği {args.timeout:.1f}s sınırını aştı.",
            file=sys.stderr,
        )
        sys.stderr.flush()
        os._exit(124)

    qt_timer = QTimer(app)
    qt_timer.setSingleShot(True)
    qt_timer.timeout.connect(_on_qt_timeout)
    qt_timer.start(max(1, int(args.timeout * 1000)))

    hard_timer = threading.Timer(args.timeout + 5.0, _on_hard_timeout)
    hard_timer.daemon = True
    hard_timer.start()

    results: list[RowResult] = []
    try:
        results.extend(run_panel_checks(app, tmp_root, out_dir, deadline=deadline))
        assert_no_leaked_windows(app, "panel grubu sonrası", max_visible=2, max_desktop=0)
        results.extend(run_triggers_and_files_checks(app, tmp_root, out_dir, deadline=deadline))
        assert_no_leaked_windows(
            app, "tetikleyici/dosya grubu sonrası", max_visible=2, max_desktop=0
        )
        results.extend(run_dictation_panel_checks(app, tmp_root, out_dir, deadline=deadline))
        assert_no_leaked_windows(
            app, "dikte paneli grubu sonrası", max_visible=2, max_desktop=0
        )
        results.extend(
            run_routines_triggers_live_checks(app, tmp_root, out_dir, deadline=deadline)
        )
        assert_no_leaked_windows(
            app, "canlı rutin/tetikleyici grubu sonrası", max_visible=2, max_desktop=0
        )
        results.extend(
            run_search_research_memory_checks(app, tmp_root, out_dir, deadline=deadline)
        )
        assert_no_leaked_windows(
            app, "arama/araştırma/hafıza grubu sonrası", max_visible=2, max_desktop=0
        )
        results.extend(run_reminders_live_checks(app, tmp_root, out_dir, deadline=deadline))
        assert_no_leaked_windows(app, "hatırlatma grubu sonrası", max_visible=2, max_desktop=0)
        results.extend(
            run_daily_summary_offline_checks(app, tmp_root, out_dir, deadline=deadline)
        )
        assert_no_leaked_windows(app, "gün özeti grubu sonrası", max_visible=2, max_desktop=0)
        results.extend(
            run_process_checks(
                tmp_root,
                out_dir,
                offscreen=args.offscreen,
                deadline=deadline,
            )
        )
    except GlobalTimeoutError as error:
        timed_out = True
        print(f"ZAMAN ASIMI: {error}", file=sys.stderr)
    finally:
        qt_timer.stop()
        hard_timer.cancel()
        terminate_tracked_processes(timeout=5.0)
        force_close_all_windows(app)
        try:
            remove_tree(tmp_root)
        except OSError as error:
            print(f"Temizleme uyarısı: {error}", file=sys.stderr)

    _passed, failed, _skipped = _print_table(results, out_dir, offscreen=args.offscreen)
    if timed_out:
        return 124
    assert_no_leaked_windows(app, "çıkış", max_visible=2, max_desktop=0)
    return 1 if failed > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
