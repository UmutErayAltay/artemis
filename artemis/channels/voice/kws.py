"""Cihaz içi uyandırma sözcüğü ÖN-KAPI'sı (ADR 0069): sherpa-onnx KeywordSpotter.

Anahtar sözcük kısıtlı kod çözme, canlı yazının (live_text) kullandığı AYNI Türkçe akışlı
transducer üzerinde yapılır: ``mihuai/turkish-stt-66m-v2`` (mihu Community License).
Ağırlıklar repoda yoktur; çalışma anında indirilir, yeniden dağıtılmaz. Bir eşleşme asla
komut çalıştırmaz ve tek başına uyandırmaz; nihai uyandırma kararı Whisper transkriptindedir
(``find_wake_word``). Bu modül yalnızca hangi bölütlerin STT'ye ulaşacağını azaltabilir.
"""

from __future__ import annotations

import importlib
import tempfile
from pathlib import Path
from typing import Any

from artemis.text import turkish_fold

SAMPLE_RATE = 16000
# int8 transducer dosyaları + sözlük; ad listesi modelin dosya düzenidir, kimliği değil.
MODEL_FILES = ("tokens.txt", "encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx")


MAX_SEGMENTATIONS = 64


def keyword_segmentations(
    word: str, vocabulary: set[str], *, limit: int = MAX_SEGMENTATIONS
) -> list[list[str]]:
    """``"▁" + katlanmış sözcük``'ün sözlükteki tüm token parçalamaları (en çok ``limit``).

    Tek parçalama tahmin edilmez: modelin BPE'si "artemis"i "▁ar t em is" böler, açgözlü
    en-uzun-önek ise "▁ar te m is" seçer ve gerçek modelde hiç tetiklemez (2026-11-14 ölçümü:
    tüm parçalamalarla 8/8 isabet, 12 olumsuz cümlede 0 yanlış alarm). Sözcük boşsa/boşluk
    içeriyorsa ya da hiç parçalanamıyorsa ValueError.
    """
    if not word or any(ch.isspace() for ch in word):
        raise ValueError("Uyandırma sözcüğü boş olamaz ve boşluk içeremez.")
    target = "▁" + turkish_fold(word)
    found: list[list[str]] = []

    def walk(position: int, prefix: list[str]) -> None:
        if len(found) >= limit:
            return
        if position == len(target):
            found.append(prefix)
            return
        for end in range(len(target), position, -1):
            piece = target[position:end]
            if piece in vocabulary:
                walk(end, [*prefix, piece])

    walk(0, [])
    if not found:
        raise ValueError(f"Uyandırma sözcüğü sözlükte token'lara ayrılamadı: {word!r}")
    return found


def read_vocabulary(tokens_path: str) -> set[str]:
    """tokens.txt'i UTF-8 okur; her boş olmayan satırın ilk alanını döndürür."""
    vocabulary: set[str] = set()
    with open(tokens_path, encoding="utf-8") as handle:
        for line in handle:
            parts = line.split()
            if parts:
                vocabulary.add(parts[0])
    return vocabulary


class WakeSpotter:
    """Akışlı transducer üzerinde uyandırma sözcüğü arar; ``feed`` eşleşme olursa True döner."""

    def __init__(
        self,
        model: str,
        revision: str,
        wake_word: str,
        *,
        threshold: float = 0.2,
        score: float = 1.0,
        num_threads: int = 1,
        modules: tuple[Any, Any, Any] | None = None,
    ) -> None:
        if not 0.0 < threshold < 1.0:
            raise ValueError("Eşik (threshold) 0 ile 1 arasında olmalıdır.")
        if score <= 0.0:
            raise ValueError("Anahtar sözcük skoru (score) 0'dan büyük olmalıdır.")
        self.model = model
        self.revision = revision
        self.wake_word = wake_word
        self.threshold = threshold
        self.score = score
        self.num_threads = num_threads
        self._modules = modules
        self._spotter: Any = None
        self._stream: Any = None
        self._np: Any = None
        self._keywords_path: str | None = None

    def prepare(self) -> None:
        """Engelleyici: paketleri içe aktarır, dosyaları indirir/önbellekten alır, kurar."""
        try:
            if self._modules is not None:
                numpy, sherpa, download = self._modules
            else:
                numpy = importlib.import_module("numpy")
                sherpa = importlib.import_module("sherpa_onnx")
                download = importlib.import_module("huggingface_hub").hf_hub_download
        except ImportError as error:
            raise RuntimeError(
                "Uyandırma sözcüğü algısı için `pip install .[stream]` gerekli."
            ) from error
        self._np = numpy
        tokens, encoder, decoder, joiner = (
            download(repo_id=self.model, filename=name, revision=self.revision)
            for name in MODEL_FILES
        )
        vocabulary = read_vocabulary(tokens)
        label = turkish_fold(self.wake_word)
        lines = [
            " ".join(tokens) + " @" + label
            for tokens in keyword_segmentations(self.wake_word, vocabulary)
        ]
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".txt", delete=False
        ) as handle:
            # Her parçalama bir satır, aynı etiket: "▁ar t em is @artemis" vb.
            handle.write("\n".join(lines) + "\n")
            self._keywords_path = handle.name
        self._spotter = sherpa.KeywordSpotter(
            tokens=tokens, encoder=encoder, decoder=decoder, joiner=joiner,
            num_threads=self.num_threads, keywords_file=self._keywords_path,
            keywords_score=self.score, keywords_threshold=self.threshold, provider="cpu",
        )
        self._stream = self._spotter.create_stream()

    def feed(self, pcm16: bytes) -> bool:
        """16 kHz int16 PCM besler; bu çağrıda eşleşme olduysa True döndürür."""
        if self._spotter is None or self._stream is None or self._np is None:
            raise RuntimeError("WakeSpotter.prepare() çağrılmadan feed() çağrılamaz.")
        samples = self._np.frombuffer(pcm16[: len(pcm16) // 2 * 2], dtype=self._np.int16)
        self._stream.accept_waveform(SAMPLE_RATE, samples.astype(self._np.float32) / 32768.0)
        hit = False
        while self._spotter.is_ready(self._stream):
            self._spotter.decode_stream(self._stream)
            # sherpa'da get_result bir dize döndürür; boş olmayan = eşleşme.
            if self._spotter.get_result(self._stream):
                hit = True
                self._spotter.reset_stream(self._stream)
        return hit

    def reset(self) -> None:
        # Taze akış: sıfırdan kod çözme durumu; hazır değilse sessiz no-op.
        if self._spotter is not None:
            self._stream = self._spotter.create_stream()

    def close(self) -> None:
        self._stream = None
        self._spotter = None
        self._np = None
        if self._keywords_path is not None:
            Path(self._keywords_path).unlink(missing_ok=True)
            self._keywords_path = None
