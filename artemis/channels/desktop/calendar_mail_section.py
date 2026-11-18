"""Ayarlar > Takvim ve posta: iki sır (ICS adresi, IMAP parolası) ve "Kaydet ve dene" (ADR 0059).

Sır alanları Telegram token alanıyla aynı desendedir (ADR 0051): parola kipinde, yalnız kimlik
kasasına yazılır, doğrulanmadan yazılmaz, hiçbir yerde gösterilmez/loglanmaz. Sunucu, kullanıcı ve
kutular ana panelin otomatik kaydındadır; deneme KAYITLI ayarları okur. Ağ çağrısı QThread'dedir.
"""

from __future__ import annotations

import http.client
import imaplib
import urllib.error
from collections.abc import Callable
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from PySide6.QtCore import QThread
from PySide6.QtWidgets import QLineEdit, QPushButton, QVBoxLayout, QWidget

from artemis.channels.desktop.form_kit import button_row, field, meta_label
from artemis.config.settings import load_settings
from artemis.tools import calendar_mail as core
from artemis.tools.credentials import delete_secret, write_secret


def check_calendar(url: str, timezone: str) -> str | None:
    """Adres okunup ayrıştırılabiliyorsa None, yoksa kullanıcıya gösterilecek hata."""
    try:
        local = ZoneInfo(timezone)
        begin, end = core.day_window(local, None, 1)
        core.parse_events(core.fetch_ics(url), local, begin, end)
    except core.SourceError as error:
        return str(error)
    except ZoneInfoNotFoundError:
        return "Geçersiz saat dilimi."
    except (ValueError, OSError):
        return "Takvim okunamadı."
    return None


def check_mail(host: str, port: int, username: str, password: str, folder: str) -> str | None:
    try:
        core.read_unread(
            core.connect_imap, host=host, port=port, username=username,
            password=password, folder=folder, limit=1,
        )
    except core.SourceError as error:
        return str(error)
    except (imaplib.IMAP4.error, OSError, ValueError):
        return "Posta okunamadı."
    return None


class _Job(QThread):
    def __init__(self, job: Callable[[], str | None], parent: QWidget) -> None:
        super().__init__(parent)
        self._job = job
        self.error: str | None = None
        # Doğrulama gerçekten None ile tamamlanmadıkça sır kaydedilmemelidir.
        self.verified = False

    def run(self) -> None:
        try:
            error = self._job()
        except core.SourceError as exc:
            self.error = str(exc) or "Doğrulama tamamlanamadı."
            self.verified = False
        except (
            ZoneInfoNotFoundError,
            imaplib.IMAP4.error,
            urllib.error.URLError,
            http.client.HTTPException,
            OSError,
            ValueError,
        ):
            # Ham hata iletisi gizli adres/parola içerebileceğinden genel metin verilir.
            self.error = "Doğrulama tamamlanamadı."
            self.verified = False
        else:
            if error is None:
                self.error = None
                self.verified = True
            else:
                self.error = str(error) or "Doğrulama tamamlanamadı."
                self.verified = False


