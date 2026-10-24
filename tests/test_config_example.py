"""config.example.yaml, panelin yazdığı ve ses katmanının okuduğu her alanı belgeler."""

import re
from pathlib import Path

import pytest

from artemis.config.settings import VoiceConfig

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "config.example.yaml"


def _documented(key: str, text: str) -> bool:
    # Yorum satırındaki örnek (`# noise_model_dir: ...`) da belge sayılır: isteğe bağlı bölümler
    # dosyada bilinçli olarak yorumlu duruyor.
    return re.search(rf"^\s*(#\s*)?{re.escape(key)}:", text, re.MULTILINE) is not None


@pytest.mark.parametrize("field", sorted(VoiceConfig.model_fields))
def test_every_voice_field_is_in_example(field: str) -> None:
    assert _documented(field, EXAMPLE.read_text(encoding="utf-8")), field


def test_every_panel_setting_is_in_example() -> None:
    pytest.importorskip("PySide6")
    from artemis.channels.desktop.settings_panel import PANEL_BINDINGS

    text = EXAMPLE.read_text(encoding="utf-8")
    for binding in PANEL_BINDINGS:
        assert _documented(binding.path[-1], text), binding.path
