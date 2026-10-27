"""Mesaj işlem kartlarının gruplama ve özet kuralları (Qt'siz)."""

from __future__ import annotations

from typing import Any

from artemis.channels.desktop.operations import CARDS_KEPT, OperationLog

LABELS = {
    "open_application": ("Uygulama açma", "Uygulama açıldı"),
    "computer_use": ("Bilgisayar kullanımı", "Bilgisayar kullanıldı"),
}
SECRET = "«gizli-metin»"


def _log() -> OperationLog:
    return OperationLog(clock=lambda: "12:00")


def _feed(log: OperationLog, request: str, event: str, **fields: Any) -> None:
    log.feed(request, event, fields, LABELS)


def _tool_flow(log: OperationLog, request: str, status: str = "ok", success: bool = True) -> None:
    args = {"app": "notepad", "text": SECRET}
    _feed(log, request, "turn_start", turn_id="t")
    _feed(log, request, "tool_requested", tool="open_application", arguments=args)
    _feed(log, request, "approval", tool="open_application", arguments=args, approved=True)
    _feed(log, request, "tool_dispatch", tool="open_application", arguments=args)
    _feed(
        log, request, "tool_result", tool="open_application", elapsed_seconds=0.25,
        result={"status": status, "success": success, "message": "Notepad açıldı."},
    )
    _feed(log, request, "turn_end", stop_reason="completed")


def test_two_messages_two_cards_newest_first() -> None:
    log = _log()
    log.begin("r1", "ilk mesaj")
    log.begin("r2", "ikinci mesaj")
    _tool_flow(log, "r1")
    _tool_flow(log, "r2")
    assert [c.title for c in log.newest_first()] == ["ikinci mesaj", "ilk mesaj"]
    assert all(len(c.ops) == 1 for c in log.cards.values())


def test_request_dispatch_result_merge_into_one_row() -> None:
    log = _log()
    log.begin("r1", "aç")
    _tool_flow(log, "r1")
    (op,) = log.cards["r1"].ops
    assert op.label == "Uygulama açma" and op.summary.startswith("notepad")
    assert op.state == "ok" and op.state_text == "✓ tamamlandı"
    assert op.message == "Notepad açıldı." and op.duration == "250 ms"
    assert op.approval == "Onay verildi"
    assert log.cards["r1"].chip == "Tamam"


def test_running_until_result_then_chip_changes() -> None:
    log = _log()
    log.begin("r1", "aç")
    _feed(log, "r1", "tool_requested", tool="open_application", arguments={})
    assert log.cards["r1"].chip == "Çalışıyor"
    assert log.cards["r1"].ops[0].state == "running"


def test_sent_status_reads_unverified() -> None:
    log = _log()
    log.begin("r1", "aç")
    _tool_flow(log, "r1", status="sent", success=False)
    card = log.cards["r1"]
    assert card.ops[0].state == "sent" and "doğrulanamadı" in card.ops[0].state_text
    assert "✓" not in card.ops[0].state_text
    assert card.chip == "Doğrulanamadı"


def test_chip_for_cancel_error_and_failure() -> None:
    log = _log()
    for request, reason in (("a", "cancelled"), ("b", "timeout")):
        log.begin(request, request)
        _feed(log, request, "turn_end", stop_reason=reason)
    assert log.cards["a"].chip == "İptal" and log.cards["b"].chip == "Hata"
    log.begin("c", "c")
    log.fail("c")
    assert log.cards["c"].chip == "Hata"
    log.begin("d", "d")
    _tool_flow(log, "d", status="error", success=False)
    assert log.cards["d"].chip == "Hata"