class CalendarMailSection(QWidget):
    """Gizli alanlar; kutuları (Takvim, Posta) ayar sayfası kurar ve bu bölüme verir."""

    def __init__(
        self, config_path: Path, *, demo: bool = False,
        calendar: QVBoxLayout, mail: QVBoxLayout,
    ) -> None:
        super().__init__()
        self.config_path = config_path
        self._job: _Job | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(24)
        self.calendar_url = QLineEdit()
        self.calendar_url.setEchoMode(QLineEdit.EchoMode.Password)
        self.calendar_url.setAccessibleName("Gizli takvim adresi")
        field(
            calendar, "Gizli ICS adresi", self.calendar_url,
            "Google Takvim: Ayarlar > takvimi entegre et > iCal biçiminde gizli adres. "
            "https:// ile başlamalı; yalnız kimlik kasasına yazılır.",
        )
        self.calendar_save = QPushButton("Kaydet ve dene")
        self.calendar_save.clicked.connect(self._save_calendar)
        self.calendar_delete = QPushButton("Kaydı sil")
        self.calendar_delete.clicked.connect(self._delete_calendar)
        calendar.addLayout(button_row(self.calendar_delete, self.calendar_save))
        self.mail_password = QLineEdit()
        self.mail_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.mail_password.setAccessibleName("Posta uygulama parolası")
        field(
            mail, "Uygulama parolası", self.mail_password,
            "Gmail için hesap parolası değil, uygulama parolası. Sunucu ve kullanıcıyı yukarıya "
            "yazıp “Kaydedildi” yazısını bekle, sonra dene.",
        )
        self.mail_save = QPushButton("Kaydet ve dene")
        self.mail_save.clicked.connect(self._save_mail)
        self.mail_delete = QPushButton("Kaydı sil")
        self.mail_delete.clicked.connect(self._delete_mail)
        mail.addLayout(button_row(self.mail_delete, self.mail_save))
        self.message = meta_label("")
        self.message.setAccessibleName("Takvim ve posta işlem sonucu")
        layout.addWidget(self.message)
        layout.addStretch(1)
        if demo:
            for widget in (
                self.calendar_url, self.calendar_save, self.calendar_delete,
                self.mail_password, self.mail_save, self.mail_delete,
            ):
                widget.setEnabled(False)

    def _busy(self) -> bool:
        return self._job is not None and not self._job.isFinished()

    def _start(self, job: Callable[[], str | None], secret: str, target: str, label: str) -> None:
        if self._busy():
            return
        self.message.setText("Deneniyor…")
        thread = _Job(job, self)

        def done() -> None:
            if thread.error is not None or not thread.verified:
                detail = thread.error or "Deneme sonucu alınamadı."
                self.message.setText(f"{detail} Kaydedilmedi.")
                return
            try:
                write_secret(secret, target)
            except OSError:
                self.message.setText("Saklanamadı; ilgili ortam değişkenini tanımlayın.")
                return
            self.message.setText(f"{label} çalışıyor ve kimlik kasasına kaydedildi.")

        thread.finished.connect(done)
        self._job = thread
        thread.start()

    def _save_calendar(self) -> None:
        url = self.calendar_url.text().strip()
        self.calendar_url.clear()
        if not url:
            self.message.setText("Önce gizli takvim adresini yaz.")
            return
        try:
            timezone = load_settings(self.config_path).proactive.timezone
        except (OSError, ValueError):
            self.message.setText("Ayarlar okunamadı.")
            return
        self._start(lambda: check_calendar(url, timezone), url, core.ICS_TARGET, "Takvim")

    def _save_mail(self) -> None:
        password = self.mail_password.text().strip()
        self.mail_password.clear()
        if not password:
            self.message.setText("Önce uygulama parolasını yaz.")
            return
        try:
            mail = load_settings(self.config_path).mail
        except (OSError, ValueError):
            self.message.setText("Ayarlar okunamadı.")
            return
        if not (mail.host and mail.username):
            self.message.setText(
                "Önce sunucu ve kullanıcı adını yazıp “Kaydedildi” yazısını bekle."
            )
            return
        self._start(
            lambda: check_mail(mail.host, mail.port, mail.username, password, mail.folder),
            password, core.MAIL_TARGET, "Posta",
        )

    # Bağlı yöntem: self'i yakalayan lambda Qt bağlantısında referans döngüsü kurar ve gc başka
    # iş parçacığında toplarsa uygulama çöker (bkz. tests/test_no_signal_cycles.py).
    def _delete_calendar(self) -> None:
        self._delete(core.ICS_TARGET, "Takvim adresi")

    def _delete_mail(self) -> None:
        self._delete(core.MAIL_TARGET, "Posta parolası")

    def _delete(self, target: str, label: str) -> None:
        delete_secret(target)
        self.message.setText(f"{label} kaydı silindi (ortam değişkenine dokunulmadı).")

