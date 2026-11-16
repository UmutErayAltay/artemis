"""WakeSpotter birim testleri: gerçek model, ağ ve sherpa-onnx kullanılmaz."""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Any

import pytest

from artemis.channels.voice.kws import (
    MODEL_FILES,
    SAMPLE_RATE,
    WakeSpotter,
    keyword_segmentations,
    read_vocabulary,
)
from artemis.text import turkish_fold

try:
    import numpy as _real_numpy
except ImportError:
    _real_numpy = None


class _FakeArray:
    """numpy import edilemiyorsa minimal sahte dizi: len/astype/bölme destekler."""

    def __init__(self, values: Any) -> None:
        self.values = list(values)

    def __len__(self) -> int:
        return len(self.values)

    def astype(self, _dtype: Any) -> _FakeArray:
        return self

    def __truediv__(self, other: float) -> _FakeArray:
        return _FakeArray([value / other for value in self.values])


class _FakeNumpy:
    """numpy'nin feed() içinde kullanılan küçücük alt kümesi."""

    int16 = "int16"
    float32 = "float32"

    @staticmethod
    def frombuffer(buffer: bytes, dtype: Any = None) -> _FakeArray:
        count = len(buffer) // 2
        return _FakeArray(struct.unpack(f"<{count}h", buffer[: count * 2]))


def _numpy_module() -> Any:
    return _real_numpy if _real_numpy is not None else _FakeNumpy


class _FakeStream:
    def __init__(self) -> None:
        self.waveforms: list[tuple[int, Any]] = []

    def accept_waveform(self, sample_rate: int, samples: Any) -> None:
        self.waveforms.append((sample_rate, samples))


class _FakeSpotter:
    """is_ready/decode_stream/get_result/reset_stream davranışı test tarafından sürülür."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        # keywords dosyası close()'da silindiği için içeriği kurulum anında yakala.
        with open(kwargs["keywords_file"], encoding="utf-8") as handle:
            self.keywords_content = handle.read()
        self.streams: list[_FakeStream] = []
        self.results: list[str] = []
        self.ready = False
        self.decode_count = 0
        self.reset_count = 0

    def create_stream(self) -> _FakeStream:
        stream = _FakeStream()
        self.streams.append(stream)
        return stream

    def is_ready(self, _stream: Any) -> bool:
        return self.ready

    def decode_stream(self, _stream: Any) -> None:
        self.decode_count += 1
        self.ready = False

    def get_result(self, _stream: Any) -> str:
        return self.results.pop(0) if self.results else ""

    def reset_stream(self, _stream: Any) -> None:
        self.reset_count += 1


class _FakeSherpa:
    """sherpa_onnx yerine geçer; kurulan spotter'ları kaydeder."""

    def __init__(self) -> None:
        self.spotters: list[_FakeSpotter] = []

    def KeywordSpotter(self, **kwargs: Any) -> _FakeSpotter:
        spotter = _FakeSpotter(**kwargs)
        self.spotters.append(spotter)
        return spotter


TOKEN_LINES = [
    "▁ar 0", "te 1", "m 2", "is 3", "▁a 4", "r 5", "t 6", "e 7", "i 8", "s 9", "em 10",
]


def _make_download(
    tmp_path: Path, token_lines: list[str]
) -> tuple[Any, list[tuple[str, str, str]]]:
    calls: list[tuple[str, str, str]] = []

    def download(*, repo_id: str, filename: str, revision: str) -> str:
        calls.append((repo_id, filename, revision))
        target = tmp_path / filename
        if filename == "tokens.txt":
            target.write_text("\n".join(token_lines) + "\n", encoding="utf-8")
        else:
            target.write_bytes(b"onnx")
        return str(target)

    return download, calls


def _prepare(
    tmp_path: Path,
    wake_word: str = "artemis",
    token_lines: list[str] | None = None,
    **kwargs: Any,
) -> tuple[WakeSpotter, _FakeSherpa, list[tuple[str, str, str]]]:
    download, calls = _make_download(tmp_path, token_lines or TOKEN_LINES)
    sherpa = _FakeSherpa()
    spotter = WakeSpotter(
        "mihuai/turkish-stt-66m-v2",
        "abc123",
        wake_word,
        modules=(_numpy_module(), sherpa, download),
        **kwargs,
    )
    spotter.prepare()
    return spotter, sherpa, calls


