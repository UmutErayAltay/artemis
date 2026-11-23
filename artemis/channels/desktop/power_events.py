"""Windows güç olayları (WM_POWERBROADCAST) uyku/uyanma algılayıcısı (ADR 0086).

`DesktopWindow.nativeEvent` tarafından çağrılır; Windows dışı platformlarda veya geçersiz
işaretçilerde sessizce `False` döner. Hiçbir yan etki üretmez.
"""

from __future__ import annotations

import sys
from ctypes import wintypes
from typing import Any

WM_POWERBROADCAST = 0x0218
PBT_APMRESUMESUSPEND = 0x0007
PBT_APMRESUMEAUTOMATIC = 0x0012
RESUME_WPARAMS = frozenset({PBT_APMRESUMEAUTOMATIC, PBT_APMRESUMESUSPEND})


def is_resume_message(msg_id: int, w_param: int) -> bool:
    """Verilen Win32 mesaj kimliği ve wParam'ın sistem uyanma olayı olup olmadığını döndürür."""
    return msg_id == WM_POWERBROADCAST and w_param in RESUME_WPARAMS


def is_resume_native_event(event_type: Any, message: Any) -> bool:
    """Qt `nativeEvent` girdisinin Windows uyku dönüşü olayı olup olmadığını denetler."""
    if sys.platform != "win32" or message is None:
        return False
    try:
        raw_type = bytes(event_type)
    except (TypeError, ValueError):
        return False
    if raw_type != b"windows_generic_MSG":
        return False
    try:
        addr = int(message)
        if addr <= 0:
            return False
        win_msg = wintypes.MSG.from_address(addr)
        return is_resume_message(int(win_msg.message), int(win_msg.wParam))
    except (TypeError, ValueError, OSError):
        return False
