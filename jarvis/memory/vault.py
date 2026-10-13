"""Obsidian vault'unu okuma yolları: gürültü süzme, parçalama ve sözcük araması.

Vault kullanıcının notlarıdır ve bir git deposudur; içinde web'den kırpılmış metinler
de olabilir. Bu modül ağa çıkmaz ve alt süreç başlatmaz (ADR 0015). Yol kuran her girdi
ortak `safe_join` üzerinden geçer; ek olarak çözülmüş (symlink sonrası) yol da hariç
tutma kurallarından geçirilir, çünkü vault içindeki bir bağlantı `daily/` veya `.git`
altındaki bir dosyaya işaret edip yalnız ad üzerinden yapılan süzmeyi atlatabilir.
"""

from __future__ import annotations

import math
import os
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from jarvis.safety.paths import safe_join

_EXCERPT_LIMIT = 300
_TITLE_LIMIT = 200
_CHUNK_CHARS = 800
# Başlık eşleşmesi gövde eşleşmesinden daha güçlü bir konu işaretidir.
_TITLE_WEIGHT = 2.0
_MIN_TOKEN_CHARS = 2

_FRONTMATTER = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)
_TITLE_LINE = re.compile(r"^title:[ \t]*(.+)$", re.MULTILINE)
_H1_LINE = re.compile(r"^# (.+)$", re.MULTILINE)
_PARAGRAPH_SPLIT = re.compile(r"\n\s*\n")
_TOKEN = re.compile(r"\w+")


class ExcludedNote(ValueError):  # noqa: N818 - ad sözleşmeden: durum bir hata değil, bir red nedenidir
    """Yol vault içinde ama gizli, hariç tutulmuş veya `.md` olmayan bir nota çıkıyor."""


@dataclass(frozen=True)
class SearchHit:
    """Bir arama sonucu; `path` vault'a göre göreli POSIX yoludur."""

    path: str
    title: str
    score: float
    excerpt: str


def turkish_fold(text: str) -> str:
    """Türkçe büyük/küçük harf eşlemesiyle casefold uygular.

    `casefold()` tek başına "I" harfini "i"ye çevirir ve "İ"yi "i̇" (noktalı birleşik
    karakter) yapar; Türkçede ise "I"→"ı", "İ"→"i" olmalıdır. Önce bu iki harf elle
    eşlenir, yoksa "ISPARTA" ile "ısparta" hiç eşleşmez.
    """
    return text.replace("I", "ı").replace("İ", "i").casefold()


def _segments(path: str) -> list[str]:
    """Windows ayırıcısını normalleştirir; boş ve "." bölümleri atar."""
    return [part for part in path.replace("\\", "/").split("/") if part not in ("", ".")]


def _folder_entry_matches(folder_path: str, exclude: Sequence[str]) -> bool:
    """`/` ile biten girdilerden biri klasör yolunda geçiyor mu? (başta veya içte)"""
    haystack = "/".join(_segments(folder_path)).casefold() + "/"
    for entry in exclude:
        if not entry.replace("\\", "/").endswith("/"):
            continue
        if "/".join(_segments(entry)).casefold() + "/" in haystack:
            return True
    return False


def is_excluded(relative: str, exclude: Sequence[str]) -> bool:
    """Gizli bölüm, `.md` olmayan dosya veya hariç tutma kalıbı varsa True döner.

    "/" ile biten girdi yolun başında VEYA içinde geçen klasör parçasıdır; companion
    klasörü "🔮 850-Companion" gibi önek taşıyabildiği için tam bölüm eşlemesi yetmez
    ve fazla dışlamak, gizli bir notu sızdırmaktan güvenli taraftır. Diğer girdiler
    vault'a göre tam POSIX yoludur. Karşılaştırma büyük/küçük harfe duyarsızdır:
    Windows'ta `Daily/` ile `daily/` aynı klasördür.
    """
    parts = _segments(relative)
    if not parts:
        return True
    if any(part.startswith(".") for part in parts):
        return True
    if not parts[-1].casefold().endswith(".md"):
        return True
    joined = "/".join(parts).casefold()
    for entry in exclude:
        normalized = entry.replace("\\", "/")
        folded = "/".join(_segments(normalized)).casefold()
        if not folded:
            continue
        if normalized.endswith("/"):
            if folded + "/" in joined:
                return True
        elif joined == folded:
            return True
    return False


