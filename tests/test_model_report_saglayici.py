"""Model testi: sağlayıcı hatalarının ayrı sayılması, gözlem kipi ve bilgisayar tehlikesi."""

from __future__ import annotations

from decimal import Decimal

from artemis.brain.model_report import group_stats, merge_retry, recommend, report_summary
from bench.bilgisayar_testi import ComputerResult
from bench.model_testi import build_report
from bench.run import Expected, Scenario, ScenarioResult


def _scenario(sid: str, level: str = "temel") -> Scenario:
    return Scenario(id=sid, category="multi_step", text="t", expected=Expected(), level=level)


def _chat_result(
    scenario: Scenario, *, ok: bool = True, provider_error: bool = False, dangerous: int = 0
) -> ScenarioResult:
    stop = "provider_error" if provider_error else ("completed" if ok else "failed")
    return ScenarioResult(
        id=scenario.id, category=scenario.category, success=ok and not provider_error,
        dangerous_errors=dangerous, unapproved_side_effects=0, wrong_target_actions=0,
        tool_dispatches=0, tool_successes=0, tool_sent=0, errors=[], dangerous_reasons=[],
        steps=1, tool_calls=0, elapsed_seconds=1.0, input_tokens=10, output_tokens=5,
        cost_usd=Decimal("0"), approvals=[], answer="yanıt", stop_reason=stop,
    )


def test_group_stats_excludes_provider_errors_and_marks_unmeasured() -> None:
    items = [
        {"id": f"temel_{i}", "success": i < 4, "provider_error": i >= 5}
        for i in range(7)
    ]
    stats = group_stats(items, "temel")
    assert stats == {"passed": 4, "total": 5, "provider_errors": 2, "rate": 0.8}
    assert "unmeasured" not in stats

    # 7 senaryodan 4'ü sağlayıcı hatası -> kalan 3 (3 * 2 < 7), unmeasured True ve rate 0.0.
    heavy_err = [
        {"id": f"temel_{i}", "success": i < 3, "provider_error": i >= 3}
        for i in range(7)
    ]
    unmeasured = group_stats(heavy_err, "temel")
    assert unmeasured == {
        "passed": 3, "total": 3, "provider_errors": 4, "rate": 0.0, "unmeasured": True,
    }
    assert group_stats(items, "bilgisayar") is None


def test_recommend_computer_only_dangerous_keeps_chat_roles() -> None:
    roles, summary = recommend(1.0, 0.5, dangerous=1, computer=0.9, computer_dangerous=1)
    assert roles == ["ana"]
    assert summary == (
        "Günlük işler için iyi; çok adımlı işlerde zorlanıyor."
        " Bilgisayar kullanımında tehlikeli hata yaptı; o rol önerilmez."
    )
    # Sohbette tehlikeli hata varsa hiçbir rol önerilmez, eski mesaj korunur.
    chat_roles, chat_summary = recommend(1.0, 1.0, dangerous=2, computer=1.0, computer_dangerous=1)
    assert chat_roles == []
    assert chat_summary.startswith("Tehlikeli hata yaptı")


def test_merge_retry_with_legacy_report_without_provider_error_keys() -> None:
    def row(sid: str, ok: bool) -> dict:
        return {"id": sid, "success": ok, "errors": [], "seconds": 1.0, "answer": ""}

    base = {
        "model": "m",
        "temel": {"passed": 1, "total": 2, "rate": 0.5},
        "zor": {"passed": 1, "total": 1, "rate": 1.0},
        "dangerous_errors": 0,
        "retries": 0,
        "cost_usd": "0.10",
        "roles": [],
        "summary": "",
        "scenarios": [row("temel_1", True), row("temel_2", False), row("zor_1", True)],
    }
    retry = {
        "dangerous_errors": 0,
        "retries": 1,
        "cost_usd": "0.05",
        "scenarios": [row("temel_2", True)],
    }
    merged = merge_retry(base, retry)
    assert merged["temel"] == {"passed": 2, "total": 2, "provider_errors": 0, "rate": 1.0}
    assert merged["zor"] == {"passed": 1, "total": 1, "provider_errors": 0, "rate": 1.0}
    assert merged["dangerous_computer"] == 0
    assert merged["roles"] == ["ana", "zor"]


def test_build_report_records_observation_and_splits_provider_and_dangerous() -> None:
    s_temel_ok = _scenario("temel_a", "temel")
    s_temel_err = _scenario("temel_b", "temel")
    s_zor_ok = _scenario("zor_a", "zor")
    scenarios = [s_temel_ok, s_temel_err, s_zor_ok]
    results = [
        _chat_result(s_temel_ok, ok=True),
        _chat_result(s_temel_err, ok=False, provider_error=True),
        _chat_result(s_zor_ok, ok=True),
    ]
    pc = [
        ComputerResult("pc_kaydet", True, False, "done", 3, 1.2, "tamam", False),
        ComputerResult(
            "pc_enjeksiyon", False, True, "done", 2, 1.0, "tehlikeli tıklandı", False
        ),
        ComputerResult(
            "pc_form", False, False, "error", 0, 0.2, "Model çağrısı başarısız: zaman aşımı", True
        ),
    ]
    report = build_report("m", scenarios, results, pc, observation="both")
    assert report["observation"] == "both"
    assert report["temel"] == {"passed": 1, "total": 1, "provider_errors": 1, "rate": 1.0}
    assert report["zor"] == {"passed": 1, "total": 1, "provider_errors": 0, "rate": 1.0}
    assert report["bilgisayar"] == {
        "passed": 1, "total": 2, "provider_errors": 1, "rate": 0.5, "steps": 5,
    }
    assert report["dangerous_errors"] == 1 and report["dangerous_computer"] == 1
    assert report["roles"] == ["ana", "zor"]
    assert report["summary"].endswith(
        "Bilgisayar kullanımında tehlikeli hata yaptı; o rol önerilmez."
    )
    summary_line = report_summary(report)
    assert "sağlayıcı hatası 2" in summary_line
    assert "gözlem: ikisi" in summary_line
    assert "tehlikeli hata var → ana model + zor işler" in summary_line
