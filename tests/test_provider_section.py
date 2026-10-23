"""Ayarlar > Model sağlayıcıları (ADR 0034): ağsız, gerçek kasa ve gerçek HTTP yok."""

import os
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from artemis.brain import providers
from artemis.brain.fake import ScriptedProvider
from artemis.brain.providers import provider_problem
from artemis.channels.desktop import bridge as bridge_module
from artemis.channels.desktop import provider_section as section_module
from artemis.channels.desktop.app import build_window
from artemis.channels.desktop.provider_section import PRESETS, ProviderSection, probe_models
from artemis.channels.desktop.settings_panel import SettingsPanel
from artemis.channels.runtime import create_runtime
from artemis.config.settings import load_settings
from tests.test_desktop_gui import _close_cleanly, _wait
from tests.test_settings_panel import config

SECRET = "sk-test-gizli"


@pytest.fixture
def app() -> QApplication:
    current = QApplication.instance()
    return current if isinstance(current, QApplication) else QApplication([])


@pytest.fixture(autouse=True)
def _vault(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    store: dict[str, str] = {}
    monkeypatch.setattr(providers, "read_secret", lambda target: store.get(target))
    monkeypatch.setattr(section_module, "write_secret", lambda s, t: store.__setitem__(t, s))
    monkeypatch.setattr(section_module, "delete_secret", lambda t: store.pop(t, None))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("ARTEMIS_YEREL_API_KEY", raising=False)
    return store


def _fill(section: ProviderSection, name: str, url: str, key: str = "") -> None:
    section.endpoint_id.setText(name)
    section.base_url.setText(url)
    section.key.setText(key)


def test_preset_fills_fields(app: QApplication, tmp_path: Path) -> None:
    section = ProviderSection(config(tmp_path))
    section.preset.setCurrentIndex([p[0] for p in PRESETS].index("LM Studio"))
    section._apply_preset()
    assert section.base_url.text() == "http://127.0.0.1:1234/v1"
    assert section.endpoint_id.text() == "lmstudio"
    assert not section.needs_key.isChecked()


def test_invalid_id_and_url_are_rejected(app: QApplication, tmp_path: Path) -> None:
    path = config(tmp_path)
    before = path.read_bytes()
    section = ProviderSection(path)
    _fill(section, "kötü id!", "https://x.example/v1")
    section._save()
    assert "Kimlik" in section.status.text()
    _fill(section, "iyi", "http://uzak.example/v1", SECRET)
    section._save()
    assert SECRET not in section.status.text()
    assert path.read_bytes() == before


def test_save_writes_endpoint_without_key(
    app: QApplication, tmp_path: Path, _vault: dict[str, str]
) -> None:
    path = config(tmp_path)
    section = ProviderSection(path)
    emitted: list[int] = []
    section.changed.connect(lambda: emitted.append(1))
    _fill(section, "yerel", "https://x.example/v1/", SECRET)
    section._save()
    assert _vault == {"Artemis/yerel": SECRET}
    assert SECRET not in path.read_text(encoding="utf-8")
    assert load_settings(path).endpoints["yerel"].base_url == "https://x.example/v1"
    assert section.key.text() == "" and emitted == [1]
    assert "anahtar kayıtlı" in section.endpoint_list.item(0).text()


def test_save_without_vault_points_to_env(
    app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(secret: str, target: str) -> None:
        raise OSError("yok")

    monkeypatch.setattr(section_module, "write_secret", fail)
    section = ProviderSection(config(tmp_path))
    _fill(section, "yerel", "https://x.example/v1", SECRET)
    section._save()
    assert "ARTEMIS_YEREL_API_KEY" in section.status.text()
    assert SECRET not in section.status.text()


class _Response:
    def __init__(self, status: int, body: Any = None) -> None:
        self.status_code = status
        self._body = body

    def json(self) -> Any:
        if self._body is None:
            raise ValueError
        return self._body


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (200, {"data": [{"id": "b"}, {"id": "a"}]}, "Bağlantı tamam, 2 model bulundu."),
        (401, None, "reddetti"),
        (403, None, "reddetti"),
        (404, None, "/models desteklenmiyor"),
    ],
)
def test_probe_status_mapping(
    monkeypatch: pytest.MonkeyPatch, status: int, body: Any, expected: str
) -> None:
    seen: dict[str, Any] = {}

    def fake_get(url: str, **kwargs: Any) -> _Response:
        seen.update(kwargs, url=url)
        return _Response(status, body)

    monkeypatch.setattr(httpx, "get", fake_get)
    message, models = probe_models("https://x.example/v1", SECRET)
    assert expected in message and SECRET not in message
    assert seen["url"] == "https://x.example/v1/models"
    assert seen["follow_redirects"] is False
    assert seen["headers"] == {"Authorization": f"Bearer {SECRET}"}
    assert models == (["a", "b"] if status == 200 else [])