def resolve_note(
    vault: Path, relative: str, exclude: Sequence[str], *, allow_symlinks: bool = True
) -> tuple[Path, str]:
    """Vault içinde izinli bir not yolunu çözer; (mutlak yol, kanonik göreli yol) döner.

    `safe_join` mutlak yolu, `..`'ı, kökün kendisini ve kök dışına çözülen bağlantıyı
    reddeder. Hariç tutma ise hem verilen hem de çözülmüş yola uygulanır: aksi halde
    vault içinde `daily/x.md`ye işaret eden masum adlı bir bağlantı süzmeyi atlatırdı.
    """
    target = safe_join(vault, relative, allow_symlinks=allow_symlinks)
    canonical = target.relative_to(vault.resolve()).as_posix()
    if is_excluded(relative, exclude) or is_excluded(canonical, exclude):
        raise ExcludedNote("Not gizli, hariç tutulmuş veya .md değil.")
    return target, canonical


def iter_notes(vault: Path, exclude: Sequence[str]) -> list[Path]:
    """Vault'taki izinli `.md` dosyalarını göreli yola göre sıralı döndürür.

    Sembolik bağlantılı (ve Windows junction'lı) klasörlere hiç inilmez; vault dışını
    veya hariç tutulan klasörleri tarama yolu olarak açmazlar. Bağlantılı dosyalar yalnız
    hedefi vault içinde kalıyorsa ve hedef de hariç tutulmuyorsa alınır.
    """
    root = vault.resolve()
    found: dict[str, Path] = {}
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(directory)
        relative_dir = current.relative_to(root).as_posix()
        kept: list[str] = []
        for name in sorted(dirnames):
            child = current / name
            if name.startswith(".") or child.is_symlink() or os.path.isjunction(child):
                continue
            child_relative = name if relative_dir == "." else f"{relative_dir}/{name}"
            # Hariç tutulan klasörün (örn. binlerce günlük kaydı) içi hiç gezilmez.
            if _folder_entry_matches(child_relative, exclude):
                continue
            kept.append(name)
        dirnames[:] = kept
        for name in filenames:
            relative = name if relative_dir == "." else f"{relative_dir}/{name}"
            if is_excluded(relative, exclude):
                continue
            try:
                target, canonical = resolve_note(root, relative, exclude)
            except ValueError:
                continue
            if target.is_file():
                found[canonical] = target
    return [found[key] for key in sorted(found)]


def split_passages(body: str, limit: int = _CHUNK_CHARS) -> list[str]:
    """Gövdeyi paragraf sınırlarında yaklaşık `limit` karakterlik parçalara böler.

    Kelime asla bölünmez: tek başına `limit`ten uzun bir paragraf boşluklardan kesilir
    (boşluksuz devasa bir sözcük olduğu gibi bir parça olur). Parçalar küçük kalır ki
    arama bir notun yalnız en alakalı bölümünü öne çıkarsın.
    """
    pieces: list[str] = []
    for paragraph in _PARAGRAPH_SPLIT.split(body):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if len(paragraph) <= limit:
            pieces.append(paragraph)
            continue
        current = ""
        for word in paragraph.split():
            if current and len(current) + 1 + len(word) > limit:
                pieces.append(current)
                current = word
            else:
                current = f"{current} {word}" if current else word
        if current:
            pieces.append(current)

    chunks: list[str] = []
    current = ""
    for piece in pieces:
        if current and len(current) + 2 + len(piece) > limit:
            chunks.append(current)
            current = piece
        else:
            current = f"{current}\n\n{piece}" if current else piece
    if current:
        chunks.append(current)
    return chunks


def title_and_body(text: str, stem: str) -> tuple[str, str]:
    """Frontmatter `title`'ı (yoksa ilk `# ` satırı, o da yoksa dosya adı) ve gövdeyi döndürür."""
    title: str | None = None
    body = text
    match = _FRONTMATTER.match(text)
    if match:
        body = text[match.end() :]
        found = _TITLE_LINE.search(match.group(1))
        if found:
            title = found.group(1).strip().strip("\"'").strip()
    if not title:
        heading = _H1_LINE.search(body)
        title = heading.group(1).strip() if heading else stem
    return title or stem, body


