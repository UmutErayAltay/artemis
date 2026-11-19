"""ADR 0065 yerel dosya araması kabul doğrulaması: gerçek dosya sistemi ve SQLite."""

from __future__ import annotations

import io
import shutil
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import fields
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from artemis.brain.fake import ScriptedProvider  # noqa: E402
from artemis.channels.runtime import create_runtime  # noqa: E402
from artemis.config.settings import FileSearchConfig, Settings  # noqa: E402
from artemis.safety.audit import AuditLog  # noqa: E402
from artemis.tools.file_index import SNIPPET_CHARS, FileIndex, is_secret_name  # noqa: E402


def _make_settings(
    base: Path,
    *,
    enabled: bool = True,
    roots: list[Path] | None = None,
) -> tuple[Settings, AuditLog, Path]:
    docs = base / "Documents"
    docs.mkdir(parents=True, exist_ok=True)
    state = base / "state"
    state.mkdir(parents=True, exist_ok=True)
    effective_roots = [str(docs)] if roots is None else [str(r) for r in roots]
    settings = Settings(
        workspace=base,
        state_dir=state,
        audit_path=base / "audit.jsonl",
        persist_memory=False,
        file_search=FileSearchConfig(enabled=enabled, roots=effective_roots),
    )
    return settings, AuditLog(settings.audit_path), docs


def _write_text(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _create_corpus(
    docs: Path, count: int, control_name: str, control_phrase: str
) -> None:
    filler = ("ornek arsiv metni veri satiri kayit numarasi belge inceleme " * 142)[:8192]
    buckets = 10
    for idx in range(count):
        folder = docs / f"alt_{idx % buckets}"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"belge_{idx:04d}.txt").write_text(filler, encoding="utf-8")
    _write_text(docs / "alt_0" / control_name, f"Baslik {control_phrase} sonu.")


