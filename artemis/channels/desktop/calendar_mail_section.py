"""Ayarlar > Takvim ve posta: iki sır (ICS adresi, IMAP parolası) ve "Kaydet ve dene" (ADR 0059).

Sır alanları Telegram token alanıyla aynı desendedir (ADR 0051): parola kipinde, yalnız kimlik
kasasına yazılır, doğrulanmadan yazılmaz, hiçbir yerde gösterilmez/loglanmaz. Sunucu, kullanıcı ve
kutular ana panelin otomatik kaydındadır; deneme KAYITLI ayarları okur. Ağ çağrısı QThread'dedir.
"""

from __future__ import annotations

import contextlib
import http.client
import imaplib
import smtplib
import urllib.error
from collections.abc import Callable
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from PySide6.QtCore import QThread
from PySide6.QtWidgets import QLineEdit, QPushButton, QVBoxLayout, QWidget

from artemis.channels.desktop.form_kit import button_row, field, meta_label
from artemis.config.settings import load_settings
from artemis.i18n import t
from artemis.tools import calendar_mail as core
from artemis.tools import calendar_mail_write as core_write
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
        return t("Geçersiz saat dilimi.")
    except (ValueError, OSError):
        return t("Takvim okunamadı.")
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
        return t("Posta okunamadı.")
    return None


def check_smtp(host: str, port: int, username: str, password: str) -> str | None:
    """SMTP bağlantısını ve kimlik doğrulamasını dener; ileti göndermeden kapatır."""
    try:
        smtp = core_write.connect_smtp(host, port)
        try:
            smtp.login(username, password)
        finally:
            with contextlib.suppress(smtplib.SMTPException, OSError):
                smtp.quit()
    except smtplib.SMTPAuthenticationError:
        return t("SMTP girişi reddedildi; kullanıcı adı ve uygulama parolasını kontrol edin.")
    except (smtplib.SMTPException, OSError, ValueError):
        return t("SMTP sunucusuna bağlanılamadı.")
    return None


