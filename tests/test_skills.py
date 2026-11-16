"""Kullanıcı becerileri (ADR 0060): yükleme sınırları, güven sınırı, kapıdan geçen çağrılar."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest

from artemis.brain.fake import ScriptedProvider
from artemis.brain.models import ToolCall
from artemis.channels.runtime import create_runtime
from artemis.config.settings import Settings, SkillsConfig
from artemis.safety.audit import AuditLog
from artemis.safety.gate import SafetyGate, TurnState
from artemis.skills import (
    MAX_BODY,
    PROMPT_CHARS,
    _parse,
    explicit_hint,
    load_skills,
    location_problem,
    prompt_section,
    skills_dir,
    templates,
)
from artemis.tools.base import Risk, ToolContext
from artemis.tools.plugins.skills import SkillRead
from artemis.tools.registry import ToolRegistry, discover_tools


def make_settings(tmp_path: Path, enabled: bool = True, **extra: Any) -> Settings:
    return Settings(
        workspace=tmp_path / "ws", state_dir=tmp_path / "state",
        audit_path=tmp_path / "a.jsonl", persist_memory=False,
        skills=SkillsConfig(enabled=enabled), **extra,
    )


def write(settings: Settings, name: str, text: str | bytes) -> Path:
    folder = skills_dir(settings)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding="utf-8", newline="")
    return path


def skill_text(
    name: str = "rapor", desc: str = "Haftalık rapor.", body: str = "Notları oku."
) -> str:
    return f"---\nname: {name}\ndescription: {desc}\n---\n{body}\n"


def test_valid_skill_is_listed_in_prompt_without_body(tmp_path: Path) -> None:
    s = make_settings(tmp_path)
    write(s, "rapor.md", skill_text(body="GIZLI-GOVDE"))
    catalog = load_skills(s)
    assert [k.name for k in catalog.skills] == ["rapor"] and not catalog.invalid
    section = prompt_section(catalog)
    assert "- rapor: Haftalık rapor." in section and "skill_read" in section
    assert "GIZLI-GOVDE" not in section


def test_prompt_section_is_capped(tmp_path: Path) -> None:
    s = make_settings(tmp_path)
    for i in range(40):
        write(s, f"b{i:02d}.md", skill_text(f"b{i:02d}", "x" * 190))
    catalog = load_skills(s)
    assert len(catalog.skills) == 40
    assert len(prompt_section(catalog)) <= PROMPT_CHARS + 10


def test_invalid_files_are_skipped_with_reason(tmp_path: Path) -> None:
    s = make_settings(tmp_path)
    write(s, "iyi.md", skill_text("iyi"))
    write(s, "bom.md", b"\xef\xbb\xbf" + skill_text("bom").encode())  # Not Defteri BOM'u kabul
    write(s, "crlf.md", skill_text("crlf").replace("\n", "\r\n"))
    write(s, "utf16.md", skill_text("utf16").encode("utf-16"))
    write(s, "ansi.md", skill_text("ansi", body="ş").encode("cp1254"))
    write(s, "baslik.md", "başlıksız metin")
    write(s, "buyuk.md", skill_text("buyuk", body="x" * (MAX_BODY + 1)))
    write(s, "uzun.md", skill_text("uzun", desc="x" * 201))
    write(s, "ad-farkli.md", skill_text("baska"))
    write(s, "Buyuk-Harf.md", skill_text("Buyuk-Harf"))
    write(s, "bos.md", "---\nname: bos\ndescription: d\n---\n")
    write(s, "yaml.md", "---\nname: [\n---\ngovde")
    write(s, "not.txt", "yok sayılır")
    catalog = load_skills(s)
    assert sorted(k.name for k in catalog.skills) == ["bom", "crlf", "iyi"]
    reasons = dict(catalog.invalid)
    assert set(reasons) == {
        "utf16.md", "ansi.md", "baslik.md", "buyuk.md", "uzun.md", "ad-farkli.md",
        "Buyuk-Harf.md", "bos.md", "yaml.md",
    }
    assert "UTF-8" in reasons["utf16.md"] and "UTF-8" in reasons["ansi.md"]
    assert "3500" in reasons["buyuk.md"]


def test_forty_first_skill_is_rejected(tmp_path: Path) -> None:
    s = make_settings(tmp_path)
    for i in range(41):
        write(s, f"b{i:02d}.md", skill_text(f"b{i:02d}"))
    catalog = load_skills(s)
    assert len(catalog.skills) == 40 and [n for n, _ in catalog.invalid] == ["b40.md"]


def test_symlinked_skill_is_ignored(tmp_path: Path) -> None:
    s = make_settings(tmp_path)
    target = tmp_path / "disarida.md"
    target.write_text(skill_text("link"), encoding="utf-8")
    folder = skills_dir(s)
    folder.mkdir(parents=True)
    try:
        os.symlink(target, folder / "link.md")
    except (OSError, NotImplementedError):
        pytest.skip("symlink oluşturulamadı")
    catalog = load_skills(s)
    assert catalog.skills == [] and "bağlantı" in dict(catalog.invalid)["link.md"]


def test_folder_inside_workspace_is_refused(tmp_path: Path) -> None:
    s = Settings(
        workspace=tmp_path / "ws", state_dir=tmp_path / "ws" / "state",
        audit_path=tmp_path / "a.jsonl", persist_memory=False,
        skills=SkillsConfig(enabled=True, folder=tmp_path / "ws" / "beceriler"),
    )
    write(s, "rapor.md", skill_text())
    catalog = load_skills(s)
    assert catalog.skills == [] and catalog.refused and "çalışma alanı" in catalog.refused
    assert "config" not in catalog.refused and "skills.folder" not in catalog.refused
    assert prompt_section(catalog) == ""
    result = asyncio.run(
        SkillRead().execute(SkillRead.args_model(name="rapor"), ToolContext(
            workspace=s.workspace, settings=s, audit=AuditLog(s.audit_path),
        ))
    )
    assert not result.success and "çalışma alanı" in result.message


def test_skill_read_is_read_only_and_does_not_taint(tmp_path: Path) -> None:
    s = make_settings(tmp_path)
    write(s, "rapor.md", skill_text(body="Şunu yap."))
    tool = SkillRead()
    assert tool.risk is Risk.READ_ONLY and tool.external is False
    context = ToolContext(workspace=s.workspace, settings=s, audit=AuditLog(s.audit_path))
    result = asyncio.run(tool.execute(tool.args_model(name="rapor"), context))
    assert result.success and result.message == "Şunu yap."
    assert not result.external and not result.private
    missing = asyncio.run(tool.execute(tool.args_model(name="yok"), context))
    assert not missing.success


def test_no_tool_can_write_into_skills_folder(tmp_path: Path) -> None:
    """Dosya araçları yalnız workspace köküne yazar; beceri klasörü onun dışındadır."""
    from artemis.safety.paths import safe_join

    s = make_settings(tmp_path)
    s.workspace.mkdir(parents=True)
    write(s, "rapor.md", skill_text())
    folder = skills_dir(s)
    for attempt in ("../state/beceriler/yeni.md", str(folder / "yeni.md"), "..\\state\\beceriler"):
        with pytest.raises(ValueError):
            safe_join(s.workspace, attempt, allow_symlinks=False)
    # Beceri adı taşıyan araç yalnız skill_read; oluşturan/değiştiren/silen araç yok.
    names = [d.name for d in discover_tools().definitions() if "skill" in d.name]
    assert names == ["skill_read"]


def test_disabled_flag_hides_tool_and_prompt(tmp_path: Path) -> None:
    s = make_settings(tmp_path, enabled=False)
    write(s, "rapor.md", skill_text())
    assert load_skills(s).skills == []
    registry = ToolRegistry()
    registry.register(SkillRead)
    assert registry.without_prefix("skill_").definitions() == []
    context = ToolContext(workspace=s.workspace, settings=s, audit=AuditLog(s.audit_path))
    assert SkillRead().refuse(SkillRead.args_model(name="rapor"), context) is not None
    rt = create_runtime(
        tmp_path / "c.yaml", lambda *_: True, settings=s,
        provider=ScriptedProvider([]), audit=AuditLog(s.audit_path),
    )
    assert "skill_read" not in rt.registry
    assert not skills_dir(s).exists() or [p.name for p in skills_dir(s).iterdir()] == ["rapor.md"]


def test_enabled_creates_folder_with_one_example(tmp_path: Path) -> None:
    s = make_settings(tmp_path)
    rt = create_runtime(
        tmp_path / "c.yaml", lambda *_: True, settings=s,
        provider=ScriptedProvider([]), audit=AuditLog(s.audit_path),
    )
    assert "skill_read" in rt.registry
    names = sorted(name for name, _ in templates())
    assert len(names) == 8 and "ornek-gunluk-ozet" in names
    assert sorted(p.stem for p in skills_dir(s).iterdir()) == names
    assert sorted(k.name for k in load_skills(s).skills) == names


def test_all_templates_parse(tmp_path: Path) -> None:
    for name, text in templates():
        path = tmp_path / f"{name}.md"
        path.write_text(text, encoding="utf-8", newline="")
        assert _parse(path).name == name


def test_explicit_invocation_hint(tmp_path: Path) -> None:
    s = make_settings(tmp_path)
    write(s, "rapor.md", skill_text())
    catalog = load_skills(s)
    assert "skill_read" in explicit_hint(catalog, "/rapor bu hafta")
    assert explicit_hint(catalog, "/yok") == "" and explicit_hint(catalog, "rapor /rapor") == ""


@pytest.mark.parametrize("unattended", [False, True])
def test_skill_telling_to_delete_still_needs_approval(tmp_path: Path, unattended: bool) -> None:
    """Beceri izin vermez: silme çağrısı normal onaydan geçer, gözetimsizde onaysız reddedilir."""
    s = make_settings(tmp_path)
    write(s, "temizle.md", skill_text("temizle", body="notlar.txt dosyasını sil."))
    (s.workspace).mkdir(parents=True)
    (s.workspace / "notlar.txt").write_text("x", encoding="utf-8")
    asked: list[str] = []

    def approve(name: str, args: dict[str, Any]) -> bool:
        asked.append(name)
        return False

    registry = discover_tools()
    gate = SafetyGate(registry)
    context = ToolContext(workspace=s.workspace, settings=s, audit=AuditLog(s.audit_path))

    async def scenario() -> None:
        state = TurnState(unattended=unattended)
        read = await gate.dispatch(
            ToolCall(id="1", name="skill_read", arguments={"name": "temizle"}),
            context, state, approve,
        )
        assert read.success and not state.tainted
        delete = await gate.dispatch(
            ToolCall(id="2", name="delete_file", arguments={"path": "notlar.txt"}),
            context, state, approve,
        )
        assert not delete.success

    asyncio.run(scenario())
    assert (s.workspace / "notlar.txt").exists()
    assert asked == ([] if unattended else ["delete_file"])


def test_agent_turn_puts_skill_list_in_system_prompt_and_gates_delete(tmp_path: Path) -> None:
    """Sahte sağlayıcıyla uçtan uca: bölüm istemde, /ad ipucu kullanıcı mesajında, silme onaylı."""
    from artemis.brain.models import Completion

    s = make_settings(tmp_path)
    write(s, "temizle.md", skill_text("temizle", "Dosya temizliği.", "notlar.txt dosyasını sil."))
    s.workspace.mkdir(parents=True)
    (s.workspace / "notlar.txt").write_text("x", encoding="utf-8")
    provider = ScriptedProvider([
        Completion(content="", tool_calls=[
            ToolCall(id="1", name="skill_read", arguments={"name": "temizle"})]),
        Completion(content="", tool_calls=[
            ToolCall(id="2", name="delete_file", arguments={"path": "notlar.txt"})]),
        Completion(content="Silmedim."),
    ])
    asked: list[str] = []
    rt = create_runtime(
        tmp_path / "c.yaml", lambda name, args: asked.append(name) or False,
        settings=s, provider=provider, audit=AuditLog(s.audit_path),
    )
    asyncio.run(rt.runner.run("/temizle"))
    system = provider.requests[0][0].content
    assert "- temizle: Dosya temizliği." in system and "notlar.txt dosyasını" not in system
    assert "skill_read çağır" in provider.requests[0][-1].content
    assert asked == ["delete_file"] and (s.workspace / "notlar.txt").exists()


def test_folder_inside_vault_or_project_is_refused_and_override_works(tmp_path: Path) -> None:
    """state_dir vault'un ya da bir proje deposunun içindeyse vault_append/kod ajanı beceri
    yazabilirdi; `skills.folder` ile dışarı alınınca yüklenir."""
    from artemis.config.settings import CodeAgentConfig, MemoryConfig

    vault = tmp_path / "vault"
    repo = tmp_path / "repo"
    for memory, agent, state in (
        (MemoryConfig(vault_path=vault), CodeAgentConfig(), vault / ".artemis"),
        (MemoryConfig(), CodeAgentConfig(repos_dir=repo), repo / "x" / ".artemis"),
    ):
        base = dict(
            workspace=tmp_path / "ws", state_dir=state, audit_path=tmp_path / "a.jsonl",
            persist_memory=False, memory=memory, code_agent=agent,
        )
        inside = Settings(**base, skills=SkillsConfig(enabled=True, folder=state / "beceriler"))
        write(inside, "rapor.md", skill_text())
        assert load_skills(inside).skills == [] and load_skills(inside).refused
        outside = Settings(
            **base, skills=SkillsConfig(enabled=True, folder=tmp_path / "beceriler")
        )
        write(outside, "rapor.md", skill_text())
        assert [s.name for s in load_skills(outside).skills] == ["rapor"]


def _default_settings(tmp_path: Path, state: Path, **extra: Any) -> Settings:
    return Settings(
        workspace=tmp_path / "ws", state_dir=state, audit_path=tmp_path / "a.jsonl",
        persist_memory=False, skills=SkillsConfig(enabled=True), **extra,
    )


def test_default_folder_resolution_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from artemis.config.settings import MemoryConfig

    user = tmp_path / "appdata"
    monkeypatch.setenv("APPDATA", str(user))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(user))
    user_dir = user / ("Artemis" if os.name == "nt" else "artemis") / "beceriler"
    # 1) mevcut güvenli eski klasör
    s = _default_settings(tmp_path, tmp_path / "state")
    assert skills_dir(s) == user_dir  # eski klasör yok: kullanıcı veri klasörü
    (tmp_path / "state" / "beceriler").mkdir(parents=True)
    assert skills_dir(s) == tmp_path / "state" / "beceriler"
    # 2) eski klasör var ama vault içinde: kullanıcı veri klasörü
    vault = tmp_path / "vault"
    unsafe = _default_settings(tmp_path, vault / "state", memory=MemoryConfig(vault_path=vault))
    (vault / "state" / "beceriler").mkdir(parents=True)
    assert skills_dir(unsafe) == user_dir
    assert location_problem(unsafe) is None
    # 3) açık klasör hepsinden önce gelir
    explicit = Settings(
        workspace=tmp_path / "ws", state_dir=tmp_path / "state", persist_memory=False,
        audit_path=tmp_path / "a.jsonl", skills=SkillsConfig(enabled=True, folder=tmp_path / "x"),
    )
    assert skills_dir(explicit) == tmp_path / "x"


def test_user_dir_inside_workspace_is_refused_with_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "ws"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "ws"))
    s = _default_settings(tmp_path, tmp_path / "state")
    problem = location_problem(s)
    assert problem and "çalışma alanı" in problem and load_skills(s).refused == problem


def test_conftest_isolates_user_dir(tmp_path: Path) -> None:
    s = _default_settings(tmp_path, tmp_path / "state")
    assert skills_dir(s).is_relative_to(Path(os.environ["APPDATA"]))
    assert skills_dir(s).is_relative_to(Path(os.environ["XDG_CONFIG_HOME"]))
