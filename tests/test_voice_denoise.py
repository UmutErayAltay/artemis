"""Gürültü engelleme (ADR 0037): saf DSP, akış sarmalayıcı, model bütünlüğü, gerçek model.

Pakete dahil durumlu adım modeli (``models/dfn3``) her zaman vardır; onunla çalışan testler
sentetik sinyalle CI'da da koşar. Resmi tam dizi grafiklerle parite testleri
``JARVIS_DFN_MODEL_DIR`` (enc/erb_dec/df_dec.onnx) olmadan atlanır.
``JARVIS_DFN_ASSETS`` DeepFilterNet ``assets`` dizinini (noisy_snr0.wav, clean_freesound_33711.wav),
``JARVIS_DFN_GOLDEN_WAV`` resmi ``deep-filter -D`` çıktısını gösterir; yoksa ilgili test atlanır.
"""

from __future__ import annotations

import asyncio
import logging
import os
import wave
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("onnxruntime")

from jarvis.channels.voice import denoise as dn  # noqa: E402
from jarvis.channels.voice.core import AudioFrame  # noqa: E402
from jarvis.config.model_paths import DEFAULT_NOISE_MODEL_DIR  # noqa: E402


class IdentityGraphRunner:
    """Tam dizi: mask=1, DF merkez katsayı=1 (j. kare için j kareyi aynen verir), lsnr=0."""

    def infer(self, feat_erb: Any, feat_spec: Any) -> dn.GraphOutputs:
        steps = feat_erb.shape[0]
        coefs = np.zeros((steps, dn.NB_DF, dn.DF_ORDER, 2), dtype=np.float32)
        coefs[:, :, 2, 0] = 1.0
        mask = np.ones((steps, dn.NB_ERB), np.float32)
        return dn.GraphOutputs(mask, coefs, np.zeros(steps, np.float32))


class IdentityRunner:
    """Durumlu: IdentityGraphRunner'ın kare kare eşi; çağrı ve sıfırlama sayılır."""

    def __init__(self) -> None:
        self.calls = 0
        self.resets = 0

    def reset(self) -> None:
        self.resets += 1

    def step(self, feat_erb: Any, feat_spec: Any) -> dn.StepOutputs:
        self.calls += 1
        coefs = np.zeros((dn.NB_DF, dn.DF_ORDER, 2), dtype=np.float32)
        coefs[:, 2, 0] = 1.0
        return dn.StepOutputs(np.ones(dn.NB_ERB, np.float32), coefs, 0.0)


def _snr(reference: Any, estimate: Any) -> float:
    ref = np.asarray(reference, dtype=np.float64)
    est = np.asarray(estimate, dtype=np.float64)
    return float(10 * np.log10((ref**2).sum() / (((ref - est) ** 2).sum() + 1e-30)))


def _si_sdr(reference: Any, estimate: Any) -> float:
    ref = np.asarray(reference, dtype=np.float64)
    est = np.asarray(estimate, dtype=np.float64)
    scale = (ref * est).sum() / (ref**2).sum()
    target = scale * ref
    return float(10 * np.log10((target**2).sum() / (((est - target) ** 2).sum() + 1e-30)))


def _tone(seconds: float, rate: int, freqs: tuple[float, ...]) -> Any:
    t = np.arange(int(seconds * rate)) / rate
    waves = sum(np.sin(2 * np.pi * f * t + i) for i, f in enumerate(freqs))
    return (waves * 0.1).astype(np.float32)


def test_erb_widths_follow_libdf_and_cover_every_bin() -> None:
    widths = dn.erb_widths()
    assert len(widths) == dn.NB_ERB and sum(widths) == dn.FREQ_SIZE
    assert min(widths) >= dn.MIN_NB_ERB_FREQS
    assert widths[:3] == [2, 2, 2] and widths == sorted(widths)


def test_norm_alpha_matches_libdf_rounding() -> None:
    assert dn.norm_alpha() == pytest.approx(0.99, abs=1e-6)


def test_vorbis_window_is_princen_bradley() -> None:
    window = dn.vorbis_window().astype(np.float64)
    half = dn.FFT_SIZE // 2
    assert np.abs(window[:half] ** 2 + window[half:] ** 2 - 1).max() < 1e-6


def test_stft_istft_perfect_reconstruction_with_one_hop_delay() -> None:
    rng = np.random.default_rng(1)
    signal = (rng.standard_normal(dn.HOP_SIZE * 40) * 0.1).astype(np.float32)
    stft, istft = dn.Stft(), dn.Istft()
    out = istft.synthesize(stft.analyze(signal))
    # Kare t hop t-1'i tamamlar: çıktı bir hop (480 örnek) gecikmeli.
    error = np.abs(out[dn.HOP_SIZE :] - signal[: -dn.HOP_SIZE]).max()
    assert error < 1e-6


def test_stft_state_is_independent_of_chunking() -> None:
    rng = np.random.default_rng(2)
    signal = rng.standard_normal(dn.HOP_SIZE * 12).astype(np.float32)
    whole = dn.Stft().analyze(signal)
    stft = dn.Stft()
    hop = dn.HOP_SIZE
    parts = np.concatenate([stft.analyze(signal[i : i + hop]) for i in range(0, signal.size, hop)])
    assert np.abs(whole - parts).max() < 1e-6


