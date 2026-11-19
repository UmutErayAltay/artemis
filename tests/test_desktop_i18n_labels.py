"""Demo akışı, hızlı komut durumu ve pet durum metinleri için yerelleştirme testleri."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("PySide6")


@pytest.fixture(autouse=True)
def _restore_language() -> Iterator[None]:
    from artemis.i18n import set_language

    set_language("tr")
    try:
        yield
    finally:
        set_language("tr")


def _ensure_app(monkeypatch: pytest.MonkeyPatch) -> object:
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
        app = QApplication([])
    return app


def test_demo_triggers_for_english_and_turkish() -> None:
    from artemis.brain.models import Message
    from artemis.channels.desktop.demo import DemoProvider

    provider = DemoProvider()
    for prompt in ("sample flow", "SAMPLE FLOW", "örnek akış"):
        completion = asyncio.run(
            provider.complete([Message(role="user", content=prompt)], None, timeout=1)
        )
        assert [call.name for call in completion.tool_calls] == [
            "demo_read_sample",
            "demo_write_memory",
        ]


def test_demo_write_arguments_follow_language() -> None:
    from artemis.brain.models import Message
    from artemis.channels.desktop.demo import DemoProvider
    from artemis.i18n import set_language

    provider = DemoProvider()

    set_language("en")
    en_completion = asyncio.run(
        provider.complete([Message(role="user", content="sample flow")], None, timeout=1)
    )
    assert en_completion.tool_calls[1].arguments == {
        "key": "last_sample",
        "value": "temporary demo record",
    }

    set_language("tr")
    tr_completion = asyncio.run(
        provider.complete([Message(role="user", content="örnek akış")], None, timeout=1)
    )
    assert tr_completion.tool_calls[1].arguments == {
        "key": "son_örnek",
        "value": "geçici demo kaydı",
    }


def test_demo_english_hint_mentions_sample_flow() -> None:
    from artemis.brain.models import Message
    from artemis.channels.desktop.demo import DemoProvider
    from artemis.i18n import set_language

    set_language("en")
    completion = asyncio.run(
        DemoProvider().complete([Message(role="user", content="merhaba")], None, timeout=1)
    )
    assert completion.content == (
        "DEMO · This input only reached the offline demo provider; no real action was taken. "
        "Type ‘sample flow’ for a safe approval example."
    )
    assert "sample flow" in completion.content
    assert "örnek akış" not in completion.content


def test_quick_overlay_status_retranslates(monkeypatch: pytest.MonkeyPatch) -> None:
    _ensure_app(monkeypatch)
    from artemis.channels.desktop.chat_widgets import QuickOverlay
    from artemis.channels.desktop.retranslate import retranslate_widgets
    from artemis.i18n import set_language, t

    set_language("tr")
    overlay = QuickOverlay(lambda: None, lambda: None, lambda: None)
    try:
        overlay.set_busy(False, t("Hazır"))
        assert overlay.status.text() == "Hazır"

        set_language("en")
        retranslate_widgets([overlay], "en")
        assert overlay.status.text() == "Ready"

        set_language("tr")
        retranslate_widgets([overlay], "tr")
        assert overlay.status.text() == "Hazır"
    finally:
        overlay.close()


def test_pet_status_description_translates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _ensure_app(monkeypatch)
    from PySide6.QtCore import QSettings

    from artemis.channels.desktop.pet import PetWidget
    from artemis.channels.desktop.retranslate import retranslate_widgets
    from artemis.i18n import set_language

    ini_path = tmp_path / "pet.ini"
    settings = QSettings(str(ini_path), QSettings.Format.IniFormat)
    pet = PetWidget(
        on_toggle_voice=lambda: None,
        on_show_panel=lambda: None,
        on_quick=lambda: None,
        on_approval=lambda: None,
        on_quit=lambda: None,
        on_submit=lambda _text: True,
        settings=settings,
        reduced_motion=True,
    )
    try:
        set_language("en")
        pet.set_voice("idle")
        assert pet.toolTip() == "Ready"
        assert pet.accessibleDescription() == "Ready"

        set_language("tr")
        retranslate_widgets([pet], "tr")
        assert pet.toolTip() == "Hazır"
        assert pet.accessibleDescription() == "Hazır"
    finally:
        pet.close()


def test_approval_dialog_translates_demo_after_external_read_and_keeps_audit_turkish(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Dış okuma sonrası demo onayı İngilizcede çevrilir; gate ve denetim Türkçe kalır."""
    _ensure_app(monkeypatch)
    import json

    from PySide6.QtWidgets import QLabel, QWidget

    from artemis.brain.models import ToolCall
    from artemis.channels.desktop.approval_dialog import ApprovalDialog
    from artemis.channels.desktop.bridge import Approval
    from artemis.channels.desktop.demo import DemoWriteTool, create_demo_parts
    from artemis.i18n import set_language
    from artemis.safety.gate import TAINT_NOTICE, SafetyGate, TurnState
    from artemis.tools.base import ToolContext

    settings, _provider, registry, audit = create_demo_parts(tmp_path)
    context = ToolContext(
        settings.workspace, settings, audit, services={"demo_records": {"sample": "veri"}}
    )
    gate = SafetyGate(registry)
    state = TurnState()
    captured_notice: list[str] = []

    def approve(_name: str, _args: dict[str, object]) -> bool:
        captured_notice.append(gate.pending_notice)
        return False

    asyncio.run(
        gate.dispatch(
            ToolCall(id="c1", name="demo_read_sample", arguments={"key": "sample"}),
            context,
            state,
            approve,
        )
    )
    assert state.tainted is True
    asyncio.run(
        gate.dispatch(
            ToolCall(
                id="c2",
                name="demo_write_memory",
                arguments={"key": "last_sample", "value": "temporary demo record"},
            ),
            context,
            state,
            approve,
        )
    )
    expected_raw_notice = f"{TAINT_NOTICE}. {DemoWriteTool.description}"
    assert captured_notice == [expected_raw_notice]

    # Denetim kaydındaki metin dil ne olursa olsun Türkçe kaynak olarak saklanır.
    records = [
        json.loads(line)
        for line in settings.audit_path.read_text(encoding="utf-8").splitlines()
        if json.loads(line).get("event") == "approval"
    ]
    assert len(records) == 1
    assert records[0]["notice"] == expected_raw_notice

    item = Approval(
        "req-1",
        state.turn_id,
        "app-1",
        DemoWriteTool.name,
        {"key": "last_sample", "value": "temporary demo record"},
        captured_notice[0],
        None,
        DemoWriteTool.label,
    )
    parent = QWidget()

    set_language("en")
    en_dialog = ApprovalDialog(item, lambda *_: True, parent)
    try:
        en_title = en_dialog.findChild(QLabel, "title")
        en_warning = en_dialog.findChild(QLabel, "approvalNotice")
        assert en_title is not None and en_title.text() == "Demo write memory"
        assert (
            en_warning is not None
            and en_warning.text() == "This step was requested after reading external content."
        )
        assert en_dialog.tool_note_label is not None
        assert en_dialog.tool_note_label.text() == (
            "Only updates temporary demo memory; does not use real files or services."
        )
    finally:
        en_dialog._choose(False)

    set_language("tr")
    tr_dialog = ApprovalDialog(item, lambda *_: True, parent)
    try:
        tr_title = tr_dialog.findChild(QLabel, "title")
        tr_warning = tr_dialog.findChild(QLabel, "approvalNotice")
        assert tr_title is not None and tr_title.text() == "Demo belleğe yazma"
        assert (
            tr_warning is not None
            and tr_warning.text() == "Bu adım dış bir içerik okunduktan sonra istendi."
        )
        assert tr_dialog.tool_note_label is not None
        assert tr_dialog.tool_note_label.text() == DemoWriteTool.description
    finally:
        tr_dialog._choose(False)


