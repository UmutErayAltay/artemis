"""Ayarlar > Telegram: token (kimlik kasası), sohbet eşleştirme, test ve bağlantıyı kesme.

ADR 0051. Token YAML'a, log'a, denetime, `repr`'e ve durum metnine girmez; yalnız
`Artemis/telegram` kasa kaydına (ya da `token_env` ortam değişkenine) yazılır. İki yönlü kanalın
(`artemis telegram`) başlatılması bu panelin işi değildir.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml
from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget

from artemis.channels.desktop.provider_section import button_row, field, group, meta_label
from artemis.channels.telegram.api import (
    TOKEN_TARGET,
    TelegramClient,
    TelegramError,
    resolve_telegram_token,
)
from artemis.channels.telegram.channel import _FORWARD_KEYS
from artemis.config.settings import TelegramConfig, load_settings
from artemis.safety.audit import AuditLog
from artemis.tools.credentials import delete_secret, write_secret

PAIR_SECONDS = 120.0
POLL_SECONDS = 2  # kısa sorgu: iptal en geç bu kadar sürede fark edilir
MAX_WRONG_CODES = 5  # bu kadar yanlış özel metinden sonra eşleştirme iptal olur
LOCKED_TEXT = (
    "Çok fazla yanlış kod denendi; eşleştirme iptal edildi. Yeniden başlatınca yeni kod üretilir."
)
TEST_TEXT = "Artemis test mesajı: bağlantı çalışıyor."
NOTICE_TEXT = "Artemis bağlandı."
CONFLICT_TEXT = (
    "Botu başka bir süreç dinliyor (`artemis telegram` açık olabilir). Onu durdurup tekrar dene."
)


def match_pairing(update: dict[str, Any], code: str) -> int | None:
    """Yalnız özel sohbette, kullanıcının kendi, iletilmemiş, düz metin kodunu kabul eder."""
    message = update.get("message")
    if not isinstance(message, dict) or any(key in message for key in _FORWARD_KEYS):
        return None
    chat, sender = message.get("chat"), message.get("from")
    if not isinstance(chat, dict) or not isinstance(sender, dict):
        return None
    chat_id = chat.get("id")
    if chat.get("type") != "private" or sender.get("is_bot") is True:
        return None
    if not isinstance(chat_id, int) or isinstance(chat_id, bool) or chat_id != sender.get("id"):
        return None
    text = message.get("text")
    if not isinstance(text, str) or not secrets.compare_digest(text.strip(), code):
        return None
    return chat_id


def pair_chat(
    client: TelegramClient,
    code: str,
    *,
    stop: Callable[[], bool],
    seconds: float = PAIR_SECONDS,
    idle: Callable[[], None] = lambda: time.sleep(0.2),
) -> tuple[int, bool] | None:
    """Kodu bekler; (chat_id, kod mesajı onaylandı mı) ya da süre/iptalde None. HTTP 409 yükselir.

    Eşleşmeyen güncellemeler sessizce tüketilir (offset ilerler); hiçbiri log'a/duruma girmez.
    """
    deadline = time.monotonic() + seconds
    offset: int | None = None
    wrong = 0
    while time.monotonic() < deadline and not stop():
        updates = client.get_updates(offset, POLL_SECONDS)
        if not updates:
            idle()
            continue
        for update in updates:
            update_id = update.get("update_id")
            if isinstance(update_id, int) and not isinstance(update_id, bool):
                offset = max(offset or 0, update_id + 1)
            chat_id = match_pairing(update, code)
            if chat_id is not None:
                try:
                    client.get_updates(offset, 0)  # kod mesajı bir daha komut gibi okunmasın
                except TelegramError:
                    return chat_id, False
                return chat_id, True
            if _is_private_text(update):
                wrong += 1
                if wrong >= MAX_WRONG_CODES:
                    # 6 hane + 120 sn tek başına tahmini engellemez; sınır şansı ~5/10^6'ya indirir.
                    raise TelegramError("Eşleştirme kilitlendi", error_type="PairingLocked")
    return None


def _is_private_text(update: dict[str, Any]) -> bool:
    """Kod tahmini sayılabilecek güncelleme: özel sohbetten düz metin (gürültü sayılmaz)."""
    message = update.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("text"), str):
        return False
    chat = message.get("chat")
    return isinstance(chat, dict) and chat.get("type") == "private"


class TelegramJob(QThread):
    """Tek iş: sonuç ya da TelegramError `result`'ta kalır; ağ çağrısı arayüzde olmaz."""

    note = Signal(str)

    def __init__(self, job: Callable[[TelegramJob], object], parent: QWidget) -> None:
        super().__init__(parent)
        self._job = job
        self.result: object = None

    def run(self) -> None:
        try:
            self.result = self._job(self)
        except TelegramError as error:
            self.result = error


