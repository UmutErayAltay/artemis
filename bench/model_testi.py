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
from artemis.brain.light_mode import light_mode_on
from artemis.brain.model_report import (
    CHAT_SCENARIO_IDS,
    COMPUTER_SCENARIO_IDS,
    GROUP_PREFIXES,
    group_stats,
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
LIGHT_CHOICES: dict[str, bool | None] = {"oto": None, "ac": True, "kapali": False}
# Testlerde yama ile bekleme atlanır.
_sleep = asyncio.sleep


def build_report(
    alias: str, scenarios: Sequence[Scenario], results: Sequence[ScenarioResult],
    computer: Sequence[ComputerResult] = (), retries: int = 0, *, observation: str = "uia",
) -> dict[str, Any]:
    scenario_items = [
        {"id": result.id, "success": result.success,
         "dangerous": bool(result.dangerous_errors),
         "provider_error": result.stop_reason == "provider_error",
         "errors": result.errors + result.dangerous_reasons,
         "seconds": round(result.elapsed_seconds, 1), "answer": result.answer[:300]}
        for result in results
    ] + [
        {"id": item.id, "success": item.success, "dangerous": item.dangerous,
         "provider_error": item.provider_error,
         "errors": [] if item.success else [f"{item.reason}: {item.summary}"],
         "seconds": item.seconds, "answer": item.summary, "steps": item.steps}
        for item in computer
    ]
    empty = {"passed": 0, "total": 0, "provider_errors": 0, "rate": 0.0}
    temel = group_stats(scenario_items, "temel") or dict(empty)
    zor = group_stats(scenario_items, "zor") or dict(empty)
    pc = group_stats(scenario_items, "bilgisayar")
    dangerous_computer = sum(item.dangerous for item in computer)
    dangerous = sum(result.dangerous_errors for result in results) + dangerous_computer
    roles, summary = recommend(
        temel["rate"], zor["rate"], dangerous, pc["rate"] if pc else None, dangerous_computer
    )
    # Hiçbir senaryo modele ulaşamadıysa (anahtar yok, bağlantı yok) bu modelin başarısızlığı
    # değildir: "yeterince başarılı değil" yerine nedeni söylenir, rol önerilmez.
    failed = [result for result in results if result.stop_reason == "provider_error"]
    if results and len(failed) == len(results):
        reason = (failed[0].answer or "; ".join(failed[0].errors) or "sağlayıcı hatası").strip()
        roles, summary = [], f"Test yapılamadı: {reason[:200]}"
    times = [result.elapsed_seconds for result in results] or [0.0]
    return {
        "model": alias,
        "observation": observation,
        "temel": temel,
        "zor": zor,
        **({"bilgisayar": pc} if pc else {}),
        "dangerous_errors": dangerous,
        "dangerous_computer": dangerous_computer,
        "retries": retries,
        "median_seconds": round(statistics.median(times), 1),
        "input_tokens": sum(result.input_tokens for result in results),
        "cost_usd": str(sum((result.cost_usd for result in results), Decimal("0"))),
        "roles": roles,
        "summary": summary,
        "scenarios": scenario_items,
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
    light: str = "oto",
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
    # ADR 0092: --hafif ac/kapali yalnız bu testin kopyasını değiştirir; config'e yazılmaz.
    if light in LIGHT_CHOICES and LIGHT_CHOICES[light] is not None:
        model = model.model_copy(update={"light_mode": LIGHT_CHOICES[light]})
        hard = hard.model_copy(update={"light_mode": LIGHT_CHOICES[light]}) if hard else None
    effective_light = {
        name: light_mode_on(item, settings.endpoints)
        for name, item in ((alias, model), (hard_alias, hard)) if name and item
    }
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
                    provider_timeout_seconds=settings.provider_timeout_seconds,
                    max_seconds=settings.max_seconds,
                ),
                scenario,
            )
        retries += used
        done += 1
        mark = "tehlikeli" if result.dangerous_errors else "geçti" if result.success else "kaldı"
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
            pc_result = ComputerResult(
                item.id, False, False, "error", 0, 0.0,
                f"Sağlayıcı hatası: {type(error).__name__}", provider_error=True,
            )
        pc_results.append(pc_result)
        mark = "tehlikeli" if pc_result.dangerous else "geçti" if pc_result.success else "kaldı"
        print(f"ilerleme {index}/{total} {item.id} {mark}", flush=True)
    report = build_report(
        f"{alias} + {hard_alias}" if hard_alias else alias, chosen, results, pc_results, retries,
        observation=settings.computer_use.observation,
    )
    report["light_mode"] = {"requested": light, "effective": effective_light}
    return report


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
    parser.add_argument("--hafif", choices=list(LIGHT_CHOICES), default="oto",
                        help="hafif model kipi: oto (config/otomatik), ac, kapali (ADR 0092)")
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
            parallel=max(1, min(args.paralel, MAX_PARALLEL)), light=args.hafif,
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
