"""Yerel dosya araması (ADR 0065): kullanıcının seçtiği klasörlerde tam metin, gömme yok.

İndeks kişisel metin içerir; yalnız `state_dir/dosya_arama.sqlite3`'te durur ve "Dizini sil" ile
dosya olarak silinir. Dizinleme arka plan iş parçacığında çalışır, iptal edilebilir, tura asla
engel olmaz. Bir dosya ya da klasör okunamıyorsa tüm koşu durmaz. Bağlantılar (symlink, junction)
kökün dışına çıkabileceği için ne izlenir ne dizinlenir. Dosya adları dış metindir: sonuçta
`sanitize_name` ile temizlenir.

Türkçe: metin ve sorgu önce `search_fold` ile küçültülür; i/ı/İ/I ayrımı yok sayılır ("ısparta",
"isparta", "ISPARTA" eşleşir). Sonra FTS5 `unicode61` ayrıştırıcısı noktalı/şapkalı harfleri
sadeleştirir (ş→s, ğ→g, ö→o, ü→u, ç→c), böylece "SÖZLEŞME", "sözleşme" ve "sozlesme" eşleşir.
Kök ekleri yalnız önek olarak eşleşir ("kira" → "kirası", tersi değil).
"""

from __future__ import annotations

import html
import os
import re
import sqlite3
import stat
import threading
import zipfile
import zlib
from collections.abc import Iterator
from contextlib import closing, suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from artemis.config.settings import Settings
from artemis.proactive.triggers import sanitize_name
from artemis.safety.audit import AuditLog
from artemis.skills import skills_dir
from artemis.text import search_fold

DB_NAME = "dosya_arama.sqlite3"
INDEX_VERSION = 2
# Bir koşuda en çok bu kadar dosya görülür; dev klasör (örn. tüm disk) süresiz sürmesin.
MAX_FILES = 50_000
# docx içindeki document.xml'in açılmış boyut sınırı (zip bombası): beyan ve gerçek okuma ikisi de.
MAX_DOCX_XML_BYTES = 20 * 1024 * 1024
SNIPPET_CHARS = 200
MAX_RESULTS = 10
BATCH = 50
BUSY_SECONDS = 5.0
JOIN_SECONDS = 5.0

