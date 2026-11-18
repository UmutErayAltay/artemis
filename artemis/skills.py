"""Kullanıcı becerileri (ADR 0060): `<beceri klasörü>/<ad>.md` dosyaları.

Klasör: `skills.folder`; yoksa güvenliyse mevcut `state_dir/beceriler`; yoksa kullanıcı veri
klasörü (Windows: APPDATA/Artemis/beceriler; diğerlerinde XDG_CONFIG_HOME/artemis/beceriler).

Hazır şablonlar `artemis/skill_templates/` içinde paketle gelir; klasör ilk kez açılırken ve
panelde kullanıcı açıkça "Yeni beceri" dediğinde YENİ bir dosya olarak yazılır (asla üzerine
yazılmaz, silinmez; araç ya da model bunu tetikleyemez).

Beceri KULLANICININ yazdığı talimattır ve gövdesi kullanıcının kendi isteği kadar güvenilir
sayılır. Bu yalnız klasöre kullanıcıdan başkası yazamıyorsa güvenlidir: hiçbir araç beceri
oluşturmaz/değiştirmez/silmez ve klasör çalışma alanının (dosya araçlarının kökü) içinde ya da
bağlantı olarak çözülüyorsa beceriler hiç yüklenmez. Beceri izin vermez; araç çağrıları normal
kapıdan geçer. Dosyalar her turda ve "Yenile"de okunur (en çok 40 küçük dosya).
"""

from __future__ import annotations

import contextlib
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

import yaml

from artemis.config.settings import Settings
from artemis.safety.paths import safe_join

NAME_PATTERN = re.compile(r"^[a-z0-9-]{1,40}$")
MAX_SKILLS = 40
MAX_DESCRIPTION = 200
# Araç sonucu modele ~4000 karakterle sınırlı gider (kural 4); daha uzun gövde sessizce
# kesilirdi. Sınır o yüzden bunun altında tutulur.
MAX_BODY = 3500
PROMPT_CHARS = 2000
# Bundan büyük dosya okunmadan reddedilir (gövde sınırının rahat üstü, UTF-8 çok baytlı olabilir).
_MAX_FILE_BYTES = MAX_BODY * 4 + 2000

def templates() -> list[tuple[str, str]]:
    """Pakete gömülü hazır beceri şablonları: (ad, metin), ada göre sıralı."""
    folder = resources.files("artemis") / "skill_templates"
    found = [(f.name.removesuffix(".md"), f.read_text(encoding="utf-8"))
             for f in folder.iterdir() if f.name.endswith(".md")]
    return sorted(found)


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


def _user_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "Artemis" / "beceriler"
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "artemis" / "beceriler"


def skills_dir(settings: Settings) -> Path:
    if settings.skills.folder:
        return settings.skills.folder
    # Eski varsayılan yalnız varsa ve güvenliyse sürer; vault içindeyse yenisine geçilir.
    legacy = settings.state_dir / "beceriler"
    if legacy.is_dir() and location_problem(settings, legacy) is None:
        return legacy
    return _user_dir()


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


def location_problem(settings: Settings, folder: Path | None = None) -> str | None:
    """Klasör bir aracın/kod ajanının yazabileceği yerdeyse neden; güvenliyse None.

    Beceri gövdesi kullanıcının kendi talimatı sayılır; bu yalnız oraya kullanıcıdan başka
    kimse yazamıyorsa doğrudur. Yalnız çalışma alanına bakmak yetmez: vault_append vault'a,
    kod ajanı kendi çalışma klasörüne yazar. `folder` verilmezse geçerli klasör denetlenir.
    Metin kullanıcıya gösterilir: yapılandırma anahtarı içermez.
    """
    folder = folder or skills_dir(settings)
    if folder.is_symlink() or os.path.isjunction(folder):
        return "Beceriler klasörü sembolik bağlantı olamaz. Klasör seç ile başka bir klasör seç."
    real = folder.resolve()
    for label, root in _write_roots(settings):
        if real.is_relative_to(root) or root.is_relative_to(real):
            return (
                f"Bu klasör {label} ile iç içe ({root}); araçlar oraya yazabilirdi. "
                "Klasör seç ile bu konumların dışında bir klasör seç."
            )
    return None


