"""Windows UI Automation (UIA) üzerinden doğrudan seçili metin okuyucu (ADR 0062).

Pano üzerinden Ctrl+C göndermeden önce odaktaki UIA öğesinin `IsPassword` durumunu
ve `TextPattern.GetSelection()` aralığını okur. Tüm çağrılar süreç içi `ctypes` COM
üzerinden çalışır; dış süreç veya üçüncü parti COM paketi gerektirmez. Windows dışı
platformlarda ve COM hatalarında istisna fırlatmadan güvenli boş sonuç döner.
"""

from __future__ import annotations

import ctypes
import sys
import uuid
from ctypes import wintypes
from typing import Any

_COINIT_APARTMENTTHREADED = 2
_CLSCTX_INPROC_SERVER = 1
_UIA_TEXT_PATTERN_ID = 10014

# Metin barındırabilen UIA denetim türleri (TextPattern tembel açıldığında dahi adaydır;
# düğme/kaydırma çubuğu/menü gibi denetimlerde fare ipucu çıkarılmaz).
_TEXT_LIKE_CONTROL_TYPES = frozenset({
    50004,  # Edit
    50020,  # Text
    50025,  # Custom
    50026,  # Group
    50030,  # Document
    50033,  # Pane
})


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def _make_guid(text: str) -> _GUID:
    guid = _GUID()
    ctypes.memmove(ctypes.byref(guid), uuid.UUID(text).bytes_le, 16)
    return guid


_CLSID_CUI_AUTOMATION = _make_guid("{ff48dba4-60ef-4201-aa87-54103eef594e}")
_IID_IUI_AUTOMATION = _make_guid("{30cbe57d-d9d0-452a-ab13-7ac5ac4825ee}")
_IID_TEXT_PATTERN = _make_guid("{32eba289-3583-42c9-9c59-3b6d9a1e9b6a}")

_WINFUNC: Any = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)


def _vtbl(ptr: ctypes.c_void_p) -> Any:
    return ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents


def _release(ptr: ctypes.c_void_p | None) -> None:
    if ptr is not None and ptr.value:
        release_fn = _WINFUNC(ctypes.c_ulong, ctypes.c_void_p)(_vtbl(ptr)[2])
        release_fn(ptr)


def _read_text_ranges(
    elem: ctypes.c_void_p, max_chars: int, sys_free_string: Any
) -> str | None:
    if sys.platform != "win32":
        return None
    pattern = ctypes.c_void_p()
    ranges = ctypes.c_void_p()
    try:
        get_pat = _WINFUNC(
            ctypes.HRESULT,
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.POINTER(_GUID),
            ctypes.POINTER(ctypes.c_void_p),
        )(_vtbl(elem)[14])
        if (
            get_pat(
                elem,
                _UIA_TEXT_PATTERN_ID,
                ctypes.byref(_IID_TEXT_PATTERN),
                ctypes.byref(pattern),
            )
            != 0
            or not pattern.value
        ):
            return None

        get_sel = _WINFUNC(
            ctypes.HRESULT, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)
        )(_vtbl(pattern)[5])
        if get_sel(pattern, ctypes.byref(ranges)) != 0 or not ranges.value:
            return None

        length_fn = _WINFUNC(
            ctypes.HRESULT, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)
        )(_vtbl(ranges)[3])
        count = ctypes.c_int(0)
        if length_fn(ranges, ctypes.byref(count)) != 0 or count.value <= 0:
            return None

        get_el = _WINFUNC(
            ctypes.HRESULT,
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_void_p),
        )(_vtbl(ranges)[4])
        parts: list[str] = []
        remaining = max_chars
        for idx in range(count.value):
            if remaining <= 0:
                break
            rng = ctypes.c_void_p()
            bstr = ctypes.c_void_p()
            try:
                if get_el(ranges, idx, ctypes.byref(rng)) != 0 or not rng.value:
                    continue
                get_text = _WINFUNC(
                    ctypes.HRESULT,
                    ctypes.c_void_p,
                    ctypes.c_int,
                    ctypes.POINTER(ctypes.c_void_p),
                )(_vtbl(rng)[12])
                if get_text(rng, remaining, ctypes.byref(bstr)) != 0 or not bstr.value:
                    continue
                chunk = ctypes.wstring_at(bstr.value)
                if chunk:
                    parts.append(chunk)
                    remaining -= len(chunk)
            finally:
                if bstr.value:
                    sys_free_string(bstr)
                _release(rng)
        joined = "".join(parts)
        return joined if joined.strip() else None
    finally:
        _release(ranges)
        _release(pattern)


def probe_uia_selection(max_chars: int = 6001) -> tuple[str | None, bool, bool]:
    """Odaktaki UIA öğesinden `(metin, parola_mi, metin_turu_mu)` üçlüsünü döner."""
    if sys.platform != "win32":
        return None, False, False

    loader_name = "WinDLL"
    win_dll: Any = getattr(ctypes, loader_name)
    ole32 = win_dll("ole32")
    oleaut32 = win_dll("oleaut32")

    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    ole32.CoInitializeEx.restype = ctypes.HRESULT
    ole32.CoCreateInstance.argtypes = [
        ctypes.POINTER(_GUID),
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_GUID),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    ole32.CoCreateInstance.restype = ctypes.HRESULT
    ole32.CoUninitialize.restype = None
    oleaut32.SysFreeString.argtypes = [ctypes.c_void_p]
    oleaut32.SysFreeString.restype = None

    hr_init = int(ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED))
    uia = ctypes.c_void_p()
    elem = ctypes.c_void_p()
    try:
        if ole32.CoCreateInstance(
            ctypes.byref(_CLSID_CUI_AUTOMATION),
            None,
            _CLSCTX_INPROC_SERVER,
            ctypes.byref(_IID_IUI_AUTOMATION),
            ctypes.byref(uia),
        ) != 0 or not uia.value:
            return None, False, False

        get_focused = _WINFUNC(
            ctypes.HRESULT, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)
        )(_vtbl(uia)[8])
        if get_focused(uia, ctypes.byref(elem)) != 0 or not elem.value:
            return None, False, False

        is_pwd_fn = _WINFUNC(
            ctypes.HRESULT, ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL)
        )(_vtbl(elem)[35])
        is_pwd = wintypes.BOOL(False)
        if is_pwd_fn(elem, ctypes.byref(is_pwd)) == 0 and bool(is_pwd.value):
            return None, True, False

        ctrl_fn = _WINFUNC(
            ctypes.HRESULT, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)
        )(_vtbl(elem)[21])
        ctrl_type = ctypes.c_int(0)
        is_text_like = (
            ctrl_fn(elem, ctypes.byref(ctrl_type)) == 0
            and int(ctrl_type.value) in _TEXT_LIKE_CONTROL_TYPES
        )

        text = _read_text_ranges(elem, max_chars, oleaut32.SysFreeString)
        return text, False, is_text_like
    except (OSError, ValueError, RuntimeError, ctypes.ArgumentError):
        return None, False, False
    finally:
        _release(elem)
        _release(uia)
        if hr_init in (0, 1):
            ole32.CoUninitialize()


def read_uia_selection(max_chars: int = 6001) -> tuple[str | None, bool]:
    """Odaktaki UIA öğesinden `(seçili_metin, parola_mi)` ikilisini döner."""
    text, is_password, _is_text_like = probe_uia_selection(max_chars=max_chars)
    return text, is_password