def test_offline_identity_runner_reconstructs_input_aligned() -> None:
    rng = np.random.default_rng(3)
    signal = (rng.standard_normal(48000) * 0.1).astype(np.float32)
    out = dn.DeepFilterNetDenoiser(runner=IdentityRunner()).enhance_48k(signal)
    assert out.shape == signal.shape
    assert np.abs(out - signal).max() < 1e-5
    reference = dn.enhance_fullseq(signal, IdentityGraphRunner())  # tam dizi referans yolu
    assert np.abs(reference - signal).max() < 1e-5


def test_resampler_round_trip_snr_after_delay_alignment() -> None:
    x = _tone(1.0, 16000, (300.0, 1500.0, 3200.0, 6000.0))
    up, down = dn.Upsampler3(), dn.Downsampler3()
    outs = [down.process(up.process(x[i : i + 320])) for i in range(0, x.size, 320)]
    y = np.concatenate(outs)
    delay = dn.RESAMPLE_DELAY_16K
    assert delay == 64
    assert _snr(x[200 : -delay - 200], y[delay + 200 : -200]) > 60


def test_resampler_chunking_does_not_change_output() -> None:
    x = _tone(0.5, 16000, (500.0, 2500.0))
    whole = dn.Upsampler3().process(x)
    up = dn.Upsampler3()
    parts = np.concatenate([up.process(x[i : i + 320]) for i in range(0, x.size, 320)])
    assert np.abs(whole - parts).max() < 1e-5


def test_atten_lim_follows_libdf_semantics() -> None:
    assert dn.atten_lim_linear(100) is None and dn.atten_lim_linear(120) is None
    assert dn.atten_lim_linear(0) == 1.0
    assert dn.atten_lim_linear(20) == pytest.approx(0.1, rel=1e-6)
    assert dn.atten_lim_linear(-20) == pytest.approx(0.1, rel=1e-6)


def test_atten_limit_mixes_noisy_back() -> None:
    class ZeroMask(IdentityRunner):
        def step(self, feat_erb: Any, feat_spec: Any) -> dn.StepOutputs:
            out = super().step(feat_erb, feat_spec)
            return dn.StepOutputs(out.mask * 0, out.coefs * 0, out.lsnr)

    rng = np.random.default_rng(4)
    signal = (rng.standard_normal(24000) * 0.1).astype(np.float32)
    full = dn.DeepFilterNetDenoiser(runner=ZeroMask(), atten_lim_db=100).enhance_48k(signal)
    limited = dn.DeepFilterNetDenoiser(runner=ZeroMask(), atten_lim_db=20).enhance_48k(signal)
    assert np.abs(full).max() < 1e-6
    assert np.abs(limited - 0.1 * signal).max() < 1e-5


def _stream(den: dn.DeepFilterNetDenoiser, x: Any) -> Any:
    pcm = np.clip(np.rint(x * 32768), -32768, 32767).astype("<i2")
    out = []
    for i in range(0, pcm.size - den.frame_samples + 1, den.frame_samples):
        chunk = den.process_frame(pcm[i : i + den.frame_samples].tobytes())
        assert len(chunk) == den.frame_samples * 2
        out.append(np.frombuffer(chunk, "<i2"))
    return np.concatenate(out).astype(np.float32) / 32768


@pytest.mark.parametrize("frame_samples", [160, 320, 480])
def test_streaming_identity_is_input_delayed_by_documented_delay(frame_samples: int) -> None:
    x = _tone(2.0, 16000, (400.0, 1800.0, 5000.0))
    runner = IdentityRunner()
    den = dn.DeepFilterNetDenoiser(runner=runner, frame_samples=frame_samples)
    out = _stream(den, x)
    delay = den.delay_samples
    assert out.size == x.size - x.size % frame_samples
    assert den.underruns == 0 and runner.calls == out.size // 160  # hop başına tek model çağrısı
    # Isınma sessizliktir; yalnız son 64 örnekte simetrik FIR'ın ön-yankısı görülebilir.
    assert np.abs(out[: delay - 2 * dn.RESAMPLE_DELAY_16K]).max() < 1e-6
    assert _snr(x[300 : out.size - delay - 300], out[delay + 300 : out.size - 300]) > 55


def test_streaming_delay_is_documented_value_without_block_latency() -> None:
    den = dn.DeepFilterNetDenoiser(runner=IdentityRunner())
    assert (den.delay_hops, den.delay_samples) == (3, 544)  # 34 ms (eskiden 114 ms)


def test_reset_restarts_stream_deterministically() -> None:
    x = _tone(1.0, 16000, (700.0,))
    runner = IdentityRunner()
    den = dn.DeepFilterNetDenoiser(runner=runner)
    first = _stream(den, x)
    den.reset()
    assert runner.resets == 2  # kurucu + açık reset: model durumu da sıfırlanır
    assert np.array_equal(first, _stream(den, x))


def test_process_frame_rejects_wrong_length() -> None:
    den = dn.DeepFilterNetDenoiser(runner=IdentityRunner())
    with pytest.raises(ValueError, match="320"):
        den.process_frame(b"\0\0" * 100)


