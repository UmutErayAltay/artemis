"""CLI onay politikasını belirlemez; SafetyGate'in istediği içeriği gösterir."""

import argparse
import asyncio
import json
import signal
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from jarvis.brain.errors import ProviderError
from jarvis.brain.providers import build_provider
from jarvis.channels.runtime import Runtime, create_runtime
from jarvis.config.settings import load_settings
from jarvis.tools.registry import discover_tools


def request_approval(
    tool_name: str,
    arguments: dict[str, Any],
    notice: str = "",
    *,
    on_interrupt: Callable[[], Any] | None = None,
) -> bool:
    if notice:
        print(notice)
    print(f"Onay isteniyor: {tool_name}")
    print(json.dumps(arguments, ensure_ascii=False, indent=2))
    try:
        answer = input("Onaylıyor musunuz? [evet/Hayır]: ")
    except EOFError:
        print("\nOnay verilmedi.")
        return False
    except KeyboardInterrupt:
        print("\nOnay verilmedi; tur iptal ediliyor.")
        if on_interrupt is not None:
            on_interrupt()
        return False
    return answer.strip().casefold() == "evet"


async def chat(config_path: Path) -> None:
    settings = load_settings(config_path)
    if not settings.primary_model:
        print("Config içinde resmi katalogdan doğrulanmış bir primary_model tanımlayın.")
        return
    approval_active = False
    runtime_ref: list[Runtime] = []

    def approve(name: str, arguments: dict[str, Any]) -> bool:
        nonlocal approval_active
        approval_active = True
        try:
            return request_approval(
                name,
                arguments,
                runtime_ref[0].gate.pending_notice,
                on_interrupt=runtime_ref[0].runner.cancel_current,
            )
        finally:
            approval_active = False

    runtime = create_runtime(
        config_path,
        approve,
        settings=settings,
        provider_builder=build_provider,
        registry_builder=discover_tools,
    )
    runtime_ref.append(runtime)
    runner, context = runtime.runner, runtime.context
    loop = asyncio.get_running_loop()
    # Runner'ın SIGINT sayacı ikinci Ctrl+C'de loop'u kapatır. Kendi handler'ımız
    # aktif turu iptal işaretler; onay input'unda KeyboardInterrupt ret yolunu kullanır.
    previous_sigint = signal.getsignal(signal.SIGINT)

    def handle_sigint(_signum: int, _frame: Any) -> None:
        if approval_active:
            raise KeyboardInterrupt
        if runner.cancel_current():
            # Sinyal işleyicisi ana thread'de olsa da self-pipe ile selector uyandırılır.
            loop.call_soon_threadsafe(context.cancelled.set)
            return
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, handle_sigint)
    try:
        print(
            "Jarvis hazır. Çıkmak için /çık, yeni sohbet için /yeni yazın. "
            "Onaylarda varsayılan Hayır."
        )
        while True:
            try:
                text = input("Siz: ")
            except EOFError:
                break
            if text.strip().casefold() in {"/çık", "/cik", "/exit"}:
                break
            if text.strip().casefold() == "/yeni":
                # Komut tur çalıştırmaz ve modele gitmez; yalnız geçmişi unutturur.
                if runner.reset_context():
                    print("Yeni sohbet başladı; önceki konuşma unutuldu.")
                else:
                    print("Aktif işlem sürerken sohbet sıfırlanamaz.")
                continue
            turn_task = asyncio.create_task(runner.run(text))
            result = await asyncio.shield(turn_task)
            print(f"Jarvis: {result.text}")
    finally:
        signal.signal(signal.SIGINT, previous_sigint)


def main() -> None:
    parser = argparse.ArgumentParser(description="Türkçe güvenli masaüstü asistanı")
    commands = parser.add_subparsers(dest="command", required=True)
    chat_parser = commands.add_parser("chat", help="CLI sohbetini başlat")
    chat_parser.add_argument("--config", type=Path, default=Path("config/config.yaml"))
    voice_parser = commands.add_parser(
        "voice", help="Masaüstünü ses açık başlatır (`desktop --ses` ile aynı)"
    )
    voice_parser.add_argument("--config", type=Path, default=Path("config/config.yaml"))
    desktop_parser = commands.add_parser("desktop", help="Masaüstü panelini başlat")
    desktop_parser.add_argument("--config", type=Path, default=Path("config/config.yaml"))
    desktop_parser.add_argument(
        "--demo", action="store_true", help="Gerçek araç kullanmayan çevrimdışı demoyu aç"
    )
    desktop_parser.add_argument(
        "--ses",
        action="store_true",
        help="Panel açılınca sesi de aç (mikrofon yalnız bu bayrakla veya Ses düğmesiyle açılır)",
    )
    telegram_parser = commands.add_parser(
        "telegram", help="Telegram kanalını başlat (yalnız yapılandırılmış özel sohbet, ADR 0016)"
    )
    telegram_parser.add_argument("--config", type=Path, default=Path("config/config.yaml"))
    args = parser.parse_args()
    try:
        if args.command in {"voice", "desktop"}:
            try:
                from jarvis.channels.desktop.app import run_desktop
            except ImportError:
                print(
                    "Masaüstü bileşeni kurulu değil. `pip install 'jarvis-tr[desktop]'` "
                    "komutuyla isteğe bağlı GUI paketini kurun. Ses de masaüstü içinde çalışır."
                )
                raise SystemExit(2) from None
            # Ses artık ayrı terminal döngüsü değil; onay yalnız ekrandaki pencereden gelir
            # (ADR 0014). `jarvis voice`, `jarvis desktop --ses` ile aynı yoldur.
            use_voice = args.command == "voice" or args.ses
            demo = args.command == "desktop" and args.demo
            raise SystemExit(run_desktop(args.config, demo=demo, voice=use_voice))
        elif args.command == "telegram":
            # Lazy import: sohbet/ses kullanıcıları Telegram modüllerini yüklemez.
            from jarvis.channels.telegram.channel import run_telegram

            raise SystemExit(asyncio.run(run_telegram(args.config)))
        else:
            asyncio.run(chat(args.config))
    except (OSError, ValueError, ValidationError, yaml.YAMLError, ProviderError) as error:
        # Config doğrulama hatasının ham metni kullanıcı yanlışlıkla sır yazmış olabilir.
        print(
            f"Başlatma/çalıştırma başarısız ({type(error).__name__}). Ayar ve erişimi kontrol edin."
        )
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        print("\nJarvis durduruldu.")
