"""İki yönlü Telegram kanalı: mesajı çalıştırır, onayı düğmeyle sorar (ADR 0016).

Kanalın tek güvenlik kararı kimin konuştuğudur; ne yapılacağına `SafetyGate` karar verir.
Bu yüzden burada ikinci bir onay politikası yoktur: `approve` yalnız kapının sorduğu
soruyu Telegram'a taşır ve cevap gelmezse HAYIR sayar.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from artemis.channels.runtime import Runtime, create_runtime
from artemis.channels.telegram.api import (
    TELEGRAM_TEXT_LIMIT,
    TelegramClient,
    TelegramError,
    build_telegram_client,
)
from artemis.config.settings import load_settings
from artemis.safety.audit import AuditLog

# İletilmiş mesajın herhangi bir alanı varsa metin başkasının yazdığıdır: kullanıcının
# komutu değil, dış içeriktir (ADR 0016).
_FORWARD_KEYS = (
    "forward_origin",
    "forward_from",
    "forward_from_chat",
    "forward_sender_name",
    "forward_date",
)
_STALE_CALLBACK_TEXT = "Bu onay artık geçerli değil."
_APPROVED = "Onaylandı"
_DENIED = "Reddedildi"
_PRIVATE_APPROVAL_NOTICE = (
    "Bu onayın argümanları gizli içerik (dosya/pano) taşıyabilir; Telegram'da sorulmadı "
    "ve işlem yapılmadı. Masaüstünden yapın ya da /yeni ile yeni sohbet başlatın."
)
_TURN_EXPIRED_NOTICE = "Turun süresi doldu; onay sorulmadı."
_EXPIRED_SUFFIX = "\n\nKarar: Süre doldu, reddedildi"
_PRIVATE_WITHHELD = "Bu yanıt gizli içerik (dosya/pano) kullandığı için Telegram'a gönderilmedi."
_HELP_TEXT = (
    "Artemis Telegram kanalı. Komutunuzu yazın; yan etkili işlemler için düğmeyle onay "
    "sorulur ve varsayılan cevap hayırdır. /yeni yeni sohbet başlatır."
)
_REJECTION_NOTICES = {
    "forwarded": "İletilen mesajlar komut olarak çalıştırılmaz.",
    "not_text": "Yalnız metin mesajları desteklenir.",
    "stale": "Bu mesaj geç ulaştı; güvenlik için çalıştırılmadı. Yeniden yazın.",
}
# Boş poll'larda CPU'yu yakmamak için kısa bekleme (yalnız zaman aşımı 0 ise gerekir).
_IDLE_SLEEP = 0.1
_MAX_BACKOFF = 30.0


@dataclass(frozen=True)
class Rejection:
    reason: str


def _strict_ids(*values: object) -> bool:
    return all(isinstance(value, int) and not isinstance(value, bool) for value in values)


def _nested_id(container: object, key: str) -> int | None:
    if not isinstance(container, Mapping):
        return None
    value = container.get(key)
    node = value.get("id") if isinstance(value, Mapping) else None
    return node if isinstance(node, int) and not isinstance(node, bool) else None


def check_message(
    message: Mapping[str, Any], chat_id: int, now: float, max_age_seconds: int
) -> Rejection | None:
    """Sıra önemlidir: önce kimlik, sonra içerik; yabancıya hiçbir ayrıntı sızmaz."""
    chat = message.get("chat")
    if not isinstance(chat, Mapping) or chat.get("type") != "private":
        return Rejection("not_private")
    chat_ref = chat.get("id")
    sender = message.get("from")
    sender_ref = sender.get("id") if isinstance(sender, Mapping) else None
    # bool, int alt sınıfıdır; True == 1 kimliğiyle eşleşmesin diye tür açıkça sınanır.
    if not _strict_ids(chat_ref, sender_ref) or chat_ref != chat_id or sender_ref != chat_id:
        return Rejection("unauthorized_chat")
    if any(key in message for key in _FORWARD_KEYS):
        return Rejection("forwarded")
    text = message.get("text")
    if not isinstance(text, str) or not text.strip():
        return Rejection("not_text")
    sent = message.get("date")
    if not isinstance(sent, int) or isinstance(sent, bool) or now - sent > max_age_seconds:
        return Rejection("stale")
    return None


def split_text(text: str, limit: int = TELEGRAM_TEXT_LIMIT) -> list[str]:
    """Yanıtı sınırı aşmayan parçalara böler; mümkünse satır sonunda keser."""
    chunks: list[str] = []
    rest = text
    while len(rest) > limit:
        cut = rest.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(rest[:cut])
        rest = rest[cut:].lstrip("\n") if cut != limit else rest[cut:]
    chunks.append(rest)
    return chunks


class TelegramChannel:
    def __init__(
        self,
        client: TelegramClient,
        chat_id: int,
        *,
        audit: AuditLog,
        approval_timeout: float,
        max_message_age: int,
        poll_timeout: int,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.client = client
        self.chat_id = chat_id
        self.audit = audit
        self.approval_timeout = approval_timeout
        self.max_message_age = max_message_age
        self.poll_timeout = poll_timeout
        self.clock = clock
        self.monotonic = monotonic
        self.runtime: Runtime | None = None
        self._offset: int | None = None
        # Gizli içerik görmüş turların kimlikleri (`observe` doldurur).
        self._private_turns: set[str] = set()

    def observe(self, record: dict[str, Any]) -> None:
        """AuditLog.on_record'a bağlanır: hangi turun gizli içerik gördüğünü izler.

        Onay mesajı argümanları Telegram'a (bulut) taşır; gizli dosya/pano içeriği bir
        araç argümanına kopyalanmış olabilir. Kararı denetim olayından almak, gate'e veya
        döngüye dokunmadan aynı gizlilik kaynağını kullanır.
        """
        event = record.get("event")
        turn_id = record.get("turn_id")
        if event == "turn_start":
            # Gözetimsiz brifing turu (ADR 0027) aynı denetime yazar ve kullanıcının
            # yürüyen turu bekliyorken başlayabilir; onun gizlilik kaydını silmemeli.
            if not record.get("unattended"):
                self._private_turns.clear()
        elif (
            event == "privacy_decision"
            and record.get("cloud_allowed") is False
            and isinstance(turn_id, str)
        ):
            self._private_turns.add(turn_id)

    def _private_reason(self, runtime: Runtime) -> str | None:
        if runtime.context.turn_id in self._private_turns:
            return "private_turn"
        history = runtime.runner.conversation.render()
        if any(message.private for message in history.messages):
            return "private_history"
        return None

    def attach(self, runtime: Runtime) -> None:
        self.runtime = runtime

    # ------------------------------------------------------------------ yardımcılar

    def _advance(self, update: Mapping[str, Any]) -> None:
        update_id = update.get("update_id")
        if isinstance(update_id, int) and not isinstance(update_id, bool):
            self._offset = update_id + 1

    def _notify(self, text: str, kind: str = "notice") -> None:
        """Bildirim hatası turu bozmaz: istemci başarısızlığı zaten denetime yazdı."""
        with contextlib.suppress(TelegramError, ValueError):
            self.client.send_message(self.chat_id, text, kind=kind)

    def _answer(self, callback_id: object, text: str) -> None:
        if not isinstance(callback_id, str):
            return
        with contextlib.suppress(TelegramError, ValueError):
            self.client.answer_callback_query(callback_id, text, kind="callback")

    def _audit_rejected(self, message: Mapping[str, Any], reason: str) -> None:
        # Metnin kendisi yazılmaz: reddedilen mesaj yabancı veya zararlı olabilir.
        text = message.get("text")
        self.audit.emit(
            "telegram_rejected",
            reason=reason,
            chat_id=_nested_id(message, "chat"),
            from_id=_nested_id(message, "from"),
            chars=len(text) if isinstance(text, str) else 0,
        )

    def _audit_unauthorized_callback(self, callback: Mapping[str, Any]) -> None:
        self.audit.emit(
            "telegram_rejected",
            reason="unauthorized_callback",
            chat_id=None,
            from_id=_nested_id(callback, "from"),
            chars=0,
        )

    def _callback_authorized(self, callback: Mapping[str, Any]) -> bool:
        sender = _nested_id(callback, "from")
        message = callback.get("message")
        chat = _nested_id(message, "chat") if isinstance(message, Mapping) else None
        return sender == self.chat_id and chat == self.chat_id

    # ------------------------------------------------------------------ onay

    def approve(self, tool_name: str, arguments: dict[str, Any]) -> bool:
        """SafetyGate'in onay geri çağrısı; her belirsizlik HAYIR'dır."""
        runtime = self.runtime
        if runtime is None:
            return False
        try:
            return self._approve(runtime, tool_name, arguments)
        except (TelegramError, ValueError, TypeError):
            self.audit.emit("approval_error", tool=tool_name, error_type="channel_failure")
            return False

    def _approve(self, runtime: Runtime, tool_name: str, arguments: dict[str, Any]) -> bool:
        reason = self._private_reason(runtime)
        if reason is not None:
            # Argümanlar yazılmaz; yalnız araç adı ve neden denetlenir.
            self.audit.emit("approval_withheld", tool=tool_name, reason=reason)
            self._notify(_PRIVATE_APPROVAL_NOTICE)
            return False
        # Kapı, tur süresi dolduktan sonra onaylansa bile aracı çalıştırmaz; telefondaki
        # kullanıcıya "Onaylandı" deyip hiçbir şey yapmamak yerine bekleme turla sınırlanır.
        # Tur son tarihi gerçek time.monotonic ile kurulur, bu yüzden enjekte saat değil
        # gerçek saat kullanılır.
        wait_limit = self.approval_timeout
        turn_deadline = runtime.context.deadline
        if turn_deadline is not None:
            turn_remaining = turn_deadline - time.monotonic()
            # Yalnız turun kalan süresi sınanır; kısa ayarlanmış onay süresi soruyu engellemez.
            if turn_remaining <= 1:
                self._notify(_TURN_EXPIRED_NOTICE)
                return False
            wait_limit = min(wait_limit, turn_remaining)
        tool = runtime.registry.get(tool_name)
        label = (tool.label if tool is not None else "") or tool_name
        notice = runtime.gate.pending_notice
        text = (
            f"Onay gerekiyor: {label}\nAraç: {tool_name}\n{notice}\n\n"
            f"Argümanlar:\n{json.dumps(arguments, ensure_ascii=False, indent=2)}\n\n"
            f"Bu onay {int(wait_limit)} sn içinde yanıtlanmazsa reddedilir.\n"
            "Varsayılan cevap hayırdır."
        )
        if len(text) > TELEGRAM_TEXT_LIMIT:
            # Argümanların bir kısmını gizleyen onay, onay değildir; sığmıyorsa reddedilir.
            self._notify("Argümanlar tek mesaja sığmadığı için onay sorulmadı; işlem yapılmadı.")
            return False
        approval_id = secrets.token_hex(8)
        markup = {
            "inline_keyboard": [
                [
                    {"text": "Onayla", "callback_data": f"ok:{approval_id}"},
                    {"text": "Reddet", "callback_data": f"no:{approval_id}"},
                ]
            ]
        }
        try:
            sent = self.client.send_message(
                self.chat_id, text, kind="approval", reply_markup=markup
            )
        except TelegramError:
            return False
        deadline = self.monotonic() + wait_limit
        while True:
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                self._finish_approval(sent.message_id, text, _EXPIRED_SUFFIX)
                return False
            wait = max(0, min(self.poll_timeout, int(remaining)))
            try:
                updates = self.client.get_updates(self._offset, wait)
            except TelegramError:
                self.audit.emit("approval_error", tool=tool_name, error_type="poll_failed")
                return False
            for update in updates:
                self._advance(update)
                decision = self._approval_update(update, approval_id, sent.message_id, text)
                if decision is not None:
                    return decision
            if not updates and wait == 0:
                time.sleep(min(_IDLE_SLEEP, max(remaining, 0.0)))

    def _approval_update(
        self, update: Mapping[str, Any], approval_id: str, message_id: int, text: str
    ) -> bool | None:
        """Karar çıkarsa True/False, aksi halde None döner (beklemeye devam)."""
        callback = update.get("callback_query")
        if isinstance(callback, Mapping):
            if not self._callback_authorized(callback):
                # Yabancıya cevap vermek bile varlığımızı doğrular; yalnız denetim.
                self._audit_unauthorized_callback(callback)
                return None
            data = callback.get("data")
            if data not in (f"ok:{approval_id}", f"no:{approval_id}"):
                self._answer(callback.get("id"), _STALE_CALLBACK_TEXT)
                return None
            approved = bool(data == f"ok:{approval_id}")
            word = _APPROVED if approved else _DENIED
            self._answer(callback.get("id"), word)
            self._finish_approval(message_id, text, f"\n\nKarar: {word}")
            return approved
        message = update.get("message")
        if isinstance(message, Mapping):
            rejection = check_message(message, self.chat_id, self.clock(), self.max_message_age)
            if rejection is not None and rejection.reason in {"unauthorized_chat", "not_private"}:
                self._audit_rejected(message, rejection.reason)
            else:
                self._notify("Önce bekleyen onayı yanıtlayın; bu mesaj çalıştırılmadı.")
        return None

    def _finish_approval(self, message_id: int, text: str, suffix: str) -> None:
        """Onay mesajını karar ile günceller; düğmeler de kalkar."""
        final = text + suffix
        if len(final) > TELEGRAM_TEXT_LIMIT:
            final = suffix.strip()
        with contextlib.suppress(TelegramError, ValueError):
            self.client.edit_message_text(self.chat_id, message_id, final, kind="approval_result")

    # ------------------------------------------------------------------ güncellemeler

    def skip_backlog(self) -> None:
        """Kanal kapalıyken biriken komutlar açılışta çalışmasın."""
        updates = self.client.get_updates(-1, 0)
        for update in updates:
            self._advance(update)

    async def handle_update(self, update: Mapping[str, Any]) -> None:
        callback = update.get("callback_query")
        if isinstance(callback, Mapping):
            # Bekleyen onay yoksa düğme eskidir; yalnız izinli kullanıcıya cevap verilir.
            if _nested_id(callback, "from") == self.chat_id:
                self._answer(callback.get("id"), _STALE_CALLBACK_TEXT)
            else:
                self._audit_unauthorized_callback(callback)
            return
        message = update.get("message")
        if isinstance(message, Mapping):
            await self._handle_message(message)

    async def _handle_message(self, message: Mapping[str, Any]) -> None:
        runtime = self.runtime
        rejection = check_message(message, self.chat_id, self.clock(), self.max_message_age)
        if rejection is not None:
            self._audit_rejected(message, rejection.reason)
            if rejection.reason in _REJECTION_NOTICES:
                self._notify(_REJECTION_NOTICES[rejection.reason])
            return
        raw = message.get("text")
        text = raw.strip() if isinstance(raw, str) else ""
        command = text.split("@", 1)[0].casefold() if text.startswith("/") else ""
        if command in {"/start", "/yardim", "/yardım"}:
            self._notify(_HELP_TEXT)
            return
        if runtime is None:
            return
        if command == "/yeni":
            if runtime.runner.reset_context():
                self._notify("Yeni sohbet başladı.")
            else:
                self._notify("Aktif işlem sürerken sıfırlanamaz.")
            return
        result = await runtime.runner.run(text)
        if result.record is None:
            self._notify(f"Tur tamamlanmadı ({result.stop_reason}).")
        elif result.record.answer_private:
            self._notify(_PRIVATE_WITHHELD)
        else:
            for chunk in split_text(result.text or "(boş yanıt)"):
                try:
                    self.client.send_message(self.chat_id, chunk, kind="reply")
                except TelegramError:
                    # Tur gerçekleşti ve denetimde; yanıt iletilemese de döngü sürer.
                    break

    async def serve(self, stop: asyncio.Event) -> None:
        backoff = 1.0
        while not stop.is_set():
            try:
                updates = await asyncio.to_thread(
                    self.client.get_updates, self._offset, self.poll_timeout
                )
            except TelegramError:
                # Hata istemcide denetlendi; kısa süre bekleyip yeniden dener.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=backoff)
                backoff = min(backoff * 2, _MAX_BACKOFF)
                continue
            backoff = 1.0
            for update in updates:
                # Önce ilerlet: işlenirken çökse bile aynı komut ikinci kez çalışmaz.
                self._advance(update)
                await self.handle_update(update)
                if stop.is_set():
                    break


def _start_proactive(
    runtime: Runtime, client: TelegramClient, stop: asyncio.Event
) -> asyncio.Task[None] | None:
    """Masaüstü kapalıyken de hatırlatma/brifing çalışsın (ADR 0027).

    Masaüstü de açıksa iki zamanlayıcı aynı depoyu kullanır; oluşum talebi tek teslimi
    garanti eder. İçe aktarma burada: proaktif katman kanal modülüne bağımlıdır.
    """
    from artemis.proactive.delivery import Deliverer
    from artemis.proactive.scheduler import ProactiveScheduler
    from artemis.proactive.store import ProactiveStore

    store = runtime.context.services.get("proactive_store")
    if not runtime.settings.proactive.enabled or not isinstance(store, ProactiveStore):
        return None
    deliverer = Deliverer(runtime.settings, runtime.audit, telegram=client)
    scheduler = ProactiveScheduler(runtime, store, deliverer)
    return asyncio.create_task(scheduler.serve(stop))


async def run_telegram(config_path: Path) -> int:
    settings = load_settings(config_path)
    config = settings.telegram
    if config.chat_id is None:
        print("Telegram kapalı: config içinde telegram.chat_id tanımlayın.")
        return 2
    audit = AuditLog(settings.audit_path)
    client = build_telegram_client(settings, audit)
    if client is None:
        print(f"Telegram token'ı bulunamadı: {config.token_env} ortam değişkenini ayarlayın.")
        return 2
    if not settings.primary_model:
        client.close()
        print("Config içinde resmi katalogdan doğrulanmış bir primary_model tanımlayın.")
        return 2
    channel = TelegramChannel(
        client,
        config.chat_id,
        audit=audit,
        approval_timeout=config.approval_timeout_seconds,
        max_message_age=config.max_message_age_seconds,
        poll_timeout=config.poll_timeout_seconds,
    )
    # Gizlilik izlemesi: runtime kurulmadan önce de olaylar kaçmasın diye hemen bağlanır.
    audit.on_record = channel.observe
    stop = asyncio.Event()
    runtime: Runtime | None = None
    try:
        runtime = create_runtime(config_path, channel.approve, settings=settings, audit=audit)
        channel.attach(runtime)
        # notify_telegram aracı aynı istemciyi kullanır; ikinci bağlantı havuzu açılmaz.
        runtime.context.services["telegram_client"] = client
        try:
            channel.skip_backlog()
            client.send_message(
                config.chat_id,
                "Artemis Telegram kanalı açık. Komut yazabilirsiniz; onaylar düğmeyle sorulur.",
                kind="notice",
            )
        except TelegramError as error:
            print(f"Telegram'a bağlanılamadı ({error.error_type}).")
            return 1
        print("Artemis Telegram kanalı açık. Durdurmak için Ctrl+C.")
        proactive = _start_proactive(runtime, client, stop)
        try:
            await channel.serve(stop)
        finally:
            stop.set()
            if proactive is not None:
                # Yürüyen brifing iptal edilir; talep edilmiş oluşum belirsiz raporlanır.
                proactive.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await proactive
    finally:
        stop.set()
        if runtime is not None:
            runtime.shutdown()
        client.close()
    return 0