def test_constructor_validates_parameters() -> None:
    with pytest.raises(ValueError):
        dn.DeepFilterNetDenoiser(runner=IdentityRunner(), frame_samples=300)
    with pytest.raises(ValueError):
        dn.DeepFilterNetDenoiser()


# --- Model bütünlüğü ------------------------------------------------------------------


VENDORED = DEFAULT_NOISE_MODEL_DIR


def test_vendored_model_is_present_and_matches_pinned_hash_and_manifest() -> None:
    import hashlib
    import json

    blob = dn.verify_step_model(VENDORED)
    assert hashlib.sha256(blob).hexdigest() == dn.STEP_MODEL_SHA256
    manifest = json.loads((VENDORED / "dfn3_step.json").read_text(encoding="utf-8"))
    assert manifest["onnx_sha256"] == dn.STEP_MODEL_SHA256
    assert max(manifest["parity"]["torch_full_sequence_max_abs_err"].values()) < 1e-4
    assert max(manifest["parity"]["official_onnx_max_abs_err"].values()) < 1e-4
    assert (VENDORED / "NOTICE").is_file()
    assert {k: tuple(v) for k, v in manifest["inputs"].items() if k in dn.STATE_SHAPES} == (
        dn.STATE_SHAPES
    )


def test_model_hash_mismatch_raises_value_error(tmp_path: Path) -> None:
    (tmp_path / dn.STEP_MODEL_NAME).write_bytes(b"bozuk")
    with pytest.raises(ValueError, match="sha256"):
        dn.DeepFilterNetDenoiser(tmp_path)
    # Tek bayt değişmiş gerçek model de reddedilir.
    blob = bytearray((VENDORED / dn.STEP_MODEL_NAME).read_bytes())
    blob[-1] ^= 1
    (tmp_path / dn.STEP_MODEL_NAME).write_bytes(bytes(blob))
    with pytest.raises(ValueError, match="sha256"):
        dn.StatefulOnnxRunner(tmp_path)


def test_missing_model_dir_and_file_are_os_errors(tmp_path: Path) -> None:
    with pytest.raises(OSError, match="okunamadı"):
        dn.DeepFilterNetDenoiser(tmp_path / "yok")
    with pytest.raises(OSError, match="okunamadı"):
        dn.DeepFilterNetDenoiser(tmp_path)


def test_official_full_sequence_hash_mismatch_still_raises(tmp_path: Path) -> None:
    for name in dn.MODEL_SHA256:
        (tmp_path / name).write_bytes(b"bozuk")
    with pytest.raises(ValueError, match="sha256"):
        dn.OnnxGraphRunner(tmp_path)


# --- Gerçek adım modeli (pakete dahil; sentetik sinyal, CI'da da koşar) ----------------


def _speechlike(seconds: float, rate: int = 16000) -> Any:
    """Gürültülü konuşma benzeri sinyal (tam ölçekli değil): armonikler + gürültü."""
    rng = np.random.default_rng(7)
    t = np.arange(int(seconds * rate)) / rate
    pitch = 130 + 25 * np.sin(2 * np.pi * 0.6 * t)
    phase = 2 * np.pi * np.cumsum(pitch) / rate
    envelope = 0.5 + 0.5 * np.sin(2 * np.pi * 1.3 * t)
    voiced = sum(np.sin(h * phase) / h for h in range(1, 20)) * envelope
    noise = rng.standard_normal(t.size) * 0.08
    return (0.12 * voiced + noise).astype(np.float32)


@pytest.fixture(scope="module")
def vendored() -> dn.DeepFilterNetDenoiser:
    return dn.DeepFilterNetDenoiser(VENDORED)


def _streaming_vs_offline_snr(den: dn.DeepFilterNetDenoiser, x16: Any) -> float:
    """Akış çıktısı ile aynı girdinin çevrimdışı çıktısı (aynı FIR zinciri), gecikme hizalı."""
    offline = dn.Downsampler3().process(den.enhance_48k(dn.Upsampler3().process(x16)))
    streamed = _stream(den, x16)
    a = streamed[den.delay_hops * 160 :]  # offline yalnız FIR gecikmeli; akış +3 hop daha geç
    b = offline[: a.size]
    n = min(a.size, b.size)
    return _snr(b[200 : n - 200], a[200 : n - 200])


def test_stateful_streaming_matches_offline_exactly_after_delay_alignment(
    vendored: dn.DeepFilterNetDenoiser,
) -> None:
    snr = _streaming_vs_offline_snr(vendored, _speechlike(4.0))
    print(f"\nSENTETİK AKIŞ vs ÇEVRİMDIŞI: {snr:.2f} dB SNR")
    assert snr >= 40  # fark yalnız akışın int16 nicemlemesidir


def _stream48(den: dn.DeepFilterNetDenoiser, x48: Any) -> Any:
    """48 kHz girdiyi 960 örneklik karelerle verir; 16 kHz kareler döner."""
    pcm = np.clip(np.rint(x48 * 32768), -32768, 32767).astype("<i2")
    step = den.frame_samples * 3
    out = []
    for i in range(0, pcm.size - step + 1, step):
        chunk = den.process_frame_48k(pcm[i : i + step].tobytes())
        assert len(chunk) == den.frame_samples * 2
        out.append(np.frombuffer(chunk, "<i2"))
    return np.concatenate(out).astype(np.float32) / 32768


