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
from artemis.config.work_profile import is_local_only_profile, should_suppress_toast
from artemis.safety.audit import AuditLog
from artemis.tools.toast import ToastOutcome, show_toast

# Panele (Qt kuyruğuna) başlık ve gövde ekler; eklenemezse False döner.
PanelSink = Callable[[str, str], bool]
# Aynısı, ek olarak düğme için rutin adı (ADR 0058), sohbet kutusuna yazılacak istem (ADR 0061)
# ya da gün özeti tarihi (ADR 0066); üçünden en çok biri doludur.
RoutinePanelSink = Callable[[str, str, str, str, str], bool]
ToastFunction = Callable[[str, str], ToastOutcome]

PRIVATE_WITHHELD = (
    "Artemis kendiliğinden bir mesaj hazırladı; gizli içerik (dosya/pano) taşıdığı için "
    "Telegram'a gönderilmedi. Masaüstü panelinde görebilirsiniz."
)


@dataclass(frozen=True)
class DeliveryOutcome:
    delivered: bool
    # Hedef -> "ok" | "error" | "disabled" | "withheld" | "suppressed"
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
        profile_getter: Callable[[], str] | None = None,
    ) -> None:
        self.settings = settings
        self.audit = audit
        self.panel = panel
        self.routine_panel = routine_panel
        self.toast = toast
        self.telegram = telegram
        self._profile_getter = profile_getter

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
        suggest: str = "",
        summary_day: str = "",
        local_only: bool = False,
    ) -> DeliveryOutcome:
        """`local_only`: dosya adı gibi kişisel metin; Telegram'a hiç gitmez (ADR 0061)."""
        targets: dict[str, str] = {}
        if (routine or suggest or summary_day) and self.routine_panel is not None:
            sent = self.routine_panel(title, body, routine, suggest, summary_day)
            targets["panel"] = "ok" if sent else "error"
        elif self.panel is not None:
            targets["panel"] = "ok" if self.panel(title, body) else "error"
        profile = (
            self._profile_getter()
            if self._profile_getter is not None
            else self.settings.work_profile
        )
        if self.toast is not None:
            if should_suppress_toast(profile):
                targets["toast"] = "suppressed"
            else:
                outcome = await asyncio.to_thread(self.toast, title, body)
                targets["toast"] = outcome.status
        private_profile = is_local_only_profile(profile)
        chat_id = self.settings.telegram.chat_id
        if (
            self.telegram is not None
            and chat_id is not None
            and self.settings.proactive.telegram
        ):
            if private_profile:
                if not local_only:
                    targets["telegram"] = "withheld"
            elif not local_only:
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
