"""Vault notları için isteğe bağlı anlamsal (embedding) arama.

Artemis ölçümünde sözcük araması paraphrase ağırlıklı sorularda hit@4 0.43, e5-large
0.73 verdi (ADR 0015). Bu modül notları vektöre çevirip kosinüs benzerliğiyle sıralar.
KAPALI gelir: `memory.embedding_model` verilmedikçe plugin bu modülü hiç çağırmaz.

Tasarım ilkeleri:

- `fastembed` ve `numpy` isteğe bağlı `memory` ek paketindedir ve YALNIZ kullanılırken
  `importlib` ile yüklenir. Böylece çekirdek kurulum ve CI (bu paketler yok) modülü
  import edip tip denetiminden geçirebilir; paket yoksa `EmbeddingUnavailable` fırlar.
- İndeks vault'un DIŞINDAKİ `cache_dir` altında durur: vault kullanıcının git deposudur
  ve üretilmiş ikili dosyalarla kirletilmemeli. Yapıcı, vault içine işaret eden bir
  önbellek klasörünü reddeder.
- Önbellek TEK bir `.npz`'dir (meta JSON'u içine gömülü) ve geçici dosya + `os.replace`
  ile yazılır: matris ve meta ayrı dosya olsaydı, aralarında kesilen bir yazma uyumsuz
  bir indeks bırakırdı.
- Artımlı: her not baytlarının sha256'sıyla izlenir; yalnız değişen/yeni notlar yeniden
  gömülür, silinenler düşer.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import logging
import os
import re
import tempfile
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jarvis.memory.vault import (
    SearchHit,
    iter_notes,
    make_excerpt,
    make_title,
    split_passages,
    title_and_body,
)

logger = logging.getLogger(__name__)

# (metinler, sorgu_mu) -> vektörler. e5 önekleri metne bu modülde eklenir; gömücü önek bilmez.
Embedder = Callable[[list[str], bool], Sequence[Sequence[float]]]

_BATCH_SIZE = 32
# Parçalama veya meta biçimi değişirse artırılır; eski önbellek yeniden kurulur.
_CACHE_VERSION = 1
# Devasa bir dosya indeksleme sırasında belleği şişirmesin.
_MAX_NOTE_BYTES = 1_000_000


class EmbeddingUnavailable(RuntimeError):  # noqa: N818 - ad sözleşmeden: "kullanılamıyor" bir durumdur
    """`fastembed`/`numpy` kurulu değil ya da model yüklenemedi (indirme, ağ, bozuk dosya)."""


@dataclass(frozen=True)
class _Note:
    path: str
    sha: str
    title: str
    passages: list[str]
    embed_texts: list[str]


def _numpy() -> Any:
    """numpy yalnız `memory` ekinde bulunur; tip denetimi için bilerek `Any` döner."""
    try:
        return importlib.import_module("numpy")
    except ImportError as exc:
        raise EmbeddingUnavailable(
            "numpy kurulu değil; embedding için `pip install 'jarvis-tr[memory]'` gerekir."
        ) from exc


def _safe_name(model_name: str) -> str:
    """Model adını dosya adına uygun hale getirir (`org/model` → `org_model`)."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", model_name)


