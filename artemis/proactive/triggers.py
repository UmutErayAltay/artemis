"""Tetikleyiciler: izlenen klasöre yeni dosya gelince sabit bildirim (ADR 0061).

Zamanlayıcının taramasında yoklanır (`os.scandir`, özyinelemesiz, izleyici iş parçacığı yok).
Bu modül yalnız saf yardımcıları taşır; talep, teslim ve sınır `ProactiveScheduler`'dadır.
Dosya adları dış metindir: yalnız temizlenip bildirim metnine konur, hiçbir yerde talimat
sayılmaz ve hiçbir şey kendiliğinden çalıştırılmaz.
"""

from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path

# Bir taramada en çok bu kadar girdi okunur: dev klasör zamanlayıcıyı tutamaz.
MAX_SCAN_ENTRIES = 2000
MAX_FILE_NAME_CHARS = 120
MAX_LISTED_FILES = 3
# Yarım inen dosyaların geçici uzantıları: bitmeden bildirilmez.
TEMP_SUFFIXES = (".crdownload", ".part", ".tmp", ".partial", ".download", ".opdownload")
# Dış kaynaklı (takvim/klasör) yer tutucular: öneri şablonunda kullanıldığında "Yaz" ile
# başlatılan tur dış içerikle lekeli sayılır.
EXTERNAL_SUGGEST_PLACEHOLDERS = ("{title}", "{location}", "{file}", "{path}", "{app}")
# Denetim, yön (bidi) ve görünmez karakterler: dosya adı bildirimi başka bir şey gibi
# gösteremesin. Kaçışla yazılır ki kaynakta görünmez karakter kalmasın.
_CONTROL = re.compile(
    "[\x00-\x1f\x7f-\x9f\u00ad\u061c\u200b-\u200f\u2028-\u202e\u2066-\u2069\ufeff]"
)


def sanitize_name(name: str, limit: int = MAX_FILE_NAME_CHARS) -> str:
    """Denetim ve yön değiştirme karakterlerini atar, uzunluğu sınırlar."""
    clean = _CONTROL.sub("", name)
    return clean if len(clean) <= limit else clean[: limit - 1] + "…"


def scan_folder(folder: str, pattern: str) -> tuple[dict[str, int], bool]:
    """Düz dosya adı -> boyut ve "kesildi mi". Klasör okunamazsa OSError yükselir (çağıran yakalar).

    Sembolik bağlantılar, klasörler, geçici uzantılar, desene uymayan ve UTF-8'e çevrilemeyen
    adlar atlanır; tek bozuk girdi taramayı bozmaz.
    """
    found: dict[str, int] = {}
    truncated = False
    with os.scandir(folder) as entries:
        for count, entry in enumerate(entries):
            if count >= MAX_SCAN_ENTRIES:
                truncated = True
                break
            name = entry.name
            if name.lower().endswith(TEMP_SUFFIXES) or not fnmatch.fnmatch(name, pattern):
                continue
            try:
                name.encode("utf-8")  # SQLite'a yazılamayacak (çözülemeyen) ad
                if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                    continue
                found[name] = entry.stat(follow_symlinks=False).st_size
            except (OSError, UnicodeEncodeError):
                continue
    return found, truncated


def fill(template: str, **values: str) -> str:
    """`{ad}` yer tutucularını doldurur; başka süslü parantezler olduğu gibi kalır (format yok)."""
    for key, value in values.items():
        template = template.replace("{" + key + "}", value)
    return template


def suggest_uses_external(template: str | None) -> bool:
    """Öneri şablonu dış kaynaklı yer tutucu içeriyorsa True döner."""
    return bool(template and any(p in template for p in EXTERNAL_SUGGEST_PLACEHOLDERS))


def build_notice(
    trigger: str, folder: str, message: str, suggest: str | None, files: list[str]
) -> tuple[str, str, str]:
    """(başlık, gövde, önerilen istem). Birden çok dosya tek bildirimde birleşir."""
    files = sorted(files)
    shown = [sanitize_name(name) for name in files]
    if len(files) == 1:
        body = fill(message, name=trigger, file=shown[0])
    else:
        listed = ", ".join(shown[:MAX_LISTED_FILES])
        rest = len(files) - MAX_LISTED_FILES
        body = f"{len(files)} yeni dosya: {listed}" + (f" ve {rest} tane daha" if rest > 0 else "")
    prompt = ""
    if suggest:
        # Sohbet kutusuna yazılır, gönderilmez; kullanıcı okuyup kendisi yollar.
        path = sanitize_name(str(Path(folder) / files[0]), 400)
        prompt = fill(suggest, path=path, name=trigger, file=shown[0])
    return f"Tetikleyici · {trigger}", sanitize_name(body, 600), prompt
