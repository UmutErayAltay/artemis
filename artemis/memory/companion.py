"""Artemis'in kendi küçük hafıza dosyaları (ADR 0054): `state_dir/hafiza/`.

Vault kullanıcının dış içeriğidir ve tura kendiliğinden girmez (ADR 0015). Bu dosyalar ise
Artemis'in kendi sınırlı belleğidir; her kurulumda vardır. Bu aşamada yalnız iskelet
oluşturulur ve okunur; yazma/yansıtma yolu sonraki aşamadır.
"""

from pathlib import Path

COMPANION_FILES = {
    "kimlik": "Kimlik.md",
    "isler": "Isler.md",
    "son_oturum": "SonOturum.md",
}

_TEMPLATES = {
    "kimlik": "# Kimlik\n\nKullanıcının adı, hitabı ve çalışma tercihleri buraya yazılır.\n",
    "isler": "# Açık işler\n\nHenüz kayıtlı açık iş yok.\n",
    "son_oturum": "# Son oturum\n\nSon oturumun kısa özeti ve kaldığımız yer buraya yazılır.\n",
}


def companion_dir(state_dir: Path) -> Path:
    return state_dir / "hafiza"


def ensure_skeleton(state_dir: Path) -> list[str]:
    """Eksik dosyaları "x" kipiyle açar: var olan kullanıcı/Artemis içeriği ASLA ezilmez.

    Symlink reddedilir; yoksa hafıza klasörü bağlantı üzerinden state_dir dışına yazardı.
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    folder = companion_dir(state_dir)
    folder.mkdir(exist_ok=True)
    if folder.is_symlink():
        raise OSError("Hafıza klasörü sembolik bağlantı olamaz.")
    created: list[str] = []
    for key, name in COMPANION_FILES.items():
        target = folder / name
        if target.is_symlink():
            raise OSError(f"{name} sembolik bağlantı olamaz.")
        try:
            with target.open("x", encoding="utf-8", newline="") as stream:
                stream.write(_TEMPLATES[key])
        except FileExistsError:
            continue
        created.append(name)
    return created


def read_companion(state_dir: Path, key: str, limit: int) -> str:
    """Dosyanın başını `limit` karaktere kadar okur; yok/bağlantı/okunamaz ise boş döner."""
    folder = companion_dir(state_dir)
    path = folder / COMPANION_FILES[key]
    if folder.is_symlink() or path.is_symlink():
        return ""
    try:
        with path.open("r", encoding="utf-8") as stream:
            return stream.read(limit)
    except (OSError, UnicodeError):
        return ""
