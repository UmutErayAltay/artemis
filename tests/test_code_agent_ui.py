"""Projeler ajan/klasör seçimi, onay penceresi ve Ayarlar "Kod ajanları" (ADR 0049 arayüzü)."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication, QFileDialog, QInputDialog, QScrollArea, QWidget

from artemis.channels.desktop import code_agent_config, projects_panel, themes
from artemis.channels.desktop.app import ApprovalDialog
from artemis.channels.desktop.bridge import Approval
from artemis.channels.desktop.code_agent_config import (
    AgentDialog,
    AgentFormError,
    command_status,
    parse_agent_form,
)
from artemis.channels.desktop.projects_panel import ProjectsPanel, spec_to_html
from artemis.channels.desktop.settings_panel import SettingsPanel
from artemis.config.settings import load_settings
from artemis.tools.base import ToolResult
from tests.test_settings_panel import config


@pytest.fixture
def app() -> QApplication:
    current = QApplication.instance()
    instance = current if isinstance(current, QApplication) else QApplication([])
    instance.setQuitOnLastWindowClosed(False)
    return instance


def _write(path: Path, **code_agent: Any) -> Path:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["code_agent"] = code_agent
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


def _repo(tmp_path: Path, name: str = "repo-a") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    return repo


def _new_shape(tmp_path: Path) -> Path:
    """Yeni biçim: bir proje sözlük (ajan + klasör), biri eski `alias: yol`; vault + klasör."""
    vault = tmp_path / "vault"
    vault.mkdir()
    notes = tmp_path / "notes"
    notes.mkdir()
    path = config(tmp_path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["memory"] = {"vault_path": str(vault)}
    raw["code_agent"] = {
        "projects": {
            "alfa": {"path": str(_repo(tmp_path)), "agent": "codex", "workdir": "ikinci-beyin"},
            "beta": str(_repo(tmp_path, "repo-b")),
        },
        "agents": {"codex": {"kind": "codex", "command": "codex"}},
        "workdirs": {"notlar": str(notes)},
    }
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


def _old_shape(tmp_path: Path) -> Path:
    return _write(config(tmp_path), projects={"alfa": str(_repo(tmp_path))})


def _status(panel: ProjectsPanel, projects: list[str], drafts: list[Any] | None = None) -> None:
    panel.accept_result(
        "code_agent_status",
        ToolResult(
            success=True, status="ok", message="ok",
            data={"projects": projects, "drafts": drafts or [], "jobs": []},
        ),
    )


def _items(combo: Any) -> list[str]:
    return [combo.itemData(i) for i in range(combo.count())]


def _panel(path: Path, projects: list[str], *, demo: bool = False) -> ProjectsPanel:
    panel = ProjectsPanel(demo=demo, config_path=path)
    panel.set_busy(False)
    _status(panel, projects)
    return panel


# --- Seçenekler ve varsayılanlar ---------------------------------------------------------------


def test_combos_from_old_shape_config(tmp_path: Path, app: QApplication) -> None:
    panel = _panel(_old_shape(tmp_path), ["alfa"])
    assert _items(panel.agent_combo) == ["claude"]
    assert _items(panel.workdir_combo) == ["proje"]
    assert panel.workdir_combo.itemText(0) == "Proje klasörü"
    assert panel.selected_choice() == ("claude", "proje")
    assert not panel.agent_combo.isEnabled()  # tek ajan: seçici kapalı, nedeni ipucunda
    assert "Yalnız Claude" in panel.agent_combo.toolTip()


def test_new_shape_combos_and_project_defaults(tmp_path: Path, app: QApplication) -> None:
    panel = _panel(_new_shape(tmp_path), ["alfa", "beta"])
    assert _items(panel.agent_combo) == ["claude", "codex"]
    assert _items(panel.workdir_combo) == ["proje", "ikinci-beyin", "notlar"]
    assert panel.selected_choice() == ("codex", "ikinci-beyin")  # alfa'nın kayıtlı varsayılanı
    panel.project.setCurrentText("beta")
    assert panel.selected_choice() == ("claude", "proje")  # eski biçim: genel varsayılan
    panel.project.setCurrentText("alfa")
    assert panel.selected_choice() == ("codex", "ikinci-beyin")


def test_poll_refresh_does_not_reset_per_job_override(tmp_path: Path, app: QApplication) -> None:
    panel = _panel(_new_shape(tmp_path), ["alfa", "beta"])
    panel.workdir_combo.setCurrentIndex(panel.workdir_combo.findData("notlar"))
    _status(panel, ["alfa", "beta"])  # 2 sn'lik yenileme
    assert panel.selected_choice()[1] == "notlar"


def test_hint_shows_resolved_path_or_unresolved(tmp_path: Path, app: QApplication) -> None:
    path = _new_shape(tmp_path)
    panel = _panel(path, ["alfa"])
    vault = str(load_settings(path).memory.vault_path)
    assert panel.workdir_hint.full_text == f"Başlangıç yolu: {vault}"
    assert panel.workdir_hint.toolTip() == vault
    panel.workdir_combo.setCurrentIndex(panel.workdir_combo.findData("proje"))
    assert panel.workdir_hint.full_text == f"Başlangıç yolu: {tmp_path / 'repo-a'}"
    empty = ProjectsPanel()  # yapılandırma yok, proje yok
    assert "(çözülmedi)" in empty.workdir_hint.full_text


# --- Hazırlık isteği yalnız takma ad taşır -----------------------------------------------------


def test_prepare_request_carries_aliases_only(tmp_path: Path, app: QApplication) -> None:
    panel = _panel(_new_shape(tmp_path), ["alfa"])
    panel.workdir_combo.setCurrentIndex(panel.workdir_combo.findData("notlar"))
    panel.task.setPlainText("t")
    panel.scope.setPlainText("s")
    panel.acceptance.setPlainText("a")
    seen: list[tuple[str, Any]] = []
    panel.tool_requested.connect(lambda name, args: seen.append((name, args)))
    panel.prepare_button.click()
    name, arguments = seen[0]
    assert name == "code_agent_prepare"
    assert (arguments.agent, arguments.workdir) == ("codex", "notlar")
    assert re.fullmatch(r"[a-z0-9_-]{1,32}", arguments.agent)  # yol değil, takma ad
    assert re.fullmatch(r"[a-z0-9_-]{1,32}", arguments.workdir)


# --- Uyarı çipi --------------------------------------------------------------------------------


def test_warning_chip_only_for_non_project_folder(tmp_path: Path, app: QApplication) -> None:
    panel = _panel(_new_shape(tmp_path), ["alfa", "beta"])
    panel.show()
    assert not panel.workdir_warning.isHidden()  # alfa: ikinci-beyin
    # Formda kısa uyarı; tam backend metni ön izleme ve onay penceresinde (ayrı testler).
    assert panel.workdir_warning.text() == projects_panel.WORKDIR_WARNING
    assert len(panel.workdir_warning.text()) < 90
    panel.project.setCurrentText("beta")
    assert panel.workdir_warning.isHidden()
    panel.workdir_combo.setCurrentIndex(panel.workdir_combo.findData("notlar"))
    assert not panel.workdir_warning.isHidden()
    panel.close()


# --- Taslak ve iş ayrıntısı --------------------------------------------------------------------


def test_preview_leads_with_agent_and_folder_block() -> None:
    spec = {
        "spec_id": "s", "project": "p", "task": "T", "scope": ["a"], "acceptance": ["b"],
        "agent": "codex", "workdir": "ikinci-beyin", "repo_path": "R:/repo",
        "workdir_path": "V:/vault",
    }
    html_text = spec_to_html("draft", spec)
    assert html_text.index("Ajan ve başlangıç klasörü") < html_text.index("<b>Proje</b>")
    assert "Ajan: codex" in html_text and "ikinci-beyin -&gt; V:/vault" in html_text
    job = {"job_id": "j", "status": "running", "spec": spec, "agent": "codex",
           "workdir": "proje", "workdir_path": "R:/repo"}
    assert "Başlangıç klasörü: proje -&gt; R:/repo" in spec_to_html("job", job)


# --- Onay penceresi ----------------------------------------------------------------------------


def _approval(workdir: str) -> Approval:
    from artemis.tools.project_spec import approval_text

    values = {"agent": "codex", "workdir": workdir, "repo_path": "R:/repo",
              "workdir_path": "R:/repo" if workdir == "proje" else "V:/vault"}
    spec = {"spec_id": "s", "project": "p", "task": "T", "scope": ["a"], "acceptance": ["b"],
            **values, "approval_text": approval_text(values)}
    return Approval("r", "t", "a", "code_agent_start", {"spec": spec}, "Onay gerekir.")


def test_approval_shows_approval_text_first_with_warning_box(app: QApplication) -> None:
    parent = QWidget()
    dialog = ApprovalDialog(_approval("ikinci-beyin"), lambda *_a: True, parent)
    dialog.show()
    assert dialog.approval_text_label is not None and dialog.workdir_warning_box is not None
    assert "Ajan: codex" in dialog.approval_text_label.text()
    assert "Bu klasördeki dosyaları değiştirebilir" in dialog.workdir_warning_box.text()
    assert dialog.workdir_warning_box.objectName() == "approvalNotice"
    # İlk düz dil bloğu: başlık ve ham addan sonraki ilk içerik, argüman özetinden önce.
    layout = dialog.findChild(QScrollArea).widget().layout()
    order = [layout.itemAt(i).widget() for i in range(layout.count()) if layout.itemAt(i).widget()]
    assert order.index(dialog.approval_text_label) < order.index(dialog.details_toggle)
    assert dialog.focusWidget() is dialog.no_button and dialog.no_button.isDefault()
    assert not dialog.yes_button.isDefault()
    dialog.reject()


def test_approval_project_folder_has_no_warning_box(app: QApplication) -> None:
    parent = QWidget()
    dialog = ApprovalDialog(_approval("proje"), lambda *_a: True, parent)
    assert dialog.approval_text_label is not None and dialog.workdir_warning_box is None
    dialog.reject()


# --- Varsayılan yaz / klasör ekle --------------------------------------------------------------


def test_make_default_writes_dict_shape_keeps_other_keys(tmp_path: Path, app: QApplication) -> None:
    path = _old_shape(tmp_path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["code_agent"]["agents"] = {"codex": {"kind": "codex", "command": "codex"}}
    raw["code_agent"]["max_minutes"] = 33
    raw["telegram"] = {"chat_id": 7}
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    panel = _panel(path, ["alfa"])
    assert not panel.default_button.isEnabled()  # seçim kayıtlı varsayılanla aynı
    saved: list[bool] = []
    panel.config_saved.connect(lambda: saved.append(True))
    panel.agent_combo.setCurrentIndex(panel.agent_combo.findData("codex"))
    assert panel.default_button.isEnabled()
    panel.default_button.click()
    after = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert after["code_agent"]["projects"]["alfa"] == {
        "path": str(tmp_path / "repo-a"), "agent": "codex",
    }
    assert after["code_agent"]["max_minutes"] == 33 and after["telegram"] == {"chat_id": 7}
    assert saved == [True]
    assert not panel.default_button.isEnabled()  # yeni varsayılan = seçim
    assert load_settings(path).code_agent.project_agent("alfa") == "codex"
    # Varsayılana dönünce sözlük yeniden eski `alias: yol` biçimine iner.
    panel.agent_combo.setCurrentIndex(panel.agent_combo.findData("claude"))
    panel.default_button.click()
    after = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert after["code_agent"]["projects"]["alfa"] == str(tmp_path / "repo-a")


def test_make_default_keeps_old_shape_when_no_difference_needed(tmp_path: Path) -> None:
    path = _old_shape(tmp_path)
    code_agent_config.save_project_default(
        path, "alfa", agent="claude", workdir="proje", default_agent="claude",
        project_path=None,
    )
    projects = yaml.safe_load(path.read_text(encoding="utf-8"))["code_agent"]["projects"]
    assert projects == {"alfa": str(tmp_path / "repo-a")}


def test_add_folder_validates_alias_and_writes_workdirs(
    tmp_path: Path, app: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _new_shape(tmp_path)
    panel = _panel(path, ["alfa"])
    chosen = tmp_path / "Yeni Klasör"
    chosen.mkdir()
    monkeypatch.setattr(
        QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: str(chosen))
    )
    asked: list[str] = []

    def answer(alias: str):  # type: ignore[no-untyped-def]
        def fake(*_a: Any, text: str = "", **_k: Any) -> tuple[str, bool]:
            asked.append(text)
            return alias, True

        return staticmethod(fake)

    # Arayüz doğrulaması yazmadan ÖNCE reddetmeli: yazma çağrılırsa (arka uç sonradan reddetse
    # bile) test kırılır; yoksa gevşetilmiş arayüz regex'i fark edilmezdi.
    writes: list[str] = []
    monkeypatch.setattr(projects_panel, "add_workdir", lambda *a, **k: writes.append(a[1]))
    for bad in ("proje", "notlar", "Büyük Harf", "", "a/b", r"a\b", "..", "a b", "a:b",
                r"C:\x", "ikinci-beyin"):
        monkeypatch.setattr(QInputDialog, "getText", answer(bad))
        panel.notice.setText("")
        panel.add_folder_button.click()
        assert writes == [], bad
        assert list(load_settings(path).code_agent.workdirs) == ["notlar"], bad
        assert panel.notice.text(), bad
    monkeypatch.undo()
    monkeypatch.setattr(
        QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: str(chosen))
    )
    assert asked[0] == "yeni-klas-r"  # klasör adından önerilen takma ad
    monkeypatch.setattr(QInputDialog, "getText", answer("yeni-klasor"))
    saved: list[bool] = []
    panel.config_saved.connect(lambda: saved.append(True))
    panel.add_folder_button.click()
    assert load_settings(path).code_agent.workdirs["yeni-klasor"] == chosen
    assert "yeni-klasor" in _items(panel.workdir_combo)
    assert panel.selected_choice()[1] == "yeni-klasor" and saved == [True]
    assert re.fullmatch(r"[a-z0-9_-]{1,32}", asked[0])


def test_demo_disables_writes_with_reason(tmp_path: Path, app: QApplication) -> None:
    path = _new_shape(tmp_path)
    before = path.read_text(encoding="utf-8")
    panel = _panel(path, ["alfa"], demo=True)
    for widget in (panel.agent_combo, panel.workdir_combo, panel.add_folder_button,
                   panel.default_button):
        assert not widget.isEnabled()
    assert "Demo" in panel.agent_reason.text()
    panel._make_default()  # doğrudan çağrılar da yazmaz
    panel._add_folder()
    assert path.read_text(encoding="utf-8") == before


# --- Yerleşim ----------------------------------------------------------------------------------


def test_action_row_inside_viewport_with_combos(tmp_path: Path, app: QApplication) -> None:
    panel = ProjectsPanel(config_path=_old_shape(tmp_path))
    panel.setStyleSheet(themes.stylesheet(themes.DEFAULT_THEME))
    panel.resize(1000, 700)
    panel.show()
    panel.set_busy(False)
    _status(panel, ["alfa"])
    app.processEvents()
    viewport = panel.findChild(QScrollArea).viewport()
    for widget in (panel.prepare_button, panel.prepare_reason):
        bottom = widget.mapTo(viewport, QPoint(0, widget.height())).y()
        assert 0 < bottom <= viewport.height(), (widget, bottom, viewport.height())
    # Geniş düzende iki seçici aynı satırda, dar düzende alt alta.
    assert panel.agent_combo.mapTo(panel, QPoint(0, 0)).y() == panel.workdir_combo.mapTo(
        panel, QPoint(0, 0)
    ).y()
    panel.resize(800, 700)
    app.processEvents()
    assert panel.agent_combo.mapTo(panel, QPoint(0, 0)).y() < panel.workdir_combo.mapTo(
        panel, QPoint(0, 0)
    ).y()
    panel.close()


def test_action_row_and_default_button_inside_viewport_with_warning_chip(
    tmp_path: Path, app: QApplication
) -> None:
    """Çip görünürken de "Taslak hazırla" ve "Varsayılan yap" kıvrımın üstünde kalır."""
    panel = ProjectsPanel(config_path=_new_shape(tmp_path))
    panel.setStyleSheet(themes.stylesheet(themes.DEFAULT_THEME))
    panel.resize(1000, 700)
    panel.show()
    panel.set_busy(False)
    _status(panel, ["alfa"])
    panel.agent_combo.setCurrentIndex(panel.agent_combo.findData("claude"))  # varsayılan yap açılır
    app.processEvents()
    assert not panel.workdir_warning.isHidden() and panel.default_button.isEnabled()
    viewport = panel.findChild(QScrollArea).viewport()
    for widget in (panel.prepare_button, panel.default_button, panel.workdir_warning):
        bottom = widget.mapTo(viewport, QPoint(0, widget.height())).y()
        assert 0 < bottom <= viewport.height(), (widget, bottom, viewport.height())
    panel.close()


def test_new_ui_sources_have_no_hardcoded_hex() -> None:
    for module in (projects_panel, code_agent_config):
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert not re.findall(r"#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3}\b", source), module.__name__


# --- Ayarlar: Kod ajanları ---------------------------------------------------------------------


def test_settings_codex_add_writes_profile_through_apply(tmp_path: Path, app: QApplication) -> None:
    from artemis.channels.desktop.settings_panel import save_choices

    path = config(tmp_path)
    panel = SettingsPanel(path)
    assert panel.agent_list.count() == 1 and panel.agent_list.item(0).text().startswith("claude")
    assert panel.add_codex_button.isEnabled() and not panel.apply_button.isEnabled()
    panel.add_codex_button.click()
    assert not panel.add_codex_button.isEnabled()  # var olan profil tekrar eklenmez
    assert panel.apply_button.isEnabled() and not panel.dirty_label.isHidden()
    assert panel.agent_list.count() == 2
    assert load_settings(path).code_agent.agents == {}  # Uygula'ya kadar diske yazılmadı
    panel.default_agent_combo.setCurrentIndex(panel.default_agent_combo.findData("codex"))
    panel.default_agent_combo.activated.emit(panel.default_agent_combo.currentIndex())
    save_choices(path, None, code_agent=panel.code_agent_values())
    agent = load_settings(path).code_agent
    assert agent.agents["codex"].kind == "codex" and agent.agents["codex"].command == "codex"
    assert agent.default_agent == "codex"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert raw["code_agent"]["agents"] == {"codex": {"kind": "codex", "command": "codex"}}
    again = SettingsPanel(path)
    assert not again.add_codex_button.isEnabled() and again.default_agent == "codex"


def test_settings_custom_agent_dialog_shows_backend_errors(app: QApplication) -> None:
    dialog = AgentDialog(("claude",))
    dialog.name.setText("aider")
    dialog.command.setText("aider")
    dialog.prompt_via.setCurrentIndex(dialog.prompt_via.findData("file"))  # {prompt_file} yok
    dialog.submit()
    assert not dialog.error.isHidden() and "{prompt_file}" in dialog.error.text()
    assert dialog.result_profile is None
    dialog.args.setPlainText("--message-file\n{prompt_file}\n")
    dialog.env.setText("aider_key")
    dialog.submit()
    assert "büyük harfli" in dialog.error.text() and dialog.result_profile is None
    dialog.env.setText("AIDER_KEY, OTHER_VAR")
    dialog.submit()
    assert dialog.result_profile == (
        "aider",
        {"kind": "generic", "command": "aider", "args": ["--message-file", "{prompt_file}"],
         "prompt_via": "file", "env": ["AIDER_KEY", "OTHER_VAR"]},
    )


def test_parse_agent_form_rules() -> None:
    for name, command, message in (
        ("Büyük", "x", "küçük harf"),
        ("claude", "x", "zaten"),
        ("var", "x", "zaten"),
        ("yeni", " ", "Komut gerekli"),
    ):
        with pytest.raises(AgentFormError, match=message):
            parse_agent_form(name, command, "", "stdin", "", ("var",))


def test_settings_custom_agent_is_staged_not_written(
    tmp_path: Path, app: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = config(tmp_path)
    panel = SettingsPanel(path)

    def fake_exec(self: AgentDialog) -> int:
        self.name.setText("aider")
        self.command.setText("aider")
        self.submit()
        return int(self.result())

    monkeypatch.setattr(AgentDialog, "exec", fake_exec)
    panel.add_custom_agent_button.click()
    assert "aider" in panel.code_agents and panel.apply_button.isEnabled()
    assert panel.code_agent_values()["agents"] == {
        "aider": {"kind": "generic", "command": "aider", "prompt_via": "stdin"}
    }
    assert load_settings(path).code_agent.agents == {}


def test_settings_demo_disables_agent_actions(tmp_path: Path, app: QApplication) -> None:
    panel = SettingsPanel(config(tmp_path), demo=True)
    for widget in (
        panel.add_codex_button, panel.add_custom_agent_button, panel.default_agent_combo
    ):
        assert not widget.isEnabled()


def test_command_status_never_launches_and_reads_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(code_agent_config.shutil, "which", lambda c: "X" if c == "git" else None)
    assert command_status("git") == "bulundu"
    assert command_status("yok-boyle-komut") == "PATH'te yok"
    assert "ilk kullanımda" in command_status("codex")
    exe = tmp_path / "codex.exe"
    exe.write_text("", encoding="utf-8")
    assert command_status(str(exe)) == "bulundu"
    monkeypatch.setitem(code_agent_config._LOCATE_CACHE, "codex", "C:/yer/codex.exe")
    assert command_status("codex") == "bulundu"
    assert command_status(None) == "komut yok"


# --- Ertelenmiş yeniden kurulum (çalışan iş / bekleyen onay) --------------------------------------


def _config_window(app: QApplication, tmp_path: Path):  # type: ignore[no-untyped-def]
    from artemis.channels.desktop.app import build_window

    window = build_window(tmp_path / "ignored.yaml", demo=True, app=app)
    window._demo = False  # demo yalnız işçi kurulumu için; yeniden kurulum yolunu sınar
    window._busy = False
    window._worker_ready = True
    restarts: list[int] = []
    window._restart_runtime = lambda: restarts.append(1)  # type: ignore[method-assign]
    return window, restarts


def _set_jobs(window, *statuses: str) -> None:  # type: ignore[no-untyped-def]
    window.projects_panel._items = [
        ("job", {"job_id": f"j{i}", "project": "p", "status": s, "spec": {}})
        for i, s in enumerate(statuses)
    ]


def test_config_restart_is_deferred_while_a_job_is_active(
    tmp_path: Path, app: QApplication
) -> None:
    window, restarts = _config_window(app, tmp_path)
    panel = window.projects_panel
    _set_jobs(window, "running")
    window._code_agent_config_saved()
    assert restarts == []
    assert panel.notice.text() == (
        "Kaydedildi; çalışan iş bitince uygulama yeniden başlayınca geçerli olur."
    )
    # Zamanlayıcı: iş sürerken taze liste gelse de yeniden kurulmaz.
    window._refresh_projects()  # liste bayat: önce durum istenir
    assert restarts == []
    panel.status_serial += 1  # taze durum geldi, iş hâlâ sürüyor
    window._refresh_projects()
    assert restarts == []
    # İş bitti: taze durum sonrası tam bir kez yeniden kurulur.
    _set_jobs(window, "done")
    panel.status_serial += 1
    window._refresh_projects()
    assert restarts == [1]
    window._refresh_projects()
    window._refresh_projects()
    assert restarts == [1]  # bayrak temizlendi: tekrar yok
    window.close()


def test_config_restart_waits_for_pending_approval_and_covers_every_active_status(
    tmp_path: Path, app: QApplication
) -> None:
    window, restarts = _config_window(app, tmp_path)
    window._approval_dialog = object()  # type: ignore[assignment]
    window._code_agent_config_saved()
    assert restarts == [] and "onay" in window.projects_panel.notice.text()
    window._approval_dialog = None
    window._config_restart_pending = False
    for status in ("queued", "running", "waiting", "cancelling", "cleanup_pending"):
        _set_jobs(window, status)
        window._code_agent_config_saved()
        assert restarts == [], status
        window._config_restart_pending = False
    window.close()


def test_config_restart_is_immediate_when_idle(tmp_path: Path, app: QApplication) -> None:
    window, restarts = _config_window(app, tmp_path)
    _set_jobs(window, "done", "error")
    window._code_agent_config_saved()
    assert restarts == [1]
    window.close()
