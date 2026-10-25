"""Söylenen uygulama adını izin listesindeki uygulamaya bağlar (ADR 0044).

Konuşma tanıma İngilizce uygulama adlarını Türkçe okunuşla yazabilir ("breyv", "kroum").
Çözüm sırası: tam/normalize ad, kullanıcının öğrettiği takma ad, sonra yakın eşleşme.
Yakın eşleşme yalnız tek ve açık aday varsa kabul edilir; yakın iki aday ya da zayıf aday
varsa çözüm "belirsiz" döner ve model kullanıcıya sorar. Aday her zaman izin listesinden
(``settings.applications``) gelir; takma ad listeyi genişletmez.

Takma adlar ``state_dir/app_aliases.json`` içindedir: kullanıcıya özeldir, depoya girmez.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Literal

MAX_ALIASES = 200
MAX_ALIAS_CHARS = 64
# Yakın eşleşmenin kendiliğinden kabul eşiği ve en yakın ikinci adaydan farkı. Eşik altı
# ama makul adaylar soruya döner; daha zayıflar hiç önerilmez.
ACCEPT_SCORE = 0.85
ACCEPT_MARGIN = 0.08
# Daha zayıf ama rakipsiz aday ("zum" -> Zoom, "eksel" -> Excel) da kabul edilir.
LONE_SCORE = 0.72
LONE_MARGIN = 0.25
CANDIDATE_SCORE = 0.7
MAX_CANDIDATES = 3
# Kaldırıcı kısayolu yakın eşleşmeyle asla seçilmez: "kaldır" söylenmeden kaldırıcı açılmasın.
UNINSTALL_WORDS = ("uninstall", "kaldır", "kaldir")

MatchKind = Literal["exact", "alias", "fuzzy", "ambiguous", "none"]

_FOLD = str.maketrans({"ı": "i", "ş": "s", "ç": "c", "ğ": "g", "ö": "o", "ü": "u"})
_VOWELS = frozenset("aeiouy")
_SOUNDS: dict[int, str | None] = {
    ord("c"): "k", ord("q"): "k", ord("w"): "v", ord("x"): "k", ord("h"): None,
}


def normalize(value: str) -> str:
    """Büyük harf, boşluk ve noktalama farkını siler; Türkçe I/İ doğru küçülür."""
    lowered = value.replace("I", "ı").replace("İ", "i").casefold()
    return "".join(char for char in lowered if char.isalnum() or char in "+#")


def _fold(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", normalize(value).translate(_FOLD))
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def skeleton(value: str) -> str:
    """Okunuş iskeleti: ünsüzler, sesli harfler atılır ("brave", "breyv", "brev" -> "brv").

    İngilizce yazımın Türkçe okunuşla yazıya dökülmesini yakalamak için ses benzeri harfler
    birleştirilir (c/q -> k, w -> v, ph -> f). İlk sesli harf de atılır: "Audacity" Türkçe
    "odasiti" diye yazılır. Tekrarlayan harf tek sayılır.
    """
    text = _fold(value)
    for source, target in (("ph", "f"), ("ck", "k"), ("sh", "s"), ("ch", "k"), ("th", "t")):
        text = text.replace(source, target)
    # İngilizcede e/i/y önündeki c "s" okunur (Audacity -> "odasiti", Excel -> "eksel").
    text = re.sub(r"c(?=[eiy])", "s", text)
    text = text.translate(_SOUNDS)
    result: list[str] = []
    for char in text:
        if char in _VOWELS:
            continue
        if result and result[-1] == char:
            continue
        result.append(char)
    return "".join(result)


def _ratio(left: str, right: str) -> float:
    return SequenceMatcher(None, left, right).ratio() if left and right else 0.0


def _score(spoken: str, target: str) -> float:
    plain = _ratio(_fold(spoken), _fold(target))
    left, right = skeleton(spoken), skeleton(target)
    if len(left) >= 3 and left == right:
        return max(plain, 0.9)
    return max(plain, (plain + _ratio(left, right)) / 2)


def similarity(spoken: str, application: str) -> float:
    """Tam ad ya da adın bir sözcüğü ("Google Chrome" için "chrome") ile benzerlik."""
    best = _score(spoken, application)
    for word in application.split():
        if len(normalize(word)) >= 3:
            best = max(best, 0.95 * _score(spoken, word))
    return best


@dataclass(frozen=True)
class AppMatch:
    kind: MatchKind
    name: str | None = None
    executable: str | None = None
    candidates: tuple[str, ...] = field(default=())

    @property
    def certain(self) -> bool:
        """Kullanıcının söylediği ya da öğrettiği ad: yakın eşleşme değil."""
        return self.kind in ("exact", "alias")


def resolve_application(
    applications: dict[str, str], aliases: dict[str, str], spoken: str, *, fuzzy: bool = True
) -> AppMatch:
    exact = next((name for name in applications if name.casefold() == spoken.casefold()), None)
    if exact is None:
        key = normalize(spoken)
        exact = next((name for name in applications if normalize(name) == key), None)
    if exact is not None:
        return AppMatch("exact", exact, applications[exact])
    target = aliases.get(normalize(spoken))
    if target is not None and target in applications:
        return AppMatch("alias", target, applications[target])
    if not fuzzy or not normalize(spoken):
        return AppMatch("none")
    scored = sorted(
        (
            (similarity(spoken, name), name)
            for name in applications
            if not any(word in name.casefold() for word in UNINSTALL_WORDS)
        ),
        key=lambda item: (-item[0], item[1]),
    )
    candidates = [(score, name) for score, name in scored if score >= CANDIDATE_SCORE]
    if not candidates:
        return AppMatch("none")
    best_score, best = candidates[0]
    runner_up = scored[1][0] if len(scored) > 1 else 0.0
    margin = best_score - runner_up
    if (best_score >= ACCEPT_SCORE and margin >= ACCEPT_MARGIN) or (
        best_score >= LONE_SCORE and margin >= LONE_MARGIN
    ):
        return AppMatch("fuzzy", best, applications[best])
    return AppMatch(
        "ambiguous", candidates=tuple(name for _, name in candidates[:MAX_CANDIDATES])
    )


class AliasStoreError(Exception):
    pass


class AliasStore:
    """Kullanıcının öğrettiği takma adlar: normalize söylenen ad -> izinli uygulama adı."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, str]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as error:
            raise AliasStoreError("Takma ad dosyası okunamadı") from error
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as error:
            raise AliasStoreError("Takma ad dosyası bozuk") from error
        if not isinstance(data, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in data.items()
        ):
            raise AliasStoreError("Takma ad dosyası bozuk")
        return dict(data)

    def save(self, aliases: dict[str, str]) -> None:
        if len(aliases) > MAX_ALIASES:
            raise AliasStoreError(f"En çok {MAX_ALIASES} takma ad tutulur")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Yarım yazılmış dosya bırakmamak için geçici dosya + atomik yer değiştirme.
        handle, temporary = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(aliases, stream, ensure_ascii=False, indent=1, sort_keys=True)
            os.replace(temporary, self.path)
        except OSError as error:
            Path(temporary).unlink(missing_ok=True)
            raise AliasStoreError("Takma ad dosyası yazılamadı") from error


def alias_store(state_dir: Path) -> AliasStore:
    return AliasStore(state_dir / "app_aliases.json")
