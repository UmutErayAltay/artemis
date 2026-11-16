"""Döngüsel çöp toplamayı yalnız GUI iş parçacığına bağlar.

Neden: Python'un döngüsel GC'si hangi iş parçacığı bellek ayırırsa onda çalışır. İçinde PySide6
sarmalayıcısı bulunan bir referans döngüsü köprü (asyncio) iş parçacığında toplanırsa Qt widget'ı
GUI dışı iş parçacığında siler ya da dokunur ve rastgele access violation çıkar. Tek tek lambda
döngülerini kapatmak olasılığı azaltır ama yok etmez (her yeni bağlantı yeniden riske sokar);
bu yüzden otomatik GC kapatılır ve eşikleri GUI iş parçacığındaki bir zamanlayıcı yoklar.
Referans sayımı (döngüsüz nesneler) etkilenmez.
"""

from __future__ import annotations

import gc

from PySide6.QtCore import QObject, QTimer
from PySide6.QtWidgets import QApplication


def collect_due() -> None:
    """Eşiği aşan en yüksek nesil toplanır; aşan yoksa neredeyse bedava. Çağıran GUI iş
    parçacığında olmalıdır (zamanlayıcı ya da test temizliği)."""
    counts = gc.get_count()
    thresholds = gc.get_threshold()
    for generation in (2, 1, 0):
        if counts[generation] > thresholds[generation]:
            gc.collect(generation)
            return


class _Collector(QObject):
    def __init__(self, app: QApplication, interval_ms: int) -> None:
        super().__init__(app)
        self._timer = QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self.collect_due)
        self._timer.start()

    def collect_due(self) -> None:
        collect_due()

    def stop(self) -> None:
        self._timer.stop()


_collector: _Collector | None = None


def install(app: QApplication, interval_ms: int = 500) -> None:
    """Otomatik GC'yi kapatıp GUI zamanlayıcısına devreder; ikinci çağrı etkisizdir."""
    global _collector
    if _collector is not None:
        return
    gc.disable()
    _collector = _Collector(app, interval_ms)


def uninstall() -> None:
    """Zamanlayıcıyı durdurup otomatik GC'yi geri açar (testler için)."""
    global _collector
    if _collector is None:
        return
    _collector.stop()
    _collector.deleteLater()
    _collector = None
    gc.enable()
