"""Bağlamsal çalışma profilleri ve politika yardımcıları (ADR 0083).

Qt'siz tek doğruluk kaynağıdır: config, beyin yönlendiricisi, ses kanalı ve proaktif teslim
buradan aynı kuralları okur.
"""

from __future__ import annotations

from typing import Literal, TypeGuard

from artemis.config.settings import Settings

WorkProfile = Literal["work", "game", "private", "meeting"]

WORK_PROFILES: tuple[WorkProfile, ...] = ("work", "game", "private", "meeting")

PROFILE_LABELS: dict[WorkProfile, str] = {
    "work": "Çalışma",
    "game": "Oyun",
    "private": "Gizli",
    "meeting": "Toplantı",
}

PROFILE_DESCRIPTIONS: dict[WorkProfile, str] = {
    "work": "Çalışma profili: normal ayarlar (değiştirmek için tıkla)",
    "game": (
        "Oyun profili: bildirimler bastırılır, hafif ses modeli kullanılır "
        "(değiştirmek için tıkla)"
    ),
    "private": (
        "Gizli profil: yalnız yerel modeller, bulut ve Telegram kapalı "
        "(değiştirmek için tıkla)"
    ),
    "meeting": "Toplantı profili: konuşma sesi ve bildirimler sessizde (değiştirmek için tıkla)",
}


def is_valid_work_profile(profile: str) -> TypeGuard[WorkProfile]:
    """Verilen dizenin desteklenen bir çalışma profili olup olmadığını döndürür."""
    return profile in WORK_PROFILES


def should_suppress_toast(profile: str) -> bool:
    """Oyun ve toplantı profillerinde masaüstü bildirim balonları (toast) bastırılır."""
    return profile in {"game", "meeting"}


def should_mute_tts(profile: str) -> bool:
    """Toplantı profilinde asistan konuşma sesi (TTS) susturulur."""
    return profile == "meeting"


def is_local_only_profile(profile: str) -> bool:
    """Gizli profilde yalnız yerel (ollama/yerel ses) modeller kullanılır."""
    return profile == "private"


def is_game_profile(profile: str) -> bool:
    """Oyun profilinde hafif ses tanıma modeli etkinleştirilir."""
    return profile == "game"


def effective_settings_for_profile(settings: Settings) -> Settings:
    """Etkin çalışma profiline göre türetilmiş ayar görünümü döndürür.

    `private` profilinde tüm bulut izinleri, uzak ses/TTS aktarımı ve Telegram teslimi
    oturum düzeyinde kapatılır; diskteki config dosyasının kendi değerleri bozulmaz.
    """
    if not is_local_only_profile(settings.work_profile):
        return settings
    return settings.model_copy(
        update={
            "allow_cloud_file_content": False,
            "allow_cloud_clipboard": False,
            "perception": settings.perception.model_copy(update={"allow_cloud_screen": False}),
            "calendar": settings.calendar.model_copy(update={"allow_cloud": False}),
            "mail": settings.mail.model_copy(update={"allow_cloud": False}),
            "voice": settings.voice.model_copy(
                update={
                    "stt_mode": "local",
                    "direct_audio_input": False,
                    "allow_cloud_tts_text": False,
                }
            ),
            "proactive": settings.proactive.model_copy(update={"telegram": False}),
        }
    )
