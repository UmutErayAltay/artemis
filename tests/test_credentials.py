import sys

import pytest

from artemis.tools import credentials


@pytest.mark.skipif(sys.platform != "win32", reason="Kimlik Bilgisi Yöneticisi yalnız Windows'ta")
def test_round_trip_and_env_load(monkeypatch):
    target = "Artemis/test-credentials-roundtrip"
    credentials.write_secret("gizli-ğüş", target)
    try:
        assert credentials.read_secret(target) == "gizli-ğüş"
        assert credentials.read_secret("Artemis/yok-boyle-bir-kayit") is None
        monkeypatch.setattr(credentials, "TARGET", target)
        monkeypatch.setattr(credentials, "read_secret", lambda: "gizli-ğüş")
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        credentials.load_into_environment()
        import os

        assert os.environ["OPENROUTER_API_KEY"] == "gizli-ğüş"
        monkeypatch.setenv("OPENROUTER_API_KEY", "ortamdaki")
        credentials.load_into_environment()
        assert os.environ["OPENROUTER_API_KEY"] == "ortamdaki"
    finally:
        import ctypes

        ctypes.WinDLL("advapi32").CredDeleteW(target, 1, 0)


@pytest.fixture
def fake_store(monkeypatch):
    """Gerçek Windows API'sine dokunmadan sahte kasa; platform win32 gibi davranır."""
    store: dict[str, str] = {}
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(credentials, "_read_secret_windows", store.get, raising=False)
    monkeypatch.setattr(
        credentials, "_write_secret_windows", lambda s, t: store.__setitem__(t, s), raising=False
    )
    monkeypatch.setattr(
        credentials, "_delete_secret_windows", lambda t: store.pop(t, None), raising=False
    )
    return store


def test_legacy_only_secret_is_read(fake_store):
    fake_store["Jarvis/x"] = "eski"
    assert credentials.read_secret("Artemis/x") == "eski"


def test_new_secret_wins_over_legacy(fake_store):
    fake_store["Jarvis/x"] = "eski"
    fake_store["Artemis/x"] = "yeni"
    assert credentials.read_secret("Artemis/x") == "yeni"


def test_write_goes_to_new_target_only(fake_store):
    credentials.write_secret("s", "Artemis/x")
    assert fake_store == {"Artemis/x": "s"}


def test_delete_removes_new_and_legacy(fake_store):
    fake_store.update({"Jarvis/x": "eski", "Artemis/x": "yeni", "Jarvis/y": "baska"})
    credentials.delete_secret("Artemis/x")
    assert fake_store == {"Jarvis/y": "baska"}
