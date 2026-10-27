"""Ayarlar > Pet > "Pet üret" sohbeti (ADR 0048 madde 8, Paket 3a: metin modeli → SVG).

Model çağrısı yalnız `ProviderRouter` üzerinden gider (allow_paid, bütçe defteri ve denetim
orada uygulanır; burada atlanmaz). Çağrı QThread'de `asyncio.run` ile yapılır. Resim modeli
yolu Paket 3b'dir; arayüzde kapalı "yakında" seçeneği olarak durur.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QImage, QPixmap, QStandardItemModel
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
from artemis.brain.models import Message
from artemis.brain.router import ProviderRouter
from artemis.channels.desktop import pet_animator, pet_creator, pet_sprite
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
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 12, 0, 0)
        layout.addWidget(QLabel("Pet üret"))
        row = QHBoxLayout()
        self.method = QComboBox()
        self.method.setAccessibleName("Üretim yöntemi")
        self.method.addItem("SVG (metin modeli)")
        self.method.addItem("Resim modeli · yakında")
        item_model = self.method.model()
        assert isinstance(item_model, QStandardItemModel)
        item = item_model.item(1)
        assert item is not None
        item.setEnabled(False)  # Paket 3b
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
        for alias, model in self._models.items():
            self.model_combo.addItem(f"{alias} · {model.provider}/{model.model}", alias)
        if not self._models:
            self.status.setText("Config'te model tanımlı değil.")
        self._update_warning()

    def _update_warning(self) -> None:
        model = self._models.get(str(self.model_combo.currentData() or ""))
        self.warning.setText(price_warning(model) if model else "")

    def _update_buttons(self) -> None:
        idle = self._call is None
        ready = not self._demo and bool(self._models)
        self.send_button.setEnabled(ready and idle)
        self.input.setEnabled(ready and idle)
        self.model_combo.setEnabled(ready and idle)
        self.save_button.setEnabled(ready and idle and self._sheet is not None)
        self.busy.setText("" if idle else "Model düşünüyor…")

    # ----------------------------------------------------------------- sohbet

    def _log(self, who: str, text: str) -> None:
        self.chat_log.appendPlainText(f"{who}: {text}")

    def send(self) -> None:
        text = self.input.text().strip()
        alias = self.model_combo.currentData()
        if not text or self._call is not None or self._demo or alias is None:
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
        """Panel kapanırken: zamanlayıcı durur, çalışan çağrının sonucu yok sayılır."""
        self._timer.stop()
        if self._call is not None:
            self._call.done.disconnect(self._on_reply)  # iş parçacığı `_RUNNING` ile yaşar
            self._call = None

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 (Qt)
        self.shutdown()
        super().closeEvent(event)
