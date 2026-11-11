"""Kullanıcı becerileri (ADR 0060): `state_dir/beceriler/<ad>.md` dosyaları.

Beceri KULLANICININ yazdığı talimattır ve gövdesi kullanıcının kendi isteği kadar güvenilir
sayılır. Bu yalnız klasöre kullanıcıdan başkası yazamıyorsa güvenlidir: hiçbir araç beceri
oluşturmaz/değiştirmez/silmez ve klasör çalışma alanının (dosya araçlarının kökü) içinde ya da
bağlantı olarak çözülüyorsa beceriler hiç yüklenmez. Beceri izin vermez; araç çağrıları normal
kapıdan geçer. Dosyalar her turda ve "Yenile"de okunur (en çok 40 küçük dosya).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from artemis.config.settings import Settings

NAME_PATTERN = re.compile(r"^[a-z0-9-]{1,40}$")
MAX_SKILLS = 40
MAX_DESCRIPTION = 200
# Araç sonucu modele ~4000 karakterle sınırlı gider (kural 4); daha uzun gövde sessizce
# kesilirdi. Sınır o yüzden bunun altında tutulur.
MAX_BODY = 3500
PROMPT_CHARS = 2000
# Bundan büyük dosya okunmadan reddedilir (gövde sınırının rahat üstü, UTF-8 çok baytlı olabilir).
_MAX_FILE_BYTES = MAX_BODY * 4 + 2000

EXAMPLE_NAME = "ornek-gunluk-ozet"
EXAMPLE = f"""---
name: {EXAMPLE_NAME}
description: Günün notlarını ve açık işleri kısa bir akşam özetine çevirir.
---
Kullanıcı günlük özet isterse:

1. memory_open_work ile açık işlere bak.
2. Üç başlıkta yaz: Bugün yapılanlar, Açık kalanlar, Yarın ilk iş.
3. Her başlıkta en çok üç madde kullan; kısa ve düz Türkçe yaz.