def _stream48_vs_offline_snr(den: dn.DeepFilterNetDenoiser, x48: Any) -> float:
    offline = dn.Downsampler3().process(den.enhance_48k(x48))
    den.reset()
    streamed = _stream48(den, x48)
    a = streamed[den.delay_hops * 160 :]  # ikisi de indirici FIR gecikmeli; akış +3 hop geç
    b = offline[: a.size]
    n = min(a.size, b.size)
    return _snr(b[200 : n - 200], a[200 : n - 200])


def test_native_48k_streaming_matches_offline_48k(vendored: dn.DeepFilterNetDenoiser) -> None:
    snr = _stream48_vs_offline_snr(vendored, _speechlike(3.0, 48000))
    print(f"\nSENTETİK 48k AKIŞ vs ÇEVRİMDIŞI 48k: {snr:.2f} dB SNR")
    assert snr >= 40


def test_48k_identity_delay_has_no_upsampler_fir() -> None:
    den = dn.DeepFilterNetDenoiser(runner=IdentityRunner())
    assert den.delay_samples_48k == 512 and den.delay_samples == 544
    x16 = _tone(2.0, 16000, (400.0, 1800.0, 5000.0))
    x48 = dn.Upsampler3().process(x16)
    out = _stream48(den, x48)
    d = den.delay_samples_48k + dn.RESAMPLE_DELAY_16K // 2  # model gecikmesi + yükseltici FIR
    # x48 zaten yükseltilmiş (32 örnek gecikmeli): girdiye göre toplam 512 + 32.
    assert _snr(x16[300 : out.size - d - 300], out[d + 300 : out.size - 300]) > 55


def test_process_frame_48k_rejects_wrong_length() -> None:
    den = dn.DeepFilterNetDenoiser(runner=IdentityRunner())
    with pytest.raises(ValueError, match="960"):
        den.process_frame_48k(b"\0\0" * 320)


def test_stateful_output_does_not_depend_on_frame_chunking(
    vendored: dn.DeepFilterNetDenoiser,
) -> None:
    x = _speechlike(2.0)
    vendored.reset()
    coarse = _stream(vendored, x)
    fine = _stream(dn.DeepFilterNetDenoiser(VENDORED, frame_samples=160), x)
    n = min(coarse.size, fine.size)
    assert np.abs(coarse[:n] - fine[:n]).max() < 1e-6


def test_reset_gives_output_identical_to_fresh_denoiser(
    vendored: dn.DeepFilterNetDenoiser,
) -> None:
    x = _speechlike(2.0)
    vendored.reset()
    _stream(vendored, _speechlike(1.0)[::-1].copy())  # durumu (GRU, evrişim, normlar) kirlet
    vendored.reset()
    after_reset = _stream(vendored, x)
    fresh = _stream(dn.DeepFilterNetDenoiser(VENDORED), x)
    assert np.array_equal(after_reset, fresh)


def test_state_actually_carries_across_hops() -> None:
    """Aynı girdi, geçmişle ve geçmişsiz farklı çıktı verir: GRU/evrişim durumu etkili."""
    rng = np.random.default_rng(5)
    erb = (rng.standard_normal((40, dn.NB_ERB)) * 0.3).astype(np.float32)
    spec = (rng.standard_normal((40, 2, dn.NB_DF)) * 0.3).astype(np.float32)
    with_history = dn.StatefulOnnxRunner(VENDORED)
    for t in range(39):
        with_history.step(erb[t], spec[t])
    carried = with_history.step(erb[39], spec[39])
    fresh = dn.StatefulOnnxRunner(VENDORED).step(erb[39], spec[39])
    assert np.abs(carried.mask - fresh.mask).max() > 1e-3
    assert np.abs(carried.coefs - fresh.coefs).max() > 1e-3


def test_step_runner_state_is_exactly_the_documented_layout() -> None:
    runner = dn.StatefulOnnxRunner(VENDORED)
    assert {k: v.shape for k, v in runner._state.items()} == dn.STATE_SHAPES
    feat_erb = np.zeros(dn.NB_ERB, np.float32)
    feat_spec = np.zeros((2, dn.NB_DF), np.float32)
    out = runner.step(feat_erb, feat_spec)
    assert out.mask.shape == (dn.NB_ERB,) and out.coefs.shape == (dn.NB_DF, dn.DF_ORDER, 2)
    assert -15.0 <= out.lsnr <= 35.0  # libDF karar aralığı: varsayılan eşiklerde atlama olmaz
    before = {k: v.copy() for k, v in runner._state.items()}
    runner.step(feat_erb + 0.1, feat_spec + 0.1)
    assert any(not np.array_equal(before[k], runner._state[k]) for k in before)
    runner.reset()
    assert all(not v.any() for v in runner._state.values())


# --- Mikrofon sarmalayıcı -------------------------------------------------------------