def ensure_example(settings: Settings) -> None:
    """Klasör yoksa hazır şablonlarla açar; var olana (silinen örnek dahil) dokunmaz."""
    folder = skills_dir(settings)
    if not settings.skills.enabled or location_problem(settings) or folder.exists():
        return
    folder.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    try:
        for name, text in templates():
            target = folder / f"{name}.md"
            try:
                with target.open("x", encoding="utf-8", newline="") as stream:
                    written.append(target)
                    stream.write(text)
            except FileExistsError:
                # Eşzamanlı başka bir süreç aynı şablonu yazmış olabilir; dokunma.
                continue
    except OSError:
        for path in written:
            with contextlib.suppress(OSError):
                path.unlink()
        with contextlib.suppress(OSError):
            folder.rmdir()
        raise


def parse_bytes(data: bytes, stem: str) -> Skill:
    """Dosya baytlarını `<stem>.md` becerisi olarak doğrular; geçersizse ValueError (kullanıcıya
    gösterilecek neden). Yükleyici ve uygulama içi düzenleyici AYNI doğrulayıcıyı kullanır: panel
    yazmadan önce tam yazılacak baytları buradan geçirir, böylece kaydedilen dosya yüklenir."""
    if len(data) > _MAX_FILE_BYTES:
        raise ValueError("Dosya çok büyük.")
    try:
        # utf-8-sig: Not Defteri'nin eklediği BOM kabul; UTF-16/ANSI dosya çözülemez.
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("UTF-8 değil; Not Defteri'nde 'UTF-8' olarak kaydedin.") from None
    text = text.replace("\r\n", "\n")
    closing = re.search(r"\n---[ \t]*(?:\n|$)", text[4:]) if text.startswith("---\n") else None
    if closing is None:
        raise ValueError("Dosya --- ile çevrili bir başlıkla başlamalı.")
    head = text[4 : 4 + closing.start()]
    body = text[4 + closing.end() :].strip()
    try:
        meta = yaml.safe_load(head)
    except yaml.YAMLError:
        raise ValueError("Başlık (frontmatter) YAML olarak okunamadı.") from None
    if not isinstance(meta, dict):
        raise ValueError("Başlıkta name ve description olmalı.")
    name, description = meta.get("name"), meta.get("description")
    if not isinstance(name, str) or not NAME_PATTERN.fullmatch(name):
        raise ValueError("name 1-40 karakter; küçük harf, rakam ve tire olmalı.")
    if name != stem:
        raise ValueError(f"name ({name}) dosya adıyla ({stem}) aynı olmalı.")
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


def _parse(path: Path) -> Skill:
    if path.stat().st_size > _MAX_FILE_BYTES:
        raise ValueError("Dosya çok büyük.")  # okumadan reddet
    return parse_bytes(path.read_bytes(), path.stem)


def compose(name: str, description: str, body: str) -> str:
    """Form alanlarından dosya metni. Açıklama yaml ile yazılır: `:` ya da `#` içeren cümle
    başlığı bozmaz; geçerliliği yine `parse_bytes` söyler."""
    head = yaml.safe_dump(
        {"name": name, "description": " ".join(description.split())},
        allow_unicode=True, default_flow_style=False, sort_keys=False, width=10_000,
    )
    return "---\n" + head + "---\n" + body.strip() + "\n"


class SkillWriteError(Exception):
    """Panelin yazma/silme işlemi reddedildi; mesaj kullanıcıya gösterilir."""


