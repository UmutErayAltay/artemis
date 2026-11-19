"""ProviderSection'ın Qt'siz saf yardımcıları (2026-11-18: dosya boyutu sınırı için ayrıldı)."""

from __future__ import annotations

import html
import json
import os
import re
from collections.abc import Callable, Mapping
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from artemis.brain.model_report import SCENARIO_IDS, pass_rate, report_summary
from artemis.brain.providers import credential_prefix, endpoint_key_variable
from artemis.config.settings import EndpointConfig, ModelConfig, Settings
from artemis.i18n import t

CLI_NAME = "Claude aboneliği"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

ModelRow = tuple[str, bool, bool, str, str, str, bool, str]


def connection_label(model: ModelConfig) -> str:
    if model.provider == "openai_compatible":
        return str(model.endpoint)
    return t(CLI_NAME) if model.provider == "claude_cli" else model.provider


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
    return t("%{pct}").format(pct=round(100 * rate)) + (
        f" · {t('tehlikeli hata')}" if report.get("dangerous_errors") else ""
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
        t("Önerilen düzen (test sonuçlarına göre): Ana: {ana}").format(ana=ana)
        + (f" · {t('Zor işler: {zor}').format(zor=zor)}" if zor else "")
        + (f" · {t('Bilgisayar kullanımı: {pc}').format(pc=pc)}" if pc else "")
        + (f" — {t('şu anki düzen bu.')}" if suggested == current else "")
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
        for name, on in ((t("Beyin"), is_brain), (t("Zor işler"), hard)) if on
    ]
    if alias == fallback and not is_brain:
        roles.append(f"<span style='color:{colors['muted']}'>{t('Yedek')}</span>")
    muted = colors["muted"]
    role_text = " · ".join(roles) or f"<span style='color:{muted}'>{t('Rol yok')}</span>"
    score = (
        t("Test sürüyor…") if alias == testing
        else _score(reports[alias]) if alias in reports
        else t("test yok")
    )
    tag = (
        f"&nbsp;&nbsp;<span style='color:{colors['warning']}'>{t('eski ayar')}</span>"
        if legacy else ""
    )
    kind = (
        f" · {t('resim')}" if model_kind == "image"
        else f" · {t('ses')}" if model_kind == "speech"
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


# ponytail: kaba ortalama yanıt; gerçek tutar metne ve modelin ses token hızına göre değişir.
SPEECH_REPLY_INPUT_TOKENS = 60    # ~240 karakterlik yanıt metni
SPEECH_REPLY_OUTPUT_TOKENS = 375  # ~15 sn ses (Gemini 25 ses token/sn)


def speech_reply_cost(input_per_million: Decimal, output_per_million: Decimal) -> Decimal:
    return (
        SPEECH_REPLY_INPUT_TOKENS * input_per_million
        + SPEECH_REPLY_OUTPUT_TOKENS * output_per_million
    ) / Decimal(1_000_000)


def _format_usd(amount: Decimal, places: int) -> str:
    quantized = amount.quantize(Decimal(10) ** -places, rounding=ROUND_HALF_UP)
    whole, _, frac = f"{quantized:f}".partition(".")
    trimmed = frac.rstrip("0")
    if len(trimmed) < 2:
        trimmed = trimmed.ljust(2, "0")
    return f"{whole}.{trimmed}"


def speech_cost_text(input_per_million: Decimal, output_per_million: Decimal) -> str:
    if input_per_million == 0 and output_per_million == 0:
        return ""
    cost = speech_reply_cost(input_per_million, output_per_million)
    one = _format_usd(cost, 4)
    hundred = _format_usd(cost * 100, 2)
    return t("Tahmin: bir sesli yanıt (~15 sn) ≈ {one} USD; 100 yanıt ≈ {hundred} USD.").format(
        one=one, hundred=hundred
    )


def speech_cost_text_from_fields(input_text: str, output_text: str) -> str:
    try:
        input_price = Decimal(input_text.strip() or "0")
        output_price = Decimal(output_text.strip() or "0")
        if (
            not input_price.is_finite()
            or not output_price.is_finite()
            or input_price < 0
            or output_price < 0
        ):
            return ""
        return speech_cost_text(input_price, output_price)
    except InvalidOperation:
        return ""


def speech_cost_map(settings: Settings) -> dict[str, str]:
    return {
        alias: speech_cost_text(model.input_per_million, model.output_per_million)
        or t("Ücretsiz olarak eklendi.")
        for alias, model in settings.models.items()
        if model.kind == "speech"
    }


def probe_models(base_url: str, key: str | None) -> tuple[str, list[str]]:
    """GET {base_url}/models; yanıt gövdesi, başlık ve anahtar mesaja asla girmez."""
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        response = httpx.get(
            f"{base_url}/models", headers=headers, timeout=5, follow_redirects=False
        )
    except httpx.HTTPError:
        return t("Sunucuya ulaşılamadı (adres doğru mu, sunucu çalışıyor mu?)."), []
    if response.status_code in {401, 403}:
        return t("Sunucu API anahtarını reddetti; anahtarı kontrol edin."), []
    if response.status_code == 404:
        return t("/models desteklenmiyor; model adını elle yazabilirsiniz."), []
    if response.status_code != 200:
        return t("Sunucu beklenmeyen yanıt verdi (HTTP {code}).").format(
            code=response.status_code
        ), []
    try:
        data = response.json()["data"]
        models = sorted(
            {
                item["id"]
                for item in data
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            }
        )
    except (ValueError, KeyError, TypeError):
        return t("Bağlantı var ama model listesi okunamadı; model adını elle yazabilirsiniz."), []
    return t("Bağlantı tamam, {count} model bulundu.").format(count=len(models)), models


def store_endpoint_key(
    name: str, typed: str, writer: Callable[[str, str], None]
) -> str | None:
    # Yazıcı açıkça verilir: testler provider_section.write_secret'i sahteler; varsayılan
    # buradaki gerçek kasaya giderdi.
    """Anahtarı kasaya yazar (YAML'a asla); kasa yoksa "ortamda X tanımlayın." döner."""
    try:
        writer(typed, f"{credential_prefix()}/{name}")
    except OSError:
        return t("ortamda {var} tanımlayın.").format(var=endpoint_key_variable(name))
    return None