def _wait_until_active(index: FileIndex, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if index.path.exists():
            return index.running
        if not index.running:
            return False
        time.sleep(0.01)
    return index.running


def _remove_tree(root: Path) -> None:
    if not root.exists():
        return
    last_error: OSError | None = None
    for attempt in range(5):
        try:
            shutil.rmtree(root)
            return
        except OSError as error:
            last_error = error
            time.sleep(0.1 * (attempt + 1))
    print(f"Temizleme uyarisi ({root}): {last_error}", file=sys.stderr)


def check_a(base: Path) -> tuple[bool, str]:
    settings, audit, docs = _make_settings(base)
    phrase = "kabul_env_gizli_91827"
    _write_text(docs / ".env", f"API_KEY={phrase}\n")
    index = FileIndex(settings, audit)
    try:
        started = index.start()
        done = index.wait(30.0)
        hits = index.search(phrase, 10)
        secret_ok = is_secret_name(".env")
        files = index.status()["files"]
        ok = started and done and secret_ok and len(hits) == 0 and files == 0
        return ok, f"is_secret_name={secret_ok}, hits={len(hits)}, indexed_files={files}"
    finally:
        index.shutdown()


def check_b(base: Path) -> tuple[bool, str]:
    settings, audit, docs = _make_settings(base)
    phrase = "kabul_ekle_sil_77412"
    target = _write_text(docs / "gecici.txt", f"Icerik {phrase} metni.")
    index = FileIndex(settings, audit)
    try:
        s1 = index.start()
        w1 = index.wait(30.0)
        hits1 = [Path(h.path).name for h in index.search(phrase, 10)]
        target.unlink()
        s2 = index.start()
        w2 = index.wait(30.0)
        hits2 = [Path(h.path).name for h in index.search(phrase, 10)]
        ok = s1 and w1 and s2 and w2 and hits1 == ["gecici.txt"] and hits2 == []
        return ok, f"after_add={hits1}, after_delete={hits2}"
    finally:
        index.shutdown()


def check_c(base: Path) -> tuple[bool, str]:
    settings, audit, docs = _make_settings(base)
    phrase = "kabul_dizini_sil_55129"
    _write_text(docs / "silinecek.txt", f"Ornek {phrase} kaydi.")
    index = FileIndex(settings, audit)
    try:
        s1 = index.start()
        w1 = index.wait(30.0)
        hits_before = len(index.search(phrase, 10))
        clear_msg = index.clear()
        exists_after = index.path.exists()
        files_after = index.status()["files"]
        hits_after = len(index.search(phrase, 10))
        exists_after_search = index.path.exists()
        ok = (
            s1
            and w1
            and hits_before == 1
            and clear_msg == "İndeks silindi."
            and not exists_after
            and files_after == 0
            and hits_after == 0
            and not exists_after_search
        )
        detail = (
            f"before={hits_before}, clear={clear_msg!r}, exists={exists_after}, "
            f"files={files_after}, after={hits_after}"
        )
        return ok, detail
    finally:
        index.shutdown()


def check_d(base: Path) -> tuple[bool, str]:
    docs1 = base / "docs1"
    docs2 = base / "docs2"
    docs1.mkdir(parents=True, exist_ok=True)
    docs2.mkdir(parents=True, exist_ok=True)
    settings1, audit, _ = _make_settings(base, roots=[docs1])
    phrase = "kabul_kok_kaldir_33218"
    _write_text(docs1 / "eski_kok.txt", f"Kok icerigi {phrase}.")
    index = FileIndex.shared(settings1, audit)
    try:
        s1 = index.start()
        w1 = index.wait(30.0)
        hits_before = [Path(h.path).name for h in index.search(phrase, 10)]
        settings2 = settings1.model_copy(
            update={"file_search": FileSearchConfig(enabled=True, roots=[str(docs2)])}
        )
        index2 = FileIndex.shared(settings2, audit)
        s2 = index2.start()
        w2 = index2.wait(30.0)
        hits_after = [Path(h.path).name for h in index2.search(phrase, 10)]
        ok = (
            s1
            and w1
            and s2
            and w2
            and index2 is index
            and hits_before == ["eski_kok.txt"]
            and hits_after == []
        )
        return ok, f"before={hits_before}, after={hits_after}, shared_same={index2 is index}"
    finally:
        index.shutdown()


def check_e(base: Path) -> tuple[bool, str]:
    off_dir = base / "off"
    on_dir = base / "on"
    off_dir.mkdir(parents=True, exist_ok=True)
    on_dir.mkdir(parents=True, exist_ok=True)
    settings_off, _, _ = _make_settings(off_dir, enabled=False, roots=[])
    settings_on, _, _ = _make_settings(on_dir, enabled=True)

    rt_off = create_runtime(
        off_dir / "config.yaml",
        lambda *_: False,
        provider=ScriptedProvider([]),
        settings=settings_off,
    )
    try:
        in_off = "file_search" in rt_off.registry
    finally:
        rt_off.shutdown()

    rt_on = create_runtime(
        on_dir / "config.yaml",
        lambda *_: False,
        provider=ScriptedProvider([]),
        settings=settings_on,
    )
    try:
        in_on = "file_search" in rt_on.registry
    finally:
        rt_on.shutdown()

    return (not in_off) and in_on, f"disabled_in_registry={in_off}, enabled_in_registry={in_on}"


def check_f(base: Path) -> tuple[bool, str]:
    settings, audit, docs = _make_settings(base)
    phrase = "kabul_gizli_klasor_88124"
    _write_text(docs / ".ssh" / "ozel_not.txt", f"Gizli {phrase}")
    _write_text(docs / "Cookies", f"Cerez {phrase}")
    _write_text(docs / "normal_belge.txt", f"Acik {phrase}")
    index = FileIndex(settings, audit)
    try:
        started = index.start()
        done = index.wait(30.0)
        hits = [Path(h.path).name for h in index.search(phrase, 10)]
        cookies_secret = is_secret_name("Cookies")
        indexed = index.status()["files"]
        ok = started and done and cookies_secret and hits == ["normal_belge.txt"] and indexed == 1
        return ok, f"cookies_secret={cookies_secret}, hits={hits}, indexed_files={indexed}"
    finally:
        index.shutdown()


def check_g(base: Path) -> tuple[bool, str]:
    settings, audit, docs = _make_settings(base)
    phrase = "kabul_g_kontrol_4491"
    _create_corpus(docs, 3000, "kontrol_g.txt", phrase)
    index = FileIndex(settings, audit)
    try:
        if not index.start():
            return False, "start() returned False"
        active = _wait_until_active(index)
        if not active or not index.running:
            index.wait(90.0)
            return False, "run finished before the action; corpus too small"
        t0 = time.monotonic()
        clear_msg = index.clear()
        clear_secs = time.monotonic() - t0
        if clear_secs > 15.0:
            index.wait(90.0)
            return False, f"clear took {clear_secs:.2f}s (>15s): {clear_msg}"
        if "silinmedi" in clear_msg or clear_msg != "İndeks silindi.":
            index.wait(15.0)
            return False, clear_msg
        waited_clear = index.wait(15.0)
        exists_after_clear = index.path.exists()
        hits_after_clear = len(index.search(phrase, 10))
        exists_after_search = index.path.exists()
        restarted = index.start()
        waited_rebuild = index.wait(90.0)
        exists_after_rebuild = index.path.exists()
        hits_after_rebuild = [Path(h.path).name for h in index.search(phrase, 10)]
        ok = (
            waited_clear
            and not exists_after_clear
            and hits_after_clear == 0
            and not exists_after_search
            and restarted
            and waited_rebuild
            and exists_after_rebuild
            and hits_after_rebuild == ["kontrol_g.txt"]
        )
        detail = (
            f"clear_s={clear_secs:.2f}, msg={clear_msg!r}, "
            f"exists_after_clear={exists_after_clear}, hits_clear={hits_after_clear}, "
            f"rebuilt_exists={exists_after_rebuild}, hits_rebuild={hits_after_rebuild}"
        )
        return ok, detail
    finally:
        index.shutdown()


def check_h(base: Path) -> tuple[bool, str]:
    settings, audit, docs = _make_settings(base)
    phrase = "kabul_h_kontrol_6623"
    _create_corpus(docs, 3000, "kontrol_h.txt", phrase)
    index = FileIndex(settings, audit)
    try:
        if not index.start():
            return False, "first start() returned False"
        active = _wait_until_active(index)
        if not active or not index.running:
            index.wait(90.0)
            return False, "run finished before the action; corpus too small"
        thread_before = index._thread
        if not index.running:
            index.wait(90.0)
            return False, "run finished before the action; corpus too small"
        second_started = index.start()
        thread_after = index._thread
        waited = index.wait(90.0)
        hits = [Path(h.path).name for h in index.search(phrase, 10)]
        files_count = index.status()["files"]
        same_thread = thread_before is not None and thread_after is thread_before
        ok = (
            (not second_started)
            and same_thread
            and waited
            and hits == ["kontrol_h.txt"]
            and files_count == 3001
        )
        detail = (
            f"second_start={second_started}, same_thread={same_thread}, "
            f"files={files_count}, hits={hits}"
        )
        return ok, detail
    finally:
        index.shutdown()


def check_i(base: Path) -> tuple[bool, str]:
    settings, audit, docs = _make_settings(base)
    _write_text(docs / "sube.txt", "ŞUBE GÖKÇE İSTANBUL ÖZET")
    _write_text(docs / "isparta_notu.txt", "ısparta")
    index = FileIndex(settings, audit)
    try:
        started = index.start()
        done = index.wait(30.0)
        expected_queries = [
            ("as_written", "ŞUBE GÖKÇE İSTANBUL ÖZET", "sube.txt"),
            ("uppercase", "ŞUBE GÖKÇE İSTANBUL ÖZET", "sube.txt"),
            ("deaccented", "SUBE GOKCE ISTANBUL OZET", "sube.txt"),
            ("lowercase", "şube gökçe istanbul özet", "sube.txt"),
            ("deaccented_lower", "sube gokce istanbul ozet", "sube.txt"),
            ("ISPARTA", "ISPARTA", "isparta_notu.txt"),
            ("ısparta", "ısparta", "isparta_notu.txt"),
        ]
        parts: list[str] = []
        failed_queries: list[str] = []
        for label, query, expected_file in expected_queries:
            names = [Path(h.path).name for h in index.search(query, 10)]
            matched = expected_file in names
            parts.append(f"{label}={'PASS' if matched else 'FAIL'}")
            if not matched:
                failed_queries.append(f"{label}({query})")
        isparta_hits = len(index.search("isparta", 10))
        parts.append(f"isparta_hits={isparta_hits}")
        ok = started and done and len(failed_queries) == 0 and isparta_hits == 1
        detail = ", ".join(parts)
        if failed_queries:
            detail += f"; failing={failed_queries}"
        return ok, detail
    finally:
        index.shutdown()


def check_j(base: Path) -> tuple[bool, str]:
    settings, audit, docs = _make_settings(base)
    (docs / "fatura_ekim.pdf").write_bytes(b"%PDF-1.4\x00\xff\xfe binary junk")
    index = FileIndex(settings, audit)
    try:
        started = index.start()
        done = index.wait(30.0)
        hits = [Path(h.path).name for h in index.search("fatura", 10)]
        ok = started and done and hits == ["fatura_ekim.pdf"]
        return ok, f"hits={hits}"
    finally:
        index.shutdown()


def check_k(base: Path) -> tuple[bool, str]:
    settings, audit, docs = _make_settings(base)
    phrase = "ortadaki_kabul_ifadesi_9042"
    half = ("kelime ornek satir metin parcasi yazi " * 85)[:2980]
    full_text = f"{half} {phrase} {half}"
    _write_text(docs / "uzun.txt", full_text)
    index = FileIndex(settings, audit)
    try:
        started = index.start()
        done = index.wait(30.0)
        hits = index.search(phrase, 10)
        if len(hits) != 1:
            return False, f"expected 1 hit, got {len(hits)}"
        hit = hits[0]
        hit_fields = tuple(f.name for f in fields(hit))
        snip_len = len(hit.snippet)
        phrase_ok = phrase in hit.snippet
        ok = (
            started
            and done
            and hit_fields == ("path", "modified", "snippet")
            and snip_len <= SNIPPET_CHARS
            and phrase_ok
            and snip_len < len(full_text) // 10
        )
        detail = (
            f"text_len={len(full_text)}, snippet_len={snip_len}, "
            f"fields={hit_fields}, phrase_in_snippet={phrase_ok}"
        )
        return ok, detail
    finally:
        index.shutdown()


def main() -> int:
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if isinstance(sys.stderr, io.TextIOWrapper):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    temp_root = Path(tempfile.mkdtemp(prefix="artemis_kabul_0065_"))
    any_fail = False
    checks: list[tuple[str, Callable[[Path], tuple[bool, str]]]] = [
        ("a", check_a),
        ("b", check_b),
        ("c", check_c),
        ("d", check_d),
        ("e", check_e),
        ("f", check_f),
        ("g", check_g),
        ("h", check_h),
        ("i", check_i),
        ("j", check_j),
        ("k", check_k),
    ]
    try:
        for item_id, fn in checks:
            item_dir = temp_root / item_id
            item_dir.mkdir(parents=True, exist_ok=True)
            try:
                ok, detail = fn(item_dir)
            except Exception as error:  # noqa: BLE001 - madde siniri
                ok = False
                detail = f"{type(error).__name__}: {error}"
            clean_detail = " ".join(str(detail).splitlines()).strip()
            status = "PASS" if ok else "FAIL"
            if not ok:
                any_fail = True
            print(f"{status} {item_id} {clean_detail}")
    finally:
        _remove_tree(temp_root)
    return 1 if any_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
