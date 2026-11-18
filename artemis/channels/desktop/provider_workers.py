"""Model ayarları panelinin arka plan işleri: bağlantı ve abonelik denemesi.

provider_section.py dosya boyutu sınırı için ayrıldı (2026-11-18); adlar oradan da dışa verilir.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import QWidget

from artemis.brain.errors import ProviderError
from artemis.brain.models import Message
from artemis.brain.providers import ClaudeCliProvider
from artemis.config.settings import ModelConfig, load_settings
from artemis.i18n import t


class ConnectionTest(QThread):
    done = Signal(str, list)

    def __init__(
        self,
        base_url: str,
        key: str | None,
        parent: QWidget,
        probe: Callable[[str, str | None], tuple[str, list[str]]],
    ) -> None:
        super().__init__(parent)
        self._base_url = base_url
        self._key = key
        # Deneme işlevi çağırandan gelir: testler provider_section.probe_models'i değiştirebilir.
        self._probe = probe

    def run(self) -> None:
        message, models = self._probe(self._base_url, self._key)
        self.done.emit(message, models)


class CliTest(QThread):
    """Tek küçük gerçek istek atar (abonelik kotasından az miktar harcar)."""

    done = Signal(str)

    def __init__(self, config_path: Path, model: str, parent: QWidget) -> None:
        super().__init__(parent)
        self._config_path = config_path
        self._model = model

    def run(self) -> None:
        try:
            settings = load_settings(self._config_path)
            model = ModelConfig(
                provider="claude_cli",
                model=self._model,
                input_per_million=Decimal(0),
                output_per_million=Decimal(0),
            )
            prompt = [Message(role="user", content="Tek kelimeyle yanıt ver: merhaba")]
            provider = ClaudeCliProvider(settings, model)
            completion = asyncio.run(provider.complete(prompt, [], timeout=60))
        except ProviderError as error:
            self.done.emit(str(error))
            return
        except (OSError, ValueError):
            self.done.emit(t("Test çalıştırılamadı."))
            return
        self.done.emit(
            t("Claude Code aboneliği çalışıyor.")
            if completion.content.strip()
            else t("Claude Code boş yanıt verdi.")
        )
