"""Ayarlar > Pet > "Pet üret" sohbeti (ADR 0048 madde 8 ve "resim modeli yolu" eki).

Metin modeli → SVG yolu: çağrı yalnız `ProviderRouter` üzerinden gider (allow_paid, bütçe defteri
ve denetim orada uygulanır; burada atlanmaz), QThread'de `asyncio.run` ile. Resim modeli yolu:
`brain.images.generate_image` (aynı kurallar) QThread'de; önce temel görsel, "Animasyonları üret"
ile 8 şerit çağrısı, canlı satır önizlemesi ve iptal. Ücretli resim modelinde çağrıdan önce
tutar×çağrı sayısı açıkça onaylatılır.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QImage, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from artemis.brain.errors import BudgetExceeded, ProviderError, RateLimitedError
from artemis.brain.images import generate_image
from artemis.brain.models import Message
from artemis.brain.router import ProviderRouter
from artemis.channels.desktop import pet_animator, pet_creator, pet_sprite
from artemis.channels.desktop import pet_image_pipeline as pipe
from artemis.channels.desktop.pet_sprite import PetError, PetExistsError
from artemis.config.settings import ModelConfig, load_settings
from artemis.safety.audit import AuditLog

CALL_TIMEOUT = 120.0
# Pencere/panel kapanırken çalışan çağrının QThread'i yok edilmesin (çökme); biter bitmez düşer.
_RUNNING: set[QThread] = set()


class ModelCall(QThread):
    """Tek model isteği; (yanıt, hata) iletir. Parent verilmez: bkz. `_RUNNING`."""

    done = Signal(str, str)

    def __init__(self, config_path: Path, alias: str, messages: list[Message]) -> None:
        super().__init__()
        self._config_path = config_path
        self._alias = alias
        self._messages = messages
        _RUNNING.add(self)
        self.finished.connect(lambda: _RUNNING.discard(self))

    def run(self) -> None:
        try:
            settings = load_settings(self._config_path)
            router = ProviderRouter(
                settings.model_copy(update={"primary_model": self._alias, "fallback_model": None}),
                AuditLog(settings.audit_path),
            )
            completion = asyncio.run(router.complete(self._messages, [], timeout=CALL_TIMEOUT))
        except BudgetExceeded as error:
            self.done.emit("", f"Ücretli model ya da bütçe sınırı: {error}")
        except RateLimitedError:
            self.done.emit("", "Model kota sınırında; biraz sonra yeniden dene.")
        except ProviderError as error:
            self.done.emit("", f"Model yanıt veremedi: {error}")
        except (OSError, ValueError) as error:
            self.done.emit("", f"Ayarlar ya da denetim kaydı kullanılamadı: {error}")
        else:
            self.done.emit(completion.content, "")


class ImageCall(QThread):
    """Resim hattı işi (temel görsel ya da 8 şerit); (sonuç, hata) iletir.

    `work(call, generate)` iş parçacığında çalışır; `generate(istem, referans, boyut)` ücret/izin
    kurallarını `generate_image` içinde uygular. İptal yalnız çağrılar arasında gözlenir.
    """

    progress = Signal(int, int, int)  # biten, toplam, şimdiki satır
    row_ready = Signal(int, object)  # satır, hücre listesi
    done = Signal(object, str)

    def __init__(
        self, config_path: Path, alias: str, work: Callable[[ImageCall, Any], object]
    ) -> None:
        super().__init__()
        self._config_path = config_path
        self._alias = alias
        self._work = work
        self.cancel_requested = False
        _RUNNING.add(self)
        self.finished.connect(lambda: _RUNNING.discard(self))

    def run(self) -> None:
        try:
            settings = load_settings(self._config_path)
            audit = AuditLog(settings.audit_path)

            def generate(prompt: str, reference: bytes | None, size: str) -> bytes:
                return generate_image(
                    settings, self._alias, prompt, reference=reference, size=size, audit=audit
                )

            result = self._work(self, generate)
        except pipe.Cancelled:
            self.done.emit(None, "İptal edildi.")
        except BudgetExceeded as error:
            self.done.emit(None, f"Ücretli model ya da bütçe sınırı: {error}")
        except RateLimitedError:
            self.done.emit(None, "Model kota sınırında; biraz sonra yeniden dene.")
        except (ProviderError, pipe.PetImageError) as error:
            self.done.emit(None, f"Resim modeli yanıt veremedi: {error}")
        except (OSError, ValueError) as error:
            self.done.emit(None, f"Ayarlar ya da denetim kaydı kullanılamadı: {error}")
        else:
            self.done.emit(result, "")


def _is_priced(model: ModelConfig) -> bool:
    return bool(model.input_per_million or model.output_per_million or model.image_cost_usd)


def image_calls_note(model: ModelConfig, calls: int) -> str:
    """Resim çağrılarının maliyet notu; ücretsiz abonelik proxy'sinde kota uyarısı."""
    if _is_priced(model):
        total = model.image_cost_usd * calls
        return (
            f"Ücretli resim modeli: çağrı başına {model.image_cost_usd} USD ayrılır; "
            f"{calls} çağrı = {total} USD (satır yeniden denemesiyle en çok {total * 2} USD). "
            "allow_paid kapalıysa reddedilir."
        )
    return "Çağrılar abonelik kotasından düşer (ayrı ücret yok)."


