"""ProviderSection'ın Qt'siz saf yardımcıları (2026-11-18: dosya boyutu sınırı için ayrıldı)."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlsplit

from artemis.brain.model_report import SCENARIO_IDS, pass_rate
from artemis.config.settings import EndpointConfig, ModelConfig

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


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