def check_caldav(url: str, username: str, password: str) -> str | None:
    """CalDAV ucunu OPTIONS ile yoklar; uygunsa None döndürür."""
    return core_write.check_caldav(url, username, password)


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
            self.error = str(exc) or t("Doğrulama tamamlanamadı.")
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
            self.error = t("Doğrulama tamamlanamadı.")
            self.verified = False
        else:
            if error is None:
                self.error = None
                self.verified = True
            else:
                self.error = str(error) or t("Doğrulama tamamlanamadı.")
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
        self.calendar_url.setAccessibleName(t("Gizli takvim adresi"))
        field(
            calendar, t("Gizli ICS adresi"), self.calendar_url,
            t(
                "Google Takvim: Ayarlar > takvimi entegre et > iCal biçiminde gizli adres. "
                "https:// ile başlamalı; yalnız kimlik kasasına yazılır."
            ),
        )
        self.calendar_save = QPushButton(t("Kaydet ve dene"))
        self.calendar_save.clicked.connect(self._save_calendar)
        self.calendar_delete = QPushButton(t("Kaydı sil"))
        self.calendar_delete.clicked.connect(self._delete_calendar)
        calendar.addLayout(button_row(self.calendar_delete, self.calendar_save))
        self.caldav_password = QLineEdit()
        self.caldav_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.caldav_password.setAccessibleName(t("CalDAV parolası"))
        field(
            calendar, t("CalDAV parolası"), self.caldav_password,
            t("İsteğe bağlı; yalnız kimlik kasasına yazılır."),
        )
        self.caldav_save = QPushButton(t("CalDAV parolasını kaydet ve dene"))
        self.caldav_save.clicked.connect(self._save_caldav)
        self.caldav_delete = QPushButton(t("CalDAV kaydını sil"))
        self.caldav_delete.clicked.connect(self._delete_caldav)
        calendar.addLayout(button_row(self.caldav_delete, self.caldav_save))
        self.mail_password = QLineEdit()
        self.mail_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.mail_password.setAccessibleName(t("Posta uygulama parolası"))
        field(
            mail, t("Uygulama parolası"), self.mail_password,
            t(
                "Gmail için hesap parolası değil, uygulama parolası. Sunucu ve kullanıcıyı "
                "yukarıya yazıp “Kaydedildi” yazısını bekle, sonra dene."
            ),
        )
        self.mail_save = QPushButton(t("Kaydet ve dene"))
        self.mail_save.clicked.connect(self._save_mail)
        self.mail_delete = QPushButton(t("Kaydı sil"))
        self.mail_delete.clicked.connect(self._delete_mail)
        mail.addLayout(button_row(self.mail_delete, self.mail_save))
        self.smtp_password = QLineEdit()
        self.smtp_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.smtp_password.setAccessibleName(t("SMTP parolası"))
        field(
            mail, t("SMTP parolası"), self.smtp_password,
            t(
                "Boş bırakılırsa yukarıdaki uygulama parolası kullanılır; "
                "yalnız kimlik kasasına yazılır."
            ),
        )
        self.smtp_save = QPushButton(t("SMTP parolasını kaydet ve dene"))
        self.smtp_save.clicked.connect(self._save_smtp)
        self.smtp_delete = QPushButton(t("SMTP kaydını sil"))
        self.smtp_delete.clicked.connect(self._delete_smtp)
        mail.addLayout(button_row(self.smtp_delete, self.smtp_save))
        self.message = meta_label("")
        self.message.setAccessibleName(t("Takvim ve posta işlem sonucu"))
        layout.addWidget(self.message)
        layout.addStretch(1)
        if demo:
            for widget in (
                self.calendar_url, self.calendar_save, self.calendar_delete,
                self.caldav_password, self.caldav_save, self.caldav_delete,
                self.mail_password, self.mail_save, self.mail_delete,
                self.smtp_password, self.smtp_save, self.smtp_delete,
            ):
                widget.setEnabled(False)

    def _busy(self) -> bool:
        return self._job is not None and not self._job.isFinished()

    def _start(self, job: Callable[[], str | None], secret: str, target: str, label: str) -> None:
        if self._busy():
            return
        self.message.setText(t("Deneniyor…"))
        thread = _Job(job, self)

        def done() -> None:
            if thread.error is not None or not thread.verified:
                detail = thread.error or t("Deneme sonucu alınamadı.")
                self.message.setText(t("{detail} Kaydedilmedi.").format(detail=detail))
                return
            try:
                write_secret(secret, target)
            except OSError:
                self.message.setText(t("Saklanamadı; ilgili ortam değişkenini tanımlayın."))
                return
            self.message.setText(
                t("{label} çalışıyor ve kimlik kasasına kaydedildi.").format(label=label)
            )

        thread.finished.connect(done)
        self._job = thread
        thread.start()

    def _save_calendar(self) -> None:
        url = self.calendar_url.text().strip()
        self.calendar_url.clear()
        if not url:
            self.message.setText(t("Önce gizli takvim adresini yaz."))
            return
        try:
            timezone = load_settings(self.config_path).proactive.timezone
        except (OSError, ValueError):
            self.message.setText(t("Ayarlar okunamadı."))
            return
        self._start(lambda: check_calendar(url, timezone), url, core.ICS_TARGET, t("Takvim"))

    def _save_mail(self) -> None:
        password = self.mail_password.text().strip()
        self.mail_password.clear()
        if not password:
            self.message.setText(t("Önce uygulama parolasını yaz."))
            return
        try:
            mail = load_settings(self.config_path).mail
        except (OSError, ValueError):
            self.message.setText(t("Ayarlar okunamadı."))
            return
        if not (mail.host and mail.username):
            self.message.setText(
                t("Önce sunucu ve kullanıcı adını yazıp “Kaydedildi” yazısını bekle.")
            )
            return
        self._start(
            lambda: check_mail(mail.host, mail.port, mail.username, password, mail.folder),
            password, core.MAIL_TARGET, t("Posta"),
        )

    def _save_caldav(self) -> None:
        password = self.caldav_password.text().strip()
        self.caldav_password.clear()
        if not password:
            self.message.setText(t("Önce CalDAV parolasını yaz."))
            return
        try:
            calendar = load_settings(self.config_path).calendar
        except (OSError, ValueError):
            self.message.setText(t("Ayarlar okunamadı."))
            return
        if not calendar.caldav_url:
            self.message.setText(
                t("Önce CalDAV adresini yazıp “Kaydedildi” yazısını bekle.")
            )
            return
        self._start(
            lambda: check_caldav(calendar.caldav_url, calendar.caldav_username, password),
            password, core_write.CALDAV_PASSWORD_TARGET, t("CalDAV"),
        )

    def _save_smtp(self) -> None:
        password = self.smtp_password.text().strip()
        self.smtp_password.clear()
        if not password:
            self.message.setText(t("Önce SMTP parolasını yaz."))
            return
        try:
            mail = load_settings(self.config_path).mail
        except (OSError, ValueError):
            self.message.setText(t("Ayarlar okunamadı."))
            return
        username = (mail.smtp_username or mail.username).strip()
        if not (mail.smtp_host and username):
            self.message.setText(
                t("Önce SMTP sunucusu ve kullanıcı adını yazıp “Kaydedildi” yazısını bekle.")
            )
            return
        self._start(
            lambda: check_smtp(mail.smtp_host, mail.smtp_port, username, password),
            password, core_write.SMTP_PASSWORD_TARGET, t("SMTP"),
        )

    # Bağlı yöntem: self'i yakalayan lambda Qt bağlantısında referans döngüsü kurar ve gc başka
    # iş parçacığında toplarsa uygulama çöker (bkz. tests/test_no_signal_cycles.py).
    def _delete_calendar(self) -> None:
        self._delete(core.ICS_TARGET, t("Takvim adresi"))

    def _delete_caldav(self) -> None:
        self._delete(core_write.CALDAV_PASSWORD_TARGET, t("CalDAV parolası"))

    def _delete_mail(self) -> None:
        self._delete(core.MAIL_TARGET, t("Posta parolası"))

    def _delete_smtp(self) -> None:
        self._delete(core_write.SMTP_PASSWORD_TARGET, t("SMTP parolası"))

    def _delete(self, target: str, label: str) -> None:
        delete_secret(target)
        self.message.setText(
            t("{label} kaydı silindi (ortam değişkenine dokunulmadı).").format(label=label)
        )

