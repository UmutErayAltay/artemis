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

from jarvis.brain.agent_loop import AgentLoop
from jarvis.brain.errors import ProviderError
from jarvis.brain.providers import build_provider
from jarvis.channels.runner import CommandRunner
from jarvis.config.settings import load_settings
from jarvis.safety.audit import AuditLog
from jarvis.safety.gate import SafetyGate
from jarvis.tools.base import ToolContext
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
    settings.workspace.mkdir(parents=True, exist_ok=True)
    audit = AuditLog(settings.audit_path)
    gate = SafetyGate(discover_tools())
    context = ToolContext(workspace=settings.workspace, settings=settings, audit=audit)

    approval_active = False
    runner: CommandRunner

    def approve(name: str, arguments: dict[str, Any]) -> bool:
        nonlocal approval_active
        approval_active = True
        try:
            return request_approval(
                name,
                arguments,
                gate.pending_notice,
                on_interrupt=runner.cancel_current,
            )
        finally:
            approval_active = False

    runner = CommandRunner(AgentLoop(build_provider(settings, audit), gate, context, approve))
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
        print("Jarvis hazır. Çıkmak için /çık yazın. Onaylarda varsayılan Hayır.")
        while True:
            try:
                text = input("Siz: ")
            except EOFError:
                break
            if text.strip().casefold() in {"/çık", "/cik", "/exit"}:
                break
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
    args = parser.parse_args()
    try:
        asyncio.run(chat(args.config))
    except (OSError, ValueError, ValidationError, yaml.YAMLError, ProviderError) as error:
        # Config doğrulama hatasının ham metni kullanıcı yanlışlıkla sır yazmış olabilir.
        print(
            f"Başlatma/çalıştırma başarısız ({type(error).__name__}). Ayar ve erişimi kontrol edin."
        )
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        print("\nJarvis durduruldu.")
