"""Dondurulmuş Windows paketi (Artemis.exe) için giriş noktası."""

import multiprocessing
import runpy
import sys


def main(argv: list[str] | None = None) -> None:
    """Alt süreç `-m` öykünmesini veya masaüstü/CLI girişini çalıştırır."""
    args = sys.argv[1:] if argv is None else list(argv)
    if args[:1] == ["-m"] and len(args) >= 2:
        module_name = args[1]
        # Güvenlik sınırı: yürütülebilir dosya genel bir Python başlatıcısına dönüşemez.
        if not module_name.startswith("artemis.") or len(module_name) <= len("artemis."):
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
