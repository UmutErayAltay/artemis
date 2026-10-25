"""Söylenen uygulama adının çözümü ve öğretilmiş takma adlar (ADR 0044).

Gerçek uygulama açılmaz: masaüstü servisi sahte, takma ad dosyası tmp_path altındadır.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from artemis.brain.models import ToolCall
from artemis.config.settings import Settings
from artemis.safety.audit import AuditLog
from artemis.safety.gate import SafetyGate, TurnState
from artemis.tools.app_resolver import (
    AliasStore,
    AliasStoreError,
    alias_store,
    normalize,
    resolve_application,
    skeleton,
)
from artemis.tools.base import Risk, ToolContext, ToolResult
from artemis.tools.registry import discover_tools

APPS = {
    name: f"startapp:{name}|{name.split()[-1].lower()}.exe"
    for name in (
        "Brave", "Uninstall Brave", "Google Chrome", "Microsoft Edge", "Spotify", "WhatsApp",
        "Discord", "Visual Studio Code", "Claude", "Claude Code", "Notepad", "Steam",
        "Telegram", "Zoom", "Word", "WordPad", "Firefox", "Paint", "Calculator", "Excel",
    )
}


def _resolve(spoken: str, aliases: dict[str, str] | None = None) -> tuple[str, Any]:
    match = resolve_application(APPS, aliases or {}, spoken)
    return match.kind, match.name if match.name is not None else match.candidates


def test_normalize_and_skeleton_fold_turkish_and_english_spelling() -> None:
    assert normalize("  İNTERNET Explorer ") == "internetexplorer"
    assert normalize("Notepad++") == "notepad++"
    assert skeleton("Brave") == skeleton("breyv") == skeleton("brev") == "brv"
    assert skeleton("Chrome") == skeleton("kroum") == "krm"
    assert skeleton("WhatsApp") == skeleton("vatsap")
    assert skeleton("Audacity") == skeleton("odasiti") == "dst"


@pytest.mark.parametrize(
    ("spoken", "expected"),
    [
        ("Brave", "Brave"),
        ("breyv", "Brave"),
        ("brev", "Brave"),
        ("kroum", "Google Chrome"),
        ("krom", "Google Chrome"),
        ("spotifay", "Spotify"),
        ("vatsap", "WhatsApp"),
        ("diskord", "Discord"),
        ("telegıram", "Telegram"),
        ("zum", "Zoom"),
        ("vörd", "Word"),
        ("fayırfoks", "Firefox"),
        ("peynt", "Paint"),
        ("kalkülatör", "Calculator"),
        ("eksel", "Excel"),
        ("edge", "Microsoft Edge"),
    ],
)
def test_clear_spoken_variants_resolve_without_asking(spoken: str, expected: str) -> None:
    kind, name = _resolve(spoken)
    assert kind in ("exact", "fuzzy") and name == expected


@pytest.mark.parametrize(
    ("spoken", "candidates"),
    [
        ("kod", {"Claude Code", "Visual Studio Code"}),
        ("code", {"Claude Code", "Visual Studio Code"}),
        ("klod", {"Claude", "Claude Code"}),
    ],
)
def test_close_rivals_are_ambiguous(spoken: str, candidates: set[str]) -> None:
    kind, names = _resolve(spoken)
    assert kind == "ambiguous" and candidates <= set(names)


@pytest.mark.parametrize("spoken", ["arbitrary.exe", "vs kod", "ec", "!!!", "muhasebe"])
def test_unrelated_names_never_guess(spoken: str) -> None:
    assert _resolve(spoken) == ("none", ())


def test_unseen_catalog_generalizes_without_name_lists() -> None:
    # Testte hiç geçmeyen bir katalog: kural ad listesine değil okunuşa dayanır.
    apps = {name: name for name in ("Obsidian", "Thunderbird", "Audacity", "Blender", "GIMP")}
    expected = {
        "obsidyın": "Obsidian", "tandırbörd": "Thunderbird", "odasiti": "Audacity",
        "blendır": "Blender", "gimp": "GIMP",
    }
    for spoken, name in expected.items():
        match = resolve_application(apps, {}, spoken)
        assert match.name == name, spoken
    assert resolve_application(apps, {}, "fotoşop").kind == "none"


def test_uninstaller_is_never_a_fuzzy_target() -> None:
    apps = {"Uninstall Brave": "u.exe", "Notepad": "n.exe"}
    assert resolve_application(apps, {}, "breyv").kind == "none"
    assert resolve_application(apps, {}, "uninstall brave").name == "Uninstall Brave"


def test_alias_wins_over_fuzzy_and_cannot_widen_the_allow_list() -> None:
    assert _resolve("vs kod", {"vskod": "Visual Studio Code"}) == ("alias", "Visual Studio Code")
    # Hedefi izin listesinde olmayan takma ad yok sayılır.
    assert resolve_application(APPS, {"oyun": "Gizli Oyun"}, "oyun").kind == "none"
    # Kesin çözüm istenince yakın eşleşme yapılmaz.
    assert resolve_application(APPS, {}, "breyv", fuzzy=False).kind == "none"


def test_alias_store_round_trip_and_corruption(tmp_path: Path) -> None:
    store = alias_store(tmp_path / "state")
    assert store.load() == {}
    store.save({"breyv": "Brave"})
    assert store.load() == {"breyv": "Brave"}
    assert list((tmp_path / "state").iterdir()) == [store.path]  # geçici dosya kalmaz
    store.path.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(AliasStoreError):
        store.load()
    store.path.write_text("{bozuk", encoding="utf-8")
    with pytest.raises(AliasStoreError):
        store.load()
    with pytest.raises(AliasStoreError):
        AliasStore(store.path).save({str(index): "Brave" for index in range(201)})


class FakeDesktop:
    def __init__(self) -> None:
        self.opened: list[str] = []
        self.closed: list[str] = []

    def open_application(self, executable: str) -> None:
        self.opened.append(executable)

    def close_application(self, executable: str) -> bool:
        self.closed.append(executable)
        return True


def _context(tmp_path: Path, **overrides: Any) -> tuple[ToolContext, FakeDesktop]:
    workspace = tmp_path / "Masaüstü"
    workspace.mkdir(exist_ok=True)
    values: dict[str, Any] = {
        "workspace": workspace,
        "state_dir": tmp_path / "state",
        "audit_path": tmp_path / "audit.jsonl",
        "applications": APPS,
        **overrides,
    }
    settings = Settings(**values)
    audit = AuditLog(settings.audit_path)
    context = ToolContext(workspace=workspace, settings=settings, audit=audit)
    desktop = FakeDesktop()
    context.services["desktop"] = desktop
    return context, desktop


async def _dispatch(
    context: ToolContext,
    name: str,
    arguments: dict[str, Any],
    *,
    tainted: bool = False,
    approve: bool | None = None,
) -> tuple[ToolResult, list[dict[str, Any]]]:
    asked: list[dict[str, Any]] = []

    def approval(tool: str, args: dict[str, Any]) -> bool:
        asked.append({"tool": tool, **args})
        if approve is None:
            pytest.fail(f"{tool} onay istememeliydi")
        return approve

    gate = SafetyGate(discover_tools())
    result = await gate.dispatch(
        ToolCall(id=name, name=name, arguments=arguments),
        context,
        TurnState(tainted=tainted),
        approval,
    )
    return result, asked


def _audit(context: ToolContext) -> list[dict[str, Any]]:
    path = context.settings.audit_path
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


async def test_clean_turn_opens_the_closest_app_and_says_so(tmp_path: Path) -> None:
    context, desktop = _context(tmp_path)
    result, _ = await _dispatch(context, "open_application", {"name": "breyv"})
    assert desktop.opened == [APPS["Brave"]]
    assert result.status == "sent" and not result.success
    assert "Brave" in result.message and result.data["application"] == "Brave"
    assert any(
        record.get("event") == "app_resolution" and record.get("application") == "Brave"
        for record in _audit(context)
    )


async def test_ambiguous_name_opens_nothing_and_lists_candidates(tmp_path: Path) -> None:
    context, desktop = _context(tmp_path)
    result, _ = await _dispatch(context, "open_application", {"name": "kod"})
    assert desktop.opened == []
    assert not result.success and "sor" in result.message
    assert {"Claude Code", "Visual Studio Code"} <= set(result.data["candidates"])


async def test_tainted_turn_never_runs_a_fuzzy_match(tmp_path: Path) -> None:
    # Onay penceresi söylenen adı gösterdi; yakın eşleşmenin seçtiği uygulama açılmaz.
    context, desktop = _context(tmp_path)
    result, asked = await _dispatch(
        context, "open_application", {"name": "breyv"}, tainted=True, approve=True
    )
    assert asked == [{"tool": "open_application", "name": "breyv", "file_path": None}]
    assert desktop.opened == [] and result.data["candidates"] == ["Brave"]
    exact, _ = await _dispatch(
        context, "open_application", {"name": "Brave"}, tainted=True, approve=True
    )
    assert desktop.opened == [APPS["Brave"]] and exact.status == "sent"


async def test_close_never_uses_a_fuzzy_match(tmp_path: Path) -> None:
    context, desktop = _context(tmp_path)
    result, asked = await _dispatch(context, "close_application", {"name": "breyv"}, approve=True)
    assert desktop.closed == [] and result.data["candidates"] == ["Brave"]
    assert asked  # kapatma onaylıdır; onay verilse bile tahminle kapatılmaz


async def test_remembered_alias_opens_directly_even_in_tainted_turn(tmp_path: Path) -> None:
    context, desktop = _context(tmp_path, close_without_confirmation=["Brave"])
    saved, _ = await _dispatch(
        context, "app_alias_remember", {"spoken": "Brev!", "application": "brave"}
    )
    assert saved.success and saved.data["application"] == "Brave"
    assert alias_store(context.settings.state_dir).load() == {"brev": "Brave"}
    opened, _ = await _dispatch(
        context, "open_application", {"name": "brev"}, tainted=True, approve=True
    )
    assert desktop.opened == [APPS["Brave"]] and "en yakın" not in opened.message
    # Takma ad, hedefin onaysız kapatma tercihini taşır.
    closed, _ = await _dispatch(context, "close_application", {"name": "brev"})
    assert closed.success and desktop.closed == [APPS["Brave"]]
    forgot, _ = await _dispatch(context, "app_alias_forget", {"spoken": "BREV"})
    assert forgot.success and alias_store(context.settings.state_dir).load() == {}
    missing, _ = await _dispatch(context, "app_alias_forget", {"spoken": "brev"})
    assert not missing.success


async def test_alias_requires_an_allowed_target_and_cannot_shadow_real_names(
    tmp_path: Path,
) -> None:
    context, _ = _context(tmp_path)
    for arguments in (
        {"spoken": "breyv", "application": "Gizli Oyun"},
        {"spoken": "breyv", "application": "brav"},  # hedef yakın eşleşmeyle seçilmez
        {"spoken": "Microsoft Edge", "application": "Brave"},
        {"spoken": "brave", "application": "Brave"},
        {"spoken": "...", "application": "Brave"},
    ):
        result, _ = await _dispatch(context, "app_alias_remember", arguments)
        assert not result.success, arguments
    assert alias_store(context.settings.state_dir).load() == {}


async def test_tainted_alias_write_asks_with_full_arguments(tmp_path: Path) -> None:
    context, _ = _context(tmp_path)
    denied, asked = await _dispatch(
        context,
        "app_alias_remember",
        {"spoken": "breyv", "application": "Brave"},
        tainted=True,
        approve=False,
    )
    assert asked == [{"tool": "app_alias_remember", "spoken": "breyv", "application": "Brave"}]
    assert denied.status == "denied" and not alias_store(context.settings.state_dir).path.exists()


async def test_alias_to_uninstaller_still_requires_confirmation(tmp_path: Path) -> None:
    context, desktop = _context(tmp_path)
    saved, _ = await _dispatch(
        context, "app_alias_remember", {"spoken": "temizle", "application": "Uninstall Brave"}
    )
    assert saved.success
    tool = discover_tools().get("open_application")
    assert tool is not None
    arguments = tool.args_model.model_validate({"name": "temizle"})
    assert tool.risk_for(arguments, context) is Risk.CONFIRM_REQUIRED
    denied, asked = await _dispatch(
        context, "open_application", {"name": "temizle"}, approve=False
    )
    assert asked and denied.status == "denied" and desktop.opened == []


async def test_without_persistent_memory_aliases_are_neither_written_nor_read(
    tmp_path: Path,
) -> None:
    context, desktop = _context(tmp_path, persist_memory=False)
    alias_store(context.settings.state_dir).save({"vskod": "Visual Studio Code"})
    result, _ = await _dispatch(
        context, "app_alias_remember", {"spoken": "breyv", "application": "Brave"}
    )
    assert not result.success
    assert alias_store(context.settings.state_dir).load() == {"vskod": "Visual Studio Code"}
    opened, _ = await _dispatch(context, "open_application", {"name": "vs kod"})
    assert desktop.opened == [] and not opened.success


async def test_corrupt_alias_file_does_not_block_opening(tmp_path: Path) -> None:
    context, desktop = _context(tmp_path)
    path = alias_store(context.settings.state_dir).path
    path.parent.mkdir(parents=True)
    path.write_text("{bozuk", encoding="utf-8")
    result, _ = await _dispatch(context, "open_application", {"name": "Brave"})
    assert desktop.opened == [APPS["Brave"]] and result.status == "sent"
    assert any(record.get("event") == "app_alias_unavailable" for record in _audit(context))
    failed, _ = await _dispatch(
        context, "app_alias_remember", {"spoken": "breyv", "application": "Brave"}
    )
    assert not failed.success and path.read_text(encoding="utf-8") == "{bozuk"


async def test_alias_success_is_reported_only_after_reading_it_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Yazma sessizce etkisiz kalırsa (ör. senkron klasör geri aldı) başarı denmez.
    context, _ = _context(tmp_path)
    monkeypatch.setattr(AliasStore, "save", lambda self, aliases: None)
    result, _ = await _dispatch(
        context, "app_alias_remember", {"spoken": "breyv", "application": "Brave"}
    )
    assert not result.success and "doğrulanamadı" in result.message


def test_alias_hotwords_are_opt_in_bounded_and_only_user_taught(tmp_path: Path) -> None:
    from artemis.channels.voice.factory import alias_hotword_config
    from artemis.config.settings import VoiceConfig

    def settings(**voice: Any) -> Settings:
        return Settings(
            state_dir=tmp_path / "state", applications=APPS,
            voice=VoiceConfig(stt_hotwords=("Artemis",), **voice),
        )

    alias_store(tmp_path / "state").save(
        {"brev": "Brave", "brevv": "Brave", "kroum": "Google Chrome", "eski": "Silinmiş"}
    )
    # Kapalıyken (varsayılan) hiçbir şey eklenmez.
    assert alias_hotword_config(settings()).stt_hotwords == ("Artemis",)
    enabled = alias_hotword_config(settings(stt_alias_hotwords=True))
    # Yalnız öğretilmiş ve hâlâ izinli hedefler, tekrarsız; katalog eklenmez.
    assert enabled.stt_hotwords == ("Artemis", "Brave", "Google Chrome")
    off = settings(stt_alias_hotwords=True).model_copy(update={"persist_memory": False})
    assert alias_hotword_config(off).stt_hotwords == ("Artemis",)
    full = settings(stt_alias_hotwords=True).model_copy(update={"voice": VoiceConfig(
        stt_hotwords=tuple(f"w{index}" for index in range(100)), stt_alias_hotwords=True,
    )})
    assert len(alias_hotword_config(full).stt_hotwords) == 100
    alias_store(tmp_path / "state").path.write_text("{bozuk", encoding="utf-8")
    assert alias_hotword_config(settings(stt_alias_hotwords=True)).stt_hotwords == ("Artemis",)