class EmbeddingIndex:
    """Vault notlarının kalıcı, artımlı embedding indeksi."""

    def __init__(
        self,
        vault: Path,
        model_name: str,
        cache_dir: Path,
        exclude: Sequence[str],
        *,
        embedder: Embedder | None = None,
    ) -> None:
        if cache_dir.resolve().is_relative_to(vault.resolve()):
            raise ValueError("Embedding önbellek klasörü vault içinde olamaz.")
        self._vault = vault
        self._model_name = model_name
        self._cache_dir = cache_dir
        self._cache_file = cache_dir / f"{_safe_name(model_name)}.npz"
        self._exclude = tuple(exclude)
        self._embedder = embedder
        self._e5 = "e5" in model_name.lower()

    # ------------------------------------------------------------------ #
    # Gömücü
    # ------------------------------------------------------------------ #

    def _default_embedder(self) -> Embedder:
        """`fastembed.TextEmbedding`'i yükler; paket veya model yoksa `EmbeddingUnavailable`."""
        try:
            fastembed = importlib.import_module("fastembed")
        except ImportError as exc:
            raise EmbeddingUnavailable(
                "fastembed kurulu değil; embedding için `pip install 'jarvis-tr[memory]'` gerekir."
            ) from exc
        try:
            # Model dosyaları da vault dışındaki önbellek klasörüne iner; fastembed'in
            # varsayılanı geçici dizindir ve yeniden başlatmada silinip modeli yeniden indirtir.
            model = fastembed.TextEmbedding(
                self._model_name, cache_dir=str(self._cache_dir / "models")
            )
        except (ImportError, OSError, ValueError, RuntimeError) as exc:
            raise EmbeddingUnavailable("Embedding modeli yüklenemedi veya indirilemedi.") from exc

        def embed(texts: list[str], is_query: bool) -> Sequence[Sequence[float]]:
            return list(model.embed(texts, batch_size=len(texts)))

        return embed

    def _embed(self, np: Any, texts: list[str], *, is_query: bool) -> Any:
        """Metinleri (e5 için önekli) gömer ve L2-normalize eder; kosinüs nokta çarpımı olur."""
        if self._embedder is None:
            self._embedder = self._default_embedder()
        prefix = ("query: " if is_query else "passage: ") if self._e5 else ""
        vectors = np.asarray(
            self._embedder([prefix + text for text in texts], is_query), dtype=np.float32
        )
        if vectors.ndim != 2 or vectors.shape[0] != len(texts):
            raise ValueError("Gömücü beklenen biçimde vektör döndürmedi.")
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return np.asarray(vectors / norms, dtype=np.float32)

    # ------------------------------------------------------------------ #
    # Korpus
    # ------------------------------------------------------------------ #

    def _read_notes(self) -> list[_Note]:
        root = self._vault.resolve()
        notes: list[_Note] = []
        for file in iter_notes(self._vault, self._exclude):
            try:
                with file.open("rb") as stream:
                    raw = stream.read(_MAX_NOTE_BYTES)
            except OSError as exc:
                logger.warning("Not embedding için okunamadı, atlandı: %s", type(exc).__name__)
                continue
            title, body = title_and_body(raw.decode("utf-8", errors="replace"), file.stem)
            passages = split_passages(body) or [title]  # gövdesi boş not başlığıyla bulunsun
            notes.append(
                _Note(
                    path=file.relative_to(root).as_posix(),
                    sha=hashlib.sha256(raw).hexdigest(),
                    title=title,
                    passages=passages,
                    embed_texts=[f"{title}\n{passage}" for passage in passages],
                )
            )
        return notes

    # ------------------------------------------------------------------ #
    # Önbellek
    # ------------------------------------------------------------------ #

    def _load_cache(self, np: Any) -> tuple[dict[str, Any], Any] | None:
        """Önbelleği okur; yoksa, bozuksa veya sürüm/model uyuşmuyorsa `None` döner."""
        if not self._cache_file.exists():
            return None
        try:
            with np.load(self._cache_file, allow_pickle=False) as data:
                meta = json.loads(str(data["meta"]))
                matrix = np.asarray(data["matrix"], dtype=np.float32)
            if meta["version"] != _CACHE_VERSION or meta["model"] != self._model_name:
                return None
            total = sum(int(entry["count"]) for entry in meta["notes"].values())
            if matrix.ndim != 2 or matrix.shape[0] != total:
                raise ValueError("matris satır sayısı meta ile uyuşmuyor")
        except (OSError, ValueError, KeyError, TypeError, EOFError, zipfile.BadZipFile) as exc:
            # Önbellek yeniden üretilebilir; bozuğu aramayı değil yalnız hızı etkiler.
            logger.warning(
                "Embedding önbelleği okunamadı, yeniden kurulacak: %s", type(exc).__name__
            )
            return None
        return meta, matrix

    def _save_cache(self, np: Any, meta: dict[str, Any], matrix: Any) -> None:
        """Geçici dosyaya yazıp `os.replace` ile yerine koyar; yarım dosya kalmaz."""
        tmp_path: str | None = None
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(
                dir=self._cache_dir, prefix=".embedding-", suffix=".tmp"
            )
            with os.fdopen(fd, "wb") as handle:
                packed = np.array(json.dumps(meta, ensure_ascii=False))
                np.savez(handle, matrix=matrix, meta=packed)
            os.replace(tmp_path, self._cache_file)
            tmp_path = None
        except OSError as exc:
            # Yazılamayan önbellek aramayı bozmaz: sonuçlar bellekteki indeksten doğru çıkar.
            logger.warning("Embedding önbelleği yazılamadı: %s", type(exc).__name__)
        finally:
            if tmp_path is not None:
                try:
                    Path(tmp_path).unlink(missing_ok=True)
                except OSError as exc:
                    logger.warning("Geçici önbellek dosyası silinemedi: %s", type(exc).__name__)

    def _sync(self, np: Any) -> tuple[list[tuple[str, str, list[str]]], Any]:
        """Önbelleği korpusla eşitler; ((yol, başlık, parçalar) listesi, satır matrisi) döner."""
        cached = self._load_cache(np)
        old_meta: dict[str, Any]
        if cached is None:
            old_meta, old_matrix = {"notes": {}}, np.zeros((0, 0), dtype=np.float32)
        else:
            old_meta, old_matrix = cached
        old_notes: dict[str, Any] = old_meta["notes"]

        current = self._read_notes()
        reusable = {
            note.path: old_notes[note.path]
            for note in current
            if note.path in old_notes and old_notes[note.path]["sha"] == note.sha
        }
        to_embed = [note for note in current if note.path not in reusable]
        changed = bool(to_embed) or set(old_notes) != {note.path for note in current}

        new_vectors: dict[str, Any] = {}
        if to_embed:
            flat = [text for note in to_embed for text in note.embed_texts]
            parts = [
                self._embed(np, flat[i : i + _BATCH_SIZE], is_query=False)
                for i in range(0, len(flat), _BATCH_SIZE)
            ]
            stacked = np.concatenate(parts, axis=0)
            row = 0
            for note in to_embed:
                new_vectors[note.path] = stacked[row : row + len(note.passages)]
                row += len(note.passages)

        blocks: list[Any] = []
        notes_meta: dict[str, Any] = {}
        entries: list[tuple[str, str, list[str]]] = []
        offset = 0
        for note in current:
            if note.path in reusable:
                old = reusable[note.path]
                start = int(old["offset"])
                block = old_matrix[start : start + int(old["count"])]
                passages = list(old["passages"])
            else:
                block = new_vectors[note.path]
                passages = note.passages
            notes_meta[note.path] = {
                "sha": note.sha,
                "title": note.title,
                "count": len(passages),
                "offset": offset,
                "passages": passages,
            }
            blocks.append(block)
            entries.append((note.path, note.title, passages))
            offset += len(passages)

        matrix = np.concatenate(blocks, axis=0) if blocks else np.zeros((0, 0), dtype=np.float32)
        if (changed or cached is None) and blocks:
            meta = {"version": _CACHE_VERSION, "model": self._model_name, "notes": notes_meta}
            self._save_cache(np, meta, matrix)
        return entries, matrix

    # ------------------------------------------------------------------ #
    # Arama
    # ------------------------------------------------------------------ #

    def search(self, query: str, limit: int) -> list[SearchHit]:
        """Sorguya en yakın `limit` notu döndürür; not skoru parçalarının en yüksek kosinüsüdür.

        Raises:
            EmbeddingUnavailable: numpy, fastembed veya model yok.
            OSError, ValueError: indeks ya da gömücü sorunu; çağıran dürüstçe bildirir.
        """
        if not query.strip() or limit <= 0:
            return []
        np = _numpy()
        entries, matrix = self._sync(np)
        if not entries:
            return []
        query_vector = self._embed(np, [query], is_query=True)[0]
        if matrix.shape[1] != query_vector.shape[0]:
            raise ValueError("Sorgu vektörü indeksin boyutuyla uyuşmuyor.")
        scores = matrix @ query_vector
        hits: list[SearchHit] = []
        row = 0
        for path, title, passages in entries:
            block = scores[row : row + len(passages)]
            best = int(np.argmax(block))
            hits.append(
                SearchHit(
                    path=path,
                    title=make_title(title),
                    score=float(block[best]),
                    excerpt=make_excerpt(passages[best]),
                )
            )
            row += len(passages)
        hits.sort(key=lambda hit: (-hit.score, hit.path))
        return hits[:limit]
