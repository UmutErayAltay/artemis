"""Ayarlar > Telegram (ADR 0051): token kasası, getMe, eşleştirme, test ve bağlantıyı kesme.

Ağ yalnız loopback sahte Telegram sunucusudur; kimlik kasası sözlükle değiştirilir (gerçek
Windows kasasına ne okuma ne yazma yapılır). Gerçek bot token'ı yoktur.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml
from telegram_fake import CHAT_ID, FAKE_TOKEN, FakeTelegram, make_message, method_bodies

from artemis.channels.telegram import api
from artemis.channels.telegram.api import TOKEN_TARGET, TelegramClient, TelegramError
from artemis.channels.telegram.channel import STATUS_CONFLICT, STATUS_RUNNING
from artemis.config.settings import load_settings
from artemis.safety.audit import AuditLog

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from artemis.channels.desktop import telegram_section as section_module
from artemis.channels.desktop.settings_panel import SettingsPanel
from artemis.channels.desktop.telegram_section import (
    TelegramSection,
    match_pairing,
    pair_chat,
)

CODE = "123456"
OTHER_ID = 777


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    data: dict[str, str] = {}
    monkeypatch.setattr(api, "read_secret", lambda target: data.get(target))
    monkeypatch.setattr(section_module, "write_secret", lambda s, t: data.__setitem__(t, s))
    monkeypatch.setattr(section_module, "delete_secret", lambda t: data.pop(t, None))
    monkeypatch.delenv("ARTEMIS_TELEGRAM_BOT_TOKEN", raising=False)
    return data


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeTelegram]:
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    server = FakeTelegram()
    server.start()
    yield server
    server.stop()


def make_config(tmp_path: Path, fake: FakeTelegram, *, chat_id: int | None = None) -> Path:
    telegram: dict[str, object] = {"api_base": fake.api_base}
    if chat_id is not None:
        telegram["chat_id"] = chat_id
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({
        "workspace": "desktop", "state_dir": "state", "audit_path": "audit.jsonl",
        "primary_model": "first",
        "openrouter_base_url": "http://127.0.0.1:8317/v1",
        "applications": {"brave": "brave.exe"},
        "models": {"first": {"provider": "openrouter", "model": "first-model",
                             "input_per_million": "0", "output_per_million": "0"}},
        "voice": {"stt_device": "cpu", "stt_model": "models/whisper"},
        "telegram": telegram,
    }), encoding="utf-8")
    return path


def section(path: Path, *, demo: bool = False) -> TelegramSection:
    QApplication.instance() or QApplication([])
    return TelegramSection(path, demo=demo)


def settle(panel: TelegramSection) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if panel._job is None or panel._job.isFinished():
            break
        time.sleep(0.01)
    QApplication.processEvents()


def wait_for_text(panel: TelegramSection, needle: str) -> None:
    deadline = time.monotonic() + 10
    while needle not in panel.message.text() and time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.01)


def client(fake: FakeTelegram, tmp_path: Path, token: str = FAKE_TOKEN) -> TelegramClient:
    return TelegramClient(token, fake.api_base, audit=AuditLog(tmp_path / "a.jsonl"), timeout=5)


def raw(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


# API -------------------------------------------------------------------------------------


def test_get_me_returns_username_and_audits_probe(fake: FakeTelegram, tmp_path: Path) -> None:
    assert client(fake, tmp_path).get_me() == "test_bot"
    event = (tmp_path / "a.jsonl").read_text(encoding="utf-8")
    assert '"method": "getMe"' in event and '"kind": "probe"' in event and '"ok": true' in event
    assert FAKE_TOKEN not in event


def test_get_me_error_has_no_token_and_audits_failure(fake: FakeTelegram, tmp_path: Path) -> None:
    c = client(fake, tmp_path, token="999:wrong-secret-token")
    with pytest.raises(TelegramError) as caught:
        c.get_me()
    assert caught.value.error_type == "HTTP 401"
    audit = (tmp_path / "a.jsonl").read_text(encoding="utf-8")
    for text in (str(caught.value), audit):
        assert "wrong-secret-token" not in text and "/bot" not in text
    assert '"ok": false' in audit


def test_get_me_rejects_bad_result_shape(fake: FakeTelegram, tmp_path: Path) -> None:
    fake.fail("getMe", status=200, body={"ok": True, "result": {"id": 1}})
    with pytest.raises(TelegramError) as caught:
        client(fake, tmp_path).get_me()
    assert caught.value.error_type == "BadResult"


def test_build_client_uses_store_only_when_env_is_none(
    store: dict[str, str], fake: FakeTelegram, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = load_settings(make_config(tmp_path, fake, chat_id=CHAT_ID))
    audit = AuditLog(tmp_path / "a.jsonl")
    store[TOKEN_TARGET] = FAKE_TOKEN
    assert api.build_telegram_client(settings, audit, env={}) is None  # enjekte env tek kaynak
    from_store = api.build_telegram_client(settings, audit)
    assert from_store is not None and FAKE_TOKEN not in repr(from_store)
    monkeypatch.setenv("ARTEMIS_TELEGRAM_BOT_TOKEN", "111:env-token")
    assert api.resolve_telegram_token(settings.telegram) == "111:env-token"  # ortam önce
    assert api.resolve_telegram_token(settings.telegram, env={}) is None


# Eşleştirme kuralları --------------------------------------------------------------------


def test_match_accepts_exact_code_from_private_chat() -> None:
    update = make_message(1, f"  {CODE}\n", from_id=OTHER_ID, chat_id=OTHER_ID)
    assert match_pairing(update, CODE) == OTHER_ID


@pytest.mark.parametrize(
    "update",
    [
        make_message(1, "654321"),
        make_message(1, f"kod {CODE}"),
        make_message(1, None),
        make_message(1, CODE, chat_type="group", chat_id=-100, from_id=5),
        make_message(1, CODE, from_id=5, chat_id=6),
        make_message(1, CODE, forward_origin={"type": "user"}),
        make_message(1, CODE, forward_from={"id": 3}),
        make_message(1, CODE, chat_id=True, from_id=True),
        {"update_id": 1, "callback_query": {"data": CODE}},
    ],
)
def test_match_ignores_everything_else(update: dict) -> None:
    assert match_pairing(update, CODE) is None


def test_pair_chat_skips_noise_confirms_offset_and_returns_chat(
    fake: FakeTelegram, tmp_path: Path
) -> None:
    fake.queue_message("654321", from_id=OTHER_ID, chat_id=OTHER_ID)
    fake.queue_message(CODE, chat_type="group", chat_id=-9, from_id=OTHER_ID)
    fake.queue_message(CODE, forward_from={"id": 1})
    fake.queue_message(CODE, from_id=OTHER_ID, chat_id=OTHER_ID)
    result = pair_chat(
        client(fake, tmp_path), CODE, stop=lambda: False, seconds=5, idle=lambda: None
    )
    assert result == (OTHER_ID, True)
    offsets = [b.get("offset") for b in method_bodies(fake, "getUpdates")]
    assert offsets[-1] == offsets[-2] + 1  # kod mesajının update_id+1 ile onaylandı
    audit = tmp_path / "a.jsonl"
    assert "654321" not in (audit.read_text(encoding="utf-8") if audit.exists() else "")


def test_pair_chat_times_out_and_cancels(fake: FakeTelegram, tmp_path: Path) -> None:
    c = client(fake, tmp_path)
    assert pair_chat(c, CODE, stop=lambda: False, seconds=0.3, idle=lambda: None) is None
    assert pair_chat(c, CODE, stop=lambda: True, seconds=5) is None


def test_pair_chat_locks_after_five_wrong_private_texts(
    fake: FakeTelegram, tmp_path: Path
) -> None:
    for guess in ("000001", "000002", "000003", "000004", "000005"):
        fake.queue_message(guess, from_id=OTHER_ID, chat_id=OTHER_ID)
    fake.queue_message(CODE, from_id=OTHER_ID, chat_id=OTHER_ID)  # kilitten sonra gelir
    with pytest.raises(TelegramError) as caught:
        pair_chat(client(fake, tmp_path), CODE, stop=lambda: False, seconds=5, idle=lambda: None)
    assert caught.value.error_type == "PairingLocked"


def test_pair_chat_noise_and_four_wrong_guesses_do_not_lock(
    fake: FakeTelegram, tmp_path: Path
) -> None:
    for guess in ("000001", "000002", "000003", "000004"):
        fake.queue_message(guess, from_id=OTHER_ID, chat_id=OTHER_ID)
    for _ in range(10):  # grup gürültüsü tahmin sayılmaz
        fake.queue_message("000009", chat_type="group", chat_id=-9, from_id=OTHER_ID)
    fake.queue_message(CODE, from_id=OTHER_ID, chat_id=OTHER_ID)
    result = pair_chat(
        client(fake, tmp_path), CODE, stop=lambda: False, seconds=5, idle=lambda: None
    )
    assert result == (OTHER_ID, True)


def test_locked_pairing_shows_clear_text(fake: FakeTelegram, tmp_path: Path) -> None:
    panel = section(make_config(tmp_path, fake))
    error = TelegramError("x", error_type="PairingLocked")
    panel._paired("t", None, tmp_path / "a.jsonl", error)  # type: ignore[arg-type]
    assert "yanlış kod" in panel.message.text() and "PairingLocked" not in panel.message.text()


def test_pair_chat_409_raises_once_without_retry(fake: FakeTelegram, tmp_path: Path) -> None:
    fake.fail("getUpdates", status=409, body={"ok": False, "description": "Conflict"})
    with pytest.raises(TelegramError) as caught:
        pair_chat(client(fake, tmp_path), CODE, stop=lambda: False, seconds=5, idle=lambda: None)
    assert caught.value.error_type == "HTTP 409"
    assert len(method_bodies(fake, "getUpdates")) == 1


# Panel -----------------------------------------------------------------------------------


def test_save_token_verifies_then_stores_and_leaks_nowhere(
    store: dict[str, str], fake: FakeTelegram, tmp_path: Path
) -> None:
    path = make_config(tmp_path, fake)
    panel = section(path)
    assert panel.status.text() == "Token yok" and not panel.pair_button.isEnabled()
    panel.token.setText(FAKE_TOKEN)
    panel._save_token()
    settle(panel)
    assert panel.token.text() == "" and store == {TOKEN_TARGET: FAKE_TOKEN}
    assert "@test_bot" in panel.message.text()
    assert panel.status.text() == "Token kayıtlı, sohbet bağlı değil"
    for text in (path.read_text(encoding="utf-8"), panel.status.text(), panel.message.text(),
                 (tmp_path / "audit.jsonl").read_text(encoding="utf-8"), repr(panel)):
        assert FAKE_TOKEN not in text
    panel.deleteLater()


def test_invalid_token_is_not_kept_and_old_one_survives(
    store: dict[str, str], fake: FakeTelegram, tmp_path: Path
) -> None:
    store[TOKEN_TARGET] = FAKE_TOKEN
    panel = section(make_config(tmp_path, fake))
    panel.token.setText("999:bad-token-value")
    panel._save_token()
    settle(panel)
    assert store == {TOKEN_TARGET: FAKE_TOKEN}
    assert "geçersiz" in panel.message.text() and "bad-token" not in panel.message.text()
    panel.deleteLater()


def test_vault_failure_points_to_env_var(
    store: dict[str, str], fake: FakeTelegram, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(secret: str, target: str) -> None:
        raise OSError("yok")

    monkeypatch.setattr(section_module, "write_secret", boom)
    panel = section(make_config(tmp_path, fake))
    panel.token.setText(FAKE_TOKEN)
    panel._save_token()
    settle(panel)
    assert "ARTEMIS_TELEGRAM_BOT_TOKEN" in panel.message.text()
    assert FAKE_TOKEN not in panel.message.text() and store == {}
    panel.deleteLater()


def test_pairing_end_to_end_writes_int_chat_id_and_sends_notice(
    store: dict[str, str], fake: FakeTelegram, tmp_path: Path
) -> None:
    store[TOKEN_TARGET] = FAKE_TOKEN
    path = make_config(tmp_path, fake)
    panel = section(path)
    changed: list[bool] = []
    panel.changed.connect(lambda: changed.append(True))
    panel._pair()
    wait_for_text(panel, "şu kodu yaz")
    code = panel.message.text().rsplit(": ", 1)[1]
    assert code.isdigit() and len(code) == 6 and "@test_bot" in panel.message.text()
    fake.queue_message("000000", from_id=OTHER_ID, chat_id=OTHER_ID)
    fake.queue_message(code, from_id=OTHER_ID, chat_id=OTHER_ID)
    settle(panel)
    settle(panel)  # ardışık bildirim işi
    assert raw(path)["telegram"]["chat_id"] == OTHER_ID
    assert load_settings(path).telegram.chat_id == OTHER_ID
    sent = method_bodies(fake, "sendMessage")
    assert [b["chat_id"] for b in sent] == [OTHER_ID] and sent[0]["text"] == "Artemis bağlandı."
    assert changed and panel.status.text() == f"Bağlı: @test_bot, sohbet {OTHER_ID}"
    panel.deleteLater()


def test_pairing_409_shows_stop_other_process_message(
    store: dict[str, str], fake: FakeTelegram, tmp_path: Path
) -> None:
    store[TOKEN_TARGET] = FAKE_TOKEN
    fake.fail("getUpdates", status=409, body={"ok": False, "description": "Conflict"})
    panel = section(make_config(tmp_path, fake))
    panel._pair()
    settle(panel)
    assert "artemis telegram" in panel.message.text() and "durdur" in panel.message.text()
    assert len(method_bodies(fake, "getUpdates")) == 1
    panel.deleteLater()


def test_test_message_reports_failure_honestly(
    store: dict[str, str], fake: FakeTelegram, tmp_path: Path
) -> None:
    store[TOKEN_TARGET] = FAKE_TOKEN
    panel = section(make_config(tmp_path, fake, chat_id=CHAT_ID))
    panel._send_test()
    settle(panel)
    assert "gönderildi" in panel.message.text()
    assert method_bodies(fake, "sendMessage")[0]["chat_id"] == CHAT_ID
    fake.fail("sendMessage", status=500)
    panel._send_test()
    settle(panel)
    assert "gönderilemedi" in panel.message.text() and "HTTP 500" in panel.message.text()
    panel.deleteLater()


def test_disconnect_clears_chat_id_and_secret_but_not_env(
    store: dict[str, str], fake: FakeTelegram, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store[TOKEN_TARGET] = FAKE_TOKEN
    monkeypatch.setenv("ARTEMIS_TELEGRAM_BOT_TOKEN", "111:env-token")
    path = make_config(tmp_path, fake, chat_id=CHAT_ID)
    panel = section(path)
    panel._disconnect()
    assert store == {} and "chat_id" not in raw(path)["telegram"]
    assert load_settings(path).telegram.chat_id is None
    assert os.environ["ARTEMIS_TELEGRAM_BOT_TOKEN"] == "111:env-token"
    assert "ortam değişkeninden" in panel.message.text()
    panel.deleteLater()


def test_demo_panel_is_disabled(
    store: dict[str, str], fake: FakeTelegram, tmp_path: Path
) -> None:
    QApplication.instance() or QApplication([])
    store[TOKEN_TARGET] = FAKE_TOKEN
    demo = SettingsPanel(make_config(tmp_path, fake, chat_id=CHAT_ID), demo=True)
    telegram = demo.telegram
    for widget in (telegram.token, telegram.save_button, telegram.pair_button,
                   telegram.test_button, telegram.disconnect_button, telegram.cancel_button):
        assert not widget.isEnabled()
    demo.deleteLater()


def test_close_stops_running_pairing_thread(
    store: dict[str, str], fake: FakeTelegram, tmp_path: Path
) -> None:
    store[TOKEN_TARGET] = FAKE_TOKEN
    panel = section(make_config(tmp_path, fake))
    panel._pair()
    wait_for_text(panel, "şu kodu yaz")
    assert panel._job is not None and panel._job.isRunning()
    panel.finish()
    assert not panel._job.isRunning()
    panel.deleteLater()


# Masaüstü içi kanal (ADR 0052) --------------------------------------------------------------


def test_channel_toggle_writes_yaml_and_emits_without_runtime_restart(
    store: dict[str, str], fake: FakeTelegram, tmp_path: Path
) -> None:
    store[TOKEN_TARGET] = FAKE_TOKEN
    path = make_config(tmp_path, fake, chat_id=CHAT_ID)
    panel = section(path)
    toggled: list[bool] = []
    restarts: list[bool] = []
    panel.channel_changed.connect(lambda: toggled.append(True))
    panel.changed.connect(lambda: restarts.append(True))
    assert panel.channel_check.isEnabled() and not panel.channel_check.isChecked()
    panel.channel_check.setChecked(True)
    assert raw(path)["telegram"]["run_in_desktop"] is True
    assert load_settings(path).telegram.run_in_desktop is True
    assert raw(path)["telegram"]["chat_id"] == CHAT_ID  # diğer anahtarlar korunur
    assert toggled == [True] and restarts == []
    panel.channel_check.setChecked(False)
    assert raw(path)["telegram"]["run_in_desktop"] is False and len(toggled) == 2
    panel.deleteLater()


def test_channel_toggle_is_disabled_without_token_or_chat_and_in_demo(
    store: dict[str, str], fake: FakeTelegram, tmp_path: Path
) -> None:
    panel = section(make_config(tmp_path, fake))
    assert not panel.channel_check.isEnabled()
    assert "Önce token kaydet" in panel.channel_label.text()
    panel.deleteLater()
    store[TOKEN_TARGET] = FAKE_TOKEN
    QApplication.instance() or QApplication([])
    demo = SettingsPanel(make_config(tmp_path, fake, chat_id=CHAT_ID), demo=True).telegram
    assert not demo.channel_check.isEnabled() and "Demo" in demo.channel_label.text()
    demo.deleteLater()


def test_pairing_is_disabled_while_channel_runs_and_status_is_shown(
    store: dict[str, str], fake: FakeTelegram, tmp_path: Path
) -> None:
    store[TOKEN_TARGET] = FAKE_TOKEN
    path = make_config(tmp_path, fake, chat_id=CHAT_ID)
    panel = section(path)
    assert panel.pair_button.isEnabled()
    panel._toggle_channel(True)
    panel.channel_status.emit(STATUS_RUNNING)
    QApplication.processEvents()
    assert not panel.pair_button.isEnabled()
    label = panel.channel_label.text()
    assert "Çalışıyor" in label and "kanalı kapat" in label
    panel.channel_status.emit(STATUS_CONFLICT)
    QApplication.processEvents()
    assert panel.pair_button.isEnabled() and "Çakışma" in panel.channel_label.text()
    panel.channel_status.emit("Telegram hatası (ConnectError)")
    QApplication.processEvents()
    assert "ConnectError" in panel.channel_label.text()
    panel.deleteLater()


def test_panel_no_longer_says_two_way_channel_needs_separate_process(
    store: dict[str, str], fake: FakeTelegram, tmp_path: Path
) -> None:
    panel = section(make_config(tmp_path, fake))
    assert "bu panel onu başlatmaz" not in panel.hint.text()
    assert "çakışma" in panel.hint.text()
    panel.deleteLater()