class FakeInner:
    def __init__(self, count: int) -> None:
        self.count = count
        self.events: list[str] = []
        self.sent: list[AudioFrame] = []

    async def frames(self) -> AsyncIterator[AudioFrame]:
        for index in range(self.count):
            frame = AudioFrame(index.to_bytes(2, "little") * 160, float(index))
            self.sent.append(frame)
            yield frame

    def discard_pending(self) -> None:
        self.events.append("discard")

    def pause(self) -> None:
        self.events.append("pause")

    def resume(self) -> None:
        self.events.append("resume")

    async def close(self) -> None:
        self.events.append("close")


class FakeDenoiser:
    frame_samples = 160
    delay_samples = 320  # 2 kare

    def __init__(self, fail_at: int | None = None) -> None:
        self.calls = 0
        self.resets = 0
        self.fail_at = fail_at

    def process_frame(self, pcm: bytes) -> bytes:
        self.calls += 1
        if self.fail_at is not None and self.calls == self.fail_at:
            raise RuntimeError("model çöktü")
        return pcm[::-1]  # girdiden ayırt edilebilir çıktı

    def reset(self) -> None:
        self.resets += 1


async def _collect(mic: dn.DenoisedMicrophone) -> list[AudioFrame]:
    return [frame async for frame in mic.frames()]


async def test_wrapper_keeps_frame_count_and_aligns_timestamps_with_delay() -> None:
    inner, fake = FakeInner(6), FakeDenoiser()
    out = await _collect(dn.DenoisedMicrophone(inner, fake))
    assert len(out) == 6 and fake.calls == 6
    # Gecikme 2 kare: ısınma karelerinin damgası kendi girdisinin; sonra 2 kare geriden gelir.
    assert [frame.timestamp for frame in out] == [0.0, 0.0, 0.0, 1.0, 2.0, 3.0]
    assert out[3].pcm == inner.sent[3].pcm[::-1]


async def test_wrapper_passes_controls_through_and_resets_on_discard() -> None:
    inner, fake = FakeInner(3), FakeDenoiser()
    mic = dn.DenoisedMicrophone(inner, fake, clock=lambda: -1.0)
    mic.pause()
    mic.resume()
    mic.discard_pending()
    await mic.close()
    assert inner.events == ["pause", "resume", "discard", "close"]
    await _collect(mic)  # bekleyen sıfırlama ilk karede uygulanır
    assert fake.resets == 1
    mic.discard_pending()
    await _collect(mic)
    assert fake.resets == 2


async def test_wrapper_drops_in_flight_frame_when_reset_arrives() -> None:
    inner, fake = FakeInner(3), FakeDenoiser()
    mic = dn.DenoisedMicrophone(inner, fake, clock=lambda: -1.0)
    seen: list[AudioFrame] = []
    async for frame in mic.frames():
        seen.append(frame)
        if len(seen) == 1:
            mic.discard_pending()
    assert len(seen) == 3 and fake.resets == 1


async def test_wrapper_failure_switches_to_passthrough_with_one_warning_and_state(
    caplog: pytest.LogCaptureFixture,
) -> None:
    inner, fake = FakeInner(5), FakeDenoiser(fail_at=2)
    states: list[str] = []
    mic = dn.DenoisedMicrophone(inner, fake, states.append)
    with caplog.at_level(logging.WARNING, logger=dn.logger.name):
        out = await _collect(mic)
    assert mic.passthrough
    # Gecikme içindeki ham kare (0) hata anında sırayla akıtılır: ses kaybolmaz.
    assert [f.pcm for f in out[1:]] == [f.pcm for f in inner.sent]
    assert [f.timestamp for f in out] == sorted(f.timestamp for f in out)
    assert fake.calls == 2  # geçiş sonrası denoiser'a dokunulmaz
    assert states == ["denoise:off"]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "kapatıldı" in warnings[0].getMessage()


async def test_wrapper_timestamps_never_go_backwards_across_resets() -> None:
    inner = FakeInner(12)
    mic = dn.DenoisedMicrophone(inner, FakeDenoiser(), clock=lambda: -1.0)
    stamps: list[float] = []
    async for frame in mic.frames():
        stamps.append(frame.timestamp)
        index = len(stamps)
        if index == 4:
            mic.discard_pending()
        elif index == 6:
            mic.pause()
            mic.resume()
    assert len(stamps) >= 8
    assert stamps == sorted(stamps)


async def test_non_finite_output_takes_failure_path(caplog: pytest.LogCaptureFixture) -> None:
    class NanRunner(IdentityRunner):
        def step(self, feat_erb: Any, feat_spec: Any) -> dn.StepOutputs:
            out = super().step(feat_erb, feat_spec)
            return dn.StepOutputs(out.mask * np.float32("nan"), out.coefs, out.lsnr)

    den = dn.DeepFilterNetDenoiser(runner=NanRunner())
    with pytest.raises(ValueError, match="sonlu"):
        for _ in range(20):
            den.process_frame(b"\0\0" * 320)

    class Inner(FakeInner):
        async def frames(self) -> AsyncIterator[AudioFrame]:
            for index in range(20):
                yield AudioFrame(b"\x01\x00" * 320, float(index))

    states: list[str] = []
    mic = dn.DenoisedMicrophone(
        Inner(0), dn.DeepFilterNetDenoiser(runner=NanRunner()), states.append
    )
    with caplog.at_level(logging.WARNING, logger=dn.logger.name):
        out = await _collect(mic)
    assert mic.passthrough and states == ["denoise:off"]
    assert out[-1].pcm == b"\x01\x00" * 320
    assert [f.timestamp for f in out] == sorted(f.timestamp for f in out)
    assert sum(r.levelno == logging.WARNING for r in caplog.records) == 1


