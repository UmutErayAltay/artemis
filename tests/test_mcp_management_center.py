"""Grafiksel MCP Sunucu Yönetim Merkezi testleri (M2A · Feature 15 · ADR 0028)."""

from __future__ import annotations

import gc
import os
import sys
import threading
import time
import weakref
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication, QEvent, QTimer, qInstallMessageHandler
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

import artemis.channels.desktop.mcp_section as mcp_section_mod
from artemis.channels.desktop import window_restart
from artemis.channels.desktop.mcp_dialog import MCPServerDialog, validate_mcp_server
from artemis.channels.desktop.mcp_section import MCPSection
from artemis.channels.desktop.retranslate import retranslate_widgets
from artemis.channels.desktop.settings_io import dump_mcp_server, save_choices
from artemis.channels.desktop.settings_panel import _DIRTY_ROLE, SettingsPanel
from artemis.channels.runtime import create_runtime
from artemis.config.settings import MCPServerConfig, load_settings
from artemis.i18n import set_language, t
from artemis.mcp.bridge import discover_mcp_tools, resolve_risk
from artemis.mcp.client import MCPError, MCPToolSpec, StdioMCPClient
from artemis.safety.audit import AuditLog
from artemis.tools.base import Risk
from artemis.tools.registry import ToolRegistry
from tests.test_mcp_client import wait_dead
from tests.test_settings_no_squeeze import _STYLE, _assert_no_squeeze
from tests.test_settings_panel import config

FAKE_SERVER = Path(__file__).resolve().parent / "mcp_fake_server.py"


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])  # type: ignore[return-value]


@pytest.fixture(autouse=True)
def _restore_tr_language() -> Any:
    set_language("tr")
    yield
    set_language("tr")


def _wait(predicate: Any, timeout_ms: int = 5000) -> bool:
    waited = 0
    while not predicate() and waited < timeout_ms:
        QTest.qWait(20)
        waited += 20
    return bool(predicate())


def _sample_server(name: str = "notlar", **overrides: Any) -> MCPServerConfig:
    data: dict[str, Any] = {
        "name": name,
        "command": sys.executable,
        "args": (str(FAKE_SERVER), "ok"),
        "cwd": None,
        "content": "file",
        "timeout_seconds": 10.0,
        "read_only_tools": ("read_text_file",),
        "confirm_tools": ("write_file",),
        "env_passthrough": ("GITHUB_TOKEN",),
    }
    data.update(overrides)
    return MCPServerConfig(**data)


# 1. Alan doğrulama & diyalog ---------------------------------------------------------------


def test_validate_mcp_server_rejects_invalid_and_requires_explicit_content(tmp_path: Path) -> None:
    base = {
        "name": "notlar",
        "command": "{python}",
        "args_text": "-m\nartemis.mcp_servers.browser\nC:\\My Folder\\arg with spaces",
        "cwd": str(tmp_path),
        "content": "file",
        "timeout_seconds": 20.0,
        "read_only_tools_text": "read_text_file",
        "confirm_tools_text": "write_file",
        "env_passthrough_text": "GITHUB_TOKEN, API_KEY_2",
        "taken": (),
    }
    ok_server, ok_errors = validate_mcp_server(**base)
    assert ok_server is not None and ok_errors == {}
    assert ok_server.args == ("-m", "artemis.mcp_servers.browser", "C:\\My Folder\\arg with spaces")
    assert ok_server.cwd == tmp_path
    assert ok_server.env_passthrough == ("GITHUB_TOKEN", "API_KEY_2")

    # content varsayılansız zorunlu
    srv, errs = validate_mcp_server(**{**base, "content": None})
    assert srv is None and "content" in errs

    # geçersiz adlar & çakışma
    for bad_name in ("", "Bad", "a-b", "x" * 25):
        srv, errs = validate_mcp_server(**{**base, "name": bad_name})
        assert srv is None and "name" in errs, bad_name
    srv, errs = validate_mcp_server(**{**base, "taken": ("notlar",)})
    assert srv is None and "name" in errs

    # boş komut
    srv, errs = validate_mcp_server(**{**base, "command": "   "})
    assert srv is None and "command" in errs

    # timeout sınırları
    for bad_timeout in (0.0, -5.0, 300.5, float("nan"), True):
        srv, errs = validate_mcp_server(**{**base, "timeout_seconds": bad_timeout})
        assert srv is None and "timeout_seconds" in errs

    # çakışan read_only_tools & confirm_tools
    srv, errs = validate_mcp_server(
        **{**base, "read_only_tools_text": "echo, env", "confirm_tools_text": "fail, echo"}
    )
    assert srv is None and "confirm_tools" in errs

    # env_passthrough: sır değeri / `=` / küçük harf reddi
    for bad_env in ("lower", "KEY=secret", "sk-123", "1ABC", "A" * 65):
        srv, errs = validate_mcp_server(**{**base, "env_passthrough_text": bad_env})
        assert srv is None and "env_passthrough" in errs, bad_env