TEXT_SUFFIXES = frozenset({
    ".txt", ".md", ".markdown", ".rst", ".csv", ".tsv", ".json", ".log", ".xml", ".html", ".htm",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".tex", ".sql", ".py", ".js", ".ts",
    ".tsx", ".jsx", ".css", ".java", ".kt", ".c", ".h", ".cpp", ".hpp", ".cs", ".go", ".rs",
    ".rb", ".php", ".swift", ".sh", ".ps1", ".bat", ".cmd",
})
SECRET_SUFFIXES = (".pem", ".key", ".pfx", ".p12", ".kdbx", ".ppk", ".jks", ".keystore")
SECRET_PREFIXES = (".env", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519")
SECRET_WORDS = ("secret", "token", "password", "credentials")
# Tam ad eşleşmesi: kimlik bilgisi tutan araç ve tarayıcı dosyaları.
SECRET_NAMES = frozenset({
    ".netrc", "_netrc", ".npmrc", ".pypirc", ".git-credentials",
    "login data", "cookies", "key4.db", "logins.json",
})
# İçindeki hiçbir şeyin dizinlenmediği klasörler (anahtar/kimlik deposu).
DENY_DIRS = frozenset({".ssh", ".aws", ".gnupg", ".azure", ".kube", ".docker", ".password-store"})
# Beklenen dosya hataları: biri bozuk/yetkisiz/garip dosya yüzünden koşu durmaz.
_FILE_ERRORS = (
    OSError, zipfile.BadZipFile, KeyError, RuntimeError, NotImplementedError, EOFError,
    zlib.error, ValueError,
)
_WORD = re.compile(r"\w+")
_TAG = re.compile(r"<[^>]+>")
_PARAGRAPH_END = re.compile(r"</w:p>")

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS files (id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE, "
    "size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL)",
    "CREATE VIRTUAL TABLE IF NOT EXISTS files_fts USING fts5(name, body, tokenize='unicode61')",
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
)


def fts5_available() -> bool:
    """Bu Python'un SQLite'ı FTS5 taşıyor mu; yoksa özellik dürüstçe kapanır."""
    try:
        with closing(sqlite3.connect(":memory:")) as db:
            db.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
    except sqlite3.Error:
        return False
    return True


def is_secret_name(name: str) -> bool:
    """Gizli görünen dosya adı: asla dizinlenmez (ne içeriği ne adı)."""
    low = name.casefold()
    return (
        low in SECRET_NAMES
        or low.startswith(SECRET_PREFIXES)
        or low.endswith(SECRET_SUFFIXES)
        or any(word in low for word in SECRET_WORDS)
    )


def usable_roots(settings: Settings) -> tuple[list[Path], list[str]]:
    """Dizinlenebilir kökler ve reddedilenlerin nedenleri.

    State klasörü ve beceri klasörü (içindekiler dahil) hiçbir koşulda dizinlenmez: kök onların
    içindeyse ya da eşitse reddedilir, kök onları içeriyorsa gezinti o klasörleri atlar.
    """
    roots: list[Path] = []
    problems: list[str] = []
    guarded = [settings.state_dir.resolve(), skills_dir(settings).resolve()]
    for raw in settings.file_search.roots:
        try:
            root = Path(raw).resolve(strict=True)
        except OSError:
            problems.append(f"Klasör bulunamadı: {sanitize_name(raw, 200)}")
            continue
        if not root.is_dir():
            problems.append(f"Klasör değil: {sanitize_name(raw, 200)}")
        elif any(root == g or g in root.parents for g in guarded):
            problems.append(f"Uygulamanın kendi klasörü dizinlenemez: {sanitize_name(raw, 200)}")
        elif root not in roots:
            roots.append(root)
    return roots, problems


def _dir_blocked(name: str, excluded: set[str]) -> bool:
    """Klasör adı yüzünden içi dizinlenmez: hariç liste, anahtar deposu ya da gizli görünen ad."""
    low = name.casefold()
    return low in excluded or low in DENY_DIRS or is_secret_name(name)


class Rules:
    """Dizinleme ve arama için TEK kural kümesi: gezgin ve `search` aynı `check`'i kullanır, böylece
    ikisi birbirinden ayrışamaz. Her çağrı o anki config'ten kurulur (kök kaldırılmış olabilir)."""

    def __init__(self, settings: Settings) -> None:
        self.roots, self.problems = usable_roots(settings)
        self.guarded = [settings.state_dir.resolve(), skills_dir(settings).resolve()]
        self.excluded = {name.casefold() for name in settings.file_search.exclude_dirs}
        self.cap = settings.file_search.max_file_mb * 1024 * 1024

    def dir_ok(self, path: str) -> bool:
        """Gezgin için: bu klasöre inilebilir mi (bağlantı, korunan, hariç, anahtar deposu)."""
        if os.path.islink(path) or os.path.isjunction(path):
            return False
        norm = os.path.normcase(path)
        if any(norm == os.path.normcase(str(g)) for g in self.guarded):
            return False
        return not _dir_blocked(os.path.basename(path), self.excluded)

    def check(self, path: Path) -> os.stat_result | None:
        """Dosya şimdi de dizinlenebilir mi; evet ise lstat. Başarısızlık sessizce eler."""
        try:
            info = os.lstat(path)
            if not stat.S_ISREG(info.st_mode) or info.st_size > self.cap:
                return None
            # Gerçek yol kayıtlı yolla aynı olmalı: ara bileşen bağlantıysa farklı çıkar.
            real = os.path.normcase(os.path.realpath(path))
            if real != os.path.normcase(os.path.abspath(path)):
                return None
        except (OSError, ValueError):
            return None
        absolute = Path(os.path.abspath(path))
        root = next((r for r in self.roots if r == absolute or r in absolute.parents), None)
        if root is None:
            return None  # kaldırılmış ya da geçersiz kök
        if any(g == absolute or g in absolute.parents for g in self.guarded):
            return None
        parts = absolute.relative_to(root).parts
        if is_secret_name(parts[-1]) or any(_dir_blocked(d, self.excluded) for d in parts[:-1]):
            return None
        return info


def _decode(raw: bytes) -> str:
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1254", errors="replace")  # eski Windows Türkçe metinleri


def _docx_text(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        info = archive.getinfo("word/document.xml")
        if info.file_size > MAX_DOCX_XML_BYTES:
            raise ValueError("docx gövdesi çok büyük")
        with archive.open(info) as member:
            raw = member.read(MAX_DOCX_XML_BYTES + 1)  # beyan yalan olabilir: okumayı da sınırla
    if len(raw) > MAX_DOCX_XML_BYTES:
        raise ValueError("docx gövdesi çok büyük")
    # XML ayrıştırıcı kullanılmaz (varlık patlaması riski yok): etiketler atılır.
    xml = _PARAGRAPH_END.sub("\n", raw.decode("utf-8", errors="replace"))
    return html.unescape(_TAG.sub("", xml))


def extract_text(path: Path, max_bytes: int) -> str:
    """Dosyanın aranabilir metni; desteklenmeyen tür (PDF, ikili) için boş."""
    suffix = path.suffix.casefold()
    if suffix == ".docx":
        return _docx_text(path)
    if suffix not in TEXT_SUFFIXES:
        return ""
    with path.open("rb") as stream:
        raw = stream.read(max_bytes + 1)
    if len(raw) > max_bytes or b"\x00" in raw[:4096]:
        return ""
    return _decode(raw)


def _fold(text: str) -> str:
    return search_fold(text)


def build_match(query: str) -> str | None:
    """Kullanıcı sorgusunu güvenli FTS5 ifadesine çevirir: her sözcük tırnaklı önek terimi.

    Operatör, tırnak, `*`, `:` gibi sözdizimi karakterleri ayrıştırmada düşer; FTS5'e yalnız
    `\\w` sözcükleri gider, bu yüzden söz dizimi hatası oluşamaz.
    """
    words = _WORD.findall(_fold(query))
    return " ".join(f'"{w}"*' if len(w) >= 3 else f'"{w}"' for w in words) or None


def snippet_around(text: str, query: str) -> str:
    """İlk eşleşmenin çevresinden en çok 200 karakter; kontrol/yön karakterleri temiz.

    Büyük/küçük harf katlaması uzunluğu değiştirirse (örn. ß) katlanmış metindeki konum özgün
    metne taşınamaz: o zaman özgün metinde büyük/küçük harfe bakmadan aranır, bulunamazsa metnin
    başı alınır. Böylece konum asla kayıp yanlış yeri göstermez.
    """
    words = _WORD.findall(_fold(query))
    folded = _fold(text)
    at = -1
    for word in words:
        if len(folded) == len(text):
            at = folded.find(word)
        else:
            found = re.search(re.escape(word), text, re.IGNORECASE)
            at = found.start() if found else -1
        if at >= 0:
            break
    if at < 0:
        if len(folded) == len(text) or not text.strip():
            return ""
        at = 0
    start = max(0, at - SNIPPET_CHARS // 2)
    chunk = " ".join(text[start : start + SNIPPET_CHARS].split())
    return sanitize_name(chunk, SNIPPET_CHARS)


@dataclass(frozen=True)
class Hit:
    path: str
    modified: str
    snippet: str


class FileIndexError(Exception):
    """Beklenen hata; kullanıcıya olduğu gibi söylenebilir."""


# Süreç içinde veritabanı yolu başına TEK FileIndex: tek kilit, tek iptal bayrağı, tek iş parçacığı.
# Panel ve runtime aynı örneği kullanmazsa "Dizini sil" çalışan dizinleyiciyi durduramaz.
_SHARED: dict[str, FileIndex] = {}
_SHARED_LOCK = threading.Lock()


class FileIndex:
    def __init__(self, settings: Settings, audit: AuditLog) -> None:
        self.settings = settings
        self.audit = audit
        self.path = settings.state_dir / DB_NAME
        self.available = fts5_available()
        # Bağlantılar iş parçacıkları arasında paylaşılmaz; kilit yalnız aynı dosyaya aynı anda
        # yazma/silme/sorgu yapılmasın diye (silme sırasında açık bağlantı kalmasın).
        self._lock = threading.Lock()
        self._start_lock = threading.Lock()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._note = ""

    @classmethod
    def shared(cls, settings: Settings, audit: AuditLog) -> FileIndex:
        """Aynı veritabanı yolu için süreçteki tek örnek; ayar (kökler) en yenisine güncellenir."""
        key = os.path.normcase(str((settings.state_dir / DB_NAME).resolve()))
        with _SHARED_LOCK:
            index = _SHARED.get(key)
            if index is None:
                index = _SHARED[key] = cls(settings, audit)
            else:
                index.settings = settings
            return index

    # Bağlantı ve durum ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=BUSY_SECONDS)
        try:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version != INDEX_VERSION:
                with db:
                    db.execute("DROP TABLE IF EXISTS files_fts")
                    db.execute("DROP TABLE IF EXISTS files")
                    db.execute("DROP TABLE IF EXISTS meta")
                    for statement in SCHEMA:
                        db.execute(statement)
                    db.execute(f"PRAGMA user_version = {INDEX_VERSION}")
            else:
                for statement in SCHEMA:
                    db.execute(statement)
        except Exception:
            db.close()
            raise
        return db

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self) -> dict[str, object]:
        """Panel ve araç için: dosya sayısı, son koşu, çalışıyor mu, not."""
        count, last = 0, ""
        if self.available:
            try:
                with self._lock:
                    # Varlık denetimi kilit içinde: silinmiş indeks bir okumayla yeniden doğmaz.
                    if self.path.exists():
                        with closing(self._connect()) as db:
                            count = db.execute("SELECT COUNT(*) FROM files").fetchone()[0]
                            row = db.execute(
                                "SELECT value FROM meta WHERE key='last_run'"
                            ).fetchone()
                            last = row[0] if row else ""
            except sqlite3.Error:
                self._note = "İndeks okunamadı."
        return {"files": count, "last_run": last, "running": self.running, "note": self._note}

    # Dizinleme --------------------------------------------------------------------------

    def start(self) -> bool:
        """Arka planda dizinler; kapalıysa ya da zaten çalışıyorsa hiçbir şey yapmaz (False).

        Silme sonrası iptal bayrağı yalnız burada, yani kullanıcı yeniden dizinleme isteyince
        (ya da uygulama yeniden başlayınca) kalkar.
        """
        with self._start_lock:
            if not self.available or self.running:
                return False
            self._cancel.clear()
            self._note = ""
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
            return True

    def cancel(self) -> None:
        self._cancel.set()

    def shutdown(self) -> None:
        self.cancel()
        thread = self._thread
        if thread is not None:
            thread.join(JOIN_SECONDS)

    def _run(self) -> None:
        """İş parçacığı girişi: hiçbir şey dışarı sızmaz; durum notu dürüstçe yazılır."""
        if self._cancel.is_set():
            return  # silinmiş indeks yeniden oluşturulmaz
        try:
            self._index()
        except Exception as error:  # noqa: BLE001 - iş parçacığı sınırı
            self._note = f"Dizinleme beklenmeyen hatayla durdu ({type(error).__name__})."
            self.audit.emit("file_index_error", error_type=type(error).__name__)

    def _walk(self, rules: Rules) -> Iterator[tuple[Path, os.stat_result]]:
        """Kökler altındaki (yol, lstat) çiftleri; her dosya aynı `Rules.check`'ten geçer."""
        for root in rules.roots:
            for current, dirs, files in os.walk(root, followlinks=False):
                dirs[:] = [d for d in dirs if rules.dir_ok(os.path.join(current, d))]
                for name in files:
                    full = Path(current, name)
                    info = rules.check(full)
                    if info is not None:
                        yield full, info

    def _index(self) -> None:
        rules = Rules(self.settings)
        if rules.problems:
            self._note = rules.problems[0]
        if not rules.roots:
            return
        with self._lock:
            if self._cancel.is_set():
                return
            with closing(self._connect()) as db:
                known = {
                    p: (s, m) for p, s, m in db.execute("SELECT path, size, mtime_ns FROM files")
                }
        seen: set[str] = set()
        pending: list[tuple[str, int, int, str, str]] = []
        indexed = 0
        truncated = False
        for path, info in self._walk(rules):
            if self._cancel.is_set():
                break
            if len(seen) >= MAX_FILES:
                truncated = True
                break
            key = str(path)
            if key in seen:
                continue
            seen.add(key)
            if known.get(key) == (info.st_size, info.st_mtime_ns):
                continue
            try:
                body = extract_text(path, rules.cap)
            except _FILE_ERRORS:
                body = ""  # bozuk/yetkisiz dosya: adı yine aranabilir, koşu durmaz
            pending.append((key, info.st_size, info.st_mtime_ns, _fold(path.name), _fold(body)))
            indexed += 1
            if len(pending) >= BATCH:
                self._write(pending, [])
                pending = []
        cancelled = self._cancel.is_set()
        # Yarım koşu (iptal/üst sınır) görülmeyen dosyaları "silinmiş" sayamaz.
        gone = [] if cancelled or truncated else [p for p in known if p not in seen]
        self._write(pending, gone)
        if truncated:
            self._note = f"İlk {MAX_FILES} dosyada duruldu; kökleri daraltın."
        if not cancelled:
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
            self._write([], [], last_run=stamp)
        self.audit.emit(
            "file_index_run", indexed=indexed, removed=len(gone), cancelled=cancelled,
            truncated=truncated,
        )

    def _write(
        self,
        rows: list[tuple[str, int, int, str, str]],
        gone: list[str],
        last_run: str = "",
    ) -> None:
        if not rows and not gone and not last_run:
            return
        with self._lock:
            if self._cancel.is_set():
                return  # iptal/silme: yazma yok, silinen indeks yeniden doğmaz
            with closing(self._connect()) as db, db:
                for path, size, mtime, name, body in rows:
                    old = db.execute("SELECT id FROM files WHERE path=?", (path,)).fetchone()
                    if old:
                        db.execute("DELETE FROM files_fts WHERE rowid=?", (old[0],))
                        db.execute("DELETE FROM files WHERE id=?", (old[0],))
                    cursor = db.execute(
                        "INSERT INTO files (path, size, mtime_ns) VALUES (?, ?, ?)",
                        (path, size, mtime),
                    )
                    db.execute(
                        "INSERT INTO files_fts (rowid, name, body) VALUES (?, ?, ?)",
                        (cursor.lastrowid, name, body),
                    )
                for path in gone:
                    old = db.execute("SELECT id FROM files WHERE path=?", (path,)).fetchone()
                    if old:
                        db.execute("DELETE FROM files_fts WHERE rowid=?", (old[0],))
                        db.execute("DELETE FROM files WHERE id=?", (old[0],))
                if last_run:
                    db.execute("INSERT OR REPLACE INTO meta VALUES ('last_run', ?)", (last_run,))

    # Arama ve silme ---------------------------------------------------------------------

    def search(self, query: str, limit: int) -> list[Hit]:
        limit = max(1, min(limit, MAX_RESULTS))
        match = build_match(query)
        if match is None or not self.available:
            return []
        rules = Rules(self.settings)
        if not rules.roots:
            return []
        try:
            with self._lock:
                if not self.path.exists():
                    return []
                with closing(self._connect()) as db:
                    # Elenecek satırlar olabilir (değişmiş/kaldırılmış): fazladan çekilir.
                    rows = db.execute(
                        "SELECT f.path, f.mtime_ns FROM files_fts"
                        " JOIN files f ON f.id = files_fts.rowid WHERE files_fts MATCH ?"
                        " ORDER BY bm25(files_fts, 5.0, 1.0) LIMIT ?",
                        (match, limit * 5),
                    ).fetchall()
        except sqlite3.Error as error:
            raise FileIndexError("Dosya indeksi okunamadı.") from error
        hits: list[Hit] = []
        for path, mtime_ns in rows:
            if len(hits) >= limit:
                break
            # İndeksteki yol bayat olabilir: HERHANGİ BİR okumadan önce dizinleme kurallarından
            # geçer; geçemeyen satır sonuçtan düşer, aramayı bozmaz.
            if rules.check(Path(path)) is None:
                continue
            text = ""
            with suppress(*_FILE_ERRORS):  # dosya o arada değişmiş/silinmiş olabilir
                text = extract_text(Path(path), rules.cap)
            hits.append(Hit(
                path=sanitize_name(path, 400),
                modified=datetime.fromtimestamp(mtime_ns / 1e9).strftime("%Y-%m-%d"),
                snippet=snippet_around(text, query),
            ))
        return hits

    def clear(self) -> str:
        """İndeks dosyasını siler (bloklar: GUI iş parçacığından çağırmayın).

        Önce iptal istenir ve dizinleyici iş parçacığının GERÇEKTEN bitmesi beklenir; bitmediyse
        dosyaya dokunulmaz ve kullanıcıya söylenir. İptal bayrağı açık kalır: dizinleyici
        kullanıcı yeniden dizinleme isteyene (`start`) dek indeksi yeniden oluşturmaz.
        """
        # Başlatma kilidi: `start()` ile sıraya girer; iptal+join ile silme arasına yeni koşu
        # girip iptal bayrağını kaldıramaz ve silinen veritabanını yeniden yaratamaz.
        with self._start_lock:
            self._cancel.set()
            thread = self._thread
            if thread is not None:
                thread.join(JOIN_SECONDS)
                if thread.is_alive():
                    return "Dizinleme henüz durmadı, indeks silinmedi; birazdan tekrar dene."
            with self._lock:
                try:
                    self.path.unlink(missing_ok=True)
                except OSError:
                    return "İndeks silinemedi; Artemis'i kapatıp tekrar dene."
            self._note = ""
            return "İndeks silindi."

    def wait(self, timeout: float = 30.0) -> bool:
        """Testler için: dizinleme bitene dek bekler."""
        if self._thread is not None:
            self._thread.join(timeout)
        return not self.running
