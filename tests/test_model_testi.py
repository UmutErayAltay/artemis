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
        config=ComputerUseConfig(
            enabled=True, mode="auto", step_delay_seconds=0, observation="screen"
        ),
        own_pid=1, stop=threading.Event(), on_step=lambda _l: None, provider_timeout=5,
    )
    result = asyncio.run(session.run("kaydet", "t", None))
    assert result.reason == "done" and sim.SCENARIOS[0].success(app, result.summary)


def _fake_result(
    scenario: Scenario, *, stop: str = "completed", answer: str = "",
    error_type: str | None = None,
):
    from decimal import Decimal

    from bench.run import ScenarioResult

    return ScenarioResult(
        id=scenario.id, category=scenario.category, success=stop == "completed",
        dangerous_errors=0, unapproved_side_effects=0, wrong_target_actions=0,
        tool_dispatches=0, tool_successes=0, tool_sent=0, errors=[], dangerous_reasons=[],
        steps=0, tool_calls=0, elapsed_seconds=0.0, input_tokens=0, output_tokens=0,
        cost_usd=Decimal("0"), approvals=[], answer=answer, stop_reason=stop,
        error_type=error_type,
    )


def _model_config(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(
        "primary_model: m\nmodels:\n  m: {provider: ollama, model: x,"
        " input_per_million: 0, output_per_million: 0}\n",
        encoding="utf-8",
    )
    return path


def test_parallel_keeps_order_and_respects_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import asyncio

    import bench.model_testi as mt

    running = peak = 0

    async def fake(scenario: Scenario, *_a: object, **_k: object):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep({"a": 0.06, "b": 0.01, "c": 0.03, "d": 0.0}[scenario.id])
        running -= 1
        return _fake_result(scenario)

    monkeypatch.setattr(mt, "run_scenario", fake)
    scenarios = [Scenario(id=i, category="multi_step", text="t", expected=Expected())
                 for i in "abcd"]
    report = asyncio.run(mt.run_model_test(
        _model_config(tmp_path), "m", scenarios=scenarios, computer=[], parallel=2,
    ))
    assert [s["id"] for s in report["scenarios"]] == list("abcd")
    assert peak == 2 and report["retries"] == 0
    lines = [x for x in capsys.readouterr().out.splitlines() if x.startswith("ilerleme")]
    assert [x.split()[1] for x in lines] == ["1/4", "2/4", "3/4", "4/4"]


@pytest.mark.parametrize(
    ("answer", "error_type", "calls"),
    [
        ("sınır (HTTP 429).", None, 3),  # yedek: metin eşleşmesi
        ("kota doldu", "RateLimitedError", 3),  # yapısal sinyal
        ("anahtar reddedildi", "ProviderError", 1),
        ("anahtar reddedildi", None, 1),
    ],
)
def test_rate_limit_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer: str, error_type: str | None,
    calls: int,
) -> None:
    import asyncio

    import bench.model_testi as mt

    waits: list[float] = []
    count = 0

    async def fake(scenario: Scenario, *_a: object, **_k: object):
        nonlocal count
        count += 1
        if count < 3 or calls == 1:
            return _fake_result(
                scenario, stop="provider_error", answer=answer, error_type=error_type
            )
        return _fake_result(scenario)

    async def no_sleep(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(mt, "run_scenario", fake)
    monkeypatch.setattr(mt, "_sleep", no_sleep)
    scenario = Scenario(id="a", category="multi_step", text="t", expected=Expected())
    report = asyncio.run(mt.run_model_test(
        _model_config(tmp_path), "m", scenarios=[scenario], computer=[],
    ))
    assert count == calls
    if calls == 3:
        assert waits == [2.0, 4.0] and report["retries"] == 2
        assert report["scenarios"][0]["success"]
    else:
        assert waits == [] and report["retries"] == 0


def test_merge_retry_updates_counts_and_roles() -> None:
    from bench.model_testi import merge_retry

    def item(key: str, ok: bool) -> dict:
        return {"id": key, "success": ok, "errors": [], "seconds": 1.0, "answer": ""}

    base = {
        "model": "m", "temel": {"passed": 1, "total": 2, "rate": 0.5},
        "zor": {"passed": 1, "total": 1, "rate": 1.0}, "dangerous_errors": 0, "retries": 0,
        "cost_usd": "0.5", "roles": [], "summary": "",
        "scenarios": [item("temel_a", True), item("temel_b", False), item("zor_c", True)],
    }
    retry = {"dangerous_errors": 0, "retries": 1, "cost_usd": "0.25",
             "scenarios": [item("temel_b", True)]}
    merged = merge_retry(base, retry)
    assert merged["temel"] == {"passed": 2, "total": 2, "rate": 1.0, "provider_errors": 0}
    assert merged["roles"] == ["ana", "zor"] and merged["cost_usd"] == "0.75"
    assert merged["scenarios"][1]["attempt"] == 2 and merged["retries"] == 1
    assert "attempt" not in merged["scenarios"][0] and base["temel"]["passed"] == 1


def test_all_provider_errors_report_cannot_test_not_model_failure() -> None:
    # 2026-11-16: anahtar alt sürece ulaşmayınca rapor "yeterince başarılı değil" diyordu;
    # model hiç çağrılmadan düşen test modelin başarısızlığı gibi gösterilmemeli.
    from bench.model_testi import build_report

    scenarios = load_scenarios(SCENARIOS_PATH)
    answer = "Model sağlayıcısı yanıt veremedi: 'gemini' ucunun anahtarı bulunamadı."
    results = [_fake_result(item, stop="provider_error", answer=answer) for item in scenarios]
    report = build_report("m", scenarios, results)
    assert report["summary"].startswith("Test yapılamadı:")
    assert "anahtarı bulunamadı" in report["summary"]
    assert report["roles"] == []


def test_partial_provider_error_keeps_normal_summary() -> None:
    from bench.model_testi import build_report

    scenarios = load_scenarios(SCENARIOS_PATH)
    results = [_fake_result(scenarios[0], stop="provider_error", answer="x")] + [
        _fake_result(item) for item in scenarios[1:]
    ]
    assert not build_report("m", scenarios, results)["summary"].startswith("Test yapılamadı")


def test_child_environment_passes_credential_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    from artemis.tools.platform import child_environment

    monkeypatch.setenv("ARTEMIS_CREDENTIAL_PREFIX", "Artemis-test")
    monkeypatch.setenv("OPENROUTER_API_KEY", "gizli-test")
    env = child_environment()
    assert env["ARTEMIS_CREDENTIAL_PREFIX"] == "Artemis-test"
    assert "OPENROUTER_API_KEY" not in env  # sırlar yine geçmez


def test_sim_backend_exposes_uia_elements_and_works_in_uia_mode(tmp_path: Path) -> None:
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

    app = sim._save_dialog()
    backend = sim.SimBackend(app)
    elements = backend.ui_elements(1)
    assert [e.name for e in elements[:4]] == [
        "Belge1.txt dosyasındaki değişiklikleri kaydetmek istiyor musunuz?",
        "Kaydet", "Kaydetme", "İptal",
    ]
    assert elements[1].kind == "Button" and elements[1].interactive is True
    assert elements[0].interactive is False

    click_el = {"type": "click_element", "element": 2, "reason": "Kaydet düğmesi"}
    done = {"type": "done", "text": "Belge1.txt kaydedildi.", "reason": "bitti"}
    verdict = {"verified": True, "evidence": "Belge1.txt kaydedildi yazıyor"}
    provider = ScriptedProvider([Completion(content=_json.dumps(item))
                                 for item in (click_el, done, verdict)])
    session = ComputerSession(
        provider=provider, backend=backend, approve=lambda *_: True,
        audit=AuditLog(tmp_path / "audit.jsonl"),
        config=ComputerUseConfig(
            enabled=True, mode="auto", step_delay_seconds=0, observation="uia"
        ),
        own_pid=1, stop=threading.Event(), on_step=lambda _l: None, provider_timeout=5,
    )
    result = asyncio.run(session.run("kaydet", "t", None))
    assert result.reason == "done" and sim.SCENARIOS[0].success(app, result.summary)


def test_sim_app_scrolls_with_keyboard_keys() -> None:
    from bench import bilgisayar_testi as sim

    long = sim._scroll_list()
    assert long.scroll == 0
    long.press(("pagedown",))
    assert long.scroll > 0
    before_end = long.scroll
    long.press(("end",))
    assert long.scroll >= before_end
    long.press(("home",))
    assert long.scroll == 0
