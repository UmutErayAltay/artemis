"""Tüm testlerde ağ varsayılan olarak yalnız sahte loopback sunucusuna açıktır."""

import socket

import pytest


@pytest.fixture(autouse=True)
def block_external_network(monkeypatch):
    original_connect = socket.socket.connect
    original_lookup = socket.getaddrinfo

    def checked_connect(sock, address):
        if isinstance(address, tuple) and address[0] not in {"127.0.0.1", "::1", "localhost"}:
            raise AssertionError("Test dış ağa bağlanamaz")
        return original_connect(sock, address)

    def checked_lookup(host, *args, **kwargs):
        if host not in {"127.0.0.1", "::1", "localhost", None, b"localhost", b"127.0.0.1"}:
            raise AssertionError("Test dış DNS sorgusu yapamaz")
        return original_lookup(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", checked_connect)
    monkeypatch.setattr(socket, "getaddrinfo", checked_lookup)


@pytest.fixture(autouse=True)
def isolated_credentials(monkeypatch):
    # Gerçek oturum anahtarları sağlayıcı testine veya hata çıktısına taşınmaz.
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1,::1")


@pytest.fixture(autouse=True, scope="session")
def isolated_qsettings(tmp_path_factory):
    # Pet konumu/görünürlüğü gerçek kullanıcı ayarlarına yazılmasın (kural 8).
    try:
        from PySide6.QtCore import QSettings
    except ImportError:
        return
    path = str(tmp_path_factory.mktemp("qsettings"))
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, path)


@pytest.fixture(autouse=True)
def no_real_codex_lookup(monkeypatch):
    # code_agent_status artık yerleşik codex'i arar (PowerShell'e kadar gidebilir); testler gerçek
    # makinede Appx sorgusu çalıştırmaz ve süreç geneli önbelleği kirletmez (kural 8).
    import shutil

    from artemis.tools.plugins import code_agent

    monkeypatch.setattr(
        code_agent,
        "resolve_executable",
        lambda profile: None if profile.kind == "codex" else shutil.which(profile.command or ""),
    )
