"""Yerel dosya araması (ADR 0065): dizinleme, güvenlik listeleri, sorgu güvenliği, gizlilik.

Gerçek makineye dokunmaz: her şey tmp_path altında, gerçek (küçük) dosyalarla.
"""

from __future__ import annotations

import os
import zipfile
from pathlib import Path
from typing import Any

import pytest

from artemis.brain.models import ToolCall
from artemis.config.settings import FileSearchConfig, Settings
from artemis.safety.audit import AuditLog
from artemis.safety.gate import SafetyGate, TurnState
from artemis.tools import file_index as fi
from artemis.tools.base import ToolContext
from artemis.tools.file_index import FileIndex, build_match, is_secret_name
from artemis.tools.plugins.file_search import FileSearch
from artemis.tools.registry import ToolRegistry


class Env:
    def __init__(self, tmp_path: Path, **config: Any) -> None:
        self.docs = tmp_path / "docs"
        self.docs.mkdir(parents=True, exist_ok=True)
        self.state = tmp_path / "state"
        values: dict[str, Any] = {"enabled": True, "roots": [str(self.docs)], **config}
        self.settings = Settings(
            workspace=tmp_path,
            state_dir=self.state,
            audit_path=tmp_path / "audit.jsonl",
            persist_memory=False,
            file_search=FileSearchConfig(**values),
        )
        self.audit = AuditLog(self.settings.audit_path)
        self.index = FileIndex(self.settings, self.audit)

    def run(self) -> None:
        assert self.index.start()
        assert self.index.wait(20)

    def find(self, query: str) -> list[str]:
        return [Path(hit.path).name for hit in self.index.search(query, 10)]

    def write(self, name: str, text: str = "merhaba", *, binary: bool = False) -> Path:
        path = self.docs / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if binary:
            path.write_bytes(text.encode())
        else:
            path.write_text(text, encoding="utf-8")
        return path


def docx(path: Path, text: str) -> None:
    body = f"<w:document><w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", body)