Bu dosya bir örnektir; kendi becerilerini aynı biçimde, bu klasöre .md olarak ekle.
Dosya adı ile name alanı aynı olmalı (küçük harf, rakam, tire).
"""


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    body: str


@dataclass
class Catalog:
    skills: list[Skill] = field(default_factory=list)
    invalid: list[tuple[str, str]] = field(default_factory=list)  # (dosya, neden)
    # Doluysa beceriler bilerek yüklenmedi (örn. klasör çalışma alanında).
    refused: str | None = None

    def get(self, name: str) -> Skill | None:
        return next((s for s in self.skills if s.name == name), None)


def skills_dir(settings: Settings) -> Path:
    return settings.skills.folder or settings.state_dir / "beceriler"


def _write_roots(settings: Settings) -> list[tuple[str, Path]]:
    """Bir aracın ya da kod ajanının yazabildiği kökler; beceri klasörü hepsinin dışında olmalı."""
    agent = settings.code_agent
    roots: list[tuple[str, Path | None]] = [
        ("çalışma alanı", settings.workspace),
        ("vault", settings.memory.vault_path),  # vault_append ve vault'ta başlayan kod ajanı
        ("proje depoları klasörü", agent.repos_dir),
        *(("kod ajanı klasörü", path) for path in agent.workdirs.values()),
        *(("proje deposu", path) for path in agent.projects.values()),
    ]
    return [(label, path.resolve()) for label, path in roots if path is not None]


def location_problem(settings: Settings) -> str | None:
    """Klasör bir aracın/kod ajanının yazabileceği yerdeyse neden; güvenliyse None.

    Beceri gövdesi kullanıcının kendi talimatı sayılır; bu yalnız oraya kullanıcıdan başka
    kimse yazamıyorsa doğrudur. Yalnız çalışma alanına bakmak yetmez: vault_append vault'a,
    kod ajanı kendi çalışma klasörüne yazar.
    """
    folder = skills_dir(settings)
    if folder.is_symlink():
        return "Beceriler klasörü sembolik bağlantı olamaz."
    real = folder.resolve()
    for label, root in _write_roots(settings):
        if real.is_relative_to(root) or root.is_relative_to(real):
            return (
                f"Beceriler klasörü {label} ile iç içe ({root}); araçlar oraya yazabilirdi. "
                "config'te skills.folder ile bu köklerin dışında bir klasör verin."
            )
    return None


def ensure_example(settings: Settings) -> None:
    """Klasör yoksa örnekle açar; var olana (kullanıcının sildiği örnek dahil) dokunmaz."""
    folder = skills_dir(settings)
    if not settings.skills.enabled or location_problem(settings) or folder.exists():
        return
    folder.mkdir(parents=True)
    with (folder / f"{EXAMPLE_NAME}.md").open("x", encoding="utf-8", newline="") as stream:
        stream.write(EXAMPLE)


def _parse(path: Path) -> Skill:
    """Geçersizse ValueError (kullanıcıya gösterilecek neden)."""
    if path.stat().st_size > _MAX_FILE_BYTES:
        raise ValueError("Dosya çok büyük.")
    try:
        # utf-8-sig: Not Defteri'nin eklediği BOM kabul; UTF-16/ANSI dosya çözülemez.
        text = path.read_bytes().decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("UTF-8 değil; Not Defteri'nde 'UTF-8' olarak kaydedin.") from None
    text = text.replace("\r\n", "\n")
    if not text.startswith("---\n") or "\n---" not in text[4:]:
        raise ValueError("Dosya --- ile çevrili bir başlıkla başlamalı.")
    head, _, rest = text[4:].partition("\n---")
    body = rest.partition("\n")[2].strip()
    try:
        meta = yaml.safe_load(head)
    except yaml.YAMLError:
        raise ValueError("Başlık (frontmatter) YAML olarak okunamadı.") from None
    if not isinstance(meta, dict):
        raise ValueError("Başlıkta name ve description olmalı.")
    name, description = meta.get("name"), meta.get("description")
    if not isinstance(name, str) or not NAME_PATTERN.fullmatch(name):
        raise ValueError("name 1-40 karakter; küçük harf, rakam ve tire olmalı.")
    if name != path.stem:
        raise ValueError(f"name ({name}) dosya adıyla ({path.stem}) aynı olmalı.")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("description boş olamaz.")
    description = " ".join(description.split())
    if len(description) > MAX_DESCRIPTION:
        raise ValueError(f"description {MAX_DESCRIPTION} karakteri aşıyor.")
    if not body:
        raise ValueError("Gövde boş.")
    if len(body) > MAX_BODY:
        raise ValueError(f"Gövde {MAX_BODY} karakteri aşıyor.")
    return Skill(name, description, body)


def load_skills(settings: Settings) -> Catalog:
    """Kapalıysa, klasör yoksa ya da güvensizse boş katalog; bozuk dosya diğerlerini engellemez."""
    catalog = Catalog()
    if not settings.skills.enabled:
        return catalog
    problem = location_problem(settings)
    if problem:
        catalog.refused = problem
        return catalog
    try:
        files = sorted(skills_dir(settings).glob("*.md"), key=lambda p: p.name)
    except OSError:
        return catalog
    for path in files:
        try:
            if path.is_symlink():
                raise ValueError("Sembolik bağlantı yok sayıldı.")
            if not path.is_file():
                continue
            if len(catalog.skills) >= MAX_SKILLS:
                raise ValueError(f"En çok {MAX_SKILLS} beceri yüklenir.")
            catalog.skills.append(_parse(path))
        except ValueError as error:
            catalog.invalid.append((path.name, str(error)))
        except OSError:
            catalog.invalid.append((path.name, "Dosya okunamadı."))
    return catalog


def prompt_section(catalog: Catalog) -> str:
    """Sistem istemi bölümü: yalnız ad ve açıklama (~2000 karakter), gövde değil."""
    if not catalog.skills:
        return ""
    head = (
        "\nBECERİLER (kullanıcının kendi yazdığı çalışma yöntemleri). İstek birine uyuyorsa "
        "önce skill_read(name) çağır ve tarifi izle; beceri izin vermez, her araç çağrısı "
        "normal onaydan geçer:"
    )
    lines: list[str] = []
    used = len(head)
    for skill in catalog.skills:
        line = f"\n- {skill.name}: {skill.description}"
        if used + len(line) > PROMPT_CHARS:
            lines.append("\n- …")
            break
        lines.append(line)
        used += len(line)
    return head + "".join(lines)


_EXPLICIT = re.compile(r"^/([a-z0-9-]{1,40})(?:\s|$)")


def explicit_hint(catalog: Catalog, text: str) -> str:
    """`/<ad>` ile başlayan mesaj o beceriyi ister: o tura özel kısa hatırlatma."""
    match = _EXPLICIT.match(text)
    if match is None or catalog.get(match.group(1)) is None:
        return ""
    return f"\nKullanıcı '{match.group(1)}' becerisini istedi: önce skill_read çağır, tarifi izle."
