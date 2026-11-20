"""CLI onay politikasını belirlemez; SafetyGate'in istediği içeriği gösterir."""

import argparse
import asyncio
import contextlib
import json
import signal
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from artemis.brain.errors import ProviderError
from artemis.brain.providers import build_provider
from artemis.channels.autostart import Autostart
from artemis.channels.notice import format_approval_notice
from artemis.channels.runtime import Runtime, create_runtime
from artemis.config.paths import default_config_path, ensure_config
from artemis.config.settings import load_settings
from artemis.i18n import current_language, set_language, t
from artemis.safety.audit import AuditLog
from artemis.tools.registry import discover_tools


def _apply_config_language(config_path: Path) -> None:
    """Config geçerliyse arayüz dilini yükler; okunamazsa mevcut dil korunur."""
    with contextlib.suppress(OSError, ValueError, ValidationError, yaml.YAMLError):
        set_language(load_settings(config_path).language)


def request_approval(
    tool_name: str,
    arguments: dict[str, Any],
    notice: str = "",
    *,
    on_interrupt: Callable[[], Any] | None = None,
) -> bool:
    if notice:
        print(notice)
    print(t("Onay isteniyor: {tool_name}").format(tool_name=tool_name))
    print(json.dumps(arguments, ensure_ascii=False, indent=2))
    try:
        answer = input(t("Onaylıyor musunuz? [evet/Hayır]: "))
    except EOFError:
        print("\n" + t("Onay verilmedi."))
        return False
    except KeyboardInterrupt:
        print("\n" + t("Onay verilmedi; tur iptal ediliyor."))
        if on_interrupt is not None:
            on_interrupt()
        return False
    # Onay sözcüğü: Türkçede yalnız "evet"; İngilizce arayüzde "yes" de (Umut, 2026-11-20).
    # Boş, başka bir sözcük ya da "y" gibi kısaltma her dilde hayırdır.
    accepted = {"evet", "yes"} if current_language() == "en" else {"evet"}
    return answer.strip().casefold() in accepted


async def chat(config_path: Path) -> None:
    settings = load_settings(config_path)
    if not settings.primary_model:
        print(t("Config içinde resmi katalogdan doğrulanmış bir primary_model tanımlayın."))
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
                format_approval_notice(runtime_ref[0].gate.pending_notice),
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
            t(
                "Artemis hazır. Çıkmak için /çık, yeni sohbet için /yeni yazın. "
                "Onaylarda varsayılan Hayır."
            )
        )
        while True:
            try:
                text = input(t("Siz: "))
            except EOFError:
                break
            if text.strip().casefold() in {"/çık", "/cik", "/exit"}:
                break
            if text.strip().casefold() == "/yeni":
                # Komut tur çalıştırmaz ve modele gitmez; yalnız geçmişi unutturur.
                if runner.reset_context():
                    print(t("Yeni sohbet başladı; kalıcı konuşma arşivi silinmedi."))
                else:
                    print(t("Aktif işlem sürerken sohbet sıfırlanamaz."))
                continue
            if text.strip().casefold().startswith("/hafıza"):
                from artemis.tools.plugins.history import HistoryArguments

                parts = text.strip().casefold().split()
                action = parts[1] if len(parts) > 1 else "durum"
                actions = {
                    "durum": "status",
                    "göster": "list",
                    "sil": "delete",
                    "sakla": "retention",
                }
                if action not in actions or (
                    action == "sakla"
                    and (len(parts) != 3 or not parts[2].isdigit() or int(parts[2]) > 36500)
                ):
                    print(
                        t(
                            "/hafıza durum | göster | sil | sakla <gün>; "
                            "0 otomatik silmeyi kapatır."
                        )
                    )
                    continue
                arguments = HistoryArguments.model_validate(
                    {
                        "action": actions[action],
                        "retention_days": int(parts[2]) if action == "sakla" else 0,
                    }
                )
                history_result = await runner.dispatch_tool("conversation_history", arguments)
                print(history_result.message)
                print(json.dumps(history_result.data, ensure_ascii=False, indent=2))
                continue
            turn_task = asyncio.create_task(runner.run(text))
            result = await asyncio.shield(turn_task)
            print(f"Artemis: {result.text}")
    finally:
        signal.signal(signal.SIGINT, previous_sigint)
        runtime.shutdown()


