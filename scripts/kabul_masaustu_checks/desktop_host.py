"""Artemis masaüstü kabul test konağı (gerçek ekran veya --offscreen)."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
from pathlib import Path
from typing import Any


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Artemis masaüstü kabul test konağı")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--qsettings", type=Path, required=True)
    parser.add_argument("--state-file", type=Path, required=True)
    parser.add_argument("--hide-file", type=Path, required=True)
    parser.add_argument("--quit-file", type=Path, required=True)
    parser.add_argument("--shot-request", type=Path, required=True)
    parser.add_argument("--shot-out", type=Path, required=True)
    parser.add_argument("--offscreen", action="store_true")
    return parser.parse_args()


def _write_state_atomic(state_file: Path, payload: dict[str, Any]) -> None:
    """Ana süreç yarım JSON okumasın diye geçici dosyadan atomik değiştirir."""
    state_file.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = state_file.with_name(f"{state_file.name}.{os.getpid()}.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp_path, state_file)


def _resolve_shot_target(shot_out: Path, raw_target: str) -> tuple[Path, str]:
    """Tek veya çoklu ekran görüntüsü hedefini `--shot-out` ve istek içeriğinden çözer."""
    is_file = shot_out.suffix.lower() == ".png"
    base_dir = shot_out.parent if is_file else shot_out
    default_name = shot_out.name if is_file else "shot.png"
    if not raw_target:
        return base_dir, default_name
    target = Path(raw_target)
    if target.is_absolute():
        return target.parent, target.name
    return base_dir, raw_target


def main() -> int:
    args = _parse_args()
    # PySide6 yüklenmeden önce platform eklentisi ve depo import yolu ayarlanır.
    repo_root = Path(__file__).resolve().parents[2]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    from scripts.kabul_masaustu_checks.common import (
        RecordingProvider,
        configure_qt_platform,
        fake_provider_bridge,
        grab_neutralized,
        install_safety_guards,
    )

    configure_qt_platform(offscreen=args.offscreen)

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    from artemis.channels.desktop.app import DesktopWindow, run_desktop

    app = QApplication([])
    if args.offscreen:
        assert app.platformName() == "offscreen", "Masaüstü konağı offscreen platformu kuramadı."
    else:
        assert app.platformName() != "offscreen", (
            "Masaüstü konağı offscreen platformunda çalışamaz."
        )
    install_safety_guards(args.qsettings)

    def _on_tick() -> None:
        window = next(
            (w for w in QApplication.topLevelWidgets() if isinstance(w, DesktopWindow)),
            None,
        )
        if window is None:
            return

        if args.hide_file.exists():
            args.hide_file.unlink(missing_ok=True)
            # Tepsi varsa gerçek closeEvent gizleme yolu denenir; yoksa süreç kapanmasın.
            if window._tray is not None:
                window.close()
            else:
                window.hide()

        if args.shot_request.exists():
            raw_target = args.shot_request.read_text(encoding="utf-8").strip()
            args.shot_request.unlink(missing_ok=True)
            window.pet.hide()
            app.processEvents()
            target_dir, target_name = _resolve_shot_target(args.shot_out, raw_target)
            try:
                saved = grab_neutralized(
                    app,
                    window,
                    target_dir,
                    target_name,
                    real_roots=[args.config.parent.parent],
                )
            except (AssertionError, OSError) as error:
                args.shot_request.with_suffix(".error").write_text(str(error), encoding="utf-8")
            else:
                args.shot_request.with_suffix(".done").write_text(str(saved), encoding="utf-8")

        if args.quit_file.exists():
            args.quit_file.unlink(missing_ok=True)
            if window.settings_panel.has_unsaved:
                window.settings_panel._set_dirty(False)
            window._request_exit()

        payload = {
            "pid": os.getpid(),
            "platform": app.platformName(),
            "worker_ready": bool(window._worker_ready),
            "visible": bool(window.isVisible()),
            "active": bool(window.isActiveWindow()),
            "tray": window._tray is not None,
            "status": window.status_label.text(),
        }
        # Windows'ta okuyucu dosya tutarken oluşan anlık kilit bir sonraki tikte düzelir.
        with contextlib.suppress(OSError):
            _write_state_atomic(args.state_file, payload)

    timer = QTimer(app)
    timer.setInterval(100)
    timer.timeout.connect(_on_tick)
    timer.start()

    with fake_provider_bridge(RecordingProvider):
        return run_desktop(args.config)


if __name__ == "__main__":
    raise SystemExit(main())