def test_approval_notes() -> None:
    log = _log()
    log.begin("r1", "x")
    _feed(log, "r1", "tool_requested", tool="open_application", arguments={})
    _feed(log, "r1", "approval", tool="open_application", approved=False)
    _feed(log, "r1", "tool_result", tool="open_application",
          result={"status": "denied", "success": False})
    op = log.cards["r1"].ops[0]
    assert op.approval == "Onay reddedildi" and op.state == "denied"
    _feed(log, "r1", "tool_requested", tool="computer_use", arguments={})
    _feed(log, "r1", "approval_skipped", tool="computer_use", auto_mode=True)
    assert log.cards["r1"].ops[1].approval == "Otomatik onay"


def test_typed_text_never_exposed() -> None:
    log = _log()
    log.begin("r1", "yaz")
    _tool_flow(log, "r1")
    card = log.cards["r1"]
    visible = " ".join(
        [card.title, card.chip, *[f"{o.summary} {o.message} {o.state_text}" for o in card.ops]]
    )
    assert SECRET not in visible and SECRET not in "\n".join(card.details)
    assert any("text=" + str(len(SECRET)) + " karakter" in line for line in card.details)


def test_internal_events_hidden_from_rows_but_in_details() -> None:
    log = _log()
    log.begin("r1", "x")
    _feed(log, "r1", "turn_start", turn_id="t")
    _feed(log, "r1", "model_response")
    _feed(log, "r1", "privacy_decision")
    card = log.cards["r1"]
    assert card.ops == []
    assert [line.split(" · ")[0] for line in card.details] == [
        "turn_start", "model_response", "privacy_decision",
    ]


def test_card_cap_drops_oldest() -> None:
    log = _log()
    for index in range(CARDS_KEPT + 5):
        log.begin(f"r{index}", f"m{index}")
    assert len(log.cards) == CARDS_KEPT
    assert "r0" not in log.cards and f"r{CARDS_KEPT + 4}" in log.cards


def test_computer_step_subrows_and_verdict() -> None:
    log = _log()
    log.begin("r1", "bilgisayar")
    _feed(log, "r1", "tool_requested", tool="computer_use", arguments={"goal": "Not defterini aç"})
    _feed(log, "r1", "computer_step", step=3, action="click", reason="Tıklıyorum",
          sensitive=False, auto_approved=True)
    _feed(log, "r1", "computer_step", step=4, action="type", text_length=7, reason="")
    _feed(log, "r1", "computer_verify", verified=False, evidence="pencere görünmüyor")
    _feed(log, "r1", "tool_result", tool="computer_use",
          result={"status": "sent", "success": False, "message": "ekranda doğrulanamadı: x"})
    (op,) = log.cards["r1"].ops
    assert op.summary == "Not defterini aç"
    texts = [sub.text for sub in op.subrows]
    assert texts[0].startswith("Adım 3 · Tıklama — Tıklıyorum")
    assert texts[1] == "Adım 4 · Yazma (7 karakter)"
    assert texts[2] == "Ekranda doğrulanamadı: pencere görünmüyor"
    assert op.message == "ekranda doğrulanamadı: x" and op.state == "sent"


def test_computer_verify_absent_and_orphan_events_are_harmless() -> None:
    log = _log()
    _feed(log, "r1", "computer_step", step=1, action="click")  # araç satırı yok
    _feed(log, "r1", "tool_requested", tool="computer_use", arguments={})
    _feed(log, "r1", "tool_result", tool="computer_use",
          result={"status": "ok", "success": True})
    assert log.cards["r1"].ops[0].subrows == []
    assert log.feed(None, "turn_start", {}, LABELS) is None


def test_summary_truncates_and_reduces_url_and_path() -> None:
    from artemis.channels.desktop.operations import summarize_arguments

    long = "a" * 200
    assert len(summarize_arguments({"goal": long})) <= 80
    assert summarize_arguments({"url": "https://example.com/x?token=1"}) == "example.com"
    assert summarize_arguments({"path": "C:\\a\\b\\rapor.txt"}) == "rapor.txt"
    assert summarize_arguments({"content": SECRET}) == "content: 13 karakter"