async def test_cancelled_frames_does_not_race_with_new_frames() -> None:
    import threading
    import time

    active = 0
    overlap = False
    guard = threading.Lock()

    class Probe(FakeDenoiser):
        def process_frame(self, pcm: bytes) -> bytes:
            nonlocal active, overlap
            with guard:
                active += 1
                overlap = overlap or active > 1
            time.sleep(0.05)
            with guard:
                active -= 1
            return pcm

    mic = dn.DenoisedMicrophone(FakeInner(3), Probe())
    first = asyncio.create_task(_collect(mic))
    await asyncio.sleep(0.02)  # çağrı iş parçacığında uçuşta
    first.cancel()
    await _collect(mic)
    assert not overlap


async def test_frame_captured_before_reset_is_never_emitted_after_it() -> None:
    """Onay/duraklatma öncesi yakalanan kare, iç mikrofon geç teslim etse bile atılır."""
    now = [10.0]

    class LateInner(FakeInner):
        async def frames(self) -> AsyncIterator[AudioFrame]:
            yield AudioFrame(b"\x01\x00" * 160, 9.0)  # sıfırlamadan önce yakalandı
            yield AudioFrame(b"\x02\x00" * 160, 11.0)  # sonra

    fake = FakeDenoiser()
    mic = dn.DenoisedMicrophone(LateInner(0), fake, clock=lambda: now[0])
    mic.pause()  # sıfırlama anı = 10.0; iç mikrofon eski kareyi hâlâ teslim ediyor
    mic.resume()
    out = await _collect(mic)
    assert fake.calls == 1 and [f.timestamp for f in out] == [11.0]


async def test_reset_from_another_thread_never_raises_inside_frames() -> None:
    import threading

    class Endless(FakeInner):
        async def frames(self) -> AsyncIterator[AudioFrame]:
            for index in range(300):
                yield AudioFrame(b"\0\0" * 160, float(index))
                await asyncio.sleep(0)

    mic = dn.DenoisedMicrophone(Endless(0), FakeDenoiser(), clock=lambda: -1.0)
    stop = threading.Event()

    def hammer() -> None:
        while not stop.is_set():
            mic.discard_pending()
            mic.pause()
            mic.resume()

    thread = threading.Thread(target=hammer)
    thread.start()
    try:
        out = await _collect(mic)  # IndexError/RuntimeError dışarı sızmamalı
    finally:
        stop.set()
        thread.join()
    stamps = [f.timestamp for f in out]
    assert stamps == sorted(stamps)


def test_underruns_and_late_frames_warn_rate_limited(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time

    den = dn.DeepFilterNetDenoiser(runner=IdentityRunner())
    # Kararlı akışta tampon asla boşalmaz; model çıktısı gecikirse (burada: üretilmezse) boşalır.
    monkeypatch.setattr(den, "_advance", lambda *_: np.zeros(0, dtype="float32"))
    with caplog.at_level(logging.WARNING, logger=dn.logger.name):
        for _ in range(3):
            den._fifo = den._fifo[:0]
            den.process_frame(b"\0\0" * 320)
        assert den.underruns == 3
        warned = [r for r in caplog.records if "tampon" in r.getMessage()]
        assert len(warned) == 1  # hız sınırı: 30 sn içinde tek uyarı
        base = time.monotonic()
        monkeypatch.setattr(dn.time, "monotonic", lambda: base + 31)
        den._fifo = den._fifo[:0]
        den.process_frame(b"\0\0" * 320)
        assert len([r for r in caplog.records if "tampon" in r.getMessage()]) == 2

    class Slow(IdentityRunner):
        def step(self, feat_erb: Any, feat_spec: Any) -> dn.StepOutputs:
            time.sleep(0.03)
            return super().step(feat_erb, feat_spec)

    caplog.clear()
    slow = dn.DeepFilterNetDenoiser(runner=Slow())  # bütçe 20 ms
    with caplog.at_level(logging.WARNING, logger=dn.logger.name):
        for _ in range(4):
            slow.process_frame(b"\0\0" * 320)
    assert slow.late_frames >= 1
    assert len([r for r in caplog.records if "yavaş" in r.getMessage()]) == 1


async def test_wrapper_does_not_block_event_loop_during_processing() -> None:
    import time

    class Slow(FakeDenoiser):
        def process_frame(self, pcm: bytes) -> bytes:
            time.sleep(0.05)
            return pcm

    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.005)
            ticks += 1

    task = asyncio.create_task(ticker())
    await _collect(dn.DenoisedMicrophone(FakeInner(3), Slow()))
    task.cancel()
    assert ticks >= 10


# --- 48 kHz yakalama: DenoisedMicrophone ------------------------------------------------


