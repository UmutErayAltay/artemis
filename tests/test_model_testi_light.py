"""Model testi: --hafif anahtarı ve config'ten gelen süreler (ADR 0092)."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import pytest

import bench.model_testi as mt
import bench.run as bench_run
from artemis.brain.fake import ScriptedProvider
from artemis.brain.models import Completion
from artemis.config.settings import ModelConfig, Settings
from bench.model_testi import LIGHT_CHOICES
from bench.run import Expected, Scenario
from tests.test_model_testi import _fake_result


def _config(tmp_path: Path, provider: str = "ollama", extra: str = "") -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(
        "primary_model: m\nprovider_timeout_seconds: 7\nmax_seconds: 33\nmodels:\n"
        f"  m: {{provider: {provider}, model: x, input_per_million: 0, output_per_million: 0"
        f"{extra}}}\n",
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize(
    ("flag", "config_extra", "expected"),
    [
        ("oto", "", True),  # ollama: otomatik açık
        ("kapali", "", False),
        ("ac", "", True),
        ("oto", ", light_mode: false", False),  # config'teki açık değer korunur
        ("ac", ", light_mode: false", True),  # bayrak config'i ezer
    ],
)
def test_hafif_flag_overrides_and_report_records_effective_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flag: str, config_extra: str,
    expected: bool,
) -> None:
    seen: list[ModelConfig] = []

    async def fake(scenario: Scenario, model: ModelConfig, **_k: Any) -> Any:
        seen.append(model)
        return _fake_result(scenario)

    monkeypatch.setattr(mt, "run_scenario", fake)
    scenario = Scenario(id="a", category="multi_step", text="t", expected=Expected())
    report = asyncio.run(mt.run_model_test(
        _config(tmp_path, extra=config_extra), "m", scenarios=[scenario], computer=[],
        light=flag,
    ))
    assert report["light_mode"] == {"requested": flag, "effective": {"m": expected}}
    assert seen[0].light_mode is (LIGHT_CHOICES[flag] if flag != "oto" else seen[0].light_mode)


def test_scenario_settings_carry_config_timings_and_local_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[Settings] = []

    def fake_build(settings: Settings, audit: object) -> ScriptedProvider:
        captured.append(settings)
        return ScriptedProvider([Completion(content="tamam")])

    monkeypatch.setattr(bench_run, "build_provider", fake_build)
    scenario = Scenario(id="a", category="multi_step", text="merhaba", expected=Expected())

    def model(provider: Literal["ollama", "openrouter"]) -> ModelConfig:
        return ModelConfig(provider=provider, model="x", input_per_million=Decimal(0),
                           output_per_million=Decimal(0))

    for provider in ("ollama", "openrouter"):
        monkeypatch.setenv("OPENROUTER_API_KEY", "x" * 8)
        asyncio.run(bench_run.run_scenario(
            scenario, model(provider), live=True, provider_timeout_seconds=7.0, max_seconds=33.0,
        ))
    local, cloud = captured
    assert local.max_seconds == 600.0 and local.provider_timeout_seconds == 7.0
    assert cloud.max_seconds == 33.0 and cloud.provider_timeout_seconds == 7.0
