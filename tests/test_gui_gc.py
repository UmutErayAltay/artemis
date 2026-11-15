import gc
import threading
import time

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from artemis.channels.desktop import gui_gc  # noqa: E402


@pytest.fixture
def fast_gc():
    """Oturumdaki kurulumu geçici olarak hızlı aralıklı olanla değiştirir, sonunda geri koyar."""
    current = QApplication.instance()
    app = current if isinstance(current, QApplication) else QApplication([])
    was_installed = gui_gc._collector is not None
    gui_gc.uninstall()
    gui_gc.install(app, interval_ms=10)
    yield app
    gui_gc.uninstall()
    if was_installed:
        gui_gc.install(app)


class _Node:
    def __init__(self, seen: list[int]) -> None:
        self.seen = seen
        self.me = self  # döngü: yalnız GC toplayabilir

    def __del__(self) -> None:
        self.seen.append(threading.get_ident())


def _pump(app: QApplication, seen: list[int]) -> None:
    deadline = time.monotonic() + 5
    while not seen and time.monotonic() < deadline:
        # Eşik aşılmadan gc.collect çağrılmaz; testte sayacı yapay olarak aşırız.
        _ = [[] for _ in range(gc.get_threshold()[0] + 50)]
        app.processEvents()
        time.sleep(0.02)


def test_install_disables_gc_and_uninstall_restores(fast_gc) -> None:
    assert not gc.isenabled()
    gui_gc.install(fast_gc)  # ikinci çağrı etkisiz
    gui_gc.uninstall()
    assert gc.isenabled()


def test_cycle_collected_on_timer(fast_gc) -> None:
    seen: list[int] = []
    _Node(seen)
    _pump(fast_gc, seen)
    assert seen == [threading.get_ident()]


def test_cycle_dropped_on_worker_thread_is_collected_on_gui_thread(fast_gc) -> None:
    seen: list[int] = []

    def work() -> None:
        _Node(seen)
        # Çalışan iş parçacığında bol ayırma: otomatik GC açık olsaydı burada toplanırdı.
        for _ in range(20):
            _ = [[] for _ in range(5000)]

    worker = threading.Thread(target=work)
    worker.start()
    worker.join()
    assert seen == []
    _pump(fast_gc, seen)
    assert seen == [threading.get_ident()]