def test_dialog_has_no_silent_content_default_and_shows_inline_errors(qt_app: QApplication) -> None:
    dialog = MCPServerDialog(None, ("varolan",))
    try:
        assert dialog.content_combo.currentData() is None
        dialog.name_edit.setText("varolan")
        dialog.command_edit.setText("")
        dialog.env_edit.setText("TOKEN=gizli")
        dialog.submit()
        assert dialog.result_server is None
        assert dialog.result() != QDialog.DialogCode.Accepted
        assert not dialog._errors["name"].isHidden()
        assert not dialog._errors["command"].isHidden()
        assert not dialog._errors["content"].isHidden()
        assert not dialog._errors["env_passthrough"].isHidden()

        dialog.name_edit.setText("yeni_sunucu")
        dialog.command_edit.setText("{python}")
        dialog.content_combo.setCurrentIndex(dialog.content_combo.findData("text"))
        dialog.env_edit.setText("MY_SECRET_NAME")
        dialog.submit()
        assert dialog.result() == QDialog.DialogCode.Accepted
        assert dialog.result_server is not None
        assert dialog.result_server.content == "text"
    finally:
        dialog.deleteLater()


# 2. UI CRUD, onay kutusu & demo modu -------------------------------------------------------


def test_mcp_section_crud_delete_confirm_default_cancel_and_demo(
    tmp_path: Path, qt_app: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = config(tmp_path)
    section = MCPSection(path)
    try:
        assert section.server_list.count() == 0
        assert section.server_area.currentWidget() is section.server_area.empty
        assert section.add_button.isHidden()
        assert not section.edit_button.isEnabled()
        assert not section.delete_button.isEnabled()
        assert not section.test_button.isEnabled()

        created = _sample_server("notlar")

        class _FakeAddDialog:
            DialogCode = MCPServerDialog.DialogCode

            def __init__(self, existing: Any, taken: Any, parent: Any = None) -> None:
                self.existing = existing
                self.taken = tuple(taken)
                self.result_server = created

            def exec(self) -> int:
                return int(MCPServerDialog.DialogCode.Accepted)

        monkeypatch.setattr(mcp_section_mod, "MCPServerDialog", _FakeAddDialog)
        changed_count: list[int] = []
        section.changed.connect(lambda: changed_count.append(1))
        section.server_area.empty.action_button.click()  # type: ignore[union-attr]

        assert len(changed_count) == 1
        assert section.server_list.count() == 1
        assert section.server_area.currentWidget() is section.server_list
        assert not section.add_button.isHidden()
        row_text = section.server_list.item(0).text()
        assert "notlar" in row_text
        assert "Dosya" in row_text
        assert "Salt okunur: 1" in row_text
        assert "Onaylı: 1" in row_text

        # Düzenle (aynı ada izin verilir, taken içinde kendi adı olmamalı)
        edited = _sample_server("notlar", content="text", timeout_seconds=45.0, confirm_tools=())

        class _FakeEditDialog:
            DialogCode = MCPServerDialog.DialogCode

            def __init__(self, existing: Any, taken: Any, parent: Any = None) -> None:
                assert existing is not None and existing.name == "notlar"
                assert "notlar" not in tuple(taken)
                self.result_server = edited

            def exec(self) -> int:
                return int(MCPServerDialog.DialogCode.Accepted)

        monkeypatch.setattr(mcp_section_mod, "MCPServerDialog", _FakeEditDialog)
        section.edit_button.click()
        assert len(changed_count) == 2
        assert section.servers()[0].content == "text"
        assert section.servers()[0].timeout_seconds == 45.0

        # Silme onay kutusu varsayılanı "Vazgeç"
        box, yes_btn = section.confirm_box("Silinsin mi?")
        default_btn = box.defaultButton()
        escape_btn = box.escapeButton()
        assert default_btn is not None and default_btn.text() == t("Vazgeç")
        assert escape_btn is not None and escape_btn.text() == t("Vazgeç")
        assert yes_btn.text() == t("Sil")
        box.deleteLater()

        # Reddedilen silme korur; onaylanan siler ve boş duruma döner
        monkeypatch.setattr(section, "_confirm", lambda _text: False)
        section.delete_button.click()
        assert section.server_list.count() == 1

        monkeypatch.setattr(section, "_confirm", lambda _text: True)
        section.delete_button.click()
        assert section.server_list.count() == 0
        assert section.server_area.currentWidget() is section.server_area.empty
    finally:
        section.finish()
        section.deleteLater()

    # Demo modu tüm eylemleri kapatır ve süreç açmaz
    spawned = False

    def _forbid_probe(*_args: Any, **_kwargs: Any) -> list[MCPToolSpec]:
        nonlocal spawned
        spawned = True
        return []

    demo_sec = MCPSection(path, demo=True, probe=_forbid_probe)
    try:
        assert not demo_sec.add_button.isEnabled()
        assert not demo_sec.edit_button.isEnabled()
        assert not demo_sec.delete_button.isEnabled()
        assert not demo_sec.test_button.isEnabled()
        assert demo_sec.server_area.empty.action_button is not None
        assert not demo_sec.server_area.empty.action_button.isEnabled()
        demo_sec._test_selected()
        assert not spawned
    finally:
        demo_sec.finish()
        demo_sec.deleteLater()


# 3. Kaydetme / yükleme / geri alma / kirli izleme -----------------------------------------


def test_settings_panel_mcp_dirty_save_roundtrip_and_discard(
    tmp_path: Path, qt_app: QApplication
) -> None:
    path = config(tmp_path)
    panel = SettingsPanel(path)
    try:
        panel.begin_session()
        baseline = path.read_bytes()
        srv = _sample_server(
            "notlar",
            command="{python}",
            args=("-m", "tests.mcp_fake_server", "ok"),
            cwd=tmp_path,
            content="file",
            timeout_seconds=25.0,
            read_only_tools=("echo",),
            confirm_tools=("fail",),
            env_passthrough=("MCP_CUSTOM_ENV",),
        )
        panel.mcp._servers = [srv]
        panel.mcp._render_rows("notlar")
        panel.mcp.changed.emit()

        # Henüz diske yazılmadı; Bağlantılar (satır 6) kirli işaretlendi
        assert panel.has_unsaved is True
        assert path.read_bytes() == baseline
        assert panel.sections.item(6).data(_DIRTY_ROLE) is True
        assert panel.sections.item(0).data(_DIRTY_ROLE) is not True
        assert not panel.save_button.isHidden()
        assert not panel.revert_button.isHidden()

        # discard_changes diskteki duruma döner
        panel.discard_changes()
        assert not panel.has_unsaved
        assert panel.mcp.server_list.count() == 0

        # Tekrar ekle ve window_restart.apply_settings üzerinden kaydet
        panel.mcp._servers = [srv]
        panel.mcp._render_rows("notlar")
        panel.mcp.changed.emit()
        win = SimpleNamespace(
            settings_panel=panel,
            bridge=SimpleNamespace(reload_dictation_settings=lambda _cfg: True),
            _reload_hotkeys=lambda: None,
            _demo=False,
            _exiting=False,
            _config_path=path,
            _tray=None,
        )
        window_restart.apply_settings(win, None)  # type: ignore[arg-type]
        assert not panel.has_unsaved

        loaded = load_settings(path)
        assert len(loaded.mcp.servers) == 1
        saved = loaded.mcp.servers[0]
        assert saved.name == "notlar"
        assert saved.command == "{python}"
        assert saved.args == ("-m", "tests.mcp_fake_server", "ok")
        assert saved.cwd == tmp_path
        assert saved.content == "file"
        assert saved.timeout_seconds == 25.0
        assert saved.read_only_tools == ("echo",)
        assert saved.confirm_tools == ("fail",)
        assert saved.env_passthrough == ("MCP_CUSTOM_ENV",)

        # Yeni panelde aynı veriler yüklenir
        second = SettingsPanel(path)
        try:
            assert second.mcp.server_list.count() == 1
            assert second.mcp_values() == [dump_mcp_server(saved)]
        finally:
            second.finish_inventory()
            second.deleteLater()

        # save_choices(mcp_servers=None) mevcut mcp config'ini korur
        save_choices(path, None, values={("language",): "en"}, mcp_servers=None)
        assert len(load_settings(path).mcp.servers) == 1

        # _revert oturum başındaki hâle byte-for-byte döner
        panel._revert()
        assert path.read_bytes() == baseline
        assert panel.mcp.server_list.count() == 0
    finally:
        panel.finish_inventory()
        panel.deleteLater()


# 4. Arka plan sağlık testi (mcp_fake_server.py) & temiz kapanma ---------------------------


def test_client_list_tools_cancel_kills_hanging_server(tmp_path: Path) -> None:
    pid_file = tmp_path / "list_child.pid"
    cancel = threading.Event()
    threading.Timer(0.25, cancel.set).start()
    started = time.monotonic()
    client = StdioMCPClient(
        [sys.executable, str(FAKE_SERVER), "hang_child", str(pid_file)],
        env={"SYSTEMROOT": os.environ.get("SYSTEMROOT", "")},
        timeout=10.0,
    )
    with pytest.raises(MCPError) as info:
        client.list_tools(cancel=cancel)
    assert info.value.error_type == "cancelled"
    assert time.monotonic() - started < 2.0
    if pid_file.exists():
        assert wait_dead(int(pid_file.read_text(encoding="utf-8")))


def test_background_health_test_fake_server_and_clean_shutdown(
    tmp_path: Path, qt_app: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = config(tmp_path)
    secret = "sk-gizli-mcp-test-999"
    monkeypatch.setenv("MCP_TEST_SECRET", secret)

    ok_srv = _sample_server(
        "fakeok",
        read_only_tools=(),
        confirm_tools=(),
        env_passthrough=("MCP_TEST_SECRET",),
    )
    save_choices(path, None, mcp_servers=[dump_mcp_server(ok_srv)])
    panel = SettingsPanel(path)
    try:
        ticks = 0
        timer = QTimer()
        timer.setInterval(15)

        def _tick() -> None:
            nonlocal ticks
            ticks += 1

        timer.timeout.connect(_tick)
        timer.start()

        panel.mcp.test_button.click()
        assert not panel.mcp.test_button.isEnabled()
        assert "test ediliyor" in panel.mcp.test_status.text()
        assert _wait(lambda: panel.mcp._test_thread is None and panel.mcp.test_button.isEnabled())
        timer.stop()
        assert ticks >= 1  # olay döngüsü bloke olmadı

        status = panel.mcp.test_status.text()
        assert "echo (yan etkili)" in status
        assert "env (yan etkili)" in status
        assert "fail (onaylı)" in status
        assert "mixed (yan etkili)" in status
        assert "broken" not in status
        assert "readOnlyHint" in status
        assert secret not in status
        # read_only_tools otomatik değiştirilmez!
        assert panel.mcp.servers()[0].read_only_tools == ()

        # Hata yolları: die, bad_version, timeout(hang), eksik komut
        for mode, expected_fragment, mode_timeout in (
            ("die", "başlatılamadı", 5.0),
            ("bad_version", "sürümü", 5.0),
            ("hang", "zaman aşımı", 1.0),
        ):
            err_srv = _sample_server(
                f"err_{mode}",
                args=(str(FAKE_SERVER), mode),
                timeout_seconds=mode_timeout,
                env_passthrough=("MCP_TEST_SECRET",),
            )
            panel.mcp._servers = [err_srv]
            panel.mcp._render_rows(err_srv.name)
            panel.mcp.test_button.click()
            assert _wait(lambda: panel.mcp._test_thread is None, timeout_ms=4000)
            err_text = panel.mcp.test_status.text()
            assert expected_fragment in err_text, (mode, err_text)
            assert secret not in err_text
            assert str(FAKE_SERVER) not in err_text

        missing_srv = _sample_server(
            "missing_cmd", command=str(tmp_path / "kesinlikle_olmayan_mcp_exe_xyz")
        )
        panel.mcp._servers = [missing_srv]
        panel.mcp._render_rows(missing_srv.name)
        panel.mcp.test_button.click()
        assert _wait(lambda: panel.mcp._test_thread is None, timeout_ms=5000)
        assert "başlatılamadı" in panel.mcp.test_status.text()

        # Çalışan hang testi varken finish_inventory() temiz kapatır
        hang_srv = _sample_server(
            "hang_close",
            args=(str(FAKE_SERVER), "hang"),
            timeout_seconds=10.0,
        )
        panel.mcp._servers = [hang_srv]
        panel.mcp._render_rows(hang_srv.name)
        panel.mcp.test_button.click()
        assert panel.mcp._test_thread is not None
        panel.finish_inventory()
        assert panel.mcp._test_thread is None
    finally:
        panel.finish_inventory()
        panel.deleteLater()


# 5. ADR 0028 güvenlik değişmezleri ----------------------------------------------------------


def test_adr0028_invariants_empty_spawns_nothing_risk_privacy_and_no_secrets(
    tmp_path: Path, qt_app: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = config(tmp_path)
    secret = "mcp-test-dummy-value-42"
    monkeypatch.setenv("MCP_SECRET_ENV", secret)

    # Boş liste kaydedilince create_runtime / discover_mcp_tools hiç süreç başlatmaz
    save_choices(path, None, mcp_servers=[])
    loaded_empty = load_settings(path)
    assert loaded_empty.mcp.servers == ()

    def _fail_client(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError("Boş mcp.servers varken MCP istemcisi çağrılamaz")

    monkeypatch.setattr("artemis.mcp.client.StdioMCPClient", _fail_client)
    base_reg = ToolRegistry()
    audit = AuditLog(tmp_path / "audit.jsonl")
    assert discover_mcp_tools(loaded_empty, audit, base_reg) is base_reg
    runtime = create_runtime(
        path,
        lambda _tool, _args: False,
        settings=loaded_empty,
        provider=SimpleNamespace(),  # type: ignore[arg-type]
        with_code_agent=False,
    )
    try:
        assert runtime is not None
    finally:
        runtime.shutdown()

    # Sunucunun readOnlyHint beyanı riski düşürmez; content="file" gizli kalır
    monkeypatch.undo()
    monkeypatch.setenv("MCP_SECRET_ENV", secret)
    file_srv = _sample_server(
        "notlar",
        content="file",
        read_only_tools=(),
        confirm_tools=(),
        env_passthrough=("MCP_SECRET_ENV",),
    )
    save_choices(path, None, mcp_servers=[dump_mcp_server(file_srv)])
    loaded = load_settings(path)

    echo_spec = MCPToolSpec("echo", "Echo", {"type": "object"}, {"readOnlyHint": True})
    assert resolve_risk(loaded.mcp.servers[0], echo_spec) == Risk.SIDE_EFFECT

    ro_srv = loaded.mcp.servers[0].model_copy(update={"read_only_tools": ("echo",)})
    assert resolve_risk(ro_srv, echo_spec) == Risk.READ_ONLY
    cf_srv = loaded.mcp.servers[0].model_copy(update={"confirm_tools": ("echo",)})
    assert resolve_risk(cf_srv, echo_spec) == Risk.CONFIRM_REQUIRED

    # discover_mcp_tools ile kayıtlı araçlar content_kind="file" taşır
    reg = discover_mcp_tools(loaded, audit, ToolRegistry())
    tool = reg.get("mcp_notlar_echo")
    assert tool is not None
    assert tool.content_kind == "file"
    assert tool.risk == Risk.SIDE_EFFECT

    # YAML, repr ve UI metinlerinde sır bulunmaz
    yaml_text = path.read_text(encoding="utf-8")
    assert secret not in yaml_text
    assert "MCP_SECRET_ENV" in yaml_text
    assert secret not in repr(loaded.mcp)
    assert secret not in (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    panel = SettingsPanel(path)
    try:
        assert secret not in repr(panel.mcp_values())
        assert secret not in panel.mcp.server_list.item(0).text()
    finally:
        panel.finish_inventory()
        panel.deleteLater()


# 6. Yönlendirme, arama, canlı TR/EN çeviri, sıkışmama ve döngüsüzlük ------------------------


def test_routing_search_retranslate_no_squeeze_and_no_signal_cycles(
    tmp_path: Path, qt_app: QApplication
) -> None:
    path = config(tmp_path)
    srv = _sample_server("notlar", content="file")
    save_choices(path, None, mcp_servers=[dump_mcp_server(srv)])

    font_path = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "Fonts" / "segoeui.ttf"
    font_id = QFontDatabase.addApplicationFont(str(font_path)) if font_path.is_file() else -1
    prev_font = qt_app.font()
    prev_style = qt_app.styleSheet()
    if font_id >= 0:
        qt_app.setFont(QFont("Segoe UI", 10))
    qt_app.setStyleSheet(_STYLE)

    panel = SettingsPanel(path)
    try:
        # Arama "mcp" -> Bağlantılar
        panel.search.setText("mcp")
        visible = [
            panel.sections.item(r).text()
            for r in range(panel.sections.count())
            if not panel.sections.item(r).isHidden()
        ]
        assert visible == ["Bağlantılar"]
        panel.search.clear()

        for route in ("MCP", "MCP sunucuları", "MCP Sunucuları", "MCP Servers"):
            assert panel.select_section(route), route
            assert panel.sections.currentRow() == 6
            assert panel.connections_tabs.currentIndex() == 3

        panel.show()
        for width, height in ((720, 560), (1280, 850)):
            panel.resize(width, height)
            QTest.qWait(20)
            page = panel.stack.currentWidget()
            assert page is not None
            _assert_no_squeeze(page, f"tr:{width}x{height}")

        set_language("en")
        retranslate_widgets([panel], "en")
        QTest.qWait(20)
        assert panel.connections_tabs.tabText(3) == "MCP Servers"
        assert panel.mcp.add_button.text() == "Add server"
        for width, height in ((720, 560), (1280, 850)):
            panel.resize(width, height)
            QTest.qWait(20)
            page = panel.stack.currentWidget()
            assert page is not None
            _assert_no_squeeze(page, f"en:{width}x{height}")

        set_language("tr")
        retranslate_widgets([panel], "tr")
        QTest.qWait(20)
        assert panel.connections_tabs.tabText(3) == "MCP Sunucuları"
        assert panel.mcp.add_button.text() == "Sunucu ekle"
    finally:
        panel.finish_inventory()
        panel.deleteLater()
        qt_app.setFont(prev_font)
        qt_app.setStyleSheet(prev_style)
        if font_id >= 0:
            QFontDatabase.removeApplicationFont(font_id)


def test_hang_child_via_mcp_section_kills_grandchild_on_timeout_and_close(
    tmp_path: Path, qt_app: QApplication
) -> None:
    path = config(tmp_path)
    section = MCPSection(path)
    try:
        # 1) Zaman aşımı yolunda torun süreç job ile ölür
        pid_timeout = tmp_path / "timeout_child.pid"
        srv_timeout = _sample_server(
            "hc_timeout",
            args=(str(FAKE_SERVER), "hang_child", str(pid_timeout)),
            timeout_seconds=1.5,
        )
        section._servers = [srv_timeout]
        section._render_rows(srv_timeout.name)
        section.test_button.click()
        assert _wait(lambda: section._test_thread is None, timeout_ms=6000)
        assert "zaman aşımı" in section.test_status.text()
        assert pid_timeout.exists()
        assert wait_dead(int(pid_timeout.read_text(encoding="utf-8").strip()))

        # 2) Uçuş ortasında close()/finish() 2s beklemeden iptal eder ve torunu öldürür
        pid_close = tmp_path / "close_child.pid"
        srv_close = _sample_server(
            "hc_close",
            args=(str(FAKE_SERVER), "hang_child", str(pid_close)),
            timeout_seconds=10.0,
        )
        section._servers = [srv_close]
        section._render_rows(srv_close.name)
        section.test_button.click()
        assert _wait(
            lambda: pid_close.exists() and pid_close.read_text(encoding="utf-8").strip().isdigit(),
            timeout_ms=5000,
        )
        assert section._test_thread is not None
        started = time.monotonic()
        section.close()
        assert time.monotonic() - started < 1.5
        assert section._test_thread is None
        assert not section._retiring_threads
        assert wait_dead(int(pid_close.read_text(encoding="utf-8").strip()))
    finally:
        section.finish()
        section.deleteLater()


def test_mcp_server_dialog_no_squeeze_tr_and_en_with_errors(qt_app: QApplication) -> None:
    font_path = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "Fonts" / "segoeui.ttf"
    font_id = QFontDatabase.addApplicationFont(str(font_path)) if font_path.is_file() else -1
    prev_font = qt_app.font()
    prev_style = qt_app.styleSheet()
    if font_id >= 0:
        qt_app.setFont(QFont("Segoe UI", 10))
    qt_app.setStyleSheet(_STYLE)
    try:
        for lang in ("tr", "en"):
            set_language(lang)
            dialog = MCPServerDialog(None, ("varolan",))
            try:
                dialog.show()
                for width, height in ((520, 460), (680, 720)):
                    dialog.resize(width, height)
                    QTest.qWait(20)
                    _assert_no_squeeze(dialog, f"dialog:{lang}:{width}x{height}")

                dialog.name_edit.setText("varolan")
                dialog.command_edit.setText("")
                dialog.env_edit.setText("TOKEN=gizli")
                dialog.submit()
                assert not dialog._errors["name"].isHidden()
                assert not dialog._errors["command"].isHidden()
                assert not dialog._errors["content"].isHidden()
                assert not dialog._errors["env_passthrough"].isHidden()
                for width, height in ((520, 460), (680, 720)):
                    dialog.resize(width, height)
                    QTest.qWait(20)
                    _assert_no_squeeze(dialog, f"dialog_err:{lang}:{width}x{height}")
            finally:
                dialog.close()
                dialog.deleteLater()
    finally:
        qt_app.setFont(prev_font)
        qt_app.setStyleSheet(prev_style)
        if font_id >= 0:
            QFontDatabase.removeApplicationFont(font_id)


def test_inflight_health_test_cancelled_and_gated_on_delete_edit_and_refresh(
    tmp_path: Path, qt_app: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = config(tmp_path)
    alpha = _sample_server("alpha", read_only_tools=("echo",))
    save_choices(path, None, mcp_servers=[dump_mcp_server(alpha)])

    started = threading.Event()
    cancel_events: list[threading.Event] = []
    call_releases: list[threading.Event] = []
    mode_state = {"mode": "ok"}

    def _probe(server: MCPServerConfig, cancel: threading.Event) -> list[MCPToolSpec]:
        ev = threading.Event()
        call_releases.append(ev)
        cancel_events.append(cancel)
        started.set()
        ev.wait(timeout=3.0)
        if mode_state["mode"] == "fail":
            raise MCPError("uzak hata", error_type="remote")
        return [MCPToolSpec("echo", "Echo", {"type": "object"}, {})]

    section = MCPSection(path, probe=_probe)
    try:
        # 1) Silme sırasında çalışan test iptal edilir; eski done sinyali test_status'u ezmez
        section.test_button.click()
        assert started.wait(timeout=2.0)
        monkeypatch.setattr(section, "_confirm", lambda _text: True)
        section.delete_button.click()
        assert section.server_list.count() == 0
        assert section.test_status.text() == ""
        assert section._test_thread is None
        assert cancel_events[-1].is_set()
        call_releases[-1].set()
        assert _wait(lambda: not section._retiring_threads, timeout_ms=3000)
        QTest.qWait(30)
        assert section.test_status.text() == ""

        # 2) Düzenleme sırasında eski test iptal edilir; emekli olmadan hemen yeni test açılır
        started.clear()
        section._servers = [alpha]
        section._render_rows(alpha.name)
        section.test_button.click()
        assert started.wait(timeout=2.0)
        old_cancel = cancel_events[-1]
        old_release = call_releases[-1]
        edited = _sample_server("alpha", timeout_seconds=30.0, read_only_tools=("echo",))

        class _FakeEditDialog:
            DialogCode = MCPServerDialog.DialogCode

            def __init__(self, existing: Any, taken: Any, parent: Any = None) -> None:
                self.result_server = edited

            def exec(self) -> int:
                return int(MCPServerDialog.DialogCode.Accepted)

        monkeypatch.setattr(mcp_section_mod, "MCPServerDialog", _FakeEditDialog)
        section.edit_button.click()
        assert old_cancel.is_set()
        assert section.test_status.text() == ""
        assert section._test_thread is None
        assert section.test_button.isEnabled()

        # Eski iş parçacığı hâlâ çalışırken hemen yeni test başlat; eski bitiş yeniyi bozamaz
        started.clear()
        section.test_button.click()
        assert started.wait(timeout=2.0)
        new_release = call_releases[-1]
        old_release.set()
        assert _wait(lambda: not section._retiring_threads, timeout_ms=3000)
        assert section._test_thread is not None
        assert "test ediliyor" in section.test_status.text()
        new_release.set()
        assert _wait(lambda: section._test_thread is None, timeout_ms=3000)
        assert "bağlandı" in section.test_status.text()

        # 3) refresh() sırasında çalışan test iptal edilir; eski failed sinyali test_status'u ezmez
        started.clear()
        mode_state["mode"] = "fail"
        section.test_button.click()
        assert started.wait(timeout=2.0)
        refresh_cancel = cancel_events[-1]
        refresh_release = call_releases[-1]
        section.refresh()
        assert refresh_cancel.is_set()
        assert section.test_status.text() == ""
        assert section._test_thread is None
        refresh_release.set()
        assert _wait(lambda: not section._retiring_threads, timeout_ms=3000)
        QTest.qWait(30)
        assert section.test_status.text() == ""
    finally:
        for ev in call_releases:
            ev.set()
        section.finish()
        section.deleteLater()


def _wait_thread_finished_without_events(thread: Any, timeout_s: float = 2.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not thread.isFinished() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert thread.isFinished()


def test_finished_queued_signals_ignored_on_refresh_delete_and_edit(
    tmp_path: Path, qt_app: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    """İş parçacığı bitip sinyali kuyruğa attıktan hemen sonra gelen iptallerde durum ezilmez."""
    path = config(tmp_path)
    alpha = _sample_server("alpha", read_only_tools=("echo",))
    save_choices(path, None, mcp_servers=[dump_mcp_server(alpha)])

    mode_state = {"mode": "done"}
    qt_warnings: list[str] = []
    previous_handler = qInstallMessageHandler(
        lambda _kind, _ctx, msg: qt_warnings.append(msg)
    )

    def _probe(server: MCPServerConfig, _cancel: threading.Event) -> list[MCPToolSpec]:
        if mode_state["mode"] == "mcp_err":
            raise MCPError("uzak hata", error_type="remote")
        if mode_state["mode"] == "os_err":
            raise OSError("calistirilamadi")
        return [MCPToolSpec("ghost_tool", "Ghost", {"type": "object"}, {})]

    section = MCPSection(path, probe=_probe)
    try:
        # 1) done + finished kuyrukta iken refresh() -> test_status boş kalır
        section.test_button.click()
        assert section._test_thread is not None
        _wait_thread_finished_without_events(section._test_thread)
        section.refresh()
        qt_app.processEvents()
        assert section.test_status.text() == ""
        assert section._test_thread is None
        assert not section._retiring_threads

        # 2) done + finished kuyrukta iken delete_button.click() -> test_status boş kalır
        monkeypatch.setattr(section, "_confirm", lambda _text: True)
        section.test_button.click()
        assert section._test_thread is not None
        _wait_thread_finished_without_events(section._test_thread)
        section.delete_button.click()
        qt_app.processEvents()
        assert section.server_list.count() == 0
        assert section.test_status.text() == ""
        assert section._test_thread is None

        # 3) failed(MCPError) kuyrukta iken delete_button.click() -> test_status boş kalır
        mode_state["mode"] = "mcp_err"
        section._servers = [alpha]
        section._render_rows(alpha.name)
        section.test_button.click()
        assert section._test_thread is not None
        _wait_thread_finished_without_events(section._test_thread)
        section.delete_button.click()
        qt_app.processEvents()
        assert section.test_status.text() == ""
        assert section._test_thread is None

        # 4) failed(OSError) kuyrukta iken refresh() -> test_status boş kalır
        mode_state["mode"] = "os_err"
        section.refresh()
        section.test_button.click()
        assert section._test_thread is not None
        _wait_thread_finished_without_events(section._test_thread)
        section.refresh()
        qt_app.processEvents()
        assert section.test_status.text() == ""
        assert section._test_thread is None

        # 5) done + finished kuyrukta iken edit + save -> test_status boş kalır
        mode_state["mode"] = "done"
        edited = _sample_server("alpha", timeout_seconds=45.0, read_only_tools=("echo",))

        class _FakeEditDialog:
            DialogCode = MCPServerDialog.DialogCode

            def __init__(self, existing: Any, taken: Any, parent: Any = None) -> None:
                self.result_server = edited

            def exec(self) -> int:
                return int(MCPServerDialog.DialogCode.Accepted)

        monkeypatch.setattr(mcp_section_mod, "MCPServerDialog", _FakeEditDialog)
        section.test_button.click()
        assert section._test_thread is not None
        _wait_thread_finished_without_events(section._test_thread)
        section.edit_button.click()
        qt_app.processEvents()
        assert section.test_status.text() == ""
        assert section._test_thread is None
        assert section.servers()[0].timeout_seconds == 45.0
    finally:
        section.finish()
        section.deleteLater()
        qt_app.processEvents()
        qInstallMessageHandler(previous_handler)

    qthread_msgs = [msg for msg in qt_warnings if "QThread" in msg or "still running" in msg]
    assert not qthread_msgs, qthread_msgs


def test_finished_queued_signals_do_not_clobber_immediate_beta_test(
    tmp_path: Path, qt_app: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kuyruktaki alpha sinyalleri yeni başlayan beta testini ezemez ve çökme yaratmaz."""
    path = config(tmp_path)
    alpha = _sample_server("alpha")
    beta = _sample_server("beta")
    save_choices(path, None, mcp_servers=[dump_mcp_server(alpha), dump_mcp_server(beta)])

    beta_hold = threading.Event()
    qt_warnings: list[str] = []
    previous_handler = qInstallMessageHandler(
        lambda _kind, _ctx, msg: qt_warnings.append(msg)
    )

    def _probe(server: MCPServerConfig, _cancel: threading.Event) -> list[MCPToolSpec]:
        if server.name == "alpha":
            return [MCPToolSpec("alpha_ghost", "Ghost", {"type": "object"}, {})]
        beta_hold.wait(timeout=3.0)
        return [MCPToolSpec("beta_real", "Real", {"type": "object"}, {})]

    section = MCPSection(path, probe=_probe)
    try:
        # 1) _cancel_active_test() sonrası hemen beta başlatılır
        section._render_rows("alpha")
        section.test_button.click()
        assert section._test_thread is not None
        _wait_thread_finished_without_events(section._test_thread)

        section._cancel_active_test()
        section.server_list.setCurrentRow(1)
        section.test_button.click()
        assert section._test_thread is not None
        beta_thread = section._test_thread

        qt_app.processEvents()
        assert section._test_thread is beta_thread
        assert not section.test_button.isEnabled()
        assert "beta" in section.test_status.text()
        assert "alpha_ghost" not in section.test_status.text()

        beta_hold.set()
        assert _wait(lambda: section._test_thread is None, timeout_ms=3000)
        assert "beta_real" in section.test_status.text()
        assert section.test_button.isEnabled()

        # 2) alpha bitmiş (_test_thread != None, isRunning() == False) iken _test_selected()
        # doğrudan çağrılırsa eski iş parçacığı temizce emekli edilir ve beta ezilmez.
        beta_hold.clear()
        section._render_rows("alpha")
        section.test_button.click()
        assert section._test_thread is not None
        _wait_thread_finished_without_events(section._test_thread)

        section.server_list.setCurrentRow(1)
        section._test_selected()
        assert section._test_thread is not None
        beta_thread_2 = section._test_thread

        qt_app.processEvents()
        assert section._test_thread is beta_thread_2
        assert "beta" in section.test_status.text()
        assert "alpha_ghost" not in section.test_status.text()

        beta_hold.set()
        assert _wait(lambda: section._test_thread is None, timeout_ms=3000)
        assert "beta_real" in section.test_status.text()
        assert section.test_button.isEnabled()

        # 3) delete_button.click() sonrası hemen beta başlatılır
        beta_hold.clear()
        section._servers = [alpha, beta]
        section._render_rows("alpha")
        monkeypatch.setattr(section, "_confirm", lambda _text: True)
        section.test_button.click()
        assert section._test_thread is not None
        _wait_thread_finished_without_events(section._test_thread)

        section.delete_button.click()
        assert section.server_list.count() == 1
        section.test_button.click()
        assert section._test_thread is not None
        beta_thread_3 = section._test_thread

        qt_app.processEvents()
        assert section._test_thread is beta_thread_3
        assert "beta" in section.test_status.text()
        assert "alpha_ghost" not in section.test_status.text()

        beta_hold.set()
        assert _wait(lambda: section._test_thread is None, timeout_ms=3000)
        assert "beta_real" in section.test_status.text()
        assert section.test_button.isEnabled()
    finally:
        beta_hold.set()
        section.finish()
        section.deleteLater()
        qt_app.processEvents()
        qInstallMessageHandler(previous_handler)

    qthread_msgs = [msg for msg in qt_warnings if "QThread" in msg or "still running" in msg]
    assert not qthread_msgs, qthread_msgs


def test_mcp_section_and_dialog_have_no_signal_cycles(
    tmp_path: Path, qt_app: QApplication
) -> None:
    path = config(tmp_path)
    save_choices(path, None, mcp_servers=[dump_mcp_server(_sample_server("notlar"))])

    section = MCPSection(path)
    dialog = MCPServerDialog(None, ("notlar",), section)
    dialog.name_edit.setText("diger")
    dialog.command_edit.setText("{python}")
    dialog.content_combo.setCurrentIndex(dialog.content_combo.findData("file"))
    dialog.submit()

    holder = [dialog]
    del dialog
    _assert_dies_without_gc(holder, qt_app)
    holder = [section]
    del section
    _assert_dies_without_gc(holder, qt_app)


def _assert_dies_without_gc(holder: list[Any], qt_app: QApplication) -> None:
    widget = holder.pop()
    ref = weakref.ref(widget)
    widget.close()
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qt_app.processEvents()
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        del widget
        assert ref() is None
    finally:
        if was_enabled:
            gc.enable()
