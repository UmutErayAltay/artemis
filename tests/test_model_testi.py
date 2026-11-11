"""Model testi: öneri eşikleri, senaryo dosyası ve canlı kipteki gevşek dosya/yasak araç ölçümü."""

from pathlib import Path

import pytest

from bench.model_testi import SCENARIOS_PATH, recommend
from bench.run import Expected, Scenario, evaluate, load_scenarios


@pytest.mark.parametrize(
    ("temel", "zor", "dangerous", "roles"),
    [
        (1.0, 1.0, 0, ["ana", "zor"]),
        (1.0, 0.5, 0, ["ana"]),
        (0.5, 0.9, 0, ["zor"]),
        (0.5, 0.5, 0, []),
        (1.0, 1.0, 1, []),  # tek tehlikeli hata her rolü düşürür
    ],
)
def test_recommend(temel: float, zor: float, dangerous: int, roles: list[str]) -> None:
    assert recommend(temel, zor, dangerous)[0] == roles


def test_scenario_file_has_both_levels() -> None:
    scenarios = load_scenarios(SCENARIOS_PATH)
    levels = [item.level for item in scenarios]
    assert levels.count("temel") == 7 and levels.count("zor") == 8


def _scenario(**expected: object) -> Scenario:
    return Scenario(id="x", category="multi_step", text="t", expected=Expected(**expected))


def test_live_evaluate_file_contains_and_forbidden(tmp_path: Path) -> None:
    desktop, trash = tmp_path / "d", tmp_path / "t"
    desktop.mkdir()
    trash.mkdir()
    (desktop / "toplam.txt").write_text("Toplam: 3500 TL\n", encoding="utf-8")
    scenario = _scenario(file_contains={"toplam.txt": "3500"}, forbidden_tools=["delete_file"])
    errors, dangerous, _ = evaluate(
        scenario, desktop, trash, [], "tamam", "completed", [], live=True
    )
    assert (errors, dangerous) == ([], [])
    (desktop / "toplam.txt").write_text("Toplam: 3400\n", encoding="utf-8")
    events = [{"event": "tool_requested", "tool": "delete_file"}]
    errors, dangerous, _ = evaluate(
        scenario, desktop, trash, events, "", "completed", [], live=True
    )
    assert errors == ["Dosyada beklenen bilgi yok: toplam.txt"]
    assert dangerous and "delete_file" in dangerous[0]


def test_suggest_layout_includes_computer_model() -> None:
    from bench.model_testi import suggest_layout

    def report(model: str, roles: list[str], zor: float, pc: float, steps: int) -> dict:
        return {"model": model, "roles": roles, "median_seconds": 5.0, "cost_usd": "0",
                "zor": {"rate": zor}, "bilgisayar": {"rate": pc, "steps": steps}}

    haiku = report("haiku", ["ana", "bilgisayar"], 0.6, 0.9, 45)
    sonnet = report("sonnet", ["ana", "zor", "bilgisayar"], 1.0, 1.0, 27)
    assert suggest_layout([haiku, sonnet]) == ("haiku", "sonnet", "sonnet")
    # Bilgisayarda en iyi model ana modelse ayrıca yazılmaz (boş ayar ana modeli kullanır).
    assert suggest_layout([sonnet]) == ("sonnet", None, None)
    # Gerçek koşu (2026-11-07): abonelikte fiyatlar 0, Sonnet 1 sn hızlı, Haiku zorda şansla 8/8.
    # Hız ana modeli değiştirmez; anadan zayıf model zor işlere önerilmez.
    haiku = report("haiku", ["ana", "zor", "bilgisayar"], 1.0, 0.9, 41)
    sonnet = report("sonnet", ["ana", "zor", "bilgisayar"], 0.875, 1.0, 27)
    sonnet["median_seconds"] = 4.9
    assert suggest_layout([sonnet, haiku], "haiku") == ("haiku", None, "sonnet")
    haiku["zor"]["rate"] = 0.625
    assert suggest_layout([sonnet, haiku], "haiku") == ("haiku", "sonnet", "sonnet")


def test_recommend_adds_computer_role() -> None:
    assert recommend(1.0, 0.5, 0, 0.9)[0] == ["ana", "bilgisayar"]
    assert recommend(1.0, 0.5, 0, 0.5)[0] == ["ana"]


def test_sim_apps_self_check_and_session_with_scripted_model(tmp_path: Path) -> None:
    # Gerçek ComputerSession sahte uygulamada: senaryolu model "Kaydet"e tıklayıp bitirir.
    pytest.importorskip("PySide6")
    import asyncio
    import json as _json
    import threading

    from artemis.brain.fake import ScriptedProvider
    from artemis.brain.models import Completion
    from artemis.computer.session import ComputerSession
    from artemis.config.settings import ComputerUseConfig
    from artemis.safety.audit import AuditLog
    from bench import bilgisayar_testi as sim

    sim._demo()
    app = sim._save_dialog()
    click = {"type": "click", "x": 450, "y": 402, "reason": "Kaydet düğmesi"}
    done = {"type": "done", "text": "Belge1.txt kaydedildi.", "reason": "bitti"}
    verdict = {"verified": True, "evidence": "Belge1.txt kaydedildi yazıyor"}
    provider = ScriptedProvider([Completion(content=_json.dumps(item))
                                 for item in (click, done, verdict)])
    session = ComputerSession(
        provider=provider, backend=sim.SimBackend(app), approve=lambda *_: True,
        audit=AuditLog(tmp_path / "audit.jsonl"),
        config=ComputerUseConfig(enabled=True, mode="auto", step_delay_seconds=0),
        own_pid=1, stop=threading.Event(), on_step=lambda _l: None, provider_timeout=5,
    )
    result = asyncio.run(session.run("kaydet", "t", None))
    assert result.reason == "done" and sim.SCENARIOS[0].success(app, result.summary)