def test_keyword_segmentations_lists_every_split() -> None:
    vocabulary = {"▁ar", "te", "m", "is", "t", "em", "i", "s"}
    splits = keyword_segmentations("Artemis", vocabulary)
    # Modelin gerçek BPE bölmesi ("▁ar t em is") açgözlü seçimin dışındadır; ikisi de listede.
    assert ["▁ar", "t", "em", "is"] in splits and ["▁ar", "te", "m", "is"] in splits
    assert all("".join(split) == "▁artemis" for split in splits)
    assert len(splits) == len({tuple(split) for split in splits})


def test_keyword_segmentations_turkish_dotted_i_and_limit() -> None:
    assert turkish_fold("İRİS") == "iris"
    assert keyword_segmentations("İRİS", {"▁i", "r", "is"}) == [["▁i", "r", "is"]]
    many = {"▁a", "a"} | {"▁" + "a" * n for n in range(2, 9)} | {"a" * n for n in range(2, 9)}
    assert len(keyword_segmentations("aaaaaaaa", many, limit=5)) == 5


def test_keyword_segmentations_rejects_bad_words() -> None:
    with pytest.raises(ValueError):
        keyword_segmentations("xzq", {"▁x"})
    with pytest.raises(ValueError):
        keyword_segmentations("", {"▁a"})
    with pytest.raises(ValueError):
        keyword_segmentations("art emis", {"▁a"})


def test_read_vocabulary_first_field_of_nonempty_lines(tmp_path: Path) -> None:
    path = tmp_path / "tokens.txt"
    path.write_text("▁ar 0\nte 1\n\n   \ni 9 extra\n", encoding="utf-8")
    assert read_vocabulary(str(path)) == {"▁ar", "te", "i"}


def test_init_validation() -> None:
    with pytest.raises(ValueError):
        WakeSpotter("model", "rev", "artemis", threshold=0.0)
    with pytest.raises(ValueError):
        WakeSpotter("model", "rev", "artemis", threshold=1.0)
    with pytest.raises(ValueError):
        WakeSpotter("model", "rev", "artemis", score=0.0)


def test_prepare_passes_config_and_keywords_file(tmp_path: Path) -> None:
    spotter, sherpa, calls = _prepare(tmp_path, threshold=0.3, score=2.0, num_threads=3)
    try:
        assert len(calls) == 4
        assert {filename for _, filename, _ in calls} == set(MODEL_FILES)
        assert all(repo == "mihuai/turkish-stt-66m-v2" for repo, _, _ in calls)
        assert all(revision == "abc123" for _, _, revision in calls)
        fake = sherpa.spotters[0]
        assert fake.kwargs["keywords_threshold"] == 0.3
        assert fake.kwargs["keywords_score"] == 2.0
        assert fake.kwargs["num_threads"] == 3
        assert fake.kwargs["provider"] == "cpu"
        assert fake.kwargs["tokens"].endswith("tokens.txt")
        lines = fake.keywords_content.splitlines()
        assert lines and all(line.endswith(" @artemis") for line in lines)
        assert fake.keywords_content.endswith("\n")
    finally:
        spotter.close()


def test_feed_hit_only_on_result_and_ignores_odd_byte(tmp_path: Path) -> None:
    spotter, sherpa, _ = _prepare(tmp_path)
    try:
        fake = sherpa.spotters[0]
        pcm = b"\x01\x02\x03"  # tek bayt artakalan görmezden gelinir
        fake.ready = True
        fake.results.append("artemis")
        assert spotter.feed(pcm) is True
        stream = fake.streams[-1]
        assert len(stream.waveforms) == 1
        sample_rate, samples = stream.waveforms[0]
        assert sample_rate == SAMPLE_RATE
        assert len(samples) == len(pcm) // 2
        assert fake.reset_count == 1
        fake.ready = True
        fake.results.append("")
        assert spotter.feed(b"\x04\x05") is False
        assert fake.reset_count == 1
    finally:
        spotter.close()


def test_reset_creates_a_new_stream(tmp_path: Path) -> None:
    spotter, sherpa, _ = _prepare(tmp_path)
    try:
        fake = sherpa.spotters[0]
        assert len(fake.streams) == 1
        spotter.reset()
        assert len(fake.streams) == 2
    finally:
        spotter.close()
    # Hazır değilken reset() sessiz no-op'tur.
    fresh = WakeSpotter("model", "rev", "artemis")
    fresh.reset()


def test_close_removes_keywords_file_and_is_idempotent(tmp_path: Path) -> None:
    spotter, sherpa, _ = _prepare(tmp_path)
    keywords_path = Path(sherpa.spotters[0].kwargs["keywords_file"])
    assert keywords_path.exists()
    spotter.close()
    assert not keywords_path.exists()
    spotter.close()  # ikinci çağrı hata vermemeli
