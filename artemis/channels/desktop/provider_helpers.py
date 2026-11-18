"""ProviderSection'ın Qt'siz saf yardımcıları (2026-11-18: dosya boyutu sınırı için ayrıldı)."""

from __future__ import annotations

import html
import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlsplit

from artemis.brain.model_report import SCENARIO_IDS, pass_rate, report_summary
from artemis.config.settings import EndpointConfig, ModelConfig, Settings

CLI_NAME = "Claude aboneliği"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

ModelRow = tuple[str, bool, bool, str, str, str, bool, str]


def connection_label(model: ModelConfig) -> str:
    if model.provider == "openai_compatible":
        return str(model.endpoint)
    return CLI_NAME if model.provider == "claude_cli" else model.provider


def _is_local_model(model: ModelConfig, endpoints: Mapping[str, EndpointConfig]) -> bool:
    if model.provider == "ollama":
        return True
    endpoint = endpoints.get(model.endpoint or "")
    return endpoint is not None and urlsplit(endpoint.base_url).hostname in LOCAL_HOSTS


def _scenario_ids() -> list[str]:
    return list(SCENARIO_IDS)


def _scenario_count() -> int:
    return len(_scenario_ids())


def _scenario_label(scenario_id: str) -> str:
    """'temel_dosya_oku' -> 'dosya oku' (kategori öneki ve alt çizgiler görünmez)."""
    for prefix in ("temel_", "zor_", "pc_"):
        if scenario_id.startswith(prefix):
            scenario_id = scenario_id[len(prefix):]
    return scenario_id.replace("_", " ")


def _slug(text: str) -> str:
    # ponytail: "a/b" ile "a-b" aynı rapor dosyasına düşer; takma adlar okunur kalsın diye
    # bilinçli (2026-11-18). Çakışma ölçülürse rapor adına ayrı özet ekle.
    return re.sub(r"[^A-Za-z0-9_-]", "-", text).strip("-")[:40]


def _write_json(path: Path, data: object) -> None:
    """Geçici dosyaya yazıp yerine koyar: yarım rapor kalmaz."""
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def _score(report: dict[str, object]) -> str:
    """Listede tek sayı: geçen senaryoların yüzdesi ('%87'); tehlikeli hata metinle belirtilir."""
    rate = pass_rate(report)
    if rate is None:
        return ""
    return f"%{round(100 * rate)}" + (
        " · tehlikeli hata" if report.get("dangerous_errors") else ""
    )


def filter_model_rows(rows: list[ModelRow], kind_filter: object) -> list[ModelRow]:
    """Model listesini türe göre süzer ('all' iken tümü kalır)."""
    if not kind_filter or kind_filter == "all":
        return list(rows)
    return [row for row in rows if row[5] == kind_filter]


def sort_model_rows(
    rows: list[ModelRow],
    key: object,
    reports: Mapping[str, dict[str, object]],
    fallback: str,
) -> list[ModelRow]:
    """Model satırlarını seçilen anahtara göre sıralar."""
    if key == "score":
        def score(row: ModelRow) -> float:
            report = reports.get(row[0])
            rate = pass_rate(report) if report else None
            if report is None or rate is None:
                return 2.0  # test edilmemiş en sonda
            # Tehlikeli hatalı model aynı yüzdedeki güvenli modelin altına iner.
            return -rate + (1.0 if report.get("dangerous_errors") else 0.0)
        return sorted(rows, key=score)
    if key == "connection":
        return sorted(rows, key=lambda row: (row[3].casefold(), row[0].casefold()))
    if key == "role":
        return sorted(rows, key=lambda row: (not row[1], not row[6], row[0] != fallback))
    if key == "name":
        return sorted(rows, key=lambda row: row[0].casefold())
    return list(rows)


def load_reports(settings: Settings) -> dict[str, dict[str, object]]:
    """Kaydedilmiş son test raporları (takma ad -> rapor); okunamayan rapor yok sayılır."""
    folder = settings.state_dir / "model_testleri"
    found: dict[str, dict[str, object]] = {}
    for alias in settings.models:
        path = folder / f"{_slug(alias) or 'model'}.json"
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        # Eksik/bozuk rapor öneriyi (suggest_layout) çökertmesin: alanlar doğrulanır.
        if (
            isinstance(report, dict)
            and report.get("model") == alias
            and isinstance(report.get("zor"), dict)
            and isinstance(report["zor"].get("rate"), int | float)
            and isinstance(report.get("median_seconds"), int | float)
            and isinstance(report.get("roles"), list)
        ):
            found[alias] = report
    return found


def build_model_rows(
    settings: Settings, reports: Mapping[str, dict[str, object]]
) -> list[ModelRow]:
    return [
        (
            alias,
            alias == settings.primary_model,
            # Eski openrouter/anthropic/ollama modelleri (CPA yolu) taşınmaz; kullanıcı siler.
            model.provider not in {"openai_compatible", "claude_cli"},
            connection_label(model),
            model.model,
            model.kind,
            alias == settings.hard_model,
            report_summary(reports[alias]) if alias in reports else "",
        )
        for alias, model in settings.models.items()
    ]


def layout_suggestion_text(
    suggested: tuple[str | None, str | None, str | None],
    current: tuple[str, str | None, str | None],
) -> str:
    ana, zor, pc = suggested
    if not ana:
        return ""
    return (
        f"Önerilen düzen (test sonuçlarına göre): Ana: {ana}"
        + (f" · Zor işler: {zor}" if zor else "")
        + (f" · Bilgisayar kullanımı: {pc}" if pc else "")
        + (" — şu anki düzen bu." if suggested == current else "")
    )


def model_row_html(
    row: ModelRow,
    colors: Mapping[str, str],
    fallback: str,
    testing: str,
    reports: Mapping[str, dict[str, object]],
) -> str:
    alias, is_brain, legacy, connection, model_id, model_kind, hard, _tested = row
    roles = [
        f"<span style='color:{colors['accent']}'>{name}</span>"
        for name, on in (("Beyin", is_brain), ("Zor işler", hard)) if on
    ]
    if alias == fallback and not is_brain:
        roles.append(f"<span style='color:{colors['muted']}'>Yedek</span>")
    muted = colors["muted"]
    role_text = " · ".join(roles) or f"<span style='color:{muted}'>Rol yok</span>"
    score = (
        "Test sürüyor…" if alias == testing
        else _score(reports[alias]) if alias in reports
        else "test yok"
    )
    tag = (
        f"&nbsp;&nbsp;<span style='color:{colors['warning']}'>eski ayar</span>"
        if legacy else ""
    )
    kind = (
        " · resim" if model_kind == "image"
        else " · ses" if model_kind == "speech"
        else ""
    )
    return (
        "<table width='100%' cellspacing='0'><tr>"
        f"<td><b>{html.escape(alias)}</b>{tag}<br>"
        f"<span style='color:{muted}'>"
        f"{html.escape(connection)} · {html.escape(model_id)}{kind}</span></td>"
        f"<td align='right'>{role_text}<br>"
        f"<span style='color:{muted}'>{html.escape(score)}</span></td>"
        "</tr></table>"
    )