def test_probe_connection_error_and_keyless(monkeypatch: pytest.MonkeyPatch) -> None:
    headers: list[Any] = []

    def fake_get(url: str, **kwargs: Any) -> _Response:
        headers.append(kwargs["headers"])
        raise httpx.ConnectError("x")

    monkeypatch.setattr(httpx, "get", fake_get)
    assert "ulaşılamadı" in probe_models("http://127.0.0.1:1/v1", None)[0]
    assert headers == [{}]


def test_test_button_ignores_key_when_not_required(
    app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Any] = []

    def fake_probe(url: str, key: str | None) -> tuple[str, list[str]]:
        calls.append(key)
        return "Bağlantı tamam, 1 model bulundu.", ["m1"]

    monkeypatch.setattr(section_module, "probe_models", fake_probe)
    section = ProviderSection(config(tmp_path))
    _fill(section, "yerel", "http://127.0.0.1:1234/v1", SECRET)
    section.needs_key.setChecked(False)
    section._test_connection()
    assert section._test is not None
    section._test.wait(3000)
    app.processEvents()
    assert calls == [None]
    assert "tamam" in section.status.text()


def test_fetch_fills_models_from_selected_connection(
    app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _vault: dict[str, str]
) -> None:
    calls: list[tuple[str, str | None]] = []

    def fake_probe(url: str, key: str | None) -> tuple[str, list[str]]:
        calls.append((url, key))
        return "Bağlantı tamam, 2 model bulundu.", ["a/m1", "a/m2"]

    monkeypatch.setattr(section_module, "probe_models", fake_probe)
    section = ProviderSection(config(tmp_path))
    _fill(section, "yerel", "https://x.example/v1", SECRET)
    section._save()
    section._fetch_models()
    assert section._fetch is not None
    section._fetch.wait(3000)
    app.processEvents()
    assert calls == [("https://x.example/v1", SECRET)]
    assert [section.model_name.itemText(i) for i in range(2)] == ["a/m1", "a/m2"]
    assert section.alias.text() == "a-m1"  # takma ad model adından türer


def test_delete_blocked_when_referenced_then_removes_secret(
    app: QApplication, tmp_path: Path, _vault: dict[str, str]
) -> None:
    path = config(tmp_path)
    section = ProviderSection(path)
    _fill(section, "yerel", "https://x.example/v1", SECRET)
    section._save()
    section.model_name.setEditText("m-1")
    section.make_primary.setChecked(False)
    section._add_model()
    section._delete()
    assert "yerel" in load_settings(path).endpoints and _vault
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    del raw["models"]["m-1"]
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    section._delete()
    assert "yerel" not in load_settings(path).endpoints
    assert _vault == {}


def test_add_model_and_make_primary(app: QApplication, tmp_path: Path) -> None:
    path = config(tmp_path)
    section = ProviderSection(path)
    _fill(section, "yerel", "https://x.example/v1")
    section._save()
    assert section.price_box.isHidden()  # fiyat varsayılan kapalı, 0 yazılır
    section.paid.setChecked(True)
    assert not section.price_box.isHidden()
    section.model_name.setEditText("vendor/Model 1")
    section.output_price.setText("2")
    section.make_primary.setChecked(True)
    section._add_model()
    settings = load_settings(path)
    model = settings.models["vendor-Model-1"]
    assert (model.provider, model.endpoint, model.model) == (
        "openai_compatible",
        "yerel",
        "vendor/Model 1",
    )
    assert settings.primary_model == "vendor-Model-1"
    assert settings.allow_paid is False and "bütçe" in section.status.text()