def test_approval_notice_variants_translate_and_preserve_site_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tüm kapı uyarı türleri İngilizcede çevrilir; site host'u aynen korunur."""
    _ensure_app(monkeypatch)
    from PySide6.QtWidgets import QLabel, QWidget

    from artemis.channels.desktop.approval_dialog import ApprovalDialog
    from artemis.channels.desktop.bridge import Approval
    from artemis.channels.desktop.demo import DemoReadTool, DemoWriteTool
    from artemis.channels.notice import format_approval_notice
    from artemis.i18n import set_language
    from artemis.safety.gate import (
        HISTORY_TAINT_NOTICE,
        SITE_NOTICE,
        TAINT_NOTICE,
        UNATTENDED_NOTICE,
    )

    cases = [
        (
            f"{TAINT_NOTICE}. {DemoWriteTool.description}",
            "This step was requested after reading external content.",
            "Only updates temporary demo memory; does not use real files or services.",
            f"{TAINT_NOTICE}.",
            DemoWriteTool.description,
        ),
        (
            f"{HISTORY_TAINT_NOTICE}. {DemoWriteTool.description}",
            "This step was requested after reading external content (in a previous turn).",
            "Only updates temporary demo memory; does not use real files or services.",
            f"{HISTORY_TAINT_NOTICE}.",
            DemoWriteTool.description,
        ),
        (
            f"{UNATTENDED_NOTICE}. {DemoReadTool.description}",
            "Side-effect actions are not allowed in an unattended turn.",
            "Reads fixed sample data from demo memory; does not use real files or services.",
            f"{UNATTENDED_NOTICE}.",
            DemoReadTool.description,
        ),
        (
            f"{SITE_NOTICE} (tr.wikipedia.org). {DemoReadTool.description}",
            "This step reads a website's content (tr.wikipedia.org).",
            "Reads fixed sample data from demo memory; does not use real files or services.",
            f"{SITE_NOTICE} (tr.wikipedia.org).",
            DemoReadTool.description,
        ),
        (
            f"Bu işlem onay gerektirir.. {DemoWriteTool.description}",
            "This action requires approval.",
            "Only updates temporary demo memory; does not use real files or services.",
            "Bu işlem onay gerektirir.",
            DemoWriteTool.description,
        ),
    ]
    parent = QWidget()
    for raw, en_warn, en_note, tr_warn, tr_note in cases:
        item = Approval("r", "t", "a", "demo_write_memory", {"key": "k"}, raw, None, "")

        set_language("en")
        en_dialog = ApprovalDialog(item, lambda *_: True, parent)
        try:
            notice = en_dialog.findChild(QLabel, "approvalNotice")
            assert notice is not None and notice.text() == en_warn
            assert en_dialog.tool_note_label is not None
            assert en_dialog.tool_note_label.text() == en_note
        finally:
            en_dialog._choose(False)
        assert format_approval_notice(raw) == f"{en_warn} {en_note}"

        set_language("tr")
        tr_dialog = ApprovalDialog(item, lambda *_: True, parent)
        try:
            notice = tr_dialog.findChild(QLabel, "approvalNotice")
            assert notice is not None and notice.text() == tr_warn
            assert tr_dialog.tool_note_label is not None
            assert tr_dialog.tool_note_label.text() == tr_note
        finally:
            tr_dialog._choose(False)
        assert format_approval_notice(raw) == f"{tr_warn} {tr_note}"

    set_language("en")
    assert format_approval_notice("") == ""


def test_operation_log_translates_tool_label_for_english_and_keeps_turkish() -> None:
    from artemis.channels.desktop.operations import OperationLog
    from artemis.i18n import set_language

    labels = {"demo_write_memory": ("Demo belleğe yazma", "Demo belleğe yazıldı")}

    set_language("en")
    en_log = OperationLog(clock=lambda: "12:00")
    en_card = en_log.feed("r1", "tool_requested", {"tool": "demo_write_memory"}, labels)
    assert en_card is not None and en_card.ops[0].label == "Demo write memory"

    set_language("tr")
    tr_log = OperationLog(clock=lambda: "12:00")
    tr_card = tr_log.feed("r2", "tool_requested", {"tool": "demo_write_memory"}, labels)
    assert tr_card is not None and tr_card.ops[0].label == "Demo belleğe yazma"
