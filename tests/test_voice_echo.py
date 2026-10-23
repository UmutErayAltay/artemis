"""Yankı iptali modülünün donanım gerektirmeyen testleri."""

from __future__ import annotations

import asyncio
import logging
import struct
import threading
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

import pytest

from artemis.channels.voice.core import AudioFrame
from artemis.channels.voice.echo import (
    EchoCancelledMicrophone,
    EchoCanceller,
    EchoReference,
)

np: Any = pytest.importorskip("numpy")


def _pack_i16(values: list[int]) -> bytes:
    return struct.pack(f"<{len(values)}h", *values)


@dataclass
class FakeFrame:
    data: memoryview[int]


class FakeApm:
    """Mükemmel çıkarma yapan sahte AEC işlemci."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.references: list[bytes] = []

    def process_reverse_stream(self, data: Any) -> None:
        self.events.append("reverse")
        self.references.append(bytes(data.data))

    def set_stream_delay_ms(self, delay_ms: int) -> None:
        self.events.append(f"delay:{delay_ms}")

    def process_stream(self, data: Any) -> None:
        self.events.append("stream")
        samples = np.frombuffer(data.data, dtype="<i2").copy()
        reference = np.frombuffer(self.references[-1], dtype="<i2")
        samples -= reference
        data.data[:] = samples.astype("<i2").tobytes()


class FakeApmFactory:
    def __init__(self) -> None:
        self.instances: list[FakeApm] = []

    def __call__(self) -> FakeApm:
        instance = FakeApm()
        self.instances.append(instance)
        return instance


def fake_frame_factory(data: bytes, _rate: int, samples: int) -> FakeFrame:
    return FakeFrame(memoryview(bytearray(data)))


class FakeResampler:
    """Her ikinci örneği atan sahte çevirici."""

    def __init__(self, factor: int = 2) -> None:
        self.factor = factor
        self.process_calls = 0

    def process(self, pcm: bytes) -> bytes:
        self.process_calls += 1
        count = len(pcm) // 2
        values = list(struct.unpack(f"<{count}h", pcm))
        kept = values[:: self.factor]
        return _pack_i16(kept)


class RecordingResamplerFactory:
    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []
        self.instances: list[FakeResampler] = []

    def __call__(self, input_rate: int, output_rate: int) -> FakeResampler:
        self.calls.append((input_rate, output_rate))
        instance = FakeResampler()
        self.instances.append(instance)
        return instance


class FakeMicrophone:
    def __init__(self, frames: list[AudioFrame]) -> None:
        self._frames = frames
        self.discard_pending_calls = 0
        self.pause_calls = 0
        self.resume_calls = 0
        self.close_called = False

    async def frames(self) -> AsyncIterator[AudioFrame]:
        for frame in self._frames:
            yield frame

    def discard_pending(self) -> None:
        self.discard_pending_calls += 1

    def pause(self) -> None:
        self.pause_calls += 1

    def resume(self) -> None:
        self.resume_calls += 1

    async def close(self) -> None:
        self.close_called = True


class FakeCanceller:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.reset_error: Exception | None = None
        self.process_calls = 0
        self.reset_calls = 0
        self.start_times: list[float | None] = []

    def process(self, pcm: bytes, start_time: float | None = None) -> bytes:
        self.process_calls += 1
        self.start_times.append(start_time)
        if self.error is not None:
            raise self.error
        return b"\x02\x00" * (len(pcm) // 2)

    def reset(self) -> None:
        self.reset_calls += 1
        if self.reset_error is not None:
            raise self.reset_error


async def _collect_frames(microphone: EchoCancelledMicrophone) -> list[AudioFrame]:
    return [frame async for frame in microphone.frames()]


def test_reference_same_rate_round_trip_and_zero_padding() -> None:
    reference = EchoReference(16000)
    pcm = _pack_i16([100, -200, 300])

    reference.feed(pcm, 16000, 1, 2)

    assert reference.pull(3) == pcm
    assert reference.pull(2) == b"\x00" * 4


def test_reference_averages_stereo_channels() -> None:
    reference = EchoReference(16000)
    stereo = struct.pack("<hhhh", 100, 300, -100, -300)

    reference.feed(stereo, 16000, 2, 2)

    assert reference.pull(2) == _pack_i16([200, -200])


def test_reference_converts_int32_and_int8() -> None:
    reference = EchoReference(16000)

    reference.feed(struct.pack("<i", 0x12340000), 16000, 1, 4)
    assert reference.pull(1) == _pack_i16([0x1234])

    reference.feed(struct.pack("<i", -65536), 16000, 1, 4)
    assert reference.pull(1) == _pack_i16([-1])

    reference.feed(bytes([100, 0x80]), 16000, 1, 1)
    assert reference.pull(2) == _pack_i16([25600, -32768])


def test_reference_drops_oldest_samples_when_buffer_is_full() -> None:
    reference = EchoReference(16000, max_ms=10)
    values = list(range(200))

    reference.feed(_pack_i16(values), 16000, 1, 2)

    result = reference.pull(200)
    expected = _pack_i16(values[40:]) + bytes(40 * 2)
    assert result == expected


def test_reference_rejects_invalid_sample_width() -> None:
    reference = EchoReference(16000)

    with pytest.raises(ValueError, match="Örnek genişliği"):
        reference.feed(b"\x00\x00", 16000, 1, 3)


def test_reference_clear_empties_buffer() -> None:
    reference = EchoReference(16000)
    reference.feed(_pack_i16([1, 2, 3]), 16000, 1, 2)

    reference.clear()

    assert reference.pull(3) == b"\x00" * 6


def test_reference_caches_resampler_per_input_rate() -> None:
    factory = RecordingResamplerFactory()
    reference = EchoReference(16000, resampler_factory=factory)

    reference.feed(_pack_i16([1, 2, 3, 4]), 32000, 1, 2)
    reference.feed(_pack_i16([5, 6]), 32000, 1, 2)

    assert factory.calls == [(32000, 16000)]
    assert factory.instances[0].process_calls == 2
    assert reference.pull(3) == _pack_i16([1, 3, 5])

    reference.feed(_pack_i16([9, 10]), 24000, 1, 2)
    assert factory.calls == [(32000, 16000), (24000, 16000)]

    reference.clear()
    reference.feed(_pack_i16([11, 12]), 32000, 1, 2)
    assert len(factory.calls) == 3
    assert len(factory.instances) == 3


def test_reference_retarget_clears_buffer_and_resampler_cache() -> None:
    factory = RecordingResamplerFactory()
    reference = EchoReference(16000, resampler_factory=factory)

    reference.feed(_pack_i16([1, 2]), 32000, 1, 2)
    reference.retarget(16000)

    assert reference.pull(1) == _pack_i16([1])
    assert factory.calls == [(32000, 16000)]

    reference.retarget(48000)

    assert reference.pull(1) == b"\x00\x00"
    reference.feed(_pack_i16([3, 4]), 32000, 1, 2)

    assert factory.calls == [(32000, 16000), (32000, 48000)]


def test_canceller_cancels_and_records_call_order() -> None:
    reference = EchoReference(16000)
    reference_values = list(range(320))
    reference.feed(_pack_i16(reference_values), 16000, 1, 2)

    factory = FakeApmFactory()
    canceller = EchoCanceller(
        16000,
        reference,
        apm_factory=factory,
        frame_factory=fake_frame_factory,
    )
    microphone_values = list(range(1000, 1320))

    output = canceller.process(_pack_i16(microphone_values))

    assert len(output) == len(microphone_values) * 2
    assert output == _pack_i16([1000] * 320)
    assert factory.instances[0].events == [
        "reverse",
        "delay:50",
        "stream",
        "reverse",
        "delay:50",
        "stream",
    ]


def test_canceller_rejects_invalid_rate_and_frame_size() -> None:
    reference = EchoReference(16000)

    with pytest.raises(ValueError, match="16000 veya 48000"):
        EchoCanceller(
            44100,
            reference,
            apm_factory=FakeApmFactory(),
            frame_factory=fake_frame_factory,
        )

    canceller = EchoCanceller(
        16000,
        reference,
        apm_factory=FakeApmFactory(),
        frame_factory=fake_frame_factory,
    )
    with pytest.raises(ValueError, match="10 ms"):
        canceller.process(b"\x00" * (160 * 2 - 2))


def test_canceller_reset_keeps_apm_and_clears_reference() -> None:
    reference = EchoReference(16000)
    reference.feed(_pack_i16([1, 2, 3]), 16000, 1, 2)
    factory = FakeApmFactory()
    canceller = EchoCanceller(
        16000,
        reference,
        apm_factory=factory,
        frame_factory=fake_frame_factory,
    )

    canceller.reset()

    assert len(factory.instances) == 1
    assert reference.pull(3) == b"\x00" * 6


def test_canceller_reset_from_another_thread_is_safe() -> None:
    reference = EchoReference(16000)
    process_entered = threading.Event()
    release_process = threading.Event()
    reset_started = threading.Event()
    reset_done = threading.Event()
    output_lengths: list[int] = []

    class BlockingApm(FakeApm):
        def process_stream(self, data: Any) -> None:
            process_entered.set()
            if not release_process.wait(5):
                raise TimeoutError("APM işlemi zamanında sürdürülemedi.")
            super().process_stream(data)

    apm = BlockingApm()
    canceller = EchoCanceller(
        16000,
        reference,
        apm_factory=lambda: apm,
        frame_factory=fake_frame_factory,
    )
    pcm = _pack_i16([1] * 160)

    def process_loop() -> None:
        for index in range(100):
            output = canceller.process(pcm)
            output_lengths.append(len(output))
            if index == 0 and not reset_done.wait(5):
                raise TimeoutError("Yankı iptalci sıfırlaması zamanında tamamlanmadı.")

    def reset_from_thread() -> None:
        reset_started.set()
        try:
            canceller.reset()
        finally:
            reset_done.set()

    with ThreadPoolExecutor(max_workers=2) as executor:
        process_future = executor.submit(process_loop)
        assert process_entered.wait(5)

        reset_future = executor.submit(reset_from_thread)
        assert reset_started.wait(5)

        release_process.set()
        process_future.result(timeout=10)
        reset_future.result(timeout=10)

    assert output_lengths == [len(pcm)] * 100


def test_microphone_processes_frames_and_preserves_frame_metadata() -> None:
    inner_frames = [
        AudioFrame(
            pcm=_pack_i16([1] * 160),
            timestamp=12.5,
            sample_rate=48000,
            raw_pcm=b"ham",
        )
    ]
    inner = FakeMicrophone(inner_frames)
    canceller = FakeCanceller()
    factory_calls: list[int] = []
    states: list[str] = []

    def make_canceller(rate: int) -> FakeCanceller:
        factory_calls.append(rate)
        return canceller

    microphone = EchoCancelledMicrophone(inner, make_canceller, states.append)

    frames = asyncio.run(_collect_frames(microphone))

    assert len(frames) == 1
    assert frames[0].pcm == b"\x02\x00" * 160
    assert frames[0].timestamp == 12.5
    assert frames[0].sample_rate == 48000
    assert frames[0].raw_pcm is None
    assert factory_calls == [48000]
    assert canceller.process_calls == 1
    assert states == []
    assert not microphone.passthrough


def test_microphone_runtime_error_switches_to_passthrough_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    inner_frames = [
        AudioFrame(pcm=_pack_i16([1] * 160), timestamp=1.0),
        AudioFrame(pcm=_pack_i16([2] * 160), timestamp=2.0),
    ]
    inner = FakeMicrophone(inner_frames)
    canceller = FakeCanceller(RuntimeError("Sahte çalışma hatası."))
    factory_calls: list[int] = []
    states: list[str] = []

    def make_canceller(rate: int) -> FakeCanceller:
        factory_calls.append(rate)
        return canceller

    microphone = EchoCancelledMicrophone(inner, make_canceller, states.append)
    caplog.set_level(logging.WARNING)

    frames = asyncio.run(_collect_frames(microphone))

    assert frames[0] is inner_frames[0]
    assert frames[1] is inner_frames[1]
    assert factory_calls == [16000]
    assert canceller.process_calls == 1
    assert states == ["echo:off"]
    assert microphone.passthrough
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.WARNING


def test_microphone_factory_error_switches_to_passthrough_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    inner_frames = [
        AudioFrame(pcm=_pack_i16([1] * 160), timestamp=1.0),
        AudioFrame(pcm=_pack_i16([2] * 160), timestamp=2.0),
    ]
    inner = FakeMicrophone(inner_frames)
    factory_calls: list[int] = []
    states: list[str] = []

    def make_canceller(rate: int) -> FakeCanceller:
        factory_calls.append(rate)
        raise RuntimeError("Sahte kurulum hatası.")

    microphone = EchoCancelledMicrophone(inner, make_canceller, states.append)
    caplog.set_level(logging.WARNING)

    frames = asyncio.run(_collect_frames(microphone))

    assert frames[0] is inner_frames[0]
    assert frames[1] is inner_frames[1]
    assert factory_calls == [16000]
    assert states == ["echo:off"]
    assert microphone.passthrough
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.WARNING


def test_microphone_rebuilds_canceller_when_sample_rate_changes() -> None:
    inner_frames = [
        AudioFrame(
            pcm=_pack_i16([1] * 160),
            timestamp=1.0,
            sample_rate=48000,
        ),
        AudioFrame(
            pcm=_pack_i16([2] * 160),
            timestamp=2.0,
            sample_rate=16000,
        ),
    ]
    inner = FakeMicrophone(inner_frames)
    cancellers = [FakeCanceller(), FakeCanceller()]
    factory_calls: list[int] = []
    states: list[str] = []

    def make_canceller(rate: int) -> FakeCanceller:
        factory_calls.append(rate)
        return cancellers[len(factory_calls) - 1]

    microphone = EchoCancelledMicrophone(inner, make_canceller, states.append)

    frames = asyncio.run(_collect_frames(microphone))

    assert factory_calls == [48000, 16000]
    assert cancellers[0].process_calls == 1
    assert cancellers[1].process_calls == 1
    assert frames[0].sample_rate == 48000
    assert frames[1].sample_rate == 16000
    assert states == []
    assert not microphone.passthrough


def test_microphone_type_error_propagates() -> None:
    inner = FakeMicrophone(
        [AudioFrame(pcm=_pack_i16([1] * 160), timestamp=1.0)]
    )
    canceller = FakeCanceller(TypeError("Sahte tür hatası."))

    def make_canceller(_rate: int) -> FakeCanceller:
        return canceller

    microphone = EchoCancelledMicrophone(inner, make_canceller)

    with pytest.raises(TypeError, match="Sahte tür hatası"):
        asyncio.run(_collect_frames(microphone))

    assert not microphone.passthrough


def test_microphone_delegates_lifecycle_operations() -> None:
    inner = FakeMicrophone(
        [AudioFrame(pcm=_pack_i16([1] * 160), timestamp=1.0)]
    )
    canceller = FakeCanceller()
    canceller.reset_error = RuntimeError("Sahte sıfırlama hatası.")

    def make_canceller(_rate: int) -> FakeCanceller:
        return canceller

    microphone = EchoCancelledMicrophone(inner, make_canceller)
    asyncio.run(_collect_frames(microphone))

    microphone.discard_pending()
    microphone.pause()
    microphone.resume()
    asyncio.run(microphone.close())

    assert inner.discard_pending_calls == 1
    assert canceller.reset_calls == 1
    assert inner.pause_calls == 1
    assert inner.resume_calls == 1
    assert inner.close_called


def _energy(samples: Any) -> float:
    values = np.asarray(samples, dtype=np.float64)
    return float(np.sum(values * values))


def test_real_livekit_aec_reduces_delayed_echo() -> None:
    pytest.importorskip("livekit.rtc")

    rate = 16000
    sample_count = rate * 3
    block_samples = rate // 100
    rng = np.random.default_rng(20240517)

    noise = rng.standard_normal(sample_count)
    spectrum = np.fft.rfft(noise)
    frequencies = np.fft.rfftfreq(sample_count, 1.0 / rate)
    spectrum[(frequencies < 200) | (frequencies > 3400)] = 0
    band_limited = np.fft.irfft(spectrum, n=sample_count)
    far_float = band_limited / np.max(np.abs(band_limited))
    far = np.rint(far_float * (0.45 * 32767)).astype("<i2")

    delay_samples = rate * 40 // 1000
    echo = np.zeros(sample_count, dtype="<i2")
    scaled = np.rint(far[:-delay_samples].astype(np.float64) * 0.3)
    echo[delay_samples:] = scaled.astype("<i2")

    reference = EchoReference(rate, max_ms=500)
    canceller = EchoCanceller(rate, reference, delay_ms=40)
    output = bytearray()

    for start in range(0, sample_count, block_samples):
        far_block = far[start : start + block_samples]
        echo_block = echo[start : start + block_samples]
        reference.feed(far_block.tobytes(), rate, 1, 2)
        output.extend(canceller.process(echo_block.tobytes()))

    output_samples = np.frombuffer(bytes(output), dtype="<i2")
    last_second = sample_count - rate
    echo_energy = _energy(echo[-last_second:])
    output_energy = _energy(output_samples[-last_second:])
    threshold = echo_energy / (10.0 ** (15.0 / 10.0))

    assert output_energy <= threshold, (
        "Yankı iptali yetersiz: çıkış enerjisi beklenen eşiğin üzerinde."
    )


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def test_reference_pull_at_returns_samples_played_at_that_time() -> None:
    clock = FakeClock()
    reference = EchoReference(1000, clock=clock)
    reference.feed(_pack_i16(list(range(1, 11))), 1000, 1, 2)  # 100.000-100.010 s

    # Mikrofon karesi 3 ms sonrasını ister: önceki örnekler atılır.
    assert reference.pull(3, at=100.003) == _pack_i16([4, 5, 6])
    # Tamponun başından önceki an: başı sessizlik.
    clock.now = 200.0
    reference.clear()
    reference.feed(_pack_i16([7, 8]), 1000, 1, 2)
    assert reference.pull(4, at=199.998) == _pack_i16([0, 0, 7, 8])


def test_reference_pull_at_skips_samples_when_microphone_frames_were_dropped() -> None:
    # STT döngüyü tutarken mikrofon kareleri düşerse referans sıra ile değil saatle
    # eşlenmeli; aksi halde iki akış kalıcı olarak kayar.
    clock = FakeClock()
    reference = EchoReference(1000, clock=clock)
    reference.feed(_pack_i16(list(range(1, 101))), 1000, 1, 2)  # 100.000-100.100 s
    assert reference.pull(2, at=100.000) == _pack_i16([1, 2])
    # 50 ms'lik mikrofon kareleri kayboldu; sıradaki kare 100.052'de yakalandı.
    assert reference.pull(2, at=100.052) == _pack_i16([53, 54])
    # Bütün tampon geçmişte kaldı: sessizlik.
    assert reference.pull(2, at=100.5) == b"\x00" * 4


def test_reference_fills_playback_gaps_with_silence() -> None:
    clock = FakeClock()
    reference = EchoReference(1000, clock=clock)
    reference.feed(_pack_i16([1, 2]), 1000, 1, 2)  # 100.000-100.002
    clock.now = 100.202  # cevaplar arası 200 ms sessizlik
    reference.feed(_pack_i16([3, 4]), 1000, 1, 2)
    assert reference.pull(2, at=100.202) == _pack_i16([3, 4])


def test_microphone_passes_frame_start_time_to_canceller() -> None:
    frame = AudioFrame(pcm=_pack_i16([1] * 480), timestamp=10.0, sample_rate=48000)
    canceller = FakeCanceller()
    microphone = EchoCancelledMicrophone(FakeMicrophone([frame]), lambda _rate: canceller)

    asyncio.run(_collect_frames(microphone))

    # Zaman damgası karenin sonudur; 480 örnek @48 kHz = 10 ms önce başlamıştır.
    assert canceller.start_times == [pytest.approx(9.99)]


def test_canceller_aligns_each_subframe_by_time() -> None:
    clock = FakeClock()
    reference = EchoReference(16000, clock=clock)
    reference.feed(_pack_i16(list(range(1, 481))), 16000, 1, 2)  # 30 ms
    factory = FakeApmFactory()
    canceller = EchoCanceller(
        16000, reference, apm_factory=factory, frame_factory=fake_frame_factory,
    )

    canceller.process(_pack_i16([0] * 320), start_time=100.010)

    reverse = factory.instances[0].references
    assert reverse[0] == _pack_i16(list(range(161, 321)))
    assert reverse[1] == _pack_i16(list(range(321, 481)))


def test_canceller_smooths_small_timestamp_jitter_and_resyncs_on_gaps() -> None:
    clock = FakeClock()
    reference = EchoReference(16000, clock=clock)
    reference.feed(_pack_i16(list(range(1, 16001))), 16000, 1, 2)  # 100.0-101.0 s
    factory = FakeApmFactory()
    canceller = EchoCanceller(
        16000, reference, apm_factory=factory, frame_factory=fake_frame_factory,
    )

    canceller.process(_pack_i16([0] * 160), start_time=100.000)
    # 30 ms titreşim: akış kesintisiz kalır (sıradaki örnekler 161..).
    canceller.process(_pack_i16([0] * 160), start_time=100.040)
    # 500 ms kopma (düşen kareler): saate yeniden hizalanır.
    canceller.process(_pack_i16([0] * 160), start_time=100.520)

    references = factory.instances[0].references
    assert references[1] == _pack_i16(list(range(161, 321)))
    assert references[2] == _pack_i16(list(range(8321, 8481)))