def _one_line(text: str, limit: int) -> str:
    """Her boşluk dizisini (satır sonları dahil) tek boşluğa indirir ve `limit`e kısaltır."""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def make_title(title: str) -> str:
    """Not başlığı kullanıcıdan gelir; tek satıra ve makul uzunluğa indirilir."""
    return _one_line(title, _TITLE_LIMIT)


def make_excerpt(passage: str) -> str:
    """Parçadan tek satırlık, en çok 300 karakterlik özet üretir.

    Başlık satırları atılır: modele giden şey notun konusu değil içeriği olmalı; başlık
    zaten ayrı alanda gelir. Yalnız başlıktan oluşan parça için ham metne dönülür.
    """
    body = [line for line in passage.splitlines() if not line.strip().startswith("#")]
    excerpt = _one_line(" ".join(body), _EXCERPT_LIMIT)
    return excerpt or _one_line(passage, _EXCERPT_LIMIT)


def tokenize(text: str) -> list[str]:
    """Türkçe-duyarlı küçültme sonrası `\\w+` sözcükleri; 2 karakterden kısaları atılır."""
    return [t for t in _TOKEN.findall(turkish_fold(text)) if len(t) >= _MIN_TOKEN_CHARS]


def read_capped(path: Path, max_chars: int) -> str:
    """UTF-8 notu en çok `max_chars` karakter okur; bozuk baytlar arama için yer değiştirilir."""
    with path.open("r", encoding="utf-8", errors="replace") as stream:
        return stream.read(max_chars)


@dataclass(frozen=True)
class _IndexedNote:
    path: str
    title: str
    title_counts: Counter[str]
    passages: list[str]
    passage_counts: list[Counter[str]]


def lexical_search(
    vault: Path,
    query: str,
    *,
    exclude: Sequence[str],
    limit: int,
    max_note_chars: int,
) -> list[SearchHit]:
    """tf-idf benzeri sözcük sıralaması; gömücü yokken veya kullanılamazken yedektir.

    idf, notlar değil parçalar üzerinden hesaplanır (uzun bir not ucuz biçimde her
    sözcüğü barındırıp idf'i düşürmesin). Başlık eşleşmesi ×2 ağırlıklıdır. Eşit
    skorlarda yol sırası belirleyicidir, böylece sonuç çalıştırmadan çalıştırmaya sabittir.
    """
    terms = list(dict.fromkeys(tokenize(query)))
    if not terms or limit <= 0:
        return []
    root = vault.resolve()
    notes: list[_IndexedNote] = []
    document_frequency: Counter[str] = Counter()
    passage_total = 0
    for path in iter_notes(vault, exclude):
        try:
            text = read_capped(path, max_note_chars)
        except OSError:
            # Okunamayan tek bir not (silinmiş, izin yok) tüm aramayı düşürmemeli.
            continue
        title, body = title_and_body(text, path.stem)
        passages = split_passages(body) or [title]
        all_counts = [Counter(tokenize(passage)) for passage in passages]
        for passage_count in all_counts:
            for term in terms:
                if passage_count[term]:
                    document_frequency[term] += 1
        passage_total += len(passages)
        notes.append(
            _IndexedNote(
                path=path.relative_to(root).as_posix(),
                title=title,
                title_counts=Counter(tokenize(title)),
                passages=passages,
                passage_counts=all_counts,
            )
        )

    def idf(term: str) -> float:
        frequency = document_frequency[term]
        return math.log(1 + (passage_total - frequency + 0.5) / (frequency + 0.5))

    weights = {term: idf(term) for term in terms}
    hits: list[SearchHit] = []
    for note in notes:
        best_score = 0.0
        best_passage = ""
        title_score = sum(
            _TITLE_WEIGHT * (1 + math.log(note.title_counts[term])) * weights[term]
            for term in terms
            if note.title_counts[term]
        )
        for passage, counts in zip(note.passages, note.passage_counts, strict=True):
            score = title_score + sum(
                (1 + math.log(counts[term])) * weights[term] for term in terms if counts[term]
            )
            if score > best_score:
                best_score, best_passage = score, passage
        if best_score > 0:
            hits.append(
                SearchHit(
                    path=note.path,
                    title=make_title(note.title),
                    score=best_score,
                    excerpt=make_excerpt(best_passage),
                )
            )
    hits.sort(key=lambda hit: (-hit.score, hit.path))
    return hits[:limit]
