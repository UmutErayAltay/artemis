"""Hafif model kipi (ADR 0092): zayıf/yerel modeli başarılı kılan dar araç seti ve kısa istem.

Kip yalnız modele giden istemi ve araç tanımlarını daraltır; yetki kararı hâlâ tek SafetyGate'tedir.
"""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import urlsplit

from artemis.config.settings import EndpointConfig, ModelConfig

_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "0.0.0.0"})  # noqa: S104 - karşılaştırma


def is_local_endpoint(base_url: str) -> bool:
    """Adres bu makineyi mi gösteriyor (ağdaki başka makine yerel sayılmaz)."""
    try:
        host = urlsplit(base_url).hostname
    except ValueError:
        return False
    return host is not None and host.lower() in _LOCAL_HOSTS


def is_local_model(model: ModelConfig, endpoints: Mapping[str, EndpointConfig]) -> bool:
    """Ollama ya da yerel uç noktadaki model: soğuk yükleme yavaş olabilir, asılma sayılmaz."""
    if model.provider == "ollama":
        return True
    endpoint = endpoints.get(model.endpoint or "")
    return endpoint is not None and is_local_endpoint(endpoint.base_url)


def light_mode_on(model: ModelConfig | None, endpoints: Mapping[str, EndpointConfig]) -> bool:
    """Açık `light_mode` değeri kazanır; yoksa yerel ya da ücretsiz (`:free`) modelde açık."""
    if model is None or model.kind != "chat":
        return False
    if model.light_mode is not None:
        return model.light_mode
    return is_local_model(model, endpoints) or model.model.lower().endswith(":free")
