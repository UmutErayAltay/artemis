"""Dondurulmuş Windows paketi (Artemis.exe) için giriş noktası."""

import multiprocessing
import runpy
import sys
from pathlib import Path

# Güvenlik nedeniyle yalnızca tekil olarak izin verilen artemis dışı `-m` modülleri.
_IZINLI_MODULLER = frozenset({"bench.model_testi"})


def _use_console_sibling() -> None:
    """Pencereli Artemis.exe'de stdout/stdin yoktur: `sys.executable -m ...` ile başlayan ve stdio
    üzerinden konuşan alt süreçler sessizce kırılırdı. Yanındaki konsol exe'sini (artemis-cli.exe)
    alt süreç başlatıcısı yapar; konsol penceresi açılmasını çağıranlar CREATE_NO_WINDOW ile önler.
    """
    if not getattr(sys, "frozen", False) or sys.stdout is not None:
        return
    sibling = Path(sys.executable).with_name("artemis-cli.exe")
    if sibling.is_file():
        sys.executable = str(sibling)


def main(argv: list[str] | None = None) -> None:
    """Alt süreç `-m` öykünmesini veya masaüstü/CLI girişini çalıştırır."""
    _use_console_sibling()
    args = sys.argv[1:] if argv is None else list(argv)
    if args[:1] == ["-m"] and len(args) >= 2:
        module_name = args[1]
        # Güvenlik sınırı: yürütülebilir dosya genel bir Python başlatıcısına dönüşemez.
        is_artemis_submodule = module_name.startswith("artemis.") and len(module_name) > len(
            "artemis."
        )
        if not is_artemis_submodule and module_name not in _IZINLI_MODULLER:
            raise SystemExit("Bilinmeyen modül")
        sys.argv = [module_name, *args[2:]]
        runpy.run_module(module_name, run_name="__main__", alter_sys=True)
        return

    if not args:
        args = ["desktop"]

    # `-m` alt süreçlerinde ağır CLI/UI bağımlılıklarını yüklememek için tembel içe aktarım.
    from artemis.channels.cli import main as cli_main

    sys.argv = ["Artemis", *args]
    cli_main()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
