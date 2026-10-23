"""CPA anahtarını Windows Kimlik Bilgisi Yöneticisi'nde tutar (açılışta başlama için)."""

from __future__ import annotations

import ctypes
import os
import sys

TARGET = "Artemis/OPENROUTER_API_KEY"
# Eski ad (Jarvis dönemi): okurken yedek, silerken birlikte temizlenir; yazım yalnız yeni ada.
_PREFIX = "Artemis/"
_LEGACY_PREFIX = "Jarvis/"
ENV_NAME = "OPENROUTER_API_KEY"
_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2


if sys.platform == "win32":
    from ctypes import wintypes

    class _Credential(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_char)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    def _read_secret_windows(target: str) -> str | None:
        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        pointer = ctypes.POINTER(_Credential)()
        if not advapi.CredReadW(target, _CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
            return None
        try:
            cred = pointer.contents
            raw = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
            return raw.decode("utf-16-le")
        finally:
            advapi.CredFree(pointer)

    def _write_secret_windows(secret: str, target: str) -> None:
        blob = secret.encode("utf-16-le")
        buffer = ctypes.create_string_buffer(blob, len(blob))
        cred = _Credential(
            Type=_CRED_TYPE_GENERIC,
            TargetName=target,
            CredentialBlobSize=len(blob),
            CredentialBlob=ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)),
            Persist=_CRED_PERSIST_LOCAL_MACHINE,
            UserName="artemis",
        )
        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        if not advapi.CredWriteW(ctypes.byref(cred), 0):
            raise OSError(f"Anahtar kaydedilemedi (hata {ctypes.get_last_error()}).")

    def _delete_secret_windows(target: str) -> None:
        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        advapi.CredDeleteW(target, _CRED_TYPE_GENERIC, 0)  # yoksa zaten silinmiş sayılır


def _legacy_target(target: str) -> str | None:
    return _LEGACY_PREFIX + target[len(_PREFIX) :] if target.startswith(_PREFIX) else None


def read_secret(target: str = TARGET) -> str | None:
    """Önce `Artemis/<ad>`, yoksa eski `Jarvis/<ad>` kaydı (veri kaybı olmasın diye)."""
    # Windows gövdesi platform bloğunda tanımlı; mypy Linux'ta bu dalı hiç analiz etmez.
    if sys.platform != "win32":
        return None
    value = _read_secret_windows(target)
    legacy = _legacy_target(target)
    if value is None and legacy is not None:
        value = _read_secret_windows(legacy)
    return value


def write_secret(secret: str, target: str = TARGET) -> None:
    if sys.platform == "win32":
        _write_secret_windows(secret, target)
        return
    raise OSError("Kimlik Bilgisi Yöneticisi yalnız Windows'ta var.")


def delete_secret(target: str) -> None:
    """Yeni ve eski adlı kaydı birlikte siler; eski kayıt kalırsa silinen anahtar geri okunurdu."""
    if sys.platform == "win32":
        _delete_secret_windows(target)
        legacy = _legacy_target(target)
        if legacy is not None:
            _delete_secret_windows(legacy)


def load_into_environment() -> None:
    """Ortamda anahtar yoksa kasadakini yalnız bu sürecin ortamına koyar; çocuklara geçmez."""
    if not os.environ.get(ENV_NAME):
        secret = read_secret()
        if secret:
            os.environ[ENV_NAME] = secret