def price_warning(model: ModelConfig) -> str:
    if not (model.input_per_million or model.output_per_million):
        return ""
    return (
        f"Bu model ücretli (girdi {model.input_per_million}, çıktı {model.output_per_million} "
        "USD / 1M token). Çağrı bütçeden düşer; allow_paid kapalıysa reddedilir."
    )


class CreatorPanel(QWidget):
    installed = Signal(str)  # kurulan pet kimliği (pencere seçer)

    def __init__(self, config_path: Path, pets_dir: Path, *, demo: bool = False) -> None:
        super().__init__()
        self._config_path = config_path
        self._pets_dir = pets_dir
        self._models: dict[str, ModelConfig] = {}
        self._svg = ""  # son başarılı, temizlenmiş SVG
        self._sheet: QImage | None = None
        self._frames: list[QPixmap] = []
        self._frame = 0
        self._call: ModelCall | None = None
        self._image_call: ImageCall | None = None
        self._base: QImage | None = None  # resim yolu: kırpılmış temel görsel
        self._base_png = b""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 12, 0, 0)
        header = QHBoxLayout()
        header.addWidget(QLabel("Pet üret"))
        header.addStretch(1)
        self.reset_button = QPushButton("Yeni pet")
        self.reset_button.setAccessibleName("Yeni pet")
        self.reset_button.setToolTip(
            "Sohbeti ve kaydedilmemiş çizimi temizleyerek yeni pet başlatır."
        )
        self.reset_button.clicked.connect(self.reset_conversation)
        header.addWidget(self.reset_button)
        layout.addLayout(header)
        row = QHBoxLayout()
        self.method = QComboBox()
        self.method.setAccessibleName("Üretim yöntemi")
        self.method.addItem("SVG (metin modeli)")
        self.method.addItem("Resim modeli (animasyonlu)")
        self.method.currentIndexChanged.connect(self._fill_models)
        self.model_combo = QComboBox()
        self.model_combo.setAccessibleName("Model")
        self.model_combo.currentIndexChanged.connect(self._update_warning)
        row.addWidget(self.method)
        row.addWidget(self.model_combo, 1)
        layout.addLayout(row)
        self.warning = QLabel()
        self.warning.setWordWrap(True)
        self.warning.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.warning)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("Pet adı")
        self.name_edit.setMaxLength(80)
        self.name_edit.setAccessibleName("Pet adı")
        layout.addWidget(self.name_edit)
        self.chat_log = QPlainTextEdit()
        self.chat_log.setReadOnly(True)
        self.chat_log.setMaximumHeight(140)
        self.chat_log.setAccessibleName("Pet üretici sohbeti")
        layout.addWidget(self.chat_log)
        send_row = QHBoxLayout()
        self.input = QLineEdit()
        self.input.setPlaceholderText("Nasıl bir pet? (sonra: 'daha mavi yap', 'şapka ekle')")
        self.input.setAccessibleName("Pet isteği")
        self.input.returnPressed.connect(self.send)
        self.send_button = QPushButton("Gönder")
        self.send_button.clicked.connect(self.send)
        send_row.addWidget(self.input, 1)
        send_row.addWidget(self.send_button)
        layout.addLayout(send_row)
        job_row = QHBoxLayout()
        self.animate_button = QPushButton("Animasyonları üret")
        self.animate_button.clicked.connect(self.animate)
        self.cancel_button = QPushButton("İptal")
        self.cancel_button.clicked.connect(self.cancel)
        job_row.addWidget(self.animate_button)
        job_row.addWidget(self.cancel_button)
        job_row.addStretch(1)
        layout.addLayout(job_row)
        self.busy = QLabel()
        layout.addWidget(self.busy)
        preview_row = QHBoxLayout()
        self.preview = QLabel("Önizleme yok")
        self.preview.setFixedSize(pet_sprite.CELL_W, pet_sprite.CELL_H)
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setAccessibleName("Pet önizlemesi")
        self.state_combo = QComboBox()
        self.state_combo.setAccessibleName("Önizleme durumu")
        for name in pet_animator.STATE_NAMES:
            self.state_combo.addItem(name)
        self.state_combo.currentIndexChanged.connect(self._load_frames)
        preview_row.addWidget(self.preview)
        preview_row.addWidget(self.state_combo, 0, Qt.AlignmentFlag.AlignTop)
        preview_row.addStretch(1)
        layout.addLayout(preview_row)
        self.save_button = QPushButton("Kaydet ve seç")
        self.save_button.clicked.connect(self.save)
        layout.addWidget(self.save_button)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status)
        self._timer = QTimer(self)
        self._timer.setInterval(int(1000 / pet_sprite.FPS))
        self._timer.timeout.connect(self._tick)
        self._demo = demo
        self._load_models()
        self._update_buttons()

    # -------------------------------------------------------------- hazırlık

    def _load_models(self) -> None:
        if self._demo:
            self.status.setText("Demo modunda pet üretilmez.")
            return
        try:
            self._models = dict(load_settings(self._config_path).models)
        except (OSError, ValueError) as error:
            self.status.setText(f"Model listesi okunamadı: {error}")
            return
        if not self._models:
            self.status.setText("Config'te model tanımlı değil.")
        self._fill_models()

    @property
    def _image_mode(self) -> bool:
        return self.method.currentIndex() == 1

    def _fill_models(self) -> None:
        """Model listesi yönteme göre süzülür: SVG için sohbet, resim yolu için resim modelleri."""
        want = "image" if self._image_mode else "chat"
        self.model_combo.clear()
        for alias, model in self._models.items():
            if model.kind == want:
                self.model_combo.addItem(f"{alias} · {model.provider}/{model.model}", alias)
        if self._models and self.model_combo.count() == 0:
            self.status.setText(f"Bu yöntem için config'te '{want}' türünde model yok.")
        self._update_warning()
        self._update_buttons()

    def _update_warning(self) -> None:
        model = self._models.get(str(self.model_combo.currentData() or ""))
        if model is None:
            self.warning.setText("")
        elif self._image_mode:
            self.warning.setText(
                image_calls_note(model, len(pipe.ROWS)) + " Toplam süre yaklaşık 4-6 dk."
            )
        else:
            self.warning.setText(price_warning(model))

    def _busy(self) -> bool:
        return self._call is not None or self._image_call is not None

    def _update_buttons(self) -> None:
        idle = not self._busy()
        ready = not self._demo and self.model_combo.count() > 0
        self.reset_button.setEnabled(not self._demo and idle)
        self.send_button.setEnabled(ready and idle)
        self.input.setEnabled(ready and idle)
        self.model_combo.setEnabled(ready and idle)
        self.method.setEnabled(not self._demo and idle)
        self.save_button.setEnabled(not self._demo and idle and self._sheet is not None)
        self.animate_button.setVisible(self._image_mode)
        self.animate_button.setEnabled(ready and idle and self._base is not None)
        self.cancel_button.setVisible(self._image_mode)
        self.cancel_button.setEnabled(self._image_call is not None)
        if self._call is not None:
            self.busy.setText("Model düşünüyor…")
        elif self._image_call is None:
            self.busy.setText("")

    # ----------------------------------------------------------------- sohbet

    def reset_conversation(self) -> None:
        """Yeni taslak; kurulu petler ve model/yöntem seçimi korunur."""
        if self._busy() or self._demo:
            return
        self._timer.stop()
        self._svg = ""
        self._sheet = None
        self._base = None
        self._base_png = b""
        self._frames = []
        self._frame = 0
        self.state_combo.setCurrentIndex(0)
        self.preview.clear()
        self.preview.setText("Önizleme yok")
        self.chat_log.clear()
        self.input.clear()
        self.name_edit.clear()
        self.status.clear()
        self.busy.clear()
        self._update_buttons()
        self.input.setFocus()

    def _log(self, who: str, text: str) -> None:
        self.chat_log.appendPlainText(f"{who}: {text}")

    def send(self) -> None:
        text = self.input.text().strip()
        alias = self.model_combo.currentData()
        if not text or self._busy() or self._demo or alias is None:
            return
        if self._image_mode:
            self._send_image(text, str(alias))
            return
        self.input.clear()
        self._log("Sen", text)
        content = f"Mevcut SVG:\n{self._svg}\n\nİstek: {text}" if self._svg else text
        messages = [
            Message(role="system", content=pet_creator.SYSTEM_PROMPT),
            Message(role="user", content=content),
        ]
        self._call = ModelCall(self._config_path, str(alias), messages)
        self._call.done.connect(self._on_reply)
        self._update_buttons()
        self._call.start()

    def _on_reply(self, reply: str, error: str) -> None:
        self._call = None
        try:
            if error:
                self._log("Hata", error)
                return
            raw = pet_creator.extract_svg(reply)
            if raw is None:
                self._log("Hata", "Yanıtta SVG yok; isteği biraz değiştirip yeniden dene.")
                return
            try:
                svg = pet_creator.sanitize_svg(raw)
                base = pet_animator.render_svg(svg)
                eyeless = pet_creator.without_eyes(svg)
                blink = pet_animator.render_svg(eyeless) if eyeless != svg else None
            except pet_creator.SvgError as problem:
                self._log("Hata", f"SVG kullanılamadı: {problem}")
                return
            self._svg = svg
            self._sheet = pet_animator.build_sheet(base, blink)
            self._log("Model", "Hazır; önizlemeye bak. Düzeltmek için yaz.")
            self._load_frames()
        finally:
            self._update_buttons()

    # ------------------------------------------------------------ resim yolu

    def _start_image(
        self, alias: str, calls: int, work: Callable[[ImageCall, Any], object],
        on_done: Callable[[object, str], None],
    ) -> bool:
        """Ücretli modelde tutar×çağrıyı onaylatır; sonra işi başlatır."""
        model = self._models[alias]
        if _is_priced(model) and not self._confirm(
            image_calls_note(model, calls) + " Devam edilsin mi?"
        ):
            return False
        call = ImageCall(self._config_path, alias, work)
        call.progress.connect(self._on_progress)
        call.row_ready.connect(self._on_row)
        call.done.connect(on_done)
        self._image_call = call
        self._update_buttons()
        call.start()
        return True

    def _send_image(self, text: str, alias: str) -> None:
        base_png = self._base_png if self._base is not None else None

        def work(call: ImageCall, generate: Any) -> object:
            if base_png is None:
                return pipe.make_base(text, generate)
            return pipe.edit_base(text, base_png, generate)

        if self._start_image(alias, 1, work, self._on_base):
            self.input.clear()
            self._log("Sen", text)
            self.busy.setText("Temel görsel üretiliyor (~1 dk)…")

    def _on_base(self, result: object, error: str) -> None:
        self._image_call = None
        try:
            if error or not isinstance(result, tuple):
                self._log("Hata", error or "Beklenmeyen sonuç.")
                return
            self._base, self._base_png = result
            self._sheet = None  # temel değişti: eski animasyonlar geçersiz
            self._show_still(self._base)
            self._log("Model", "Temel görsel hazır. Düzeltmek için yaz ya da animasyonları üret.")
        finally:
            self._update_buttons()

    def _show_still(self, image: QImage) -> None:
        self._timer.stop()
        self._frames = []
        self.preview.setPixmap(QPixmap.fromImage(pet_animator._fit_cell(image)))

    def animate(self) -> None:
        alias = self.model_combo.currentData()
        if self._base is None or self._busy() or self._demo or alias is None:
            return
        base, base_png = self._base, self._base_png

        def work(call: ImageCall, generate: Any) -> object:
            return pipe.animate(
                base_png, base, generate,
                progress=lambda done, total, row: call.progress.emit(done, total, row),
                row_ready=lambda row, cells: call.row_ready.emit(row, cells),
                cancelled=lambda: call.cancel_requested,
            )

        if self._start_image(str(alias), len(pipe.ROWS), work, self._on_animated):
            self._log("Model", "Animasyonlar üretiliyor (yaklaşık 4-6 dk); iptal edebilirsin.")

    def _on_progress(self, done: int, total: int, row: int) -> None:
        self.busy.setText(f"{done + 1}/{total}: {pet_animator.STATE_NAMES[row].lower()}…")

    def _on_row(self, row: int, cells: object) -> None:
        """Satır gelir gelmez önizler (satırın kendi ölçeğiyle; nihai ölçek en sonda ortaktır)."""
        assert isinstance(cells, list)
        self.state_combo.blockSignals(True)
        self.state_combo.setCurrentIndex(row)
        self.state_combo.blockSignals(False)
        self._frames = [QPixmap.fromImage(c) for c in cells]
        self._frame = 0
        self._tick()
        self._timer.start()

    def _on_animated(self, result: object, error: str) -> None:
        self._image_call = None
        try:
            if error or not isinstance(result, tuple):
                self._log("Hata", error or "Beklenmeyen sonuç.")
                return
            self._sheet, fallback = result
            if fallback:
                names = ", ".join(pet_animator.STATE_NAMES[r] for r in fallback)
                self._log("Uyarı", f"Model çıktısı alınamayan (prosedürel) satırlar: {names}.")
            self._log("Model", "Animasyonlar hazır; durumları önizle, sonra kaydet.")
            self._load_frames()
        finally:
            self._update_buttons()

    def cancel(self) -> None:
        if self._image_call is not None:
            self._image_call.cancel_requested = True
            self.busy.setText("İptal ediliyor (süren çağrı bitince)…")

    # -------------------------------------------------------------- önizleme

    def _load_frames(self) -> None:
        if self._sheet is None:
            return
        self._frames = pet_animator.state_frames(self._sheet, self.state_combo.currentIndex())
        self._frame = 0
        self._tick()
        self._timer.start()

    def _tick(self) -> None:
        if self._frames:
            self.preview.setPixmap(self._frames[self._frame % len(self._frames)])
            self._frame += 1

    # ----------------------------------------------------------------- kayıt

    def _confirm(self, text: str) -> bool:
        return QMessageBox.question(self, "Pet", text) == QMessageBox.StandardButton.Yes

    def save(self) -> None:
        if self._sheet is None or self._demo:
            return
        name = self.name_edit.text().strip() or "Yeni pet"
        pet_id = pet_creator.slugify(name)
        overwrite = False
        while True:
            try:
                info = pet_animator.install(
                    self._sheet, pet_id, name, "Artemis pet üreticisiyle üretildi.",
                    self._pets_dir, overwrite=overwrite,
                )
            except PetExistsError as error:
                if overwrite or not self._confirm(f"{error} Üzerine yazılsın mı?"):
                    self.status.setText(str(error))
                    return
                overwrite = True
                continue
            except PetError as error:
                self.status.setText(f"Pet kurulamadı: {error}")
                return
            break
        self.status.setText(f"'{info.display_name}' kuruldu ve seçildi.")
        self.installed.emit(info.id)

    def shutdown(self) -> None:
        """Panel kapanırken: zamanlayıcı durur, çalışan çağrıların sonucu yok sayılır."""
        self._timer.stop()
        if self._call is not None:
            self._call.done.disconnect(self._on_reply)  # iş parçacığı `_RUNNING` ile yaşar
            self._call = None
        if self._image_call is not None:
            self._image_call.cancel_requested = True
            for signal in (self._image_call.progress, self._image_call.row_ready,
                           self._image_call.done):
                signal.disconnect()  # sonuç yok sayılır; iş parçacığı `_RUNNING`'de biter
            self._image_call = None

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 (Qt)
        self.shutdown()
        super().closeEvent(event)