class Fake48Denoiser:
    """Sessiz 16 kHz kare döndürür; 48k çağrılarını sayar, istenen çağrıda çöker."""

    frame_samples = 160
    delay_samples = 320
    delay_samples_48k = 288

    def __init__(self, fail_at: int | None = None) -> None:
        self.calls = 0
        self.fail_at = fail_at

    def process_frame(self, pcm: bytes) -> bytes:  # pragma: no cover - 48k testlerinde çağrılmaz
        raise AssertionError("16k yolu çağrılmamalı")

    def process_frame_48k(self, pcm: bytes) -> bytes:
        self.calls += 1
        if self.fail_at is not None and self.calls == self.fail_at:
            raise RuntimeError("model çöktü")
        assert len(pcm) == 960
        return bytes(2 * 160)

    def reset(self) -> None:
        pass


class Inner48(FakeInner):
    async def frames(self) -> AsyncIterator[AudioFrame]:
        for index in range(self.count):
            samples = np.full(480, index + 1, dtype="<i2")
            frame = AudioFrame(samples.tobytes(), float(index), 48000)
            self.sent.append(frame)
            yield frame


async def test_48k_frames_yield_16k_frames_one_to_one_with_monotonic_stamps() -> None:
    fake = Fake48Denoiser()
    out = await _collect(dn.DenoisedMicrophone(Inner48(6), fake))
    assert len(out) == 6 and fake.calls == 6
    assert all(f.sample_rate == 16000 and len(f.pcm) == 320 for f in out)
    assert [f.timestamp for f in out] == sorted(f.timestamp for f in out)


async def test_48k_failure_flushes_ring_and_passthrough_is_16k(
    caplog: pytest.LogCaptureFixture,
) -> None:
    inner, fake = Inner48(6), Fake48Denoiser(fail_at=3)
    states: list[str] = []
    mic = dn.DenoisedMicrophone(inner, fake, states.append)
    with caplog.at_level(logging.WARNING, logger=dn.logger.name):
        out = await _collect(mic)
    assert mic.passthrough and states == ["denoise:off"] and fake.calls == 3
    assert all(f.sample_rate == 16000 and len(f.pcm) in range(1, 321) for f in out)
    # 2 işlenmiş kare + ham halka (2 kare, gecikme taşması kesilmiş olabilir) + hatalı kare
    # + 3 geçiş karesi; sıra ve damgalar korunur.
    stamps = [f.timestamp for f in out]
    assert stamps == sorted(stamps)
    tail = out[-4:]  # hatalı kare dahil son 4 ham kare 16 kHz'e inmiş olarak akar
    assert all(len(f.pcm) == 320 for f in tail[1:])
    levels = [int(np.frombuffer(f.pcm, "<i2")[100:200].mean()) for f in tail[1:]]
    assert levels == [4, 5, 6]  # geçiş karelerinin DC değeri (FIR DC kazancı 1) korunur
    assert sum(r.levelno == logging.WARNING for r in caplog.records) == 1


async def test_48k_failure_flush_continues_raw_downsampler_without_warmup_transient() -> None:
    """Akıtılan + ham geçen ses, ham akışın tek geçişte indirilmişiyle (hizalı) aynıdır."""
    inner, fake = Inner48(8), Fake48Denoiser(fail_at=4)
    out = await _collect(dn.DenoisedMicrophone(inner, fake))
    raw = np.concatenate([np.frombuffer(f.pcm, "<i2") for f in inner.sent]).astype(np.float32)
    expected = dn.Downsampler3().process(raw / 32768.0) * 32768.0
    expected = np.clip(np.rint(expected), -32768, 32767).astype(np.int16)
    flushed = np.concatenate([np.frombuffer(f.pcm, "<i2") for f in out[3:]])
    assert flushed.size > 320 * 3
    tail = expected[expected.size - flushed.size :]
    assert np.abs(flushed.astype(int) - tail.astype(int)).max() <= 1


async def test_48k_mixed_with_reset_drops_stale_frames() -> None:
    fake = Fake48Denoiser()

    class Late(Inner48):
        async def frames(self) -> AsyncIterator[AudioFrame]:
            yield AudioFrame(b"\x01\x00" * 480, 9.0, 48000)
            yield AudioFrame(b"\x02\x00" * 480, 11.0, 48000)

    mic = dn.DenoisedMicrophone(Late(0), fake, clock=lambda: 10.0)
    mic.pause()
    mic.resume()
    out = await _collect(mic)
    assert fake.calls == 1 and [f.timestamp for f in out] == [11.0]


# --- Resmi varlıklar ve tam dizi referans (env ile; yoksa atlanır) ----------------------

MODEL_DIR = os.environ.get("JARVIS_DFN_MODEL_DIR")
ASSETS = Path(os.environ.get("JARVIS_DFN_ASSETS", "/home/user/rikorose/deepfilternet/assets"))
GOLDEN = os.environ.get("JARVIS_DFN_GOLDEN_WAV")
official_graphs = pytest.mark.skipif(not MODEL_DIR, reason="JARVIS_DFN_MODEL_DIR yok")


def _read_wav(path: Path) -> Any:
    with wave.open(str(path), "rb") as handle:
        assert handle.getframerate() == 48000 and handle.getnchannels() == 1
        raw = np.frombuffer(handle.readframes(handle.getnframes()), "<i2")
        return raw.astype(np.float32) / 32768


