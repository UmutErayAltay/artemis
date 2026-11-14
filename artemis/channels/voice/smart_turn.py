"""Smart Turn v3.2 (ONNX): sessizlik anında "konuşan cümlesini bitirdi mi?" tahmini (ADR 0068).

Model (BSD-2-Clause, pipecat-ai/smart-turn-v3) son 8 saniyelik 16 kHz sesi Whisper log-mel
özelliklerine çevirip [1, 80, 800] girdi bekler; çıktı zaten sigmoid olasılığıdır (tur tamam).
Özellikler ``faster_whisper`` çıkarıcısıyla üretilir (yeni bağımlılık yok); resmi HF
``WhisperFeatureExtractor`` ile farkı yalnız son sütundadır (ortalama mutlak fark ~1e-5).
"""

from __future__ import annotations

import hashlib
import importlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from artemis.channels.voice.denoise import _ort_error_types

SMART_TURN_MODEL_DIR = Path(__file__).parent / "models" / "smart_turn"
SMART_TURN_MODEL_NAME = "smart-turn-v3.2-cpu.onnx"
SMART_TURN_MODEL_SHA256 = "2bb026316b14a660486a75b1733cd3fbab8c2fd0314dc9af7be49f8cca967e4f"
SAMPLE_RATE = 16000
WINDOW_SECONDS = 8
WINDOW_SAMPLES = SAMPLE_RATE * WINDOW_SECONDS
FRAMES = 800


def verify_smart_turn_model(model_dir: Path = SMART_TURN_MODEL_DIR) -> bytes:
    """Model dosyasını okur ve sabitlenmiş sha256'ya karşı doğrular (bkz. ``verify_silero_model``).

    Model pakete dahildir; dosya yoksa paket bozuktur (indirme adımı yoktur).
    """
    path = model_dir / SMART_TURN_MODEL_NAME
    try:
        data = path.read_bytes()
    except OSError as error:
        raise OSError(
            f"Smart Turn model dosyası okunamadı: {path}. Artemis'i yeniden kurun "
            "(model pakete dahildir)."
        ) from error
    actual = hashlib.sha256(data).hexdigest()
    if actual != SMART_TURN_MODEL_SHA256:
        raise ValueError(
            f"Smart Turn model dosyasının sha256 değeri beklenenle eşleşmiyor: {path} "
            f"(beklenen {SMART_TURN_MODEL_SHA256[:12]}…, bulunan {actual[:12]}…). "
            "Dosyayı paketle gelen sürümle değiştirin."
        )
    return data


def whisper_features(pcm16: bytes) -> Any:
    """Son 8 sn'yi sola sıfırla doldurup normalleştirir; [1, 80, 800] float32 döner."""
    np = importlib.import_module("numpy")
    try:
        extractor_module = importlib.import_module("faster_whisper.feature_extractor")
    except ImportError as error:
        raise RuntimeError(
            "Smart Turn için faster-whisper gerekli: `pip install .[voice]`."
        ) from error
    data = pcm16[: len(pcm16) - len(pcm16) % 2]
    audio = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0
    audio = audio[-WINDOW_SAMPLES:]
    audio = np.pad(audio, (WINDOW_SAMPLES - audio.size, 0))
    audio = (audio - audio.mean()) / np.sqrt(audio.var() + 1e-7)
    mel = extractor_module.FeatureExtractor(chunk_length=WINDOW_SECONDS)(audio)[:, :FRAMES]
    return np.ascontiguousarray(mel[None], dtype=np.float32)


class SmartTurnDetector:
    """Smart Turn v3.2; ``TurnDetector`` protokolü (``AudioSegmenter`` sessizlikte sorar)."""

    def __init__(
        self,
        *,
        threshold: float = 0.5,
        model_dir: Path = SMART_TURN_MODEL_DIR,
        session: Any = None,
        features: Callable[[bytes], Any] | None = None,
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
                    "Smart Turn için onnxruntime gerekli: `pip install .[voice]`."
                ) from error
            self._errors = _ort_error_types(ort)
            options = ort.SessionOptions()
            options.intra_op_num_threads = 1
            options.inter_op_num_threads = 1
            options.log_severity_level = 3
            try:
                session = ort.InferenceSession(
                    verify_smart_turn_model(model_dir),
                    sess_options=options,
                    providers=["CPUExecutionProvider"],
                )
            except self._errors as error:
                raise RuntimeError(f"Smart Turn modeli yüklenemedi: {error}") from error
        self._session = session
        self._features = features or whisper_features
        self.threshold = threshold
        self.probability = 0.0

    def is_complete(self, pcm16: bytes) -> bool:
        """Konuşma bitmiş görünüyorsa True; olasılık ``probability`` alanında kalır."""
        try:
            outputs = self._session.run(None, {"input_features": self._features(pcm16)})
        except self._errors as error:
            raise RuntimeError(f"Smart Turn çıkarımı başarısız: {error}") from error
        self.probability = float(outputs[0].reshape(-1)[0])
        return self.probability >= self.threshold
