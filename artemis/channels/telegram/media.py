"""Telegram ses/fotoğraf/dosya girdisinin saf yardımcıları (ADR 0064).

Telegram'dan gelen dosya adı SALDIRGAN denetimli girdi sayılır: yol parçaları, gizli/çift yönlü
karakterler, Windows aygıt adları, sondaki nokta/boşluk ve çift uzantı burada elenir. Karar
"SON uzantı izinli mi" sorusudur (`x.pdf.exe` -> exe -> reddedilir).
"""

from __future__ import annotations

import io
import unicodedata
from pathlib import Path

from artemis.safety.paths import _WINDOWS_DEVICE, safe_join

MAX_NAME_CHARS = 100
SAVE_DIR = "Telegram"
VOICE_SAMPLE_RATE = 16000
# Görünmez ama Unicode kategorisi "harf/sembol" olan doldurucular (Hangul filler, braille boşluk).
_INVISIBLE = frozenset("ᅟᅠㅤﾠ⠀᠎")
_WINDOWS_BAD = '<>:"|?*'


def sanitize_filename(raw: object) -> str | None:
    """Yalnız düz bir dosya adı döner; güvenli bir ad kurulamazsa None."""
    if not isinstance(raw, str):
        return None
    # NFKC: tam genişlikli nokta/eğik çizgi gibi görünüm hileleri ASCII karşılığına iner.
    text = unicodedata.normalize("NFKC", raw)
    kept: list[str] = []
    for char in text:
        category = unicodedata.category(char)
        if char in _INVISIBLE or category in {"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"}:
            continue
        kept.append(" " if category == "Zs" else char)
    # Yol parçaları atılır: yalnız son bileşen kalır.
    name = "".join(kept).replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join("_" if char in _WINDOWS_BAD else char for char in name)
    name = name.strip(" ").rstrip(". ")
    if not name or not name.strip("."):
        return None
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    if _WINDOWS_DEVICE.fullmatch(name.split(".", 1)[0].rstrip(" .")):
        stem = "_" + stem
    if len(ext) > 10:
        return None
    stem = stem[: MAX_NAME_CHARS - len(ext) - 1].rstrip(". ") or "dosya"
    return f"{stem}.{ext}" if ext else stem


def extension_allowed(name: str, allowed: tuple[str, ...]) -> bool:
    """Yalnız SON uzantı sayılır; uzantısız ad reddedilir."""
    suffix = Path(name).suffix.lower().lstrip(".")
    return bool(suffix) and suffix in allowed


def save_document(workspace: Path, name: str, data: bytes) -> str:
    """`Telegram/` altına yeni dosya olarak yazar, üzerine ASLA yazmaz; göreli yolu döner.

    `safe_join(allow_symlinks=False)` her aday için çağrılır; "x" kipi (O_EXCL) yarışta bile
    var olan dosyayı ezmez. Yarım yazılan dosya hata yolunda silinir. Hatada ValueError/OSError.
    """
    folder = safe_join(workspace, SAVE_DIR, allow_symlinks=False)
    folder.mkdir(exist_ok=True)
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    for number in range(1, 1000):
        candidate = name if number == 1 else f"{stem}-{number}{'.' + ext if ext else ''}"
        target = safe_join(workspace, f"{SAVE_DIR}/{candidate}", allow_symlinks=False)
        try:
            handle = target.open("xb")
        except FileExistsError:
            continue
        try:
            with handle:
                handle.write(data)
        except OSError:
            target.unlink(missing_ok=True)
            raise
        return f"{SAVE_DIR}/{candidate}"
    raise OSError("Boş bir dosya adı bulunamadı.")


def decode_voice(data: bytes, max_seconds: int) -> bytes:
    """OGG/Opus (Telegram sesli mesajı) -> 16 kHz mono int16 PCM, yalnız bellekte.

    Çözücü, `voice` ekinin zaten getirdiği faster-whisper'ın PyAV'ıdır (yeni bağımlılık yok).
    faster_whisper.decode_audio kullanılmaz: kilitli PyAV 19 ile uyumsuz bir argüman geçirir ve
    süre sınırını çözüm bittikten sonra uygular. Burada sınır çözerken uygulanır.
    Ses diske yazılmaz. Eksik ek için ImportError, bozuk/aşırı uzun ses için ValueError döner.
    """
    import av

    limit = max_seconds * VOICE_SAMPLE_RATE
    resampler = av.audio.resampler.AudioResampler(
        format="s16", layout="mono", rate=VOICE_SAMPLE_RATE
    )
    chunks: list[bytes] = []
    total = 0
    try:
        with av.open(io.BytesIO(data), mode="r") as container:
            for frame in container.decode(audio=0):
                for out in resampler.resample(frame):
                    chunk = out.to_ndarray().tobytes()
                    total += len(chunk) // 2
                    if total > limit:
                        raise ValueError("ses çok uzun")
                    chunks.append(chunk)
    except (av.error.FFmpegError, IndexError, OSError, RuntimeError):
        # Akışsız/bozuk kapsayıcı; PyAV istisna metni yol/ayrıntı taşıyabilir, atılır.
        raise ValueError("ses çözülemedi") from None
    return b"".join(chunks)
