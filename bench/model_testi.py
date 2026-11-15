"""Model testi: sohbet modelini 15 sohbet + 10 bilgisayar kullanımı senaryosunda ölçer, rol önerir.

Ayarlar → Modeller'deki "Test et" bu modülü ayrı süreçte çalıştırır. Senaryolar gerçek
makineye dokunmaz (geçici masaüstü, sahte web/Telegram/masaüstü; bench.run; bilgisayar
kullanımı Qt ile ekran dışında çizilen sahte uygulamalarda, bench.bilgisayar_testi). Ücretli modelde
test yalnız `--ucretli-onay` ile, gösterilen bütçe tavanıyla çalışır (CLAUDE.md: ücretli çağrı
açık izinle). Her senaryo bitince stdout'a bir `ilerleme` satırı, sonda JSON rapor yazılır.

Kullanım: python -m bench.model_testi --config config.yaml --model "haiku 5-5" [--rapor r.json]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
from collections.abc import Awaitable, Callable, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

from artemis.brain.errors import ProviderError, RateLimitedError
from artemis.brain.model_report import (
    CHAT_SCENARIO_IDS,
    COMPUTER_SCENARIO_IDS,
    GROUP_PREFIXES,
    recommend,
)
from artemis.brain.model_report import (
    merge_retry as merge_retry,
)
from artemis.brain.model_report import (
    suggest_layout as suggest_layout,
)
from artemis.config.settings import load_settings
from bench.bilgisayar_testi import SCENARIOS as COMPUTER_SCENARIOS
from bench.bilgisayar_testi import ComputerResult, ComputerScenario, run_computer_scenario
from bench.run import Scenario, ScenarioResult, load_scenarios, run_scenario

SCENARIOS_PATH = Path(__file__).with_name("model_testi.yaml")


def _check_catalog() -> None:
    """Arayüzün senaryo listesi (artemis.brain.model_report) gerçek senaryolarla ayrışmasın."""
    chat = load_scenarios(SCENARIOS_PATH)
    if tuple(item.id for item in chat) != CHAT_SCENARIO_IDS or any(
        GROUP_PREFIXES.get(item.id.partition("_")[0]) != item.level for item in chat
    ):
        raise RuntimeError("model_report.CHAT_SCENARIO_IDS model_testi.yaml ile uyuşmuyor")
    if tuple(item.id for item in COMPUTER_SCENARIOS) != COMPUTER_SCENARIO_IDS:
        raise RuntimeError("model_report.COMPUTER_SCENARIO_IDS bilgisayar senaryolarıyla uyuşmuyor")


RATE_LIMIT_BACKOFF = (2.0, 4.0, 8.0)  # 429'da en çok 3 yeniden deneme
RETRY_AFTER_CAP = 30.0
MAX_PARALLEL = 8
# Testlerde yama ile bekleme atlanır.
_sleep = asyncio.sleep


def _level(
    results: Sequence[ScenarioResult], scenarios: Sequence[Scenario], level: str
) -> dict[str, Any]:
    ids = {item.id for item in scenarios if item.level == level}
    picked = [result for result in results if result.id in ids]
    passed = sum(result.success for result in picked)
    return {"passed": passed, "total": len(picked),
            "rate": round(passed / len(picked), 3) if picked else 0.0}


def build_report(
    alias: str, scenarios: Sequence[Scenario], results: Sequence[ScenarioResult],
    computer: Sequence[ComputerResult] = (), retries: int = 0,
) -> dict[str, Any]:
    temel, zor = _level(results, scenarios, "temel"), _level(results, scenarios, "zor")
    dangerous = sum(result.dangerous_errors for result in results) + sum(
        result.dangerous for result in computer
    )
    pc = (
        {"passed": sum(item.success for item in computer), "total": len(computer),
         "rate": round(sum(item.success for item in computer) / len(computer), 3),
         "steps": sum(item.steps for item in computer)}
        if computer else None
    )
    roles, summary = recommend(temel["rate"], zor["rate"], dangerous, pc["rate"] if pc else None)
    times = [result.elapsed_seconds for result in results] or [0.0]
    return {
        "model": alias,
        "temel": temel,
        "zor": zor,
        **({"bilgisayar": pc} if pc else {}),
        "dangerous_errors": dangerous,
        "retries": retries,
        "median_seconds": round(statistics.median(times), 1),
        "input_tokens": sum(result.input_tokens for result in results),
        "cost_usd": str(sum((result.cost_usd for result in results), Decimal("0"))),
        "roles": roles,
        "summary": summary,
        "scenarios": [
            {"id": result.id, "success": result.success,
             "errors": result.errors + result.dangerous_reasons,
             "seconds": round(result.elapsed_seconds, 1), "answer": result.answer[:300]}
            for result in results
        ] + [
            {"id": item.id, "success": item.success,
             "errors": [] if item.success else [f"{item.reason}: {item.summary}"],
             "seconds": item.seconds, "answer": item.summary, "steps": item.steps}
            for item in computer
        ],
    }


def _provider_failure(scenario: Scenario, error: ProviderError) -> ScenarioResult:
    # Sağlayıcı hatası (kota, bağlantı) senaryoyu başarısız sayar; test sürer.
    return ScenarioResult(
        id=scenario.id, category=scenario.category, success=False,
        dangerous_errors=0, unapproved_side_effects=0, wrong_target_actions=0,
        tool_dispatches=0, tool_successes=0, tool_sent=0,
        errors=[f"Sağlayıcı hatası: {type(error).__name__}"], dangerous_reasons=[],
        steps=0, tool_calls=0, elapsed_seconds=0.0, input_tokens=0, output_tokens=0,
        cost_usd=Decimal("0"), approvals=[], answer="", stop_reason="provider_error",
    )


async def _run_with_retry(
    run: Callable[[], Awaitable[ScenarioResult]], scenario: Scenario
) -> tuple[ScenarioResult, int]:
    """Yalnız HTTP 429 (RateLimitedError) ile düşen senaryoyu 2/4/8 sn beklemeyle yeniden dener.

    Ajan döngüsü sağlayıcı hatasını `provider_error` turu olarak döndürür; 429 o durumda
    `error_type` alanıyla (yedek: cevap metnindeki "HTTP 429") anlaşılır.
    Diğer hatalar denenmez. Döner: (sonuç, deneme sayısı)."""
    for attempt in range(len(RATE_LIMIT_BACKOFF) + 1):
        wait = RATE_LIMIT_BACKOFF[attempt] if attempt < len(RATE_LIMIT_BACKOFF) else None
        try:
            result = await run()
        except RateLimitedError as error:
            if wait is None:
                return _provider_failure(scenario, error), attempt
            hint = getattr(error, "retry_after", None)
            wait = min(float(hint), RETRY_AFTER_CAP) if isinstance(hint, int | float) else wait
        except ProviderError as error:
            return _provider_failure(scenario, error), attempt
        else:
            limited = result.error_type == "RateLimitedError" or "HTTP 429" in result.answer
            if result.stop_reason != "provider_error" or not limited:
                return result, attempt
            if wait is None:
                return result, attempt
        await _sleep(wait)
    raise AssertionError("ulaşılamaz")


async def run_model_test(
    config: Path, alias: str, *, allow_paid: bool = False, budget_usd: Decimal = Decimal("0"),
    scenarios: Sequence[Scenario] | None = None, hard_alias: str | None = None,
    computer: Sequence[ComputerScenario] | None = None, parallel: int = 1,
) -> dict[str, Any]:
    settings = load_settings(config)
    picked = [settings.models.get(name) for name in (alias, hard_alias) if name]
    if any(item is None or item.kind != "chat" for item in picked):
        raise ValueError("Test edilecek model ayarlarda tanımlı bir sohbet modeli olmalı.")
    if any(item and (item.input_per_million or item.output_per_million) for item in picked) and (
        not allow_paid
    ):
        raise ValueError("Ücretli model: test için açık onay (--ucretli-onay) gerekli.")
    model = settings.models[alias]
    hard = settings.models[hard_alias] if hard_alias else None
    chosen = list(scenarios) if scenarios is not None else load_scenarios(SCENARIOS_PATH)
    pc_chosen = list(COMPUTER_SCENARIOS if computer is None else computer)
    total = len(chosen) + len(pc_chosen)
    gate = asyncio.Semaphore(max(1, min(parallel, MAX_PARALLEL)))
    done = 0
    retries = 0

    async def one(scenario: Scenario) -> ScenarioResult:
        nonlocal done, retries
        async with gate:
            result, used = await _run_with_retry(
                lambda: run_scenario(
                    scenario, model, live=True, endpoints=dict(settings.endpoints),
                    allow_paid=allow_paid, budget_usd=budget_usd, hard_model=hard,
                ),
                scenario,
            )
        retries += used
        done += 1
        mark = "geçti" if result.success else "kaldı"
        print(f"ilerleme {done}/{total} {scenario.id} {mark}", flush=True)
        return result

    # gather sırayı korur: rapor senaryo sırasıyla kalır. Her senaryo kendi Settings'ine ve
    # bütçe tavanına sahiptir (paylaşılan harcama sayacı yok); paralellik tavanı artırmaz.
    results = list(await asyncio.gather(*(one(item) for item in chosen)))
    pc_results: list[ComputerResult] = []
    # Bilgisayar senaryoları hep sırayla: ortak ekran dışı QApplication ve modül globalleri var.
    for index, item in enumerate(pc_chosen, len(chosen) + 1):
        try:
            pc_result = await run_computer_scenario(item, settings, alias)
        except ProviderError as error:
            pc_result = ComputerResult(item.id, False, False, "error", 0, 0.0,
                                       f"Sağlayıcı hatası: {type(error).__name__}")
        pc_results.append(pc_result)
        mark = "geçti" if pc_result.success else "kaldı"
        print(f"ilerleme {index}/{total} {item.id} {mark}", flush=True)
    return build_report(
        f"{alias} + {hard_alias}" if hard_alias else alias, chosen, results, pc_results, retries
    )


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--model", required=True, help="config'teki model anahtarı")
    parser.add_argument("--rapor", type=Path, help="JSON raporu ayrıca bu dosyaya yazılır")
    parser.add_argument("--ucretli-onay", action="store_true")
    parser.add_argument("--butce", type=Decimal, default=Decimal("0"))
    parser.add_argument("--senaryo", action="append", help="yalnız bu kimlik(ler)")
    parser.add_argument("--zor-model", help="kademe ölçümü: zor işler modeli anahtarı")
    parser.add_argument("--paralel", type=int, default=1,
                        help="aynı anda çalışacak sohbet senaryosu (1-8)")
    parser.add_argument("--bilgisayar-yok", action="store_true",
                        help="bilgisayar kullanımı senaryolarını atla")
    args = parser.parse_args(arguments)
    chosen = load_scenarios(SCENARIOS_PATH)
    pc_chosen = [] if args.bilgisayar_yok else list(COMPUTER_SCENARIOS)
    if args.senaryo:
        chosen = [item for item in chosen if item.id in set(args.senaryo)]
        pc_chosen = [item for item in pc_chosen if item.id in set(args.senaryo)]
    try:
        report = asyncio.run(run_model_test(
            args.config, args.model, allow_paid=args.ucretli_onay, budget_usd=args.butce,
            scenarios=chosen, hard_alias=args.zor_model, computer=pc_chosen,
            parallel=max(1, min(args.paralel, MAX_PARALLEL)),
        ))
    except (OSError, ValueError) as error:
        print(f"Model testi yapılamadı: {error}", file=sys.stderr)
        return 2
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.rapor:
        args.rapor.parent.mkdir(parents=True, exist_ok=True)
        args.rapor.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
