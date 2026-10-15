import sys

import pytest

from jarvis.tools import credentials


@pytest.mark.skipif(sys.platform != "win32", reason="Kimlik Bilgisi Yöneticisi yalnız Windows'ta")
def test_round_trip_and_env_load(monkeypatch):
    target = "Jarvis/test-credentials-roundtrip"
    credentials.write_secret("gizli-ğüş", target)
    try:
        assert credentials.read_secret(target) == "gizli-ğüş"
        assert credentials.read_secret("Jarvis/yok-boyle-bir-kayit") is None
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
