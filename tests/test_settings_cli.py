"""Config hataları ve onay varsayılanı sır veya yan etki üretmemelidir."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from jarvis.channels.cli import request_approval
from jarvis.config.settings import ModelConfig, Settings, load_settings


def test_secrets_are_not_config_fields():
    with pytest.raises(ValidationError):
        Settings.model_validate({"api_key": "yer-tutucu"})


def test_config_paths_are_relative_to_config_file(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("workspace: desktop\n", encoding="utf-8")
    assert load_settings(config).workspace == tmp_path / "desktop"


def test_example_has_no_guessed_model_or_price():
    settings = load_settings(Path("config/config.example.yaml"))
    assert not settings.models
    assert not settings.allow_paid
    assert not settings.allow_cloud_file_content


@pytest.mark.parametrize("address", ["ftp://host", "http://public.example", "https://a:b@host"])
def test_invalid_provider_endpoints(address):
    with pytest.raises(ValidationError):
        Settings(openrouter_base_url=address)


def test_model_requires_explicit_prices():
    with pytest.raises(ValidationError):
        ModelConfig.model_validate({"provider": "openrouter", "model": "test"})


@pytest.mark.parametrize("answer,expected", [("", False), ("evet", True), ("e", False)])
def test_cli_confirmation_displays_tool_and_arguments(monkeypatch, capsys, answer, expected):
    monkeypatch.setattr("builtins.input", lambda _prompt: answer)
    assert request_approval("delete_file", {"path": "önemli.txt"}) is expected
    out = capsys.readouterr().out
    assert "delete_file" in out and "önemli.txt" in out