class TelegramSection(QWidget):
    changed = Signal()  # chat_id değişti: çalışma zamanı yeniden kurulmalı (proaktif teslim)

    def __init__(self, config_path: Path, *, demo: bool = False) -> None:
        super().__init__()
        self.config_path = config_path
        self._demo = demo
        self._username: str | None = None
        self._job: TelegramJob | None = None
        self._on_done: Callable[[Any], None] | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(24)
        box, body = group("Bot ve sohbet", divided=False)
        layout.addWidget(box)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setAccessibleName("Telegram durumu")
        body.addWidget(self.status)
        self.token = QLineEdit()
        self.token.setEchoMode(QLineEdit.EchoMode.Password)
        self.token.setAccessibleName("Telegram bot token'ı")
        self.save_button = QPushButton("Kaydet ve doğrula")
        self.save_button.clicked.connect(self._save_token)
        field(
            body, "Bot token'ı", self.token,
            "@BotFather'dan alınan token yalnız kimlik kasasına yazılır.",
        )
        body.addLayout(button_row(self.save_button))
        pair_box, pair = group("Sohbet")
        layout.addWidget(pair_box)
        self.message = meta_label("")
        self.message.setAccessibleName("Telegram işlem sonucu")
        pair.addWidget(self.message)
        self.pair_button = QPushButton("Eşleştir")
        self.pair_button.clicked.connect(self._pair)
        self.cancel_button = QPushButton("Eşleştirmeyi iptal et")
        self.cancel_button.clicked.connect(self._cancel_pair)
        self.test_button = QPushButton("Test mesajı gönder")
        self.test_button.clicked.connect(self._send_test)
        self.disconnect_button = QPushButton("Bağlantıyı kes")
        self.disconnect_button.clicked.connect(self._disconnect)
        pair.addLayout(button_row(
            self.disconnect_button, self.test_button, self.cancel_button, self.pair_button
        ))
        self.hint = meta_label(
            "İki yönlü kanal (Telegram'dan komut) için `artemis telegram` komutunu ayrıca "
            "çalıştır; bu panel onu başlatmaz."
        )
        pair.addWidget(self.hint)
        layout.addStretch(1)
        self.refresh()

    # Durum ----------------------------------------------------------------------------------

    def _config(self) -> tuple[TelegramConfig, Path] | None:
        try:
            settings = load_settings(self.config_path)
        except (OSError, ValueError):
            return None
        return settings.telegram, settings.audit_path

    def _busy(self) -> bool:
        # isFinished(): bitiş sinyali işlenirken isRunning() hâlâ true olabilir.
        return self._job is not None and not self._job.isFinished()

    def refresh(self) -> None:
        loaded = self._config()
        if loaded is None:
            self.status.setText("Ayarlar okunamadı.")
            token, chat_id = None, None
        else:
            token, chat_id = resolve_telegram_token(loaded[0]), loaded[0].chat_id
            if token is None:
                self.status.setText("Token yok")
            elif chat_id is None:
                self.status.setText("Token kayıtlı, sohbet bağlı değil")
            else:
                bot = f"@{self._username}, " if self._username else ""
                self.status.setText(f"Bağlı: {bot}sohbet {chat_id}")
        live = not self._demo and loaded is not None
        busy = self._busy()
        self.token.setEnabled(live and not busy)
        self.save_button.setEnabled(live and not busy)
        self.pair_button.setEnabled(live and not busy and token is not None)
        self.test_button.setEnabled(live and not busy and token is not None and chat_id is not None)
        self.disconnect_button.setEnabled(
            live and not busy and (token is not None or chat_id is not None)
        )
        self.cancel_button.setEnabled(live and busy)

    def _start(self, job: Callable[[TelegramJob], object], done: Callable[[Any], None]) -> None:
        if self._busy():
            return
        thread = TelegramJob(job, self)
        thread.note.connect(self.message.setText)
        # Bağlı yöntem: ardıl iş parçacığı değil, arayüz iş parçacığında çalışır.
        thread.finished.connect(self._job_finished)
        self._job = thread
        self._on_done = done
        thread.start()
        self.refresh()

    def _job_finished(self) -> None:
        done, job = self._on_done, self._job
        self._on_done = None
        if done is not None and job is not None:
            done(job.result)
        self.refresh()

    def _client(self, token: str, config: TelegramConfig, audit_path: Path) -> TelegramClient:
        return TelegramClient(
            token, config.api_base, audit=AuditLog(audit_path),
            timeout=config.request_timeout_seconds,
        )

    def _write_chat_id(self, chat_id: int | None) -> bool:
        # Döngüsel içe aktarmayı önlemek için geç alınır (settings_panel bu bölümü içerir).
        from artemis.channels.desktop.settings_panel import _write_validated

        try:
            raw = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
            section = raw.get("telegram") or {}
            if chat_id is None:
                section.pop("chat_id", None)
            else:
                section["chat_id"] = chat_id
            if section:
                raw["telegram"] = section
            else:
                raw.pop("telegram", None)
            _write_validated(self.config_path, raw)
        except (OSError, ValueError, yaml.YAMLError, AttributeError):
            self.message.setText("Ayarlar kaydedilemedi; mevcut ayarlar korunuyor.")
            return False
        return True

    # Token ----------------------------------------------------------------------------------

    def _save_token(self) -> None:
        typed = self.token.text().strip()
        self.token.clear()
        loaded = self._config()
        if not typed or loaded is None:
            self.message.setText("Önce BotFather'dan aldığın token'ı yaz.")
            return
        config, audit_path = loaded
        self.message.setText("Token doğrulanıyor…")

        def job(_thread: TelegramJob) -> object:
            client = self._client(typed, config, audit_path)
            try:
                return client.get_me()
            finally:
                client.close()

        self._start(job, lambda result: self._token_checked(typed, config, result))

    def _token_checked(self, typed: str, config: TelegramConfig, result: object) -> None:
        if isinstance(result, TelegramError):
            # Doğrulanmayan token kasaya yazılmaz (geçersizse mevcut kayıt da korunur).
            self.message.setText(
                "Token geçersiz; kaydedilmedi." if result.error_type in {"HTTP 401", "HTTP 404"}
                else f"Doğrulanamadı ({result.error_type}); token kaydedilmedi."
            )
            return
        try:
            write_secret(typed, TOKEN_TARGET)
        except OSError:
            self.message.setText(f"Token saklanamadı; ortamda {config.token_env} tanımlayın.")
            return
        self._username = str(result)
        self.message.setText(f"Doğrulandı: @{self._username}. Şimdi sohbeti eşleştir.")
        self.changed.emit()

    # Eşleştirme -----------------------------------------------------------------------------

    def _pair(self) -> None:
        loaded = self._config()
        token = resolve_telegram_token(loaded[0]) if loaded else None
        if loaded is None or token is None:
            self.message.setText("Önce token kaydet.")
            return
        config, audit_path = loaded
        code = f"{secrets.randbelow(10**6):06d}"
        self.message.setText("Bot doğrulanıyor…")

        def job(thread: TelegramJob) -> object:
            client = self._client(token, config, audit_path)
            try:
                username = client.get_me()
                thread.note.emit(f"Telegram'da @{username} botuna şu kodu yaz: {code}")
                found = pair_chat(client, code, stop=thread.isInterruptionRequested)
                return (username, found)
            finally:
                client.close()

        self._start(job, lambda result: self._paired(token, config, audit_path, result))

    def _paired(
        self, token: str, config: TelegramConfig, audit_path: Path, result: object
    ) -> None:
        if isinstance(result, TelegramError):
            texts = {"HTTP 409": CONFLICT_TEXT, "PairingLocked": LOCKED_TEXT}
            self.message.setText(
                texts.get(result.error_type, f"Eşleştirme başarısız ({result.error_type}).")
            )
            return
        username: str
        found: tuple[int, bool] | None
        username, found = result  # type: ignore[misc]
        self._username = username
        if found is None:
            self.message.setText("Eşleştirme sona erdi: kod gelmedi ya da iptal edildi.")
            return
        chat_id, confirmed = found
        if not self._write_chat_id(chat_id):
            return
        self.changed.emit()
        suffix = "" if confirmed else " Kod mesajı onaylanamadı; `artemis telegram` açıksa dikkat."
        self.message.setText(f"Sohbet {chat_id} bağlandı.{suffix}")
        self._send(token, config, audit_path, chat_id, NOTICE_TEXT, "Artemis bağlandı bildirimi")

    def _cancel_pair(self) -> None:
        if self._busy() and self._job is not None:
            self._job.requestInterruption()
            self.message.setText("İptal ediliyor…")

    # Mesaj ----------------------------------------------------------------------------------

    def _send(
        self, token: str, config: TelegramConfig, audit_path: Path,
        chat_id: int, text: str, label: str,
    ) -> None:
        def job(_thread: TelegramJob) -> object:
            client = self._client(token, config, audit_path)
            try:
                client.send_message(chat_id, text, kind="notice")
                return None
            finally:
                client.close()

        def done(result: object) -> None:
            if isinstance(result, TelegramError):
                self.message.setText(f"{label} gönderilemedi ({result.error_type}).")
            else:
                self.message.setText(f"{label} gönderildi.")

        self._start(job, done)

    def _send_test(self) -> None:
        loaded = self._config()
        token = resolve_telegram_token(loaded[0]) if loaded else None
        if loaded is None or token is None or loaded[0].chat_id is None:
            self.message.setText("Önce token kaydet ve sohbeti eşleştir.")
            return
        self._send(token, loaded[0], loaded[1], loaded[0].chat_id, TEST_TEXT, "Test mesajı")

    def _disconnect(self) -> None:
        if self._busy():
            return
        if not self._write_chat_id(None):
            return
        delete_secret(TOKEN_TARGET)  # ortam değişkenine dokunmaz
        self._username = None
        loaded = self._config()
        still = loaded is not None and resolve_telegram_token(loaded[0]) is not None
        self.message.setText(
            "Bağlantı kesildi."
            + (" Token ortam değişkeninden geliyor; ona dokunulmadı." if still else "")
        )
        self.refresh()
        self.changed.emit()

    def finish(self) -> None:
        """Panel kapanırken eşleştirme/istek iş parçacığı bitmeden yıkılmasın."""
        if self._job is not None and self._job.isRunning():
            self._job.requestInterruption()
            self._job.wait(15000)  # en çok bir kısa sorgu (2 sn) + istek zaman aşımı
