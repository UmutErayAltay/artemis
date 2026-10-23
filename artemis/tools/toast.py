"""Windows toast bildirimi: sabit PowerShell betiği, metin yalnız ortam değişkeniyle gider.

Alt sürecin ortamı `os.environ` değil `child_environment()`'dır (bot/sağlayıcı token'ı
toast sürecine gitmez) ve sonuç bool değil dürüst bir `ToastOutcome`'dur.
Windows'a özgü hiçbir şey import
edilmez; toast bir `powershell.exe` alt sürecidir, bu yüzden Linux'ta da içe aktarılabilir.
"""

from __future__ import annotations

import base64
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from artemis.tools.platform import child_environment

TOAST_TITLE_LIMIT = 100
TOAST_BODY_LIMIT = 300

# Betik SABİTTİR ve kullanıcı/model metnini ASLA içermez. Metni betiğe gömmek kod
# enjeksiyonudur (`"; calc; "` gibi bir başlık betiği yönetirdi); ortam değişkeni ise
# VERİDİR, yorumlanmaz. XML'e de kaçırılarak girer (`SecurityElement.Escape`), yoksa `<` ve
# `&` içeren metin toast XML'ini bozar.
#
# Windows PowerShell 5.1 (`powershell.exe`) gerekir: PowerShell 7 WinRT tiplerini yükleyemez.
# `$appId` PowerShell'in kendi kayıtlı uygulama kimliğidir; kayıtsız
# kimlikle gösterilen toast sessizce yutulur. `Setting` Enabled değilse (odak yardımı, kapalı
# bildirimler) Show() hata vermeden hiçbir şey göstermez; bu yüzden önce sınanır ve çıkış
# kodu 3 ile ayrı bildirilir: "gösterildi" demek ancak bundan sonra doğrudur.
TOAST_SCRIPT = "\n".join(
    [
        "$ErrorActionPreference = 'Stop'",
        "[Windows.UI.Notifications.ToastNotificationManager, "
        "Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null",
        "[Windows.Data.Xml.Dom.XmlDocument, "
        "Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null",
        "$title = [System.Security.SecurityElement]::Escape($env:ARTEMIS_TOAST_TITLE)",
        "$body = [System.Security.SecurityElement]::Escape($env:ARTEMIS_TOAST_BODY)",
        "$xml = New-Object Windows.Data.Xml.Dom.XmlDocument",
        "$xml.LoadXml(\"<toast><visual><binding template='ToastGeneric'>"
        '<text>$title</text><text>$body</text></binding></visual></toast>")',
        "$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)",
        "$appId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}"
        "\\WindowsPowerShell\\v1.0\\powershell.exe'",
        "$notifier = [Windows.UI.Notifications.ToastNotificationManager]"
        "::CreateToastNotifier($appId)",
        "if ($notifier.Setting -ne 'Enabled') { exit 3 }",
        "$notifier.Show($toast)",
        "exit 0",
        "",
    ]
)


@dataclass(frozen=True)
class ToastOutcome:
    status: Literal["ok", "disabled", "error"]
    message: str


def _shorten(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def clean_toast_text(text: str, limit: int) -> str:
    """Ortam değişkenine ve XML'e güvenli hale getirir, sonra keser.

    NUL ortam değişkeninde `ValueError` üretir; XML 1.0 sekme/satır sonu dışındaki kontrol
    karakterlerini kabul etmez ve `LoadXml` patlar. İkisi de model metninde gelebilir.
    """
    cleaned = "".join(ch for ch in text if ch in "\t\n\r" or (ord(ch) >= 32 and ord(ch) != 127))
    return _shorten(cleaned, limit)


def show_toast(
    title: str,
    body: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    platform: str | None = None,
    timeout: float = 15.0,
) -> ToastOutcome:
    current = sys.platform if platform is None else platform
    if current != "win32":
        return ToastOutcome("error", "Masaüstü bildirimi yalnız Windows'ta çalışır.")
    clean_title = clean_toast_text(title, TOAST_TITLE_LIMIT)
    clean_body = clean_toast_text(body, TOAST_BODY_LIMIT)
    if not clean_title.strip() and not clean_body.strip():
        return ToastOutcome("error", "Bildirim metni boş.")
    # `-EncodedCommand`: betik UTF-16-LE + base64; tırnak/kaçış sorunu olmaz ve metin argv'de yok.
    encoded = base64.b64encode(TOAST_SCRIPT.encode("utf-16-le")).decode("ascii")
    command = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-EncodedCommand",
        encoded,
    ]
    env = {
        **child_environment(),
        "ARTEMIS_TOAST_TITLE": clean_title,
        "ARTEMIS_TOAST_BODY": clean_body,
    }
    try:
        completed = runner(
            command,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            # Konsol penceresi yanıp sönmesin. POSIX'te bayrak 0 olmak zorunda.
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        return ToastOutcome("error", "Bildirim zaman aşımına uğradı.")
    except (OSError, ValueError) as error:
        # İstisna metni yol/ortam ayrıntısı taşıyabilir; yalnız tür adı yazılır.
        return ToastOutcome("error", f"Bildirim başlatılamadı ({type(error).__name__}).")
    if completed.returncode == 0:
        return ToastOutcome("ok", "Bildirim gösterildi.")
    if completed.returncode == 3:
        return ToastOutcome("disabled", "Windows bildirimleri bu uygulama için kapalı.")
    return ToastOutcome("error", f"Bildirim gösterilemedi (çıkış kodu {completed.returncode}).")