def test_finds_by_content_and_by_name(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.write("notlar.txt", "Bu ayki kira sözleşmesi imzalandı.")
    env.write("fatura_ekim.pdf", "pdf icerigi aranmaz zebra", binary=True)
    env.run()
    assert env.find("kira sözleşmesi") == ["notlar.txt"]
    assert env.find("fatura") == ["fatura_ekim.pdf"]  # PDF: yalnız ad
    assert env.find("zebra") == []
    hit = env.index.search("imzalandı", 5)[0]
    assert "imzalandı" in hit.snippet and len(hit.snippet) <= 200
    assert len(hit.modified) == 10
    assert env.index.status()["files"] == 2


def test_incremental_update_and_deletion(tmp_path: Path) -> None:
    env = Env(tmp_path)
    kept = env.write("a.txt", "elma")
    gone = env.write("b.txt", "armut")
    env.run()
    assert env.find("armut") == ["b.txt"]
    gone.unlink()
    kept.write_text("elma ve muz muz", encoding="utf-8")
    os.utime(kept, ns=(1, 1_700_000_000_000_000_000))  # mtime kesin değişsin
    env.write("c.txt", "kiraz")
    env.run()
    assert env.find("armut") == []
    assert env.find("muz") == ["a.txt"]
    assert env.find("kiraz") == ["c.txt"]
    assert env.index.status()["files"] == 2


def test_unchanged_files_are_not_reread(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = Env(tmp_path)
    env.write("a.txt", "elma")
    env.run()
    calls: list[Path] = []
    real = fi.extract_text
    monkeypatch.setattr(fi, "extract_text", lambda p, n: calls.append(p) or real(p, n))
    env.run()
    assert calls == []


def test_excluded_dirs_secrets_and_oversize_skipped(tmp_path: Path) -> None:
    env = Env(tmp_path, max_file_mb=1)
    env.write("node_modules/paket.txt", "hayalet")
    env.write(".git/config.txt", "hayalet")
    env.write("ok.txt", "hayalet")
    secrets = [
        ".env", ".env.local", "anahtar.pem", "a.key", "x.pfx", "kasa.kdbx", "id_rsa",
        "id_rsa.pub", "my_secret_notes.txt", "Token-list.txt", "PASSWORD.md", "credentials.json",
    ]
    for name in secrets:
        env.write(name, "hayalet")
    (env.docs / "buyuk.txt").write_bytes(b"hayalet " * 200_000)  # ~1.6 MB > 1 MB
    env.run()
    assert env.find("hayalet") == ["ok.txt"]
    assert env.index.status()["files"] == 1
    for name in secrets:
        assert is_secret_name(name), name
    assert not is_secret_name("fatura.txt")


def test_symlink_outside_root_skipped(tmp_path: Path) -> None:
    env = Env(tmp_path)
    outside = tmp_path / "disarida"
    outside.mkdir()
    (outside / "gizli_belge.txt").write_text("dis dunya", encoding="utf-8")
    try:
        (env.docs / "kisayol").symlink_to(outside, target_is_directory=True)
        (env.docs / "dosya_kisayol.txt").symlink_to(outside / "gizli_belge.txt")
    except (OSError, NotImplementedError):
        pytest.skip("symlink izni yok")
    env.write("ic.txt", "ic dunya")
    env.run()
    assert env.find("dunya") == ["ic.txt"]


@pytest.mark.skipif(os.name != "nt", reason="junction yalnız Windows'ta")
def test_junction_out_of_root_skipped(tmp_path: Path) -> None:
    import _winapi  # type: ignore[import-not-found,unused-ignore]

    env = Env(tmp_path)
    outside = tmp_path / "disarida"
    outside.mkdir()
    (outside / "gizli_belge.txt").write_text("dis dunya", encoding="utf-8")
    _winapi.CreateJunction(str(outside), str(env.docs / "baglanti"))  # yetki gerektirmez
    env.write("ic.txt", "ic dunya")
    env.run()
    assert env.find("dunya") == ["ic.txt"]


def test_docx_text_and_zip_bomb_guard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = Env(tmp_path)
    docx(env.docs / "sozlesme.docx", "Kiralama &amp; teslim şartları")
    (env.docs / "bozuk.docx").write_bytes(b"PK bozuk")
    env.run()
    assert env.find("teslim şartları") == ["sozlesme.docx"]
    assert "bozuk.docx" in env.find("bozuk")  # gövdesiz ama adı aranır, koşu durmadı
    monkeypatch.setattr(fi, "MAX_DOCX_XML_BYTES", 10)
    with pytest.raises(ValueError, match="çok büyük"):
        fi.extract_text(env.docs / "sozlesme.docx", 10**6)


def test_unreadable_file_does_not_stop_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = Env(tmp_path)
    env.write("a.txt", "birinci")
    env.write("b.txt", "ikinci")
    real = fi.extract_text

    def flaky(path: Path, limit: int) -> str:
        if path.name == "a.txt":
            raise PermissionError("erişim yok")
        return real(path, limit)

    monkeypatch.setattr(fi, "extract_text", flaky)
    env.run()
    assert env.find("ikinci") == ["b.txt"]
    assert env.index.status()["files"] == 2


@pytest.mark.parametrize("query", ['"', "AND OR NOT", "a*b:c", "(((", "kira NEAR/2 x", "'; DROP"])
def test_fts_syntax_characters_do_not_error(tmp_path: Path, query: str) -> None:
    env = Env(tmp_path)
    env.write("a.txt", "kira and or not")
    env.run()
    env.index.search(query, 5)  # hata vermemeli
    assert build_match("   ") is None
    assert env.index.search("***", 5) == []


def test_turkish_case_and_diacritics(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.write("a.txt", "İSTANBUL ŞUBESİ sözleşme ISPARTA")
    env.run()
    assert env.find("istanbul") == ["a.txt"]
    assert env.find("İSTANBUL") == ["a.txt"]
    assert env.find("şubesi") == ["a.txt"]
    assert env.find("SOZLESME") == ["a.txt"]
    assert env.find("ısparta") == ["a.txt"]
    assert env.find("Isparta") == ["a.txt"]
    assert env.find("isparta") == ["a.txt"]  # i/ı ayrımı yok
    assert env.find("sozles") == ["a.txt"]  # önek


def test_ascii_uppercase_query_finds_turkish_uppercase_doc(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.write("sube.txt", "ŞUBE GÖKÇE İSTANBUL")
    env.run()
    assert env.find("SUBE GOKCE ISTANBUL") == ["sube.txt"]
    assert env.find("sube gokce istanbul") == ["sube.txt"]


def test_old_index_version_rebuilt_on_open(tmp_path: Path) -> None:
    import sqlite3
    from contextlib import closing

    env = Env(tmp_path)
    doc = env.write("belge.txt", "İSTANBUL ISPARTA")
    stat_info = doc.stat()
    env.state.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(env.index.path)) as db, db:
        for stmt in fi.SCHEMA:
            db.execute(stmt)
        # Eski sürüm (user_version=1) ve eski turkish_fold çıktısı:
        # "İSTANBUL ISPARTA" -> "istanbul ısparta" (boyut/mtime disktekiyle aynı).
        cur = db.execute(
            "INSERT INTO files (path, size, mtime_ns) VALUES (?, ?, ?)",
            (str(doc), stat_info.st_size, stat_info.st_mtime_ns),
        )
        db.execute(
            "INSERT INTO files_fts (rowid, name, body) VALUES (?, ?, ?)",
            (cur.lastrowid, "belge.txt", "istanbul ısparta"),
        )
        db.execute("INSERT INTO meta VALUES ('last_run', '2026-01-01 00:00')")
        db.execute("PRAGMA user_version = 1")

    env.run()
    assert env.find("ISTANBUL") == ["belge.txt"]
    assert env.find("isparta") == ["belge.txt"]
    with closing(sqlite3.connect(env.index.path)) as db:
        version = db.execute("PRAGMA user_version").fetchone()[0]
    assert version == fi.INDEX_VERSION


def test_result_names_are_sanitised(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.write("rapor‮txt.txt", "icerik")
    env.run()
    hit = env.index.search("icerik", 5)[0]
    assert "‮" not in hit.path


def test_root_inside_state_dir_rejected_and_state_not_walked(tmp_path: Path) -> None:
    env = Env(tmp_path)
    inside = env.state / "hafiza"
    inside.mkdir(parents=True)
    bad = Env(tmp_path, roots=[str(inside), str(env.state)])
    roots, problems = fi.usable_roots(bad.settings)
    assert roots == [] and len(problems) == 2
    # Kök state_dir'i içeriyorsa state altı gezilmez.
    (env.state / "not.txt").write_text("hafiza gizli", encoding="utf-8")
    parent = Env(tmp_path, roots=[str(tmp_path)])
    parent.write("belge.txt", "acik")
    parent.run()
    assert parent.find("gizli") == []
    assert parent.find("acik") == ["belge.txt"]


def test_clear_removes_index_file(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.write("a.txt", "elma")
    env.run()
    assert env.index.path.exists()
    assert env.index.clear() == "İndeks silindi."
    assert not env.index.path.exists()
    assert env.index.status()["files"] == 0
    assert env.find("elma") == []


def test_cancelled_run_does_not_delete_rows(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.write("a.txt", "elma")
    env.run()
    env.index._cancel.set()  # iptal edilmiş koşu: görülmeyen dosya "silinmiş" sayılmamalı
    env.index._index()
    assert env.find("elma") == ["a.txt"]


def test_fts5_missing_reports_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fi, "fts5_available", lambda: False)
    env = Env(tmp_path)
    assert not env.index.available
    assert env.index.start() is False


def _gate(env: Env) -> tuple[SafetyGate, ToolContext]:
    registry = ToolRegistry()
    registry.register(FileSearch)
    context = ToolContext(
        env.docs.parent, env.settings, env.audit, services={"file_index": env.index}
    )
    return SafetyGate(registry), context


async def _call(env: Env, **arguments: Any):  # type: ignore[no-untyped-def]
    gate, context = _gate(env)
    asked: list[str] = []

    def approve(name: str, _args: dict[str, Any]) -> bool:
        asked.append(name)
        return False

    result = await gate.dispatch(
        ToolCall(id="c", name="file_search", arguments=arguments), context, TurnState(), approve
    )
    return result, asked


async def test_tool_result_is_external_file_kind_and_private(tmp_path: Path) -> None:
    from artemis.brain.agent_loop import AgentLoop

    env = Env(tmp_path)
    env.write("a.txt", "kira sözleşmesi")
    env.run()
    result, asked = await _call(env, query="kira")
    assert result.success and asked == []  # READ_ONLY: onay yok
    assert result.external and result.content_kind == "file"
    assert result.data["results"][0]["path"].endswith("a.txt")
    # Gizlilik: dosya içeriği izni yokken sonuç gizli sayılır (agent_loop kuralı).
    loop = AgentLoop.__new__(AgentLoop)
    loop.context = _gate(env)[1]
    assert loop._result_private(result) is True


async def test_tool_disabled_and_no_roots(tmp_path: Path) -> None:
    off = Env(tmp_path, enabled=False)
    result, asked = await _call(off, query="x")
    assert not result.success and "kapalı" in result.message and asked == []
    empty = Env(tmp_path / "e", roots=[])
    result, _ = await _call(empty, query="x")
    assert not result.success and "klasör yok" in result.message


async def test_runtime_hides_tool_when_disabled(tmp_path: Path) -> None:
    from artemis.brain.fake import ScriptedProvider
    from artemis.channels.runtime import create_runtime

    registry = ToolRegistry()
    registry.register(FileSearch)
    settings = Settings(
        workspace=tmp_path, state_dir=tmp_path / "s", audit_path=tmp_path / "a.jsonl",
        persist_memory=False,
    )
    runtime = create_runtime(
        tmp_path / "x.yaml", lambda *_: True, provider=ScriptedProvider([]),
        registry=registry, settings=settings,
    )
    assert "file_search" not in runtime.registry
    assert "file_index" not in runtime.context.services
    runtime.shutdown()


def test_roots_must_be_absolute() -> None:
    with pytest.raises(ValueError, match="mutlak"):
        FileSearchConfig(roots=["goreli/klasor"])


# --- ADR 0065 ek: yeniden doğrulama, gizli klasör/ad, tek paylaşılan örnek, silme ---------------


def _junction(link: Path, target: Path) -> None:
    import _winapi  # type: ignore[import-not-found,unused-ignore]

    _winapi.CreateJunction(str(target), str(link))  # yetki gerektirmez


@pytest.mark.skipif(os.name != "nt", reason="junction yalnız Windows'ta")
def test_parent_replaced_by_junction_after_indexing_is_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = Env(tmp_path)
    env.write("alt/a.txt", "elmalı turta")
    env.run()
    assert env.find("turta") == ["a.txt"]
    outside = tmp_path / "disarida"
    outside.mkdir()
    (outside / "a.txt").write_text("elmalı turta", encoding="utf-8")
    (env.docs / "alt" / "a.txt").unlink()
    (env.docs / "alt").rmdir()
    _junction(env.docs / "alt", outside)  # kayıtlı yol artık kök dışına çıkıyor
    reads: list[Path] = []
    real = fi.extract_text
    monkeypatch.setattr(fi, "extract_text", lambda p, n: reads.append(p) or real(p, n))
    assert env.index.search("turta", 5) == []
    assert reads == []  # kök dışından hiçbir şey okunmadı


def test_removed_root_returns_nothing(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.write("a.txt", "elma")
    env.run()
    assert env.find("elma") == ["a.txt"]
    other = tmp_path / "baska"
    other.mkdir()
    for roots in ([], [str(other)]):
        env.index.settings = env.settings.model_copy(
            update={"file_search": FileSearchConfig(enabled=True, roots=roots)}
        )
        assert env.index.search("elma", 5) == []


def test_secret_dirs_and_new_secret_names_not_indexed(tmp_path: Path) -> None:
    env = Env(tmp_path)
    for folder in (".ssh", ".aws", ".gnupg", ".azure", ".kube", ".docker", ".password-store",
                   "my_secrets", "TOKENS"):
        env.write(f"{folder}/not.txt", "hayalet")
    names = [".netrc", ".npmrc", ".pypirc", ".git-credentials", "a.ppk", "b.jks", "c.keystore",
             "Login Data", "Cookies", "key4.db", "logins.json", "LOGINS.JSON", "X.KEYSTORE"]
    for name in names:
        env.write(name, "hayalet")
        assert is_secret_name(name), name
    env.write("ok.txt", "hayalet")
    env.run()
    assert env.find("hayalet") == ["ok.txt"]
    assert env.index.status()["files"] == 1


def test_row_renamed_into_secret_name_is_not_returned(tmp_path: Path) -> None:
    import sqlite3
    from contextlib import closing

    env = Env(tmp_path)
    env.write("a.txt", "elma")
    env.run()
    secret = env.write(".env", "elma")
    with closing(sqlite3.connect(env.index.path)) as db, db:  # bayat/sahte satır
        db.execute("UPDATE files SET path=? WHERE path LIKE '%a.txt'", (str(secret),))
    assert env.find("elma") == []
    # Gizli adlı klasörün içine taşınmış yol da elenir.
    hidden = env.write(".ssh/n.txt", "elma")
    with closing(sqlite3.connect(env.index.path)) as db, db:
        db.execute("UPDATE files SET path=?", (str(hidden),))
    assert env.find("elma") == []


def test_search_limit_is_clamped(tmp_path: Path) -> None:
    env = Env(tmp_path)
    for number in range(15):
        env.write(f"d{number}.txt", "ortak sözcük")
    env.run()
    assert len(env.index.search("ortak", 1000)) == 10
    assert len(env.index.search("ortak", 0)) == 1


def test_snippet_survives_length_changing_fold() -> None:
    text = "Straße ve STRASSE caddesi, Kira sözleşmesi burada"
    assert "Kira" in fi.snippet_around(text, "kira")
    assert fi.snippet_around("Straße caddesi", "yok") == "Straße caddesi"
    assert fi.snippet_around("düz metin", "yok") == ""


def test_shared_index_is_one_instance_for_runtime_and_panel_path(tmp_path: Path) -> None:
    from artemis.brain.fake import ScriptedProvider
    from artemis.channels.runtime import create_runtime

    env = Env(tmp_path)
    again = env.settings.model_copy(
        update={"file_search": FileSearchConfig(enabled=True, roots=[])}
    )
    first = FileIndex.shared(env.settings, env.audit)
    assert FileIndex.shared(again, env.audit) is first
    assert first.settings is again  # ayar en yenisine güncellenir
    runtime = create_runtime(
        tmp_path / "x.yaml", lambda *_: True, provider=ScriptedProvider([]),
        registry=ToolRegistry(), settings=again,
    )
    assert runtime.context.services["file_index"] is first
    runtime.shutdown()


class _Blocker:
    """extract_text yerine geçer: dizinleyiciyi bir dosyada bekletir (iptal denemesi için)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import threading

        self.entered = threading.Event()
        self.release = threading.Event()
        monkeypatch.setattr(fi, "extract_text", self.read)

    def read(self, path: Path, limit: int) -> str:
        self.entered.set()
        self.release.wait(10)
        return ""


def test_second_start_while_running_is_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = Env(tmp_path)
    env.write("a.txt", "elma")
    block = _Blocker(monkeypatch)
    assert env.index.start()
    assert block.entered.wait(5)
    thread = env.index._thread
    assert env.index.start() is False
    assert env.index._thread is thread
    block.release.set()
    assert env.index.wait(10)


def test_clear_while_running_stops_run_and_db_not_recreated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    env = Env(tmp_path)
    for number in range(3):
        env.write(f"a{number}.txt", "elma")
    block = _Blocker(monkeypatch)
    assert env.index.start()
    assert block.entered.wait(5)
    result: list[str] = []
    worker = threading.Thread(target=lambda: result.append(env.index.clear()))
    worker.start()
    block.release.set()
    worker.join(10)
    assert result == ["İndeks silindi."]
    assert env.index.wait(10)
    assert not env.index.path.exists()  # dizinleyici silinen indeksi yeniden oluşturmadı
    assert env.index.status()["files"] == 0 and env.index.search("elma", 5) == []
    assert not env.index.path.exists()  # okuma da yeniden doğurmadı
    monkeypatch.undo()
    env.run()  # kullanıcı yeniden dizinleme isteyince çalışır
    assert env.find("elma")


def test_clear_does_not_unlink_while_thread_still_alive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = Env(tmp_path)
    env.write("a.txt", "elma")
    block = _Blocker(monkeypatch)
    monkeypatch.setattr(fi, "JOIN_SECONDS", 0.05)
    assert env.index.start()
    assert block.entered.wait(5)
    message = env.index.clear()
    assert "silinmedi" in message
    assert env.index.path.exists()
    block.release.set()
    assert env.index.wait(10)


def test_file_search_section_close_detaches_running_clear_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    import yaml

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from artemis.channels.desktop import file_search_section as mod
    from artemis.channels.desktop.file_search_section import FileSearchSection

    app = QApplication.instance() or QApplication([])
    docs = tmp_path / "docs"
    docs.mkdir()
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "workspace": str(tmp_path),
                "state_dir": str(tmp_path / "state"),
                "audit_path": str(tmp_path / "audit.jsonl"),
                "persist_memory": False,
                "file_search": {"enabled": True, "roots": [str(docs)]},
            }
        ),
        encoding="utf-8",
    )

    entered = threading.Event()
    release = threading.Event()
    monkeypatch.setattr(mod, "_SHUTDOWN_WAIT_MS", 50)

    section = FileSearchSection(cfg)
    assert section._index is not None

    def slow_clear() -> str:
        entered.set()
        release.wait(5)
        return "İndeks silindi."

    monkeypatch.setattr(section._index, "clear", slow_clear)
    section.clear_button.click()
    assert entered.wait(3)

    thread = section._clearing
    assert thread is not None and thread.isRunning() and thread.parent() is section

    section.close()
    assert thread.parent() is None
    assert thread in mod._DETACHED
    section.deleteLater()
    app.processEvents()

    release.set()
    assert thread.wait(3000)
    app.processEvents()
    assert thread not in mod._DETACHED
