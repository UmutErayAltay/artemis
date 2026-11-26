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
    # Telegram token'ı için gerçek Windows kasası okunmaz (ADR 0051); testler kendi sahtesini koyar.
    monkeypatch.setattr("artemis.channels.telegram.api.read_secret", lambda target: None)


@pytest.fixture(autouse=True)
def isolated_user_data_dir(monkeypatch, tmp_path_factory):
    # Beceri klasörü varsayılanı gerçek APPDATA/XDG yoluna düşmesin (kural 8).
    base = tmp_path_factory.mktemp("userdata")
    monkeypatch.setenv("APPDATA", str(base))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(base))


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
def no_real_gpu_probe(monkeypatch):
    # Ayar paneli açılışı gerçek nvidia-smi çalıştırmaz (kural 8); GpuProbe testte doğrudan denenir.
    try:
        from artemis.channels.desktop import voice_settings_section
    except ImportError:
        return
    monkeypatch.setattr(voice_settings_section, "start_gpu_probe", lambda panel: None)


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


@pytest.fixture(autouse=True)
def qt_widgets_deleted_by_qt():
    """Test biterken kalan üst düzey widget'ları Qt'ye sildirir (Windows çıkış çökmesi, ~%30-50).

    Neden: paneller `lambda: self.set_busy(...)` gibi bağlantılarla referans döngüsüne girer;
    döngüdeki PySide sarmalayıcıları `gc` tarafından (çoğu kez yorumlayıcı kapanışında) RASTGELE
    sırada yıkılır ve çocuk widget'a erişim access violation verir (pytest "passed" yazdıktan
    sonra exit 139). `deleteLater` + DeferredDelete C++ nesnelerini önce ve ebeveyn -> çocuk
    sırasıyla yok eder; sarmalayıcılar sonradan geçersiz sayılıp zararsız toplanır.
    """
    import sys

    def gui_app():
        # Qt'yi hiç yüklemeyen testlerde PySide içe aktarma maliyeti ödenmez.
        widgets = sys.modules.get("PySide6.QtWidgets")
        return None if widgets is None else widgets.QApplication.instance()

    # Döngüsel GC yalnız GUI iş parçacığında (gui_gc docstring'i); uygulamayı ilk Qt testi
    # kurar, bu yüzden ilk kez teardown'da da denenir. İdempotent.
    if gui_app() is not None:
        from artemis.channels.desktop import gui_gc

        gui_gc.install(gui_app())
    yield
    app = gui_app()
    if app is None:
        return
    from artemis.channels.desktop import gui_gc

    gui_gc.install(app)
    from PySide6.QtCore import QCoreApplication, QEvent

    for widget in app.topLevelWidgets():
        widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    # Testler olay döngüsünü az çalıştırır; otomatik GC kapalıyken çöp birikir ve gui_gc
    # zamanlayıcısı onu sonraki testin içinde tek seferde toplar (zamanlama testleri, ör. Qt
    # kalp atışı, 60 ms'lik pencereyi kaçırır). Her test kendi çöpünü burada, ana iş
    # parçacığında öder. Gerçek uygulamada olay döngüsü sürekli döndüğü için birikme olmaz.
    # Yalnız eşiği aşan nesil toplanır (otomatik GC'nin kuralı); her testte tam toplama
    # takımı 5,5 dakikadan 9 dakikaya çıkarıyordu.
    gui_gc.collect_due()


@pytest.fixture(autouse=True)
def auto_mode_warning_accepted(monkeypatch):
    # Uyarı modal olduğundan testte asılı kalırdı; onay/ret yolları kendi testinde ayrıca sınanır.
    try:
        from artemis.channels.desktop.app import DesktopWindow
    except ImportError:
        return
    monkeypatch.setattr(DesktopWindow, "_confirm_auto_mode", lambda self: True)


@pytest.fixture(autouse=True)
def _no_modal_unsaved_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ayarlardan çıkışta açılan "kaydedilmemiş değişiklik" penceresi testi kilitlemesin.

    Varsayılan cevap "kaydet"tir (eski otomatik kayıt davranışı); at/vazgeç yollarını sınayan
    testler kendi cevabını verir. PySide6'sız CI işinde modül yoktur, fixture bir şey yapmaz."""
    try:
        from artemis.channels.desktop import window_restart
    except ImportError:
        return
    monkeypatch.setattr(window_restart, "_ask_unsaved", lambda _window: "save")


@pytest.fixture(autouse=True)
def close_store_ownerships(monkeypatch):
    # Bekleyen temizlik nedeniyle shutdown'ın bilerek açık bıraktığı iş deposu kilidi
    # (code_agent.lock) test bitince kapanır: sızan dosya tutacağı ResourceWarning üretir ve
    # Windows'ta geçici klasör silinirken PermissionError verir.
    from artemis.tools import code_agent_jobs

    opened: list[code_agent_jobs.StoreOwnership] = []
    original = code_agent_jobs.StoreOwnership

    def tracking(path):
        ownership = original(path)
        opened.append(ownership)
        return ownership

    monkeypatch.setattr(code_agent_jobs, "StoreOwnership", tracking)
    yield
    for ownership in opened:
        ownership.close()
