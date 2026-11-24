"""İki yönlü Telegram kanalı: mesajı çalıştırır, onayı düğmeyle sorar (ADR 0016).

Kanalın tek güvenlik kararı kimin konuştuğudur; ne yapılacağına `SafetyGate` karar verir.
Bu yüzden burada ikinci bir onay politikası yoktur: `approve` yalnız kapının sorduğu
soruyu Telegram'a taşır ve cevap gelmezse HAYIR sayar.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
import tempfile
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from artemis.brain.conversation import ToolExchange, TurnRecord
from artemis.brain.models import Message
from artemis.channels.desktop.computer_host import ComputerHost
from artemis.channels.desktop.dictation import clean_transcript
from artemis.channels.notice import format_approval_notice
from artemis.channels.runtime import Runtime, create_runtime
from artemis.channels.telegram.api import (
    TELEGRAM_TEXT_LIMIT,
    TelegramClient,
    TelegramError,
    build_telegram_client,
)
from artemis.channels.telegram.media import (
    VOICE_SAMPLE_RATE,
    decode_voice,
    extension_allowed,
    sanitize_filename,
    save_document,
)
from artemis.channels.voice.core import MAX_DIRECT_AUDIO_BYTES, SpeechRecognizer, pcm_to_wav
from artemis.channels.voice.factory import build_dictation_recognizer
from artemis.config.settings import load_settings
from artemis.config.work_profile import is_local_only_profile
from artemis.i18n import t
from artemis.perception.screen import ScreenOutcome, read_image
from artemis.safety.audit import AuditLog
from artemis.tools.base import Risk, ToolResult, normalize_risk

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
_EXPIRED_SUFFIX = "Karar: Süre doldu, reddedildi"
_CLOSING_SUFFIX = "Karar: Kanal kapanıyor, reddedildi"
_APPROVAL_POLL_CAP = 2  # sn; onay beklerken durdurma bayrağı en geç bu sürede fark edilir
_STOP_GRACE_SECONDS = 3.0  # durdurmada yürüyen turun normal iptal yoluyla bitmesi için süre
_PRIVATE_WITHHELD = "Bu yanıt gizli içerik (dosya/pano) kullandığı için Telegram'a gönderilmedi."
_FOOTER_TITLES = {
    "sent": "Gönderildi, sonucu doğrulanmadı",
    "error": "Başarısız",
    "denied": "Çalıştırılmadı",
}
_HELP_TEXT = (
    "Artemis Telegram kanalı. Komutunuzu yazın; yan etkili işlemler için düğmeyle onay "
    "sorulur ve varsayılan cevap hayırdır. /yeni yeni sohbet başlatır; /dur çalışan "
    "bilgisayar kullanımı oturumunu durdurur."
)
_STOPPING = "Bilgisayar kullanımı oturumu durduruluyor."
_NO_SESSION = "Çalışan bir bilgisayar kullanımı oturumu yok."
_PRIVATE_PROFILE_BLOCKED = "Gizli profil etkin: Telegram kanalı kapalı."
# /dur turu bloklayan döngüde dinlenir: bu aralıkla, yalnız bilgisayar oturumu sürerken yoklanır.
_STOP_POLL_SECONDS = 1.0
_REJECTION_NOTICES = {
    "forwarded": "İletilen mesajlar komut olarak çalıştırılmaz.",
    "not_text": "Bu mesaj türü desteklenmiyor.",
    "stale": "Bu mesaj geç ulaştı; güvenlik için çalıştırılmadı. Yeniden yazın.",
}
# Ses, fotoğraf ve dosya girdisi (ADR 0064): sınırlar indirmeden ÖNCE bildirilen boyutla,
# okurken de gerçek bayt sayısıyla uygulanır.
_VOICE_MAX_BYTES = 1_000_000
_VOICE_MAX_SECONDS = 60
_PHOTO_MAX_BYTES = 5_000_000
_FILE_MAX_BYTES = 20_000_000
_TRANSCRIPT_LIMIT = 2000
_CAPTION_LIMIT = 1000
_MEDIA_OFF = {
    "voice": "Ses mesajları kapalı (telegram.accept_voice).",
    "photo": "Fotoğraflar kapalı (telegram.accept_photo).",
    "document": "Dosya alımı kapalı (telegram.accept_files).",
}
_PHOTO_WITHHELD = (
    "Yanıt fotoğraftaki gizli içeriği kullandığı için Telegram'a gönderilmedi. "
    "Konuyu masaüstünden sorun."
)
_DOWNLOAD_FAILED = "Dosya Telegram'dan indirilemedi."
_TOO_BIG = "Dosya çok büyük; işlenmedi."
# Boş poll'larda CPU'yu yakmamak için kısa bekleme (yalnız zaman aşımı 0 ise gerekir).
_IDLE_SLEEP = 0.1
_MAX_BACKOFF = 30.0
_MAX_FRAMES = 4  # tek turda en çok bu kadar monitör karesi gönderilir


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


def message_kind(message: Mapping[str, Any]) -> str:
    """"text", "voice", "photo", "document" ya da "other". Metin önceliklidir."""
    text = message.get("text")
    if isinstance(text, str) and text.strip():
        return "text"
    if isinstance(message.get("voice"), Mapping):
        return "voice"
    photo = message.get("photo")
    if isinstance(photo, list) and photo:
        return "photo"
    if isinstance(message.get("document"), Mapping):
        return "document"
    return "other"


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
    if message_kind(message) == "other":
        return Rejection("not_text")
    sent = message.get("date")
    if not isinstance(sent, int) or isinstance(sent, bool) or now - sent > max_age_seconds:
        return Rejection("stale")
    return None


def _command(message: Mapping[str, Any]) -> str:
    raw = message.get("text")
    text = raw.strip() if isinstance(raw, str) else ""
    return text.split("@", 1)[0].casefold() if text.startswith("/") else ""


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
    # Sadece satır sonlarından oluşan kuyruk lstrip sonrası boşalırsa Telegram HTTP 400
    # vermesin diye boş parça eklenmez.
    if rest or not chunks:
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
        stop_poll_seconds: float = _STOP_POLL_SECONDS,
        stop_flag: threading.Event | None = None,
    ) -> None:
        self.client = client
        # Onay bekleme döngüsü olay döngüsü iş parçacığını bloklar (SafetyGate.approve
        # senkron); asyncio.Event orada görülemez, bu yüzden iş parçacığı güvenli bayrak.
        self.stop_flag = stop_flag if stop_flag is not None else threading.Event()
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
        # Eşzamanlı gözetimsiz turların gizliliğini korumak için yalnız katılımlı turlar izlenir.
        self._attended_turns: set[str] = set()
        # Bilgisayar kullanımı (ADR 0053). Onay yoklaması ile /dur yoklaması aynı anda
        # getUpdates açmasın (Telegram 409): ikisi de bu kilidi alır.
        self.computer_host: ComputerHost | None = None
        self._stop_poll_seconds = stop_poll_seconds
        self._poll_lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._config_path: Path | None = None
        # Yoklamada görülen /dur güncellemeleri (onaylanmadı): sonra normal akışta atlanır.
        self._stop_seen: set[int] = set()
        # Son ekran karesi: yalnız RAM, tur başında ve gönderimden sonra atılır.
        self._frame: tuple[bytes, ...] | None = None
        # Medya girdisi (ADR 0064): değiştirilebilir bağlantı noktaları (testler sahtesini koyar).
        self.audio_decoder: Callable[[bytes, int], bytes] = decode_voice
        self.image_reader: Callable[..., ScreenOutcome] = read_image
        self.recognizer_factory: Callable[[Runtime], Awaitable[SpeechRecognizer]] = (
            lambda runtime: build_dictation_recognizer(runtime, "telegram_audio")
        )
        self._recognizer: SpeechRecognizer | None = None

    def observe(self, record: dict[str, Any]) -> None:
        """AuditLog.on_record'a bağlanır: hangi turun gizli içerik gördüğünü izler.

        Onay mesajı argümanları Telegram'a (bulut) taşır; gizli dosya/pano içeriği bir
        araç argümanına kopyalanmış olabilir. Kararı denetim olayından almak, gate'e veya
        döngüye dokunmadan aynı gizlilik kaynağını kullanır.
        """
        event = record.get("event")
        turn_id = record.get("turn_id")
        if event == "turn_start":
            if not record.get("unattended"):
                # Yeni katılımlı tur başlarken önceki katılımlı turların izi temizlenir;
                # eşzamanlı gözetimsiz turların gizlilik kaydı korunur.
                self._private_turns -= self._attended_turns
                self._attended_turns.clear()
                if isinstance(turn_id, str):
                    self._attended_turns.add(turn_id)
        elif event == "turn_end":
            if isinstance(turn_id, str):
                self._private_turns.discard(turn_id)
                self._attended_turns.discard(turn_id)
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
        if runtime.context.settings.telegram.send_computer_screenshot:
            # Bayrak kapalıyken kare hiç tutulmaz; açıkken de gönderim kapıları ayrıca sınanır.
            runtime.context.services["computer_frame_sink"] = self.remember_frame

    def enable_computer_use(self, config_path: Path) -> None:
        """Gömülü kipte `computer_use` için host kurar (ADR 0053). Onay yalnız Telegram
        düğmesidir; adım bildirimi yoktur çünkü adım gerekçesi ekrandan türeyen metindir."""
        runtime = self.runtime
        if runtime is None:
            return
        self._config_path = config_path
        self.computer_host = ComputerHost(
            self.approve, lambda _text: None, on_escape=self.cancel_turn
        )
        runtime.context.services["computer_host"] = self.computer_host

    def remember_frame(self, pngs: tuple[bytes, ...]) -> None:
        self._frame = pngs

    def cancel_turn(self) -> None:
        """Her iş parçacığından çağrılabilir: yürüyen turu iptal ettirir (Esc ve /dur)."""
        loop, runtime = self._loop, self.runtime
        if loop is None or runtime is None:
            return
        with contextlib.suppress(RuntimeError):  # döngü kapandıysa tur zaten bitmiştir
            loop.call_soon_threadsafe(runtime.runner.cancel_current)

    def _sync_computer_mode(self, runtime: Runtime) -> None:
        """Kip ve çalışma profili masaüstü ile aynıdır: panelin yazdığı config yeniden okunur.

        Okunamazsa `accept` (sor) seçilir; belirsizlikte sormak güvenli taraftır.
        """
        if self._config_path is None:
            return
        try:
            loaded = load_settings(self._config_path)
            mode = loaded.computer_use.mode
            work_profile = loaded.work_profile
        except (OSError, ValueError, yaml.YAMLError):
            mode = "accept"
            work_profile = runtime.context.settings.work_profile
        settings = runtime.context.settings
        updates: dict[str, Any] = {}
        if settings.computer_use.mode != mode:
            updates["computer_use"] = settings.computer_use.model_copy(update={"mode": mode})
        if settings.work_profile != work_profile:
            updates["work_profile"] = work_profile
        if updates:
            runtime.context.settings = settings.model_copy(update=updates)

    def _is_private_profile(self, runtime: Runtime) -> bool:
        self._sync_computer_mode(runtime)
        return is_local_only_profile(runtime.context.settings.work_profile)

    # ------------------------------------------------------------------ yardımcılar

    def _advance(self, update: Mapping[str, Any]) -> None:
        update_id = update.get("update_id")
        if isinstance(update_id, int) and not isinstance(update_id, bool):
            self._offset = update_id + 1

    def _notify(self, text: str, kind: str = "notice", *, redact_audit: bool = False) -> None:
        """Bildirim hatası turu bozmaz: istemci başarısızlığı zaten denetime yazdı."""
        extra: dict[str, Any] = {"redact_audit": True} if redact_audit else {}
        with contextlib.suppress(TelegramError, ValueError):
            self.client.send_message(self.chat_id, text, kind=kind, **extra)

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
            self._notify(t(_PRIVATE_APPROVAL_NOTICE))
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
                self._notify(t(_TURN_EXPIRED_NOTICE))
                return False
            wait_limit = min(wait_limit, turn_remaining)
        tool = runtime.registry.get(tool_name)
        label = (t(tool.label) if tool is not None and tool.label else "") or tool_name
        notice = format_approval_notice(runtime.gate.pending_notice)
        args_json = json.dumps(arguments, ensure_ascii=False, indent=2)
        text = t(
            "Onay gerekiyor: {label}\nAraç: {tool_name}\n{notice}\n\n"
            "Argümanlar:\n{args_json}\n\n"
            "Bu onay {seconds} sn içinde yanıtlanmazsa reddedilir.\n"
            "Varsayılan cevap hayırdır."
        ).format(
            label=label,
            tool_name=tool_name,
            notice=notice,
            args_json=args_json,
            seconds=int(wait_limit),
        )
        if len(text) > TELEGRAM_TEXT_LIMIT:
            # Argümanların bir kısmını gizleyen onay, onay değildir; sığmıyorsa reddedilir.
            self._notify(
                t("Argümanlar tek mesaja sığmadığı için onay sorulmadı; işlem yapılmadı.")
            )
            return False
        approval_id = secrets.token_hex(8)
        markup = {
            "inline_keyboard": [
                [
                    {"text": t("Onayla"), "callback_data": f"ok:{approval_id}"},
                    {"text": t("Reddet"), "callback_data": f"no:{approval_id}"},
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
            if self.stop_flag.is_set():
                self._finish_approval(sent.message_id, text, "\n\n" + t(_CLOSING_SUFFIX))
                return False
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                self._finish_approval(sent.message_id, text, "\n\n" + t(_EXPIRED_SUFFIX))
                return False
            # Uzun yoklama durdurma bayrağını geciktirmesin: her bekleme en çok birkaç sn.
            wait = max(0, min(self.poll_timeout, _APPROVAL_POLL_CAP, int(remaining)))
            try:
                with self._poll_lock:
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
                self._answer(callback.get("id"), t(_STALE_CALLBACK_TEXT))
                return None
            approved = bool(data == f"ok:{approval_id}")
            word = t(_APPROVED) if approved else t(_DENIED)
            self._answer(callback.get("id"), word)
            self._finish_approval(message_id, text, "\n\n" + t("Karar: {word}").format(word=word))
            return approved
        message = update.get("message")
        if isinstance(message, Mapping):
            rejection = check_message(message, self.chat_id, self.clock(), self.max_message_age)
            if rejection is not None and rejection.reason in {"unauthorized_chat", "not_private"}:
                self._audit_rejected(message, rejection.reason)
            elif rejection is None and _command(message) == "/dur":
                # Onay beklerken durdurma: bekleyen adım reddedilir, oturum da durur.
                self._stop_session()
                self._finish_approval(message_id, text, "\n\n" + t("Karar: Durduruldu"))
                return False
            else:
                self._notify(t("Önce bekleyen onayı yanıtlayın; bu mesaj çalıştırılmadı."))
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

    def _stop_session(self) -> None:
        host = self.computer_host
        self._notify(t(_STOPPING if host is not None and host.stop_active() else _NO_SESSION))

    def _peek_stop(self) -> list[int]:
        """Onaylamadan (offset ilerletmeden) bekleyen yetkili `/dur` güncellemelerinin kimlikleri.

        Tur sürerken ana döngü bloklu olduğundan komutu bu yoklama duyar. Kilit meşgulse (onay
        yoklaması sürüyor) atlanır: onay yolu `/dur`'u kendisi işler. Reddedilen mesajlar burada
        denetlenmez; asıl akış onlara sonra bakar.
        """
        if not self._poll_lock.acquire(blocking=False):
            return []
        try:
            updates = self.client.get_updates(self._offset, 0)
        except TelegramError:
            return []
        finally:
            self._poll_lock.release()
        found: list[int] = []
        for update in updates:
            message = update.get("message")
            update_id = update.get("update_id")
            if (
                isinstance(message, Mapping)
                and isinstance(update_id, int)
                and update_id not in self._stop_seen
                and check_message(message, self.chat_id, self.clock(), self.max_message_age) is None
                and _command(message) == "/dur"
            ):
                found.append(update_id)
        return found

    async def _watch_stop(self) -> None:
        while True:
            await asyncio.sleep(self._stop_poll_seconds)
            host = self.computer_host
            if host is None or not host.active:
                continue
            for update_id in await asyncio.to_thread(self._peek_stop):
                self._stop_seen.add(update_id)
                self._stop_session()

    async def handle_update(self, update: Mapping[str, Any]) -> None:
        if update.get("update_id") in self._stop_seen:
            # Tur sürerken zaten işlenen /dur; ikinci kez "oturum yok" denmesin.
            self._stop_seen.discard(update.get("update_id"))  # type: ignore[arg-type]
            return
        callback = update.get("callback_query")
        if isinstance(callback, Mapping):
            # Bekleyen onay yoksa düğme eskidir; yalnız izinli kullanıcıya cevap verilir.
            if _nested_id(callback, "from") == self.chat_id:
                self._answer(callback.get("id"), t(_STALE_CALLBACK_TEXT))
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
                self._notify(t(_REJECTION_NOTICES[rejection.reason]))
            return
        kind = message_kind(message)
        if kind != "text":
            await self._handle_media(kind, message)
            return
        raw = message.get("text")
        text = raw.strip() if isinstance(raw, str) else ""
        command = _command(message)
        if command in {"/start", "/yardim", "/yardım"}:
            self._notify(t(_HELP_TEXT))
            return
        if runtime is None:
            return
        if self._is_private_profile(runtime):
            self.audit.emit("telegram_blocked_private")
            return
        if command == "/yeni":
            if runtime.runner.reset_context():
                self._notify(t("Yeni sohbet başladı."))
            else:
                self._notify(t("Aktif işlem sürerken sıfırlanamaz."))
            return
        if command == "/dur":
            self._stop_session()
            return
        await self._run_turn(runtime, text)

    async def _run_turn(
        self,
        runtime: Runtime,
        text: str,
        attachment: ToolResult | None = None,
        withheld: str = _PRIVATE_WITHHELD,
        audio_wav: bytes | None = None,
    ) -> None:
        self._loop = asyncio.get_running_loop()
        if self._is_private_profile(runtime):
            self.audit.emit("telegram_blocked_private")
            return
        self._frame = None
        watcher = asyncio.create_task(self._watch_stop()) if self.computer_host else None
        try:
            kwargs: dict[str, Any] = {}
            if attachment is not None:
                kwargs["attachment"] = attachment
            if audio_wav is not None:
                kwargs["audio_wav"] = audio_wav
            result = await runtime.runner.run(text, **kwargs)
        finally:
            if watcher is not None:
                watcher.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await watcher
        frame, self._frame = self._frame, None
        answer_private = result.record is not None and result.record.answer_private
        self._reply(result, withheld)
        # Kare yanıt metninden bağımsız karar verilir; kapılar `_send_frame` içindedir.
        self._send_frame(runtime, frame, answer_private)

    # ------------------------------------------------------------------ ses/fotoğraf/dosya

    def _audit_download(self, kind: str, size: int | None, outcome: str) -> None:
        # İçerik ve dosya adı yazılmaz; yalnız tür, bayt sayısı ve sonuç.
        self.audit.emit("telegram_download", kind=kind, size=size, outcome=outcome)

    def _refuse_media(self, kind: str, size: int | None, outcome: str, notice: str) -> None:
        self._audit_download(kind, size, outcome)
        self._notify(notice)

    async def _fetch(
        self, kind: str, file_id: object, reported: object, limit: int
    ) -> bytes | None:
        """Boyutu indirmeden ÖNCE sınar, okurken yine sınar. Hata/ret yanıtı burada verilir."""
        known = reported if isinstance(reported, int) and not isinstance(reported, bool) else None
        if not isinstance(file_id, str) or not file_id:
            self._refuse_media(kind, known, "bad_message", t(_DOWNLOAD_FAILED))
            return None
        if known is not None and known > limit:
            self._refuse_media(kind, known, "too_large", t(_TOO_BIG))
            return None
        try:
            path, size = await asyncio.to_thread(self.client.get_file, file_id)
            if size is not None and size > limit:
                self._refuse_media(kind, size, "too_large", t(_TOO_BIG))
                return None
            return await asyncio.to_thread(self.client.download_file, path, limit)
        except TelegramError as error:
            if error.error_type == "TooLarge":
                self._refuse_media(kind, None, "too_large", t(_TOO_BIG))
            else:
                self._refuse_media(kind, known, "download_failed", t(_DOWNLOAD_FAILED))
            return None

    async def _handle_media(self, kind: str, message: Mapping[str, Any]) -> None:
        runtime = self.runtime
        if runtime is None:
            return
        if self._is_private_profile(runtime):
            self.audit.emit("telegram_blocked_private")
            return
        config = runtime.context.settings.telegram
        enabled = {
            "voice": config.accept_voice,
            "photo": config.accept_photo,
            "document": config.accept_files,
        }
        if not enabled[kind]:
            self._audit_rejected(message, f"{kind}_disabled")
            self._notify(t(_MEDIA_OFF[kind]))
            return
        if kind == "voice":
            await self._handle_voice(runtime, message)
        elif kind == "photo":
            await self._handle_photo(runtime, message)
        else:
            await self._handle_document(runtime, message)

    async def _handle_voice(self, runtime: Runtime, message: Mapping[str, Any]) -> None:
        if self._is_private_profile(runtime):
            self.audit.emit("telegram_blocked_private")
            return
        voice = message.get("voice")
        if not isinstance(voice, Mapping):
            return
        duration = voice.get("duration")
        if (
            isinstance(duration, int)
            and not isinstance(duration, bool)
            and duration > _VOICE_MAX_SECONDS
        ):
            self._refuse_media("voice", None, "too_long", t("Ses 60 saniyeden uzun; işlenmedi."))
            return
        data = await self._fetch(
            "voice", voice.get("file_id"), voice.get("file_size"), _VOICE_MAX_BYTES
        )
        if data is None:
            return
        # Ses yalnız bellekte çözülür; hiçbir yolda diske yazılmaz.
        try:
            pcm = await asyncio.to_thread(self.audio_decoder, data, _VOICE_MAX_SECONDS)
        except ImportError:
            self._refuse_media(
                "voice", len(data), "no_decoder",
                t("Bu kurulumda ses çözücü yok (voice ekini kurun); ses işlenmedi."),
            )
            return
        except ValueError:
            self._refuse_media("voice", len(data), "decode_failed", t("Ses çözülemedi."))
            return
        try:
            if self._recognizer is None:
                self._recognizer = await self.recognizer_factory(runtime)
            transcript = clean_transcript(await self._recognizer.transcribe(pcm))
        except (OSError, RuntimeError, ValueError, ImportError):
            # İstisna metni sır/yol taşıyabilir; sabit ileti.
            self._refuse_media(
                "voice", len(data), "stt_failed", t("Ses yazıya çevrilemedi; komut çalıştırılmadı.")
            )
            return
        if not transcript:
            self._refuse_media(
                "voice", len(data), "empty", t("Ses anlaşılamadı; komut çalıştırılmadı.")
            )
            return
        transcript = transcript[:_TRANSCRIPT_LIMIT]
        self._audit_download("voice", len(data), "ok")
        self._notify(t("Şunu duydum: {transcript}").format(transcript=transcript))
        audio_wav: bytes | None = None
        if runtime.context.settings.voice.direct_audio_input and pcm:
            encoded_wav = pcm_to_wav(pcm, VOICE_SAMPLE_RATE)
            if len(encoded_wav) <= MAX_DIRECT_AUDIO_BYTES:
                audio_wav = encoded_wav
        # Yazıya çevrilen ses sıradan bir metin komutudur: onay yine yalnız düğmeyle, ve "/..."
        # kanal komutu sayılmaz (yalnız gerçek metin mesajında komut çözümlenir).
        await self._run_turn(runtime, transcript, audio_wav=audio_wav)

    async def _handle_photo(self, runtime: Runtime, message: Mapping[str, Any]) -> None:
        if self._is_private_profile(runtime):
            self.audit.emit("telegram_blocked_private")
            return
        raw = message.get("photo")
        items = raw if isinstance(raw, list) else []
        photos = [item for item in items if isinstance(item, Mapping)]

        def area(item: Mapping[str, Any]) -> int:
            width, height = item.get("width"), item.get("height")
            return width * height if isinstance(width, int) and isinstance(height, int) else 0

        def small_enough(item: Mapping[str, Any]) -> bool:
            size = item.get("file_size")
            return not isinstance(size, int) or size <= _PHOTO_MAX_BYTES

        candidates = [item for item in photos if small_enough(item)]
        if not candidates:
            self._refuse_media("photo", None, "too_large", t(_TOO_BIG))
            return
        best = max(candidates, key=area)
        data = await self._fetch(
            "photo", best.get("file_id"), best.get("file_size"), _PHOTO_MAX_BYTES
        )
        if data is None:
            return
        if not data.startswith(b"\xff\xd8"):  # Telegram fotoğrafı her zaman JPEG'dir
            self._refuse_media("photo", len(data), "not_image", t("Fotoğraf okunamadı."))
            return
        # OCR yol ister: geçici dosya HER yolda silinir (başarı, hata, iptal).
        handle, name = tempfile.mkstemp(prefix="artemis-tg-", suffix=".jpg")
        try:
            try:
                with os.fdopen(handle, "wb") as image:
                    image.write(data)
                outcome = await asyncio.to_thread(
                    self.image_reader,
                    name,
                    timeout=runtime.context.settings.perception.timeout_seconds,
                )
            except OSError:
                self._refuse_media("photo", len(data), "error", t("Fotoğraf işlenemedi."))
                return
        finally:
            with contextlib.suppress(OSError):
                os.unlink(name)
        if outcome.status != "ok":
            self._refuse_media("photo", len(data), f"ocr_{outcome.status}", outcome.message)
            return
        self._audit_download("photo", len(data), "ok")
        caption = message.get("caption")
        command = clean_transcript(caption)[:_CAPTION_LIMIT] if isinstance(caption, str) else ""
        if not command:
            # Fotoğraf metni gizli ekran içeriğidir: kullanıcıya döner ama denetime yazılmaz.
            for chunk in split_text(outcome.text):
                self._notify(chunk, "ocr_text", redact_audit=True)
            return
        # OCR metni dış veridir: tur lekelenir, gizlilik `content_kind="screen"` kuralından çıkar
        # (perception.allow_cloud_screen kapalıyken gizli). Görüntünün kendisi hiç modele gitmez.
        attachment = ToolResult(
            success=True,
            status="ok",
            message="Telegram fotoğrafından okunan metin.",
            data={"text": outcome.text, "language": outcome.language},
            external=True,
            content_kind="screen",
        )
        await self._run_turn(runtime, command, attachment, _PHOTO_WITHHELD)

    async def _handle_document(self, runtime: Runtime, message: Mapping[str, Any]) -> None:
        document = message.get("document")
        if not isinstance(document, Mapping):
            return
        config = runtime.context.settings.telegram
        name = sanitize_filename(document.get("file_name"))
        if name is None:
            self._refuse_media("document", None, "bad_name", t("Dosya adı kabul edilmedi."))
            return
        if not extension_allowed(name, config.file_extensions):
            self._refuse_media(
                "document", None, "bad_extension", t("Bu dosya türü kabul edilmiyor.")
            )
            return
        data = await self._fetch(
            "document", document.get("file_id"), document.get("file_size"), _FILE_MAX_BYTES
        )
        if data is None:
            return
        try:
            saved = save_document(runtime.context.workspace, name, data)
        except (ValueError, OSError):
            self._refuse_media("document", len(data), "save_failed", t("Dosya kaydedilemedi."))
            return
        self._audit_download("document", len(data), "ok")
        # Başlık (caption) bilerek komut sayılmaz (ADR 0064): dosya + talimat ayrı bir adımdır.
        self._notify(t("Kaydedildi: {saved}").format(saved=saved))

    def _status_footer(self, record: TurnRecord) -> str:
        """Yan etkili araçların gerçek sonucunu modelin anlatısından bağımsız bildirir.

        Kural 5: gönderildi != başarılı. Model "tamam, yaptım" dese bile doğrulanmamış,
        başarısız veya çalışmamış bir yan etki kullanıcıya görünür. Kaynak yalnız turun
        araç sonuçlarıdır; argüman ve çıktı metni asla eklenmez.
        """
        names = {
            tool_call.id: tool_call.name
            for step in record.steps
            if isinstance(step, Message)
            for tool_call in step.tool_calls
        }
        registry = self.runtime.registry if self.runtime is not None else None
        groups: dict[str, list[str]] = {}
        for step in record.steps:
            if not isinstance(step, ToolExchange) or step.result.status == "ok":
                continue
            name = names.get(step.call_id, "araç")
            tool = registry.get(name) if registry is not None else None
            # Bilinmeyen araç yan etkili sayılır (kural 3); yalnız bilinen READ_ONLY atlanır.
            if tool is not None and normalize_risk(tool.risk) is Risk.READ_ONLY:
                continue
            # Kayıtsız ad modelin ürettiği denetimsiz dizedir; Telegram'a yazılmaz.
            label = (
                (t(tool.label) if tool.label else name)
                if tool is not None
                else t("bilinmeyen araç")
            )
            labels = groups.setdefault(t(_FOOTER_TITLES[step.result.status]), [])
            if label not in labels:
                labels.append(label)
        if not groups:
            return ""
        lines = (f"{title}: {', '.join(labels)}" for title, labels in groups.items())
        return "\n\n" + "\n".join(lines)

    def _reply(self, result: Any, withheld: str = _PRIVATE_WITHHELD) -> None:
        if result.record is None:
            self._notify(
                t("Tur tamamlanmadı ({stop_reason}).").format(stop_reason=result.stop_reason)
            )
        elif result.record.answer_private:
            self._notify(t(withheld))
        else:
            body = (result.text or t("(boş yanıt)")) + self._status_footer(result.record)
            for chunk in split_text(body):
                try:
                    self.client.send_message(self.chat_id, chunk, kind="reply")
                except TelegramError:
                    # Tur gerçekleşti ve denetimde; yanıt iletilemese de döngü sürer.
                    break

    def _send_frame(
        self, runtime: Runtime, frames: tuple[bytes, ...] | None, answer_private: bool
    ) -> None:
        """Son ekran karesi (ADR 0053): üç kapı da açık olmadıkça HİÇ gönderilmez.

        Bayrak, `perception.allow_cloud_screen` (Telegram buluttur) ve tur/geçmiş gizliliği.
        Kare yalnız RAM'de durdu; denetime baytlar değil yalnız `telegram_out` olayı yazılır.
        """
        if not frames:
            return
        settings = runtime.context.settings
        allowed = settings.telegram.send_computer_screenshot
        if not (allowed and settings.perception.allow_cloud_screen):
            return
        if answer_private or self._private_reason(runtime) is not None:
            return
        # Her monitör ayrı fotoğraf (en çok _MAX_FRAMES); biri başarısız olsa diğerleri gider.
        for frame in frames[:_MAX_FRAMES]:
            with contextlib.suppress(TelegramError, ValueError):
                self.client.send_photo(self.chat_id, frame, kind="photo")

    async def _handle_or_stop(
        self, update: Mapping[str, Any], stop: asyncio.Event, stopper: asyncio.Future[Any]
    ) -> None:
        """Güncellemeyi işler; durdurma gelirse yürüyen turu iptal ettirip sınırlı bekler.

        Beklemeden çıkmak turu denetimsiz bırakır (iş parçacığı öldürülünce `turn_end` yazılmaz);
        normal iptal yolu kendi denetim kaydını yazsın diye kısa süre beklenir. Süre dolarsa
        `wait_for` görevi iptal eder ve koşucu `turn_cancelled` yazar.
        """
        task = asyncio.ensure_future(self.handle_update(update))
        await asyncio.wait({task, stopper}, return_when=asyncio.FIRST_COMPLETED)
        if task.done():
            task.result()  # beklenmeyen hata yutulmaz
            return
        if self.runtime is not None:
            self.runtime.runner.cancel_current()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(task, _STOP_GRACE_SECONDS)

    async def serve(self, stop: asyncio.Event, *, give_up_on_conflict: bool = False) -> str | None:
        """Güncellemeleri işler. `give_up_on_conflict`: başka bir yoklayıcı (HTTP 409) varsa
        yeniden denemeden çıkar ve "HTTP 409" döner (masaüstü kanalı, ADR 0052)."""
        backoff = 1.0
        # Uzun yoklama iş parçacığında sürer; durdurma onu beklemeden fark edilir.
        stopper = asyncio.ensure_future(stop.wait())
        try:
            while not stop.is_set():
                poll = asyncio.ensure_future(
                    asyncio.to_thread(self.client.get_updates, self._offset, self.poll_timeout)
                )
                await asyncio.wait({poll, stopper}, return_when=asyncio.FIRST_COMPLETED)
                if not poll.done():
                    # Terk edilen yoklama istemci kapanınca biter; sonucu hiç okunmaz.
                    poll.add_done_callback(lambda task: task.cancelled() or task.exception())
                    break
                try:
                    updates = poll.result()
                except TelegramError as error:
                    if give_up_on_conflict and error.error_type == "HTTP 409":
                        return error.error_type
                    # Hata istemcide denetlendi; kısa süre bekleyip yeniden dener.
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(stop.wait(), timeout=backoff)
                    backoff = min(backoff * 2, _MAX_BACKOFF)
                    continue
                backoff = 1.0
                if not updates and self.poll_timeout == 0:
                    # Zaman aşımı 0 ise sunucu hemen döner; boş döngü CPU'yu yakmasın.
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(stop.wait(), timeout=_IDLE_SLEEP)
                for update in updates:
                    # Önce ilerlet: işlenirken çökse bile aynı komut ikinci kez çalışmaz.
                    self._advance(update)
                    await self._handle_or_stop(update, stop, stopper)
                    if stop.is_set():
                        break
        finally:
            stopper.cancel()
        return None


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


STATUS_STARTING = "Telegram kanalı başlıyor"
STATUS_RUNNING = "Telegram kanalı açık"
STATUS_STOPPED = "Telegram kanalı durdu"
STATUS_PRIVATE = "Gizli profilde kapalı"
STATUS_CONFLICT = (
    "Çakışma: botu başka bir süreç dinliyor (`artemis telegram` açık olabilir); "
    "kanal durduruldu"
)
STATUS_ERROR = "Telegram hatası"
CONFLICT_ERROR = "HTTP 409"
_NOTICE_TEXT = "Artemis Telegram kanalı açık. Komut yazabilirsiniz; onaylar düğmeyle sorulur."


async def run_telegram(config_path: Path) -> int:
    """`artemis telegram` süreci: aynı çıktılar ve dönüş kodları, kendi proaktif zamanlayıcısı."""
    return await serve_telegram(config_path, asyncio.Event(), embedded=False, status=print)


async def serve_telegram(
    config_path: Path,
    stop: asyncio.Event,
    *,
    embedded: bool,
    status: Callable[[str], None],
    runtime_factory: Callable[..., Runtime] | None = None,
    stop_flag: threading.Event | None = None,
) -> int:
    """Kanalı `stop` kurulana dek sürdürür; 0 temiz, 1 bağlantı/çakışma, 2 kurulum eksiği.

    `embedded=False` CLI'dır (`status` = print, proaktif zamanlayıcı bu süreçte). `embedded=True`
    masaüstü içidir (ADR 0052): `status` kısa sabit Türkçe metin alır, ikinci proaktif
    zamanlayıcı BAŞLATILMAZ (masaüstünün kendisi var), proje iş deposu alınmaz, çakışmada
    (HTTP 409) yeniden denenmez. Onay, gizlilik ve denetim kuralları iki kipte aynıdır.
    `runtime_factory` yalnız testler içindir: (config_path, approve, settings=, audit=).
    """

    def say(cli_text: str, embedded_text: str) -> None:
        status(embedded_text if embedded else cli_text)

    settings = load_settings(config_path)
    if is_local_only_profile(settings.work_profile):
        say(t(_PRIVATE_PROFILE_BLOCKED), STATUS_PRIVATE)
        return 2
    config = settings.telegram
    if config.chat_id is None:
        say(
            t("Telegram kapalı: config içinde telegram.chat_id tanımlayın."),
            STATUS_ERROR,
        )
        return 2
    audit = AuditLog(settings.audit_path)
    client = build_telegram_client(settings, audit)
    if client is None:
        say(
            t("Telegram token'ı bulunamadı: {token_env} ortam değişkenini ayarlayın.").format(
                token_env=config.token_env
            ),
            f"{STATUS_ERROR} (token yok)",
        )
        return 2
    if not settings.primary_model and runtime_factory is None:
        client.close()
        say(
            t("Config içinde resmi katalogdan doğrulanmış bir primary_model tanımlayın."),
            f"{STATUS_ERROR} (model yok)",
        )
        return 2
    channel = TelegramChannel(
        client,
        config.chat_id,
        audit=audit,
        approval_timeout=config.approval_timeout_seconds,
        max_message_age=config.max_message_age_seconds,
        poll_timeout=config.poll_timeout_seconds,
        stop_flag=stop_flag,
    )
    # Gizlilik izlemesi: runtime kurulmadan önce de olaylar kaçmasın diye hemen bağlanır.
    audit.on_record = channel.observe
    runtime: Runtime | None = None
    try:
        if runtime_factory is not None:
            runtime = runtime_factory(config_path, channel.approve, settings=settings, audit=audit)
        else:
            runtime = create_runtime(
                config_path,
                channel.approve,
                settings=settings,
                audit=audit,
                with_code_agent=not embedded,
            )
        channel.attach(runtime)
        if embedded:
            # CLI'da host yok: `computer_use` orada reddedilmeye devam eder (ADR 0053).
            channel.enable_computer_use(config_path)
        # notify_telegram aracı aynı istemciyi kullanır; ikinci bağlantı havuzu açılmaz.
        runtime.context.services["telegram_client"] = client
        try:
            channel.skip_backlog()
            client.send_message(config.chat_id, t(_NOTICE_TEXT), kind="notice")
        except TelegramError as error:
            if embedded and error.error_type == CONFLICT_ERROR:
                status(STATUS_CONFLICT)
            else:
                say(
                    t("Telegram'a bağlanılamadı ({error_type}).").format(
                        error_type=error.error_type
                    ),
                    f"{STATUS_ERROR} ({error.error_type})",
                )
            return 1
        say(t("Artemis Telegram kanalı açık. Durdurmak için Ctrl+C."), STATUS_RUNNING)
        # Gömülü kipte masaüstünün kendi zamanlayıcısı vardır; ikincisi çift teslim riskidir.
        proactive = None if embedded else _start_proactive(runtime, client, stop)
        try:
            if await channel.serve(stop, give_up_on_conflict=embedded) == CONFLICT_ERROR:
                status(STATUS_CONFLICT)
                return 1
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
