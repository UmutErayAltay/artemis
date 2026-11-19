"""SafetyGate onay bildirimlerini yalnız gösterim anında etkin dile çevirir.

Denetim kaydı ve ``SafetyGate.pending_notice`` Türkçe kaynak metni korur; bu yardımcı
yalnız kullanıcıya gösterilen metni (uyarı + araç açıklaması) böler ve çevirir.
"""

from __future__ import annotations

from artemis.i18n import t
from artemis.safety.gate import SITE_NOTICE

_DEFAULT_NOTICE = "Bu işlem onay gerektirir."
_SITE_PREFIX = f"{SITE_NOTICE} ("


def _translate_warning(warning: str) -> str:
    """Uyarı bölümünü çevirir; site okuma biçimindeki dinamik host'u korur."""
    bare, dot = (warning[:-1], ".") if warning.endswith(".") else (warning, "")
    if bare.startswith(_SITE_PREFIX) and bare.endswith(")"):
        host = bare[len(_SITE_PREFIX) : -1]
        return f"{t(SITE_NOTICE)} ({host}){dot}"
    direct = t(warning)
    if direct != warning or not dot:
        return direct
    translated = t(bare)
    return f"{translated}." if translated != bare else warning


def split_approval_notice(notice: str) -> tuple[str, str]:
    """Onay bildirimini ``(uyarı, araç_notu)`` çiftine bölüp etkin dile çevirir.

    ``notice`` boşsa varsayılan onay uyarısı döner ve ``araç_notu`` boş olur.
    Uyarı sonunda nokta yoksa ve araç notu varsa noktayı tamamlar; çift nokta üretmez.
    """
    raw = notice or _DEFAULT_NOTICE
    if raw.startswith(_SITE_PREFIX):
        if "). " in raw:
            head, _sep, tool_note = raw.partition("). ")
            warning = head + ")"
        else:
            warning, tool_note = raw, ""
    else:
        warning, _sep, tool_note = raw.partition(". ")
    translated_warning = _translate_warning(warning)
    if tool_note and not translated_warning.endswith("."):
        translated_warning += "."
    translated_note = t(tool_note) if tool_note else ""
    return translated_warning, translated_note


def format_approval_notice(notice: str) -> str:
    """Onay bildirimini tek metin gösteren kanallar için etkin dile çevirir."""
    if not notice:
        return ""
    warning, tool_note = split_approval_notice(notice)
    if not tool_note:
        return warning
    return f"{warning} {tool_note}"
