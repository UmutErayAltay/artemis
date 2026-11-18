"""Konuşma hafızası için sütun düzeyinde şifreleme (Windows DPAPI, ADR 0076)."""

from __future__ import annotations

import base64
import binascii
import ctypes
import functools
import logging
import sys
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    pass

PREFIX = "dpapi1:"
PLAIN_PREFIX = "plain1:"
_ENTROPY = b"artemis-memory-v1"
_CRYPTPROTECT_UI_FORBIDDEN = 0x1

_logger = logging.getLogger(__name__)
_warned_non_windows = False


class UnreadableMemory(ValueError):
    """Şifreli hafıza kaydı çözülemediğinde fırlatılır; içerik taşımaz."""


class Protector(Protocol):
    encrypts: bool

    def protect(self, text: str) -> str: ...

    def unprotect(self, text: str) -> str: ...


class PlainProtector:
    """Şifreleme kapalıyken kullanılan koruyucu; düz metni olduğu gibi bırakır."""

    encrypts: bool = False

    def protect(self, text: str) -> str:
        if text.startswith(PREFIX) or text.startswith(PLAIN_PREFIX):
            return PLAIN_PREFIX + text
        return text

    def unprotect(self, text: str) -> str:
        if text.startswith(PLAIN_PREFIX):
            return text[len(PLAIN_PREFIX) :]
        if text.startswith(PREFIX):
            raise UnreadableMemory("Şifreli hafıza kaydı şifreleme kapalıyken okunamıyor.")
        return text


# Her çağrıda yeni ctypes.Structure/POINTER tanımlamak kalıcı önbellek sızıntısına yol açar.
@functools.lru_cache(maxsize=1)
def _load_dpapi() -> tuple[Any, Any, type[ctypes.Structure]]:
    if sys.platform != "win32":
        raise UnreadableMemory("DPAPI yalnız Windows üzerinde kullanılabilir.")
    from ctypes import wintypes

    class _DataBlob(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)

    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL

    blob_ptr = ctypes.POINTER(_DataBlob)
    crypt32.CryptProtectData.argtypes = [
        blob_ptr,
        wintypes.LPCWSTR,
        blob_ptr,
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        blob_ptr,
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL

    crypt32.CryptUnprotectData.argtypes = [
        blob_ptr,
        ctypes.POINTER(wintypes.LPWSTR),
        blob_ptr,
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        blob_ptr,
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL

    return crypt32, kernel32, _DataBlob


def _last_win_error() -> OSError:
    """Son Windows API hatası. ctypes.WinError yalnız Windows'ta tanımlı; tip denetimi Linux'ta da
    koşar, bu yüzden erişim platform dalına alınır (DPAPI yolu zaten yalnız Windows'ta çağrılır)."""
    if sys.platform == "win32":
        return ctypes.WinError(ctypes.get_last_error())
    return OSError("DPAPI yalnız Windows'ta kullanılabilir.")


def _make_blob(blob_cls: type[ctypes.Structure], data: bytes) -> tuple[Any, Any]:
    if not data:
        # CryptProtectData pbData=NULL iken ERROR_INVALID_PARAMETER (87) döner; 1 baytlık
        # sahte tampon ve cbData=0 ile boş metin güvenle şifrelenir.
        buf = (ctypes.c_ubyte * 1)()
        ptr = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
        return blob_cls(0, ptr), buf
    buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    ptr = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
    return blob_cls(len(data), ptr), buf


class DpapiProtector:
    """Windows kullanıcı kapsamlı DPAPI koruyucusu."""

    encrypts: bool = True
    prefix: str = PREFIX

    def protect(self, text: str) -> str:
        try:
            crypt32, kernel32, blob_cls = _load_dpapi()
            raw = text.encode("utf-8")
            in_blob, _in_buf = _make_blob(blob_cls, raw)
            entropy_blob, _ent_buf = _make_blob(blob_cls, _ENTROPY)
            out_blob = blob_cls()
            try:
                ok = crypt32.CryptProtectData(
                    ctypes.byref(in_blob),
                    None,
                    ctypes.byref(entropy_blob),
                    None,
                    None,
                    _CRYPTPROTECT_UI_FORBIDDEN,
                    ctypes.byref(out_blob),
                )
                if not ok:
                    raise _last_win_error()
                cipher = ctypes.string_at(out_blob.pbData, out_blob.cbData)
            finally:
                if out_blob.pbData:
                    kernel32.LocalFree(out_blob.pbData)
        except UnreadableMemory:
            raise
        except (OSError, ValueError, ctypes.ArgumentError) as error:
            raise UnreadableMemory("Hafıza kaydı şifrelenemedi.") from error
        return PREFIX + base64.b64encode(cipher).decode("ascii")

    def unprotect(self, text: str) -> str:
        if text.startswith(PLAIN_PREFIX):
            return text[len(PLAIN_PREFIX) :]
        if not text.startswith(PREFIX):
            return text
        encoded = text[len(PREFIX) :]
        try:
            cipher = base64.b64decode(encoded, validate=True)
            crypt32, kernel32, blob_cls = _load_dpapi()
            in_blob, _in_buf = _make_blob(blob_cls, cipher)
            entropy_blob, _ent_buf = _make_blob(blob_cls, _ENTROPY)
            out_blob = blob_cls()
            try:
                ok = crypt32.CryptUnprotectData(
                    ctypes.byref(in_blob),
                    None,
                    ctypes.byref(entropy_blob),
                    None,
                    None,
                    _CRYPTPROTECT_UI_FORBIDDEN,
                    ctypes.byref(out_blob),
                )
                if not ok:
                    raise _last_win_error()
                plain = ctypes.string_at(out_blob.pbData, out_blob.cbData)
            finally:
                if out_blob.pbData:
                    kernel32.LocalFree(out_blob.pbData)
            return plain.decode("utf-8")
        except UnreadableMemory:
            raise
        except (
            OSError, binascii.Error, ValueError, UnicodeDecodeError, ctypes.ArgumentError
        ) as error:
            raise UnreadableMemory("Şifreli hafıza kaydı çözülemedi.") from error


class _ReadOnlyDpapi(DpapiProtector):
    """Şifreleme kapalı ama Windows: yeni yazımlar düz metin, önceki şifreli dönemin kayıtları
    yine çözülür (kapatmak hiçbir kaydı okunmaz yapmamalı)."""

    encrypts: bool = False

    def protect(self, text: str) -> str:
        if text.startswith(PREFIX) or text.startswith(PLAIN_PREFIX):
            return PLAIN_PREFIX + text
        return text


def protector_for(enabled: bool) -> Protector:
    """Ayara ve platforma göre uygun hafıza koruyucusunu seçer."""
    global _warned_non_windows
    if enabled:
        if sys.platform == "win32":
            return DpapiProtector()
        if not _warned_non_windows:
            _warned_non_windows = True
            _logger.warning(
                "Hafıza şifrelemesi (DPAPI) yalnız Windows'ta var; şifresiz devam ediliyor."
            )
        return PlainProtector()
    return _ReadOnlyDpapi() if sys.platform == "win32" else PlainProtector()
