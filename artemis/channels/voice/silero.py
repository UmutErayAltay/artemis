"""Silero VAD (ONNX, akışlı) sarmalayıcı: 16 kHz int16 PCM -> konuşma olasılığı.

Silero VAD v5.1.2 (MIT) kullanılıyor. Model akışlıdır: her çalışma 512 yeni örnek
tüketir; girdinin başında 64 örneklik bağlam (önceki girdinin sonu) ve durum
([2, 1, 128] GRU gizli durumu) taşınır. 320/480 örneklik mikrofon kareleri 512'ye
hizalı olmadığından gelen örnekler tamponlanır; tam pencere oluşunca model bir kez
çalışır ve ``probability`` son pencerenin olasılığıyla güncellenir.
"""

from __future__ import annotations

import hashlib
import importlib
from pathlib import Path
from typing import Any

from artemis.channels.voice.denoise import _ort_error_types

SILERO_MODEL_DIR = Path(__file__).parent / "models" / "silero"
SILERO_MODEL_NAME = "silero_vad.onnx"
SILERO_MODEL_SHA256 = "2623a2953f6ff3d2c1e61740c6cdb7168133479b267dfef114a4a3cc5bdd788f"
WINDOW = 512
CONTEXT = 64
SAMPLE_RATE = 16000


def verify_silero_model(model_dir: Path = SILERO_MODEL_DIR) -> bytes:
    """``silero_vad.onnx`` dosyasını okur ve sabitlenmiş sha256'ya karşı doğrular.

    Doğrulanan baytlar doğrudan oturuma verilir (doğrulama ile yükleme arasında dosya değişemez).
    Model pakete dahildir; dosya yoksa paket bozuktur (indirme adımı yoktur).
    """
    path = model_dir / SILERO_MODEL_NAME
    try:
        data = path.read_bytes()
    except OSError as error:
        raise OSError(
            f"Silero VAD model dosyası okunamadı: {path}. Artemis'i yeniden kurun "
            "(model pakete dahildir)."
        ) from error
    actual = hashlib.sha256(data).hexdigest()
    if actual != SILERO_MODEL_SHA256:
        raise ValueError(
            f"Silero VAD model dosyasının sha256 değeri beklenenle eşleşmiyor: {path} "
            f"(beklenen {SILERO_MODEL_SHA256[:12]}…, bulunan {actual[:12]}…). "
            "Dosyayı paketle gelen sürümle değiştirin."
        )
    return data


class SileroVAD:
    """Silero VAD v5.1.2 ONNX; akışlı konuşma algılama (``VoiceActivityDetector`` protokolü)."""

    def __init__(
        self,
        *,
        threshold: float = 0.5,
        model_dir: Path = SILERO_MODEL_DIR,
        session: Any = None,
    ) -> None:
        if not 0.0 < threshold < 1.0:
            raise ValueError(
                f"threshold 0 ile 1 arasında bir değer olmalı (0 < threshold < 1), "
                f"{threshold} verildi"
            )
        self._errors: tuple[type[Exception], ...] = ()
        if session is None:
            try:
                ort = importlib.import_module("onnxruntime")
            except ImportError as error:
                raise RuntimeError(
                    "Silero VAD için onnxruntime gerekli: `pip install .[voice]`."
                ) from error
            self._errors = _ort_error_types(ort)
            options = ort.SessionOptions()
            options.intra_op_num_threads = 1
            options.inter_op_num_threads = 1
            options.log_severity_level = 3
            try:
                session = ort.InferenceSession(
                    verify_silero_model(model_dir),
                    sess_options=options,
                    providers=["CPUExecutionProvider"],
                )
            except self._errors as error:
                raise RuntimeError(f"Silero VAD modeli yüklenemedi: {error}") from error
        self._session = session
        self.threshold = threshold
        self.probability = 0.0
        self.reset()

    def reset(self) -> None:
        """Akış durumunu sıfırlar: durum/bağlam/tampon temizlenir, olasılık 0.0 olur."""
        np = importlib.import_module("numpy")
        self._state = np.zeros((2, 1, 128), dtype="float32")
        self._context = np.zeros(CONTEXT, dtype="float32")
        self._buffer = np.zeros(0, dtype="float32")
        self.probability = 0.0

    def is_speech(self, pcm: bytes) -> bool:
        """16 kHz int16 little-endian PCM karesi alır; olasılığın eşiği aşıp aşmadığını döner."""
        np = importlib.import_module("numpy")
        # Tek kalan son bayt atılamaz (yarım örnek); frombuffer çağırmadan önce kesilir.
        data = pcm[: len(pcm) - len(pcm) % 2]
        raw = np.frombuffer(data, dtype="<i2")
        samples = raw.astype(np.float32) / 32768.0
        self._buffer = np.ascontiguousarray(np.concatenate([self._buffer, samples]))
        while self._buffer.size >= WINDOW:
            window = self._buffer[:WINDOW]
            self._buffer = self._buffer[WINDOW:].copy()
            feed_input = np.concatenate([self._context, window]).reshape(1, CONTEXT + WINDOW)
            feed_input = np.ascontiguousarray(feed_input, dtype=np.float32)
            feeds = {
                "input": feed_input,
                "state": np.ascontiguousarray(self._state, dtype=np.float32),
                "sr": np.array(SAMPLE_RATE, dtype=np.int64),
            }
            try:
                outputs = self._session.run(None, feeds)
            except self._errors as error:
                raise RuntimeError(f"Silero VAD çıkarımı başarısız: {error}") from error
            prob = outputs[0]
            new_state = outputs[1]
            self.probability = float(prob.reshape(-1)[0])
            self._context = np.ascontiguousarray(feed_input[0, -CONTEXT:].copy(), dtype=np.float32)
            self._state = new_state
        # Tam pencere yoksa önceki olasılık geçerlidir (başta 0.0 -> False).
        return self.probability >= self.threshold