def _assets() -> tuple[Any, Any]:
    noisy, clean = ASSETS / "noisy_snr0.wav", ASSETS / "clean_freesound_33711.wav"
    if not (noisy.is_file() and clean.is_file()):
        pytest.skip("DeepFilterNet assets dizini yok (JARVIS_DFN_ASSETS)")
    return _read_wav(noisy), _read_wav(clean)


@official_graphs
def test_stateful_offline_matches_official_full_sequence_graphs() -> None:
    noisy, _ = _assets()
    reference = dn.enhance_fullseq(noisy, dn.OnnxGraphRunner(Path(MODEL_DIR or "")))
    ours = dn.DeepFilterNetDenoiser(VENDORED).enhance_48k(noisy)
    snr = _snr(reference, ours)
    print(f"\nDURUMLU vs RESMİ TAM DİZİ (48 kHz): {snr:.2f} dB SNR, "
          f"en büyük mutlak fark {np.abs(reference - ours).max():.2e}")
    assert snr >= 80  # model eşdeğerliği 1e-4 altında; float hassasiyetine yakın olmalı


@official_graphs
@pytest.mark.skipif(not GOLDEN, reason="JARVIS_DFN_GOLDEN_WAV (deep-filter -D çıktısı) yok")
def test_stateful_offline_matches_rust_golden(vendored: dn.DeepFilterNetDenoiser) -> None:
    noisy, _ = _assets()
    assert GOLDEN
    golden = _read_wav(Path(GOLDEN))
    ours = vendored.enhance_48k(noisy)[: golden.size]
    snr = _snr(golden, ours)
    print(f"\nPARITE: durumlu çıktı vs Rust altın çıktı = {snr:.2f} dB SNR")
    assert snr >= 30


def test_real_speech_streaming_matches_offline_and_si_sdr(
    vendored: dn.DeepFilterNetDenoiser,
) -> None:
    noisy, clean = _assets()
    size = noisy.size // 3 * 3
    x16 = dn.Downsampler3().process(noisy[:size])
    snr = _streaming_vs_offline_snr(vendored, x16)
    print(f"\nAKIŞ vs ÇEVRİMDIŞI (gerçek konuşma, durumlu): {snr:.2f} dB SNR")
    assert snr >= 40
    # Akışın temiz konuşmaya karşı SI-SDR'ı (16 kHz, gecikme hizalı) çevrimdışıya yakın olmalı.
    clean16 = dn.Downsampler3().process(clean[:size])  # x16 ile aynı FIR: birbirine hizalı
    vendored.reset()
    delay = vendored.delay_samples
    streamed = _stream(vendored, x16)[delay:]  # girdiye göre toplam gecikme kadar kaydır
    offline = dn.Downsampler3().process(vendored.enhance_48k(dn.Upsampler3().process(x16)))
    offline = offline[dn.RESAMPLE_DELAY_16K :]  # yalnız FIR gecikmeli
    n = min(streamed.size, offline.size, clean16.size) - 2000
    ref = clean16[1000:n]
    stream_sdr = _si_sdr(ref, streamed[1000:n])
    offline_sdr = _si_sdr(ref, offline[1000:n])
    print(f"SI-SDR vs temiz (16 kHz): akış {stream_sdr:.2f} dB, çevrimdışı {offline_sdr:.2f} dB")
    assert abs(stream_sdr - offline_sdr) < 0.5


def test_noise_reduction_improves_si_sdr_against_clean(
    vendored: dn.DeepFilterNetDenoiser,
) -> None:
    noisy, clean = _assets()
    n = min(noisy.size, clean.size)
    enhanced = vendored.enhance_48k(noisy)
    before = _si_sdr(clean[1000 : n - 1000], noisy[1000 : n - 1000])
    after = _si_sdr(clean[1000 : n - 1000], enhanced[1000 : n - 1000])
    print(f"\nSI-SDR vs temiz: gürültülü {before:.2f} dB -> iyileştirilmiş {after:.2f} dB")
    assert after - before > 3


def test_real_speech_native_48k_streaming_si_sdr_at_16k_output(
    vendored: dn.DeepFilterNetDenoiser,
) -> None:
    noisy, clean = _assets()
    size = min(noisy.size, clean.size) // 960 * 960
    snr = _stream48_vs_offline_snr(vendored, noisy[:size])
    print(f"\n48k AKIŞ vs ÇEVRİMDIŞI (gerçek konuşma): {snr:.2f} dB SNR")
    assert snr >= 40
    vendored.reset()
    streamed = _stream48(vendored, noisy[:size])[vendored.delay_hops * 160 :]
    clean16 = dn.Downsampler3().process(clean[:size])  # indirici FIR gecikmesi akışta da var
    noisy16 = dn.Downsampler3().process(noisy[:size])
    n = min(streamed.size, clean16.size) - 2000
    stream_sdr = _si_sdr(clean16[1000:n], streamed[1000:n])
    before = _si_sdr(clean16[1000:n], noisy16[1000:n])
    print(f"SI-SDR vs temiz (16 kHz çıktı, 48k yakalama): {before:.2f} -> {stream_sdr:.2f} dB")
    assert stream_sdr > 19
