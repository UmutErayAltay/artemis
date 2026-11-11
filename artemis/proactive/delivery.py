"""Proaktif olayın sonucunu sabit hedeflere yazar (ADR 0027).

Teslim kanalın kendi çıktısıdır, model araç çağrısı değildir: Telegram kanalının yanıtını
göndermesi gibi. Hedefleri model seçemez: panel ve toast yereldir, Telegram yalnız
config'teki `telegram.chat_id`'ye gider. Gizli içerikli sonuç buluta gönderilmez.
Hiçbir hedef başarıyı doğrulamadıysa teslim başarısızdır; sessizce "teslim edildi" denmez.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from artemis.channels.telegram.api import TelegramClient, TelegramError
from artemis.channels.telegram.channel import split_text
from artemis.config.settings import Settings
from artemis.safety.audit import AuditLog
from artemis.tools.toast import ToastOutcome, show_toast

# Panele (Qt kuyruğuna) başlık ve gövde ekler; eklenemezse False döner.
PanelSink = Callable[[str, str], bool]
# Aynısı, ek olarak panelde "Çalıştır" düğmesi gösterilecek rutin adıyla (ADR 0058).
RoutinePanelSink = Callable[[str, str, str], bool]
ToastFunction = Callable[[str, str], ToastOutcome]

PRIVATE_WITHHELD = (
    "Artemis kendiliğinden bir mesaj hazırladı; gizli içerik (dosya/pano) taşıdığı için "
    "Telegram'a gönderilmedi. Masaüstü panelinde görebilirsiniz."
)


@dataclass(frozen=True)
class DeliveryOutcome:
    delivered: bool
    # Hedef -> "ok" | "error" | "disabled" | "withheld"
    targets: dict[str, str]


class Deliverer:
    def __init__(
        self,
        settings: Settings,
        audit: AuditLog,
        *,
        panel: PanelSink | None = None,
        routine_panel: RoutinePanelSink | None = None,
        toast: ToastFunction | None = show_toast,
        telegram: TelegramClient | None = None,
    ) -> None:
        self.settings = settings
        self.audit = audit
        self.panel = panel
        self.routine_panel = routine_panel
        self.toast = toast
        self.telegram = telegram

    async def deliver(
        self,
        *,
        event_id: str,
        scheduled_for: datetime,
        kind: str,
        title: str,
        body: str,
        private: bool,
        routine: str = "",
    ) -> DeliveryOutcome:
        targets: dict[str, str] = {}
        if routine and self.routine_panel is not None:
            targets["panel"] = "ok" if self.routine_panel(title, body, routine) else "error"
        elif self.panel is not None:
            targets["panel"] = "ok" if self.panel(title, body) else "error"
        if self.toast is not None:
            outcome = await asyncio.to_thread(self.toast, title, body)
            targets["toast"] = outcome.status
        chat_id = self.settings.telegram.chat_id
        if self.telegram is not None and chat_id is not None and self.settings.proactive.telegram:
            targets["telegram"] = await asyncio.to_thread(
                self._send_telegram, chat_id, title, body, private
            )
        delivered = any(status == "ok" for status in targets.values())
        self.audit.emit(
            "proactive_delivery",
            event_id=event_id,
            scheduled_for=scheduled_for.isoformat(),
            kind=kind,
            title=title,
            body=body,
            private=private,
            targets=targets,
            delivered=delivered,
        )
        return DeliveryOutcome(delivered, targets)

    def _send_telegram(self, chat_id: int, title: str, body: str, private: bool) -> str:
        client = self.telegram
        if client is None:  # deliver() kontrol eder; mypy için dürüst dal
            return "error"
        try:
            if private:
                client.send_message(chat_id, PRIVATE_WITHHELD, kind="proactive")
                return "withheld"
            for chunk in split_text(f"{title}\n\n{body}"):
                client.send_message(chat_id, chunk, kind="proactive")
        except TelegramError:
            # Ayrıntı istemcinin telegram_out olayında; burada yalnız sonuç tutulur.
            return "error"
        except ValueError:
            return "error"
        return "ok"
