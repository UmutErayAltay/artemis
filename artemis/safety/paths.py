"""Dosya araçlarının aynı kök ve yol kurallarını kullanmasını sağlar."""

import re
from pathlib import Path, PureWindowsPath

_WINDOWS_DEVICE = re.compile(
    r"^(?:CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³])$",
    re.IGNORECASE,
)


def safe_join(base: Path, target: str, *, allow_symlinks: bool = True) -> Path:
    """Linux testleri Windows sürücü ve ayraç kaçışlarını da reddeder."""
    if not target or "\x00" in target:
        raise ValueError("Boş veya NUL içeren yol kabul edilmez.")
    windows_path = PureWindowsPath(target)
    if Path(target).is_absolute() or windows_path.drive or windows_path.root:
        raise ValueError("Mutlak veya sürücü içeren yol kabul edilmez.")
    parts = target.replace("\\", "/").split("/")
    if ".." in parts:
        raise ValueError("Üst dizin geçişi kabul edilmez.")
    for part in parts:
        if ":" in part:
            raise ValueError("Windows alternatif veri akışı kabul edilmez.")
        if part.endswith((".", " ")):
            raise ValueError("Windows'ta sonda nokta veya boşluk olan yol kabul edilmez.")
        device_stem = part.split(".", 1)[0].rstrip(" .")
        if _WINDOWS_DEVICE.fullmatch(device_stem):
            raise ValueError("Windows aygıt adı dosya yolu olarak kullanılamaz.")
    root = base.resolve()
    candidate = root
    for part in parts:
        candidate /= part
        if not allow_symlinks and candidate.is_symlink():
            raise ValueError("Değişiklik yollarında sembolik bağlantı kabul edilmez.")
    candidate = candidate.resolve()
    if candidate == root:
        raise ValueError("Çalışma kökünün kendisi hedef olamaz.")
    if not candidate.is_relative_to(root):
        raise ValueError("Yol çalışma kökünün dışına çıkamaz.")
    return candidate