def autostart(config_path: Path, action: str) -> int:
    """Kayıt yalnız bu komutla veya tepsi menüsüyle değişir; model araçla değiştiremez."""
    settings = load_settings(config_path)
    manager = Autostart(config_path, emit=AuditLog(settings.audit_path).emit)
    if action == "ac":
        result = manager.enable()
    elif action == "kapat":
        result = manager.disable()
    else:
        result = manager.status()
    print(result.message)
    return 0 if result.success else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Türkçe güvenli masaüstü asistanı")
    commands = parser.add_subparsers(dest="command", required=True)
    chat_parser = commands.add_parser("chat", help="CLI sohbetini başlat")
    chat_parser.add_argument("--config", type=Path, default=None)
    voice_parser = commands.add_parser(
        "voice", help="Masaüstünü ses açık başlatır (`desktop --ses` ile aynı)"
    )
    voice_parser.add_argument("--config", type=Path, default=None)
    desktop_parser = commands.add_parser("desktop", help="Masaüstü panelini başlat")
    desktop_parser.add_argument("--config", type=Path, default=None)
    desktop_parser.add_argument(
        "--demo", action="store_true", help="Gerçek araç kullanmayan çevrimdışı demoyu aç"
    )
    desktop_parser.add_argument(
        "--ses",
        action="store_true",
        help="Panel açılınca sesi de aç (mikrofon yalnız bu bayrakla veya Ses düğmesiyle açılır)",
    )
    desktop_parser.add_argument(
        "--arka-plan",
        dest="background",
        action="store_true",
        help="Paneli açmadan tepside başlat (Windows açılışı bunu kullanır, ADR 0030)",
    )
    autostart_parser = commands.add_parser(
        "autostart", help="Windows açılışında tepside başlamayı aç/kapat (ADR 0030)"
    )
    autostart_parser.add_argument("action", choices=["ac", "kapat", "durum"])
    autostart_parser.add_argument("--config", type=Path, default=None)
    telegram_parser = commands.add_parser(
        "telegram", help="Telegram kanalını başlat (yalnız yapılandırılmış özel sohbet, ADR 0016)"
    )
    telegram_parser.add_argument("--config", type=Path, default=None)
    args = parser.parse_args()
    from artemis.tools.credentials import load_into_environment

    load_into_environment()
    try:
        config = args.config or default_config_path()
        created = args.config is None and ensure_config(config)
        _apply_config_language(config)
        if created:
            print(
                t(
                    "İlk açılış: ayarlar {config} konumunda oluşturuldu; "
                    "modeli Ayarlar > Model > Bağlantılar'dan ekleyin."
                ).format(config=config),
                file=sys.stderr,
            )
        if args.command in {"voice", "desktop"}:
            try:
                from artemis.channels.desktop.app import run_desktop
            except ImportError:
                print(
                    t(
                        "Masaüstü bileşeni kurulu değil. `pip install 'artemis-tr[desktop]'` "
                        "komutuyla isteğe bağlı GUI paketini kurun. Ses de masaüstü içinde çalışır."
                    )
                )
                raise SystemExit(2) from None
            # Ses artık ayrı terminal döngüsü değil; onay yalnız ekrandaki pencereden gelir
            # (ADR 0014). `artemis voice`, `artemis desktop --ses` ile aynı yoldur.
            use_voice = args.command == "voice" or args.ses
            demo = args.command == "desktop" and args.demo
            background = args.command == "desktop" and args.background
            raise SystemExit(
                run_desktop(config, demo=demo, voice=use_voice, background=background)
            )
        elif args.command == "autostart":
            raise SystemExit(autostart(config, args.action))
        elif args.command == "telegram":
            # Lazy import: sohbet/ses kullanıcıları Telegram modüllerini yüklemez.
            from artemis.channels.telegram.channel import run_telegram

            raise SystemExit(asyncio.run(run_telegram(config)))
        else:
            asyncio.run(chat(config))
    except (OSError, ValueError, ValidationError, yaml.YAMLError, ProviderError) as error:
        # Config doğrulama hatasının ham metni kullanıcı yanlışlıkla sır yazmış olabilir.
        print(
            t(
                "Başlatma/çalıştırma başarısız ({error_type}). Ayar ve erişimi kontrol edin."
            ).format(error_type=type(error).__name__)
        )
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        print("\n" + t("Artemis durduruldu."))
