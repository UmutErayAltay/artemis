"""İsteğe bağlı canlı yazı: konuşurken akan, YALNIZ GÖSTERİM amaçlı metin (ADR 0047).

Çıktı hiçbir zaman komut, uyandırma, yankı veya söz kesme kararına girmez; komut hâlâ
``FasterWhisperRecognizer`` sonucundan çıkar. Model `sherpa_onnx` (Apache-2.0) ile çalışır; üçüncü
tarafın pip paketi kullanılmaz (kodu ayrı lisanslıdır). Ağırlıklar repoda değildir: ilk açılışta
config'teki depo ve SABİT revision'dan indirilir (THIRD_PARTY_NOTICES.md).
"""

from __future__ import annotations

import importlib
from typing import Any

SAMPLE_RATE = 16000
# int8 transducer dosyaları + sözlük; ad listesi modelin dosya düzenidir, kimliği değil.
MODEL_FILES = ("tokens.txt", "encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx")


class SherpaLiveTranscriber:
    """Akışlı transducer; ``feed`` o ana kadarki tüm sözün metnini döndürür."""

    def __init__(self, model: str, revision: str, *, num_threads: int = 2) -> None:
        self.model = model
        self.revision = revision
        self.num_threads = num_threads
        self._recognizer: Any = None
        self._stream: Any = None
        self._np: Any = None

    def prepare(self) -> None:
        """Engelleyici: paketleri içe aktarır, dosyaları indirir/önbellekten alır, kurar."""
        try:
            self._np = importlib.import_module("numpy")
            sherpa = importlib.import_module("sherpa_onnx")
            download = importlib.import_module("huggingface_hub").hf_hub_download
        except ImportError as error:
            raise RuntimeError("Canlı yazı için `pip install .[stream]` gerekli.") from error
        tokens, encoder, decoder, joiner = (
            download(repo_id=self.model, filename=name, revision=self.revision)
            for name in MODEL_FILES
        )
        self._recognizer = sherpa.OnlineRecognizer.from_transducer(
            tokens=tokens, encoder=encoder, decoder=decoder, joiner=joiner,
            num_threads=self.num_threads, decoding_method="modified_beam_search",
            provider="cpu",
        )
        self._stream = self._recognizer.create_stream()

    def feed(self, pcm16: bytes) -> str:
        samples = self._np.frombuffer(pcm16[: len(pcm16) // 2 * 2], dtype=self._np.int16)
        self._stream.accept_waveform(SAMPLE_RATE, samples.astype(self._np.float32) / 32768.0)
        while self._recognizer.is_ready(self._stream):
            self._recognizer.decode_stream(self._stream)
        result = self._recognizer.get_result(self._stream)
        return str(getattr(result, "text", result)).strip()

    def reset(self) -> None:
        if self._recognizer is not None:
            self._recognizer.reset(self._stream)

    def close(self) -> None:
        self._stream = None
        self._recognizer = None