def _skill_path(settings: Settings, name: str) -> tuple[Path, Path]:
    """(klasör, dosya). Konum güvensizse ya da ad geçersizse reddeder; yol `safe_join`'dan geçer."""
    problem = location_problem(settings)
    if problem:
        raise SkillWriteError(problem)
    if not NAME_PATTERN.fullmatch(name):
        raise SkillWriteError("Ad 1-40 karakter; küçük harf, rakam ve tire olmalı.")
    folder = skills_dir(settings)
    try:
        return folder, safe_join(folder, f"{name}.md", allow_symlinks=False)
    except ValueError as error:
        raise SkillWriteError(str(error)) from None


def create_skill(settings: Settings, name: str, text: str) -> None:
    """YENİ beceri dosyası; `open("x")` olduğundan var olanın üzerine asla yazmaz."""
    data = text.encode("utf-8")
    try:
        parse_bytes(data, name)
    except ValueError as error:
        raise SkillWriteError(str(error)) from None
    folder, path = _skill_path(settings, name)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        if sum(1 for _ in folder.glob("*.md")) >= MAX_SKILLS:
            raise SkillWriteError(f"En çok {MAX_SKILLS} beceri yüklenir.")
        with path.open("xb") as stream:
            stream.write(data)
    except FileExistsError:
        raise SkillWriteError(f"{name}.md zaten var; üzerine yazılmaz.") from None
    except OSError:
        raise SkillWriteError("Beceri dosyası oluşturulamadı.") from None


def replace_skill(settings: Settings, name: str, text: str, original: bytes) -> None:
    """Var olan beceriyi atomik değiştirir. Dosya `original` baytlarından farklıysa (başka biri
    değiştirmiş) hiçbir şey yazmaz: kullanıcının dış düzenlemesi sessizce ezilmez."""
    data = text.encode("utf-8")
    try:
        parse_bytes(data, name)
    except ValueError as error:
        raise SkillWriteError(str(error)) from None
    folder, path = _skill_path(settings, name)
    tmp: str | None = None
    try:
        if (
            path.is_symlink()
            or os.path.isjunction(path)
            or path.resolve().parent != folder.resolve()
        ):
            raise SkillWriteError("Dosya sembolik bağlantı; değiştirilmez.")
        if path.read_bytes() != original:
            raise SkillWriteError(
                "Dosya sen düzenlerken diskte değişti; yazılmadı. Pencereyi kapatıp yeniden aç."
            )
        # Geçici dosya aynı klasörde (os.replace aynı sürücüde atomik); *.md olmadığı için
        # yarım kalırsa bile yükleyici onu beceri saymaz.
        handle, tmp = tempfile.mkstemp(dir=folder, suffix=".tmp")
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
        os.replace(tmp, path)
        tmp = None
    except OSError:
        raise SkillWriteError("Beceri dosyası yazılamadı.") from None
    finally:
        if tmp is not None:
            Path(tmp).unlink(missing_ok=True)


def delete_skill(settings: Settings, file_name: str) -> None:
    """Tek dosyayı siler (yalnız `.md`; bağlantıysa yalnız bağlantı kalkar, hedefe dokunulmaz)."""
    problem = location_problem(settings)
    if problem:
        raise SkillWriteError(problem)
    try:
        folder = skills_dir(settings)
        # Yalnız tek parça ad: ayraç, `..`, sürücü yok. safe_join burada kullanılmaz çünkü
        # bağlantının HEDEFİNİ denetler; klasör dışına işaret eden ya da kırık bağlantı silinemezdi.
        # unlink bağlantının kendisini siler, hedefe dokunmaz.
        if (
            not file_name or Path(file_name).name != file_name or file_name in {".", ".."}
            or "/" in file_name or "\\" in file_name or ":" in file_name
        ):
            raise SkillWriteError("Geçersiz dosya adı.")
        path = folder.resolve() / file_name
        if path.suffix != ".md" or not (path.is_file() or path.is_symlink()):
            raise SkillWriteError("Silinecek beceri dosyası bulunamadı.")
        path.unlink()
    except ValueError as error:
        raise SkillWriteError(str(error)) from None
    except OSError:
        raise SkillWriteError("Dosya silinemedi.") from None


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
            if path.is_symlink() or os.path.isjunction(path):
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