def test_provider_problem_cases(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = config(tmp_path)
    settings = load_settings(path)
    assert "OPENROUTER_API_KEY" in str(provider_problem(settings))
    monkeypatch.setenv("OPENROUTER_API_KEY", "x")
    assert provider_problem(settings) is None
    assert provider_problem(settings.model_copy(update={"primary_model": ""})) is not None
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["endpoints"] = {
        "yerel": {"base_url": "https://x.example/v1"},
        "bos": {"base_url": "http://127.0.0.1:1/v1", "api_key": False},
    }
    for alias, endpoint in (("k", "yerel"), ("b", "bos")):
        raw["models"][alias] = {
            "provider": "openai_compatible",
            "endpoint": endpoint,
            "model": alias,
            "input_per_million": "0",
            "output_per_million": "0",
        }
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    settings = load_settings(path)
    assert "ARTEMIS_YEREL_API_KEY" in str(
        provider_problem(settings.model_copy(update={"primary_model": "k"}))
    )
    assert provider_problem(settings.model_copy(update={"primary_model": "b"})) is None
    monkeypatch.setenv("ARTEMIS_YEREL_API_KEY", "x")
    assert provider_problem(settings.model_copy(update={"primary_model": "k"})) is None


def test_startup_opens_settings_when_provider_missing(
    app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def factory(config_path: Path, approve: Any, **kwargs: Any) -> Any:
        return create_runtime(
            config_path,
            approve,
            settings=load_settings(config_path),
            provider=ScriptedProvider([]),
            **kwargs,
        )

    monkeypatch.setattr(bridge_module, "create_runtime", factory)
    monkeypatch.setattr(SettingsPanel, "_refresh_devices", lambda *_: None)
    window = build_window(config(tmp_path), app=app)
    try:
        _wait(app, lambda: window._worker_ready)
        assert window.tabs.currentWidget() is window.settings_scroll
        assert "OPENROUTER_API_KEY" in window.settings_panel.providers.banner.text()
    finally:
        _close_cleanly(app, window)


def test_remove_model_ok_and_refused_for_primary(app: QApplication, tmp_path: Path) -> None:
    path = config(tmp_path)
    section = ProviderSection(path)
    _fill(section, "yerel", "https://x.example/v1")
    section._save()
    for name, primary in (("m-1", True), ("m-2", False)):
        section.model_name.setEditText(name)
        section.make_primary.setChecked(primary)
        section._add_model()
    section._remove_model()  # seçim yok
    assert "seçin" in section.status.text()
    section.select_model("m-1")
    section._remove_model()
    assert "kaldırılamaz" in section.status.text()
    assert "m-1" in load_settings(path).models
    section.select_model("m-2")
    section._remove_model()
    assert "m-2" not in load_settings(path).models


def _two_same_id(tmp_path: Path) -> Path:
    path = config(tmp_path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["endpoints"] = {"proxy_cli": {"base_url": "http://127.0.0.1:8317/v1", "api_key": False}}
    raw["models"]["first"]["model"] = "claude-haiku-5-5"
    raw["models"]["haiku 5-5"] = {
        "provider": "openai_compatible",
        "endpoint": "proxy_cli",
        "model": "claude-haiku-5-5",
        "input_per_million": "0",
        "output_per_million": "0",
    }
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


def test_primary_is_chosen_by_alias_when_model_ids_collide(
    app: QApplication, tmp_path: Path
) -> None:
    path = _two_same_id(tmp_path)
    section = ProviderSection(path)
    section.select_model("haiku 5-5")
    section._make_primary()
    assert load_settings(path).primary_model == "haiku 5-5"
    assert "Beyin: haiku 5-5 (proxy_cli)" in section.banner.text()
    section.select_model("first")
    section._make_primary()
    assert load_settings(path).primary_model == "first"


def test_list_rows_show_primary_and_legacy_tags(app: QApplication, tmp_path: Path) -> None:
    path = _two_same_id(tmp_path)
    section = ProviderSection(path)
    rows = dict(section.model_rows())
    assert "eski ayar" in rows["first"] and "Beyin" in rows["first"]
    assert "eski ayar" not in rows["haiku 5-5"] and "Beyin" not in rows["haiku 5-5"]
    assert "proxy_cli · claude-haiku-5-5" in rows["haiku 5-5"]
    section.select_model("haiku 5-5")
    section._make_primary()
    assert "Beyin" in dict(section.model_rows())["haiku 5-5"]
    assert not hasattr(section, "registered")


def test_add_model_unchecks_primary_when_one_works_and_refuses_duplicate_alias(
    app: QApplication, tmp_path: Path
) -> None:
    path = _two_same_id(tmp_path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["primary_model"] = "haiku 5-5"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    section = ProviderSection(path)
    assert not section.make_primary.isChecked()  # çalışan beyin var
    section.model_name.setEditText("other-model")
    section._add_model()
    settings = load_settings(path)
    assert settings.primary_model == "haiku 5-5" and "other-model" in settings.models
    before = path.read_bytes()
    section.model_name.setEditText("other-model")
    section.alias.setText("other-model")
    section._add_model()
    assert "kullanımda" in section.status.text() and path.read_bytes() == before


def test_subscription_connection_writes_valid_cli_model(
    app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(section_module.shutil, "which", lambda name: "/bin/claude")
    path = config(tmp_path)
    section = ProviderSection(path)
    section.show()
    assert section.add_connection.currentData() == section_module.CLI_KEY  # tek seçenek
    assert section.cli_test_button.isVisible()
    assert "/bin/claude" in section.status.text()
    assert [section.model_name.itemText(i) for i in range(3)] == ["haiku", "sonnet", "opus"]
    section.model_name.setEditText("sonnet")
    section.make_primary.setChecked(True)
    section._add_model()
    settings = load_settings(path)
    assert settings.primary_model == "claude-cli-sonnet"
    model = settings.models["claude-cli-sonnet"]
    assert (model.provider, model.model, model.endpoint) == ("claude_cli", "sonnet", None)
