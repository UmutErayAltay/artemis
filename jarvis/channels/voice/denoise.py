"""İsteğe bağlı mikrofon gürültü engelleme: DeepFilterNet3 (ONNX) + kendi numpy DSP'miz (ADR 0037).

Akış yolu DURUMLU adım modeli kullanır (``models/dfn3/dfn3_step.onnx``): her 10 ms'lik hop için
model bir kez çalışır; GRU gizli durumları ve nedensel evrişim geçmişi açık girdi/çıktıdır ve
hop'lar arasında bu modülde tutulur. Resmi dışa aktarım (enc/erb_dec/df_dec) tam dizi çalıştığı
ve GRU durumunu gizlediği için eskiden son 110 hop her 10 hop'ta yeniden çalıştırılıyordu
(~11 kat gereksiz iş, yaklaşık bağlam sonucu, 80 ms blok gecikmesi). Adım modeli resmi
checkpoint'ten ``scripts/export_stateful_dfn.py`` ile üretilir ve tam dizi modele 1e-4 altında
uyduğu orada kanıtlanır. Torch ve ``deepfilterlib`` çalışma anında yoktur; STFT, ERB bantları,
özellik normalizasyonu, derin filtre ve ISTFT ``libDF`` (Rust) kaynağından birebir taşınmıştır:
``libDF/src/lib.rs`` ve ``libDF/src/tract.rs``.

Gecikme muhasebesi (48 kHz, hop 480): ISTFT yarım pencere (480) + 2 hop ileri bakış
(``conv_lookahead = df_lookahead = 2``) = 3 hop = 30 ms; ``deep-filter -D`` ile aynıdır. Buna yalnız
16k<->48k FIR gecikmesi (4 ms) eklenir: toplam 34 ms, blok toplama gecikmesi yoktur.

İleri bakış modelin dışındadır: adım modelinin t. çıktısı spektrumun t-2. karesine aittir (resmi
grafiklerle aynı sözleşme). Bu yüzden t. hop geldiğinde t-2. kare tamamlanır; ilk iki çıktı
(kare -2, -1) modelin durumunu ısıtır ve atılır.

Eşikler (libDF ``apply_stages``): libDF kodlayıcıyı her karede çalıştırır, ERB/DF kod çözücüyü ise
yalnız ilgili aşama uygulanacaksa. Varsayılan eşiklerde (-15/35/35) lsnr çıktısı zaten [-15, 35]
aralığındadır, yani kod çözücüler hiç atlanmaz ve durum ilerlemesi libDF ile birebir aynıdır.
Burada kod çözücüler her zaman çalışır (tek grafik); özel, daha dar eşiklerde libDF'in atlanan
karelerde dondurduğu kod çözücü GRU durumları bu yolda ilerler, uygulanan aşamalar ise aynıdır.

Bilinçli sapmalar (libDF ile): (1) son-işlem filtresi (post filter) yok, varsayılanı zaten
kapalıdır; (2) 5+ ardışık dijital sessizlik hop'unda libDF çıktıyı sıfırlayıp durumu
ilerletmez, burada bu hop'lar normal işlenir (sıfır girdinin çıktısı zaten sıfırdır).
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import logging
import threading
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple, Protocol

from jarvis.channels.voice.core import AudioFrame, Microphone

logger = logging.getLogger(__name__)

Array = Any  # numpy tembel içe aktarılır; tip yalnız belgeleme amaçlıdır.

# Durumlu adım modeli (scripts/export_stateful_dfn.py; kaynak: resmi DeepFilterNet3.zip).
STEP_MODEL_NAME = "dfn3_step.onnx"
STEP_MODEL_SHA256 = "a76b89869279b483c639bb437a3bcdbf6bfb0192c1115ecdcf87402f2b1d4f70"
# Durum tensörleri (adı -> şekli); yerleşim dfn3_step.json ve dışa aktarma betiğinde belgelidir.
STATE_SHAPES: dict[str, tuple[int, ...]] = {
    "gru_h": (5, 1, 256),
    "erb_hist": (1, 1, 2, 32),
    "spec_hist": (1, 2, 2, 96),
    "c0_hist": (1, 64, 4, 96),
}
_STEP_OUTPUTS = ["m", "coefs", "lsnr", *(f"{name}_out" for name in STATE_SHAPES)]

# Resmi DeepFilterNet3_onnx.tar.gz içeriği (Rikorose/DeepFilterNet, d375b2d8): yalnız tam dizi
# referans yolu (parite testi, scripts/fetch_noise_model.py); çalışma anında kullanılmaz.
MODEL_SHA256: dict[str, str] = {
    "enc.onnx": "7c5399d3da8a50ebef1c1a0ae421b33376aa5e45d0e92df16da7e83c9c131916",
    "erb_dec.onnx": "ab669a1d10afe20911728b33053a452071042317a90581092b325da7b2f9d895",
    "df_dec.onnx": "23114ce3b0f6464b763ee62f7bb8aab6b2a129a21eabd5bcfe59413db05f278a",
}

# config.ini (DeepFilterNet3): sr 48000, fft 960, hop 480, nb_erb 32, nb_df 96, norm_tau 1.
MODEL_SR = 48000
FFT_SIZE = 960
HOP_SIZE = 480
FREQ_SIZE = FFT_SIZE // 2 + 1
NB_ERB = 32
NB_DF = 96
MIN_NB_ERB_FREQS = 2
DF_ORDER = 5
LOOKAHEAD = 2  # max(conv_lookahead, df_lookahead)
DELAY_HOPS = LOOKAHEAD + 1  # ISTFT yarım pencere + ileri bakış
NORM_TAU = 1.0
MEAN_NORM_INIT = (-60.0, -90.0)
UNIT_NORM_INIT = (0.001, 0.0001)

IN_SR = 16000
RESAMPLE_FACTOR = MODEL_SR // IN_SR  # 3
_FIR_TAPS = 193  # tek sayı; gecikme 96 örnek (48 kHz) = 32 örnek (16 kHz) her yönde
_FIR_CUTOFF_HZ = 8000.0
_FIR_KAISER_BETA = 7.86  # ~80 dB bastırma
RESAMPLE_DELAY_16K = 2 * ((_FIR_TAPS - 1) // 2) // RESAMPLE_FACTOR  # 64 örnek = 4 ms

# libDF enhance_wav ikilisinin varsayılanları (-15/35/35): lsnr aralığında her kare tam işlenir.
DEFAULT_MIN_DB_THRESH = -15.0
DEFAULT_MAX_DB_ERB_THRESH = 35.0
DEFAULT_MAX_DB_DF_THRESH = 35.0


# VoiceConfig.noise_suppression_level -> libDF atten_lim_db (ADR 0037).
NOISE_LEVEL_ATTEN_DB: dict[str, float] = {"hafif": 12.0, "orta": 24.0, "guclu": 100.0}


class DenoiseDependencyError(RuntimeError):
    """numpy/onnxruntime kurulu değil; model dosyası sorunundan ayrı raporlanır."""


def _np() -> Any:
    try:
        return importlib.import_module("numpy")
    except ImportError as error:
        raise DenoiseDependencyError(
            "Gürültü engelleme için numpy gerekli: `pip install .[voice]`."
        ) from error


def atten_lim_linear(atten_lim_db: float) -> float | None:
    """libDF semantiği: >=100 dB sınırsız (None); <0.01 dB hiç bastırma yok (1.0)."""
    lim = abs(atten_lim_db)
    if lim >= 100.0:
        return None
    if lim < 0.01:
        return 1.0
    return float(10.0 ** (-lim / 20.0))


# --- Saf numpy DSP yardımcıları -------------------------------------------------------


def erb_widths(
    sr: int = MODEL_SR,
    fft_size: int = FFT_SIZE,
    nb_bands: int = NB_ERB,
    min_nb_freqs: int = MIN_NB_ERB_FREQS,
) -> list[int]:
    """``erb_fb`` (lib.rs): her ERB bandındaki FFT bin sayısı; toplam fft_size/2 + 1.

    Rust f32 aritmetiği ve ``round`` (yarıdan uzağa) taklit edilir.
    """
    np = _np()
    f32 = np.float32

    def freq2erb(freq: Any) -> Any:
        return f32(9.265) * np.log1p(f32(freq) / (f32(24.7) * f32(9.265)))

    def erb2freq(erb: Any) -> Any:
        return f32(24.7) * f32(9.265) * (np.exp(f32(erb) / f32(9.265)) - f32(1.0))

    freq_width = f32(sr) / f32(fft_size)
    erb_low = freq2erb(f32(0.0))
    erb_high = freq2erb(f32(sr // 2))
    step = (erb_high - erb_low) / f32(nb_bands)
    widths = [0] * nb_bands
    prev_freq = 0
    freq_over = 0
    for i in range(1, nb_bands + 1):
        freq = erb2freq(erb_low + f32(i) * step)
        fb = int(np.floor(freq / freq_width + f32(0.5)))
        nb_freqs = fb - prev_freq - freq_over
        if nb_freqs < min_nb_freqs:
            freq_over = min_nb_freqs - nb_freqs
            nb_freqs = min_nb_freqs
        else:
            freq_over = 0
        widths[i - 1] = nb_freqs
        prev_freq = fb
    widths[-1] += 1
    too_large = sum(widths) - (fft_size // 2 + 1)
    if too_large > 0:
        widths[-1] -= too_large
    if sum(widths) != fft_size // 2 + 1:
        raise ValueError("ERB bant genişlikleri FFT bin sayısına eşit değil")
    return widths


def norm_alpha(sr: int = MODEL_SR, hop: int = HOP_SIZE, tau: float = NORM_TAU) -> float:
    """``calc_norm_alpha`` (tract.rs): exp(-dt/tau), yuvarlanarak 1'in altında tutulur."""
    np = _np()
    alpha = np.exp(np.float32(-(hop / sr) / tau))
    a = np.float32(1.0)
    precision = 3
    while a >= 1.0:
        scale = np.float32(10**precision)
        a = np.round(alpha * scale) / scale
        precision += 1
    return float(a)


def vorbis_window(size: int = FFT_SIZE) -> Array:
    """sin(pi/2 * sin^2(pi (i+0.5) / (N/2))) — Princen-Bradley, w[i]^2 + w[i+N/2]^2 = 1."""
    np = _np()
    index = np.arange(size, dtype=np.float64)
    inner = np.sin(0.5 * np.pi * (index + 0.5) / (size // 2))
    return np.sin(0.5 * np.pi * inner * inner).astype(np.float32)


class Stft:
    """Durumlu analiz: pencere = [önceki hop, yeni hop]; çıktı 1/N ile ölçeklenir (wnorm)."""

    def __init__(self) -> None:
        np = _np()
        self._window = vorbis_window()
        self._wnorm = np.float32(1.0 / (FFT_SIZE**2 / (2 * HOP_SIZE)))
        self.reset()

    def reset(self) -> None:
        self._mem = _np().zeros(FFT_SIZE - HOP_SIZE, dtype="float32")

    def analyze(self, signal: Array) -> Array:
        """``len(signal)`` HOP_SIZE'ın katı olmalı; (hop sayısı, FREQ_SIZE) complex64 döner."""
        np = _np()
        if signal.size % HOP_SIZE:
            raise ValueError("STFT girdisi hop boyunun katı olmalı")
        hops = signal.size // HOP_SIZE
        padded = np.concatenate([self._mem, signal.astype("float32", copy=False)])
        # Pencere t: padded[t*HOP : t*HOP + FFT]; FFT = 2*HOP olduğundan (hops+1, HOP) şekli.
        blocks = padded.reshape(hops + 1, HOP_SIZE)
        frames = np.concatenate([blocks[:-1], blocks[1:]], axis=1) * self._window
        self._mem = padded[-(FFT_SIZE - HOP_SIZE):].copy()
        return (np.fft.rfft(frames, axis=1) * self._wnorm).astype("complex64")


class Istft:
    """Durumlu sentez: ters FFT (N ile çarpılır) * pencere, yarım pencere bindirme-toplama."""

    def __init__(self) -> None:
        self._window = vorbis_window()
        self.reset()

    def reset(self) -> None:
        self._mem = _np().zeros(FFT_SIZE - HOP_SIZE, dtype="float32")

    def synthesize(self, spec: Array) -> Array:
        """(T, FREQ_SIZE) -> T*HOP_SIZE örnek. Kare t, hop t-1'in bölgesini tamamlar."""
        np = _np()
        frames = np.fft.irfft(spec, n=FFT_SIZE, axis=1) * FFT_SIZE * self._window
        frames = frames.astype("float32")
        first = frames[:, :HOP_SIZE].copy()
        first[0] += self._mem
        first[1:] += frames[:-1, HOP_SIZE:]
        self._mem = frames[-1, HOP_SIZE:].copy()
        return first.reshape(-1)


class FeatureExtractor:
    """Durumlu ERB (log + üstel ortalama normu) ve karmaşık (birim norm) özellikler."""

    def __init__(self, widths: list[int] | None = None, alpha: float | None = None) -> None:
        np = _np()
        self._widths = widths if widths is not None else erb_widths()
        self._starts = np.concatenate([[0], np.cumsum(self._widths)[:-1]]).astype("int64")
        self._inv_widths = (1.0 / np.asarray(self._widths, dtype="float32")).astype("float32")
        self._alpha = np.float32(alpha if alpha is not None else norm_alpha())
        self.reset()

    def reset(self) -> None:
        np = _np()
        self._mean = np.linspace(*MEAN_NORM_INIT, NB_ERB, dtype="float32")
        self._unit = np.linspace(*UNIT_NORM_INIT, NB_DF, dtype="float32")

    def push(self, spec: Array) -> tuple[Array, Array]:
        """spec: (T, FREQ_SIZE) complex64 -> erb (T, 32) float32, cplx (T, 96) complex64."""
        np = _np()
        one_minus = np.float32(1.0) - self._alpha
        power = spec.real * spec.real + spec.imag * spec.imag
        band = np.add.reduceat(power, self._starts, axis=1) * self._inv_widths
        db = (np.log10(band + np.float32(1e-10)) * np.float32(10.0)).astype("float32")
        erb = np.empty_like(db)
        mean = self._mean
        for t in range(db.shape[0]):
            mean = db[t] * one_minus + mean * self._alpha
            erb[t] = (db[t] - mean) / np.float32(40.0)
        self._mean = mean
        low = spec[:, :NB_DF]
        mag = np.abs(low).astype("float32")
        scale = np.empty_like(mag)
        unit = self._unit
        for t in range(mag.shape[0]):
            unit = mag[t] * one_minus + unit * self._alpha
            scale[t] = unit
        self._unit = unit
        return erb, (low / np.sqrt(scale)).astype("complex64")


@dataclass(frozen=True)
class RuntimeParams:
    """libDF ``RuntimeParams`` alt kümesi: yerel SNR eşikleri ve bastırma sınırı."""

    atten_lim_db: float = 100.0
    min_db_thresh: float = DEFAULT_MIN_DB_THRESH
    max_db_erb_thresh: float = DEFAULT_MAX_DB_ERB_THRESH
    max_db_df_thresh: float = DEFAULT_MAX_DB_DF_THRESH


def enhance_spectra(
    noisy_ctx: Array,
    mask: Array,
    coefs: Array,
    lsnr: Array,
    widths: list[int],
    params: RuntimeParams,
) -> Array:
    """Model çıktılarından iyileştirilmiş spektrum (``DfTract::process`` ardı).

    ``noisy_ctx``: (n + 4, FREQ_SIZE) gürültülü kareler; çıktı i, ``noisy_ctx[i + 2]`` karesine
    karşılık gelir ve DF için ``noisy_ctx[i : i + 5]`` kullanılır (2 geri + 2 ileri).
    ``mask``: (n, 32); ``coefs``: (n, 96, 5, 2); ``lsnr``: (n,).
    """
    np = _np()
    n = mask.shape[0]
    noisy = noisy_ctx[LOOKAHEAD : LOOKAHEAD + n]
    gains_on = lsnr >= params.min_db_thresh
    gains_zero = lsnr < params.min_db_thresh
    clean = lsnr > params.max_db_erb_thresh
    # apply_stages: temiz konuşma (> erb eşiği) hiç işlenmez; gürültüsüz (< min) sıfır maske.
    gains_apply = gains_on & ~clean
    df_apply = gains_apply & ~(lsnr > params.max_db_df_thresh)
    band_gain = np.where(gains_zero[:, None], np.float32(0.0), mask).astype("float32")
    band_gain = np.where(gains_apply[:, None] | gains_zero[:, None], band_gain, np.float32(1.0))
    bin_gain = np.repeat(band_gain, widths, axis=1)
    out = (noisy * bin_gain).astype("complex64")
    if df_apply.any():
        coef = (coefs[..., 0] + 1j * coefs[..., 1]).astype("complex64")  # (n, 96, 5)
        acc = np.zeros((n, NB_DF), dtype="complex64")
        for tap in range(DF_ORDER):
            acc += noisy_ctx[tap : tap + n, :NB_DF] * coef[:, :, tap]
        out[:, :NB_DF] = np.where(df_apply[:, None], acc, out[:, :NB_DF])
    lim = atten_lim_linear(params.atten_lim_db)
    if lim is not None:
        out = (out * np.float32(1.0 - lim) + noisy * np.float32(lim)).astype("complex64")
    return out


def _design_fir() -> Array:
    """Kaiser pencereli sinc alçak geçiren (48 kHz'de fc 8 kHz), DC kazancı 1."""
    np = _np()
    center = (_FIR_TAPS - 1) // 2
    index = np.arange(_FIR_TAPS, dtype=np.float64) - center
    taps = np.sinc(2.0 * _FIR_CUTOFF_HZ / MODEL_SR * index) * np.kaiser(
        _FIR_TAPS, _FIR_KAISER_BETA
    )
    return (taps / taps.sum()).astype(np.float32)


class Upsampler3:
    """16 kHz -> 48 kHz (tam çarpan 3), durumlu FIR. Çıktı gecikmesi 96 örnek (48 kHz)."""

    def __init__(self) -> None:
        self._taps = _design_fir() * RESAMPLE_FACTOR
        self.reset()

    def reset(self) -> None:
        self._tail = _np().zeros(_FIR_TAPS - 1, dtype="float32")

    def process(self, samples: Array) -> Array:
        np = _np()
        stuffed = np.zeros(samples.size * RESAMPLE_FACTOR, dtype="float32")
        stuffed[::RESAMPLE_FACTOR] = samples
        joined = np.concatenate([self._tail, stuffed])
        self._tail = joined[-(_FIR_TAPS - 1):].copy()
        return np.convolve(joined, self._taps, mode="valid").astype("float32")


class Downsampler3:
    """48 kHz -> 16 kHz, durumlu FIR + her 3. örnek. Girdi uzunluğu 3'ün katı olmalı."""

    def __init__(self) -> None:
        self._taps = _design_fir()
        self.reset()

    def reset(self) -> None:
        self._tail = _np().zeros(_FIR_TAPS - 1, dtype="float32")

    def process(self, samples: Array) -> Array:
        np = _np()
        if samples.size % RESAMPLE_FACTOR:
            raise ValueError("48 kHz girdi 3'ün katı olmalı")
        joined = np.concatenate([self._tail, samples.astype("float32", copy=False)])
        self._tail = joined[-(_FIR_TAPS - 1):].copy()
        filtered = np.convolve(joined, self._taps, mode="valid")
        return filtered[::RESAMPLE_FACTOR].astype("float32")


# --- Grafik çalıştırıcı ---------------------------------------------------------------


class GraphOutputs(NamedTuple):
    mask: Array  # (S, 32) ERB kazançları
    coefs: Array  # (S, 96, 5, 2) derin filtre katsayıları (gerçek, sanal)
    lsnr: Array  # (S,) yerel SNR tahmini (dB)


class GraphRunner(Protocol):
    """enc + erb_dec + df_dec tam dizi çıkarımı; testlerde sahte çalıştırıcı enjekte edilir."""

    def infer(self, feat_erb: Array, feat_spec: Array) -> GraphOutputs:
        """feat_erb: (S, 32); feat_spec: (2, S, 96) [gerçek, sanal]."""
        ...


def _ort_error_types(ort: Any) -> tuple[type[Exception], ...]:
    state = getattr(getattr(ort, "capi", None), "onnxruntime_pybind11_state", None)
    names = (
        "Fail", "InvalidArgument", "RuntimeException", "InvalidGraph", "EPFail",
        "NoSuchFile", "NoModel", "ModelLoaded", "NotImplemented", "InvalidProtobuf",
    )
    found: list[type[Exception]] = []
    for name in names:
        error_type = getattr(state, name, None)
        if isinstance(error_type, type) and issubclass(error_type, Exception):
            found.append(error_type)
    return tuple(found)


def verify_model_files(model_dir: Path) -> dict[str, bytes]:
    """Üç grafiği okur ve sabitlenmiş sha256'ya karşı doğrular; baytları döndürür.

    Doğrulanan baytlar doğrudan oturuma verilir (doğrulama ile yükleme arasında dosya değişemez).
    """
    if not model_dir.is_dir():
        raise OSError(
            f"Gürültü engelleme modeli bulunamadı: {model_dir}. "
            "`python scripts/fetch_noise_model.py` ile indirin."
        )
    blobs: dict[str, bytes] = {}
    for name, expected in MODEL_SHA256.items():
        path = model_dir / name
        try:
            data = path.read_bytes()
        except OSError as error:
            raise OSError(
                f"Gürültü engelleme model dosyası okunamadı: {path}. "
                "`python scripts/fetch_noise_model.py` ile indirin."
            ) from error
        actual = hashlib.sha256(data).hexdigest()
        if actual != expected:
            raise ValueError(
                f"Gürültü engelleme model dosyasının sha256 değeri beklenenle eşleşmiyor: {path} "
                f"(beklenen {expected[:12]}…, bulunan {actual[:12]}…). "
                "Dosyayı `python scripts/fetch_noise_model.py` ile yeniden indirin."
            )
        blobs[name] = data
    return blobs


class OnnxGraphRunner:
    """onnxruntime CPU, tek iş parçacığı; üç grafik dizi uzunluğu S için dinamiktir."""

    def __init__(self, model_dir: Path) -> None:
        blobs = verify_model_files(model_dir)
        try:
            ort = importlib.import_module("onnxruntime")
        except ImportError as error:
            raise DenoiseDependencyError(
                "Gürültü engelleme için onnxruntime gerekli: `pip install .[voice]`."
            ) from error
        self._errors = _ort_error_types(ort)
        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        options.log_severity_level = 3
        try:
            self._enc, self._erb_dec, self._df_dec = (
                ort.InferenceSession(
                    blobs[name], sess_options=options, providers=["CPUExecutionProvider"]
                )
                for name in ("enc.onnx", "erb_dec.onnx", "df_dec.onnx")
            )
        except self._errors as error:
            raise RuntimeError(f"Gürültü engelleme modeli yüklenemedi: {error}") from error

    def infer(self, feat_erb: Array, feat_spec: Array) -> GraphOutputs:
        np = _np()
        try:
            e0, e1, e2, e3, emb, c0, lsnr = self._enc.run(
                None,
                {
                    "feat_erb": np.ascontiguousarray(feat_erb[None, None], dtype="float32"),
                    "feat_spec": np.ascontiguousarray(feat_spec[None], dtype="float32"),
                },
            )
            (mask,) = self._erb_dec.run(
                ["m"], {"emb": emb, "e3": e3, "e2": e2, "e1": e1, "e0": e0}
            )
            (coefs,) = self._df_dec.run(["coefs"], {"emb": emb, "c0": c0})
        except self._errors as error:
            raise RuntimeError(f"Gürültü engelleme çıkarımı başarısız: {error}") from error
        steps = feat_erb.shape[0]
        return GraphOutputs(
            mask=mask.reshape(steps, NB_ERB),
            coefs=coefs.reshape(steps, NB_DF, DF_ORDER, 2),
            lsnr=lsnr.reshape(steps),
        )


class StepOutputs(NamedTuple):
    mask: Array  # (32,) ERB kazançları
    coefs: Array  # (96, 5, 2) derin filtre katsayıları (gerçek, sanal)
    lsnr: float  # yerel SNR tahmini (dB)


class StepRunner(Protocol):
    """Durumlu tek-kare çıkarımı; durum çalıştırıcıdadır. Testlerde sahtesi enjekte edilir."""

    def reset(self) -> None:
        """Tüm GRU/evrişim durumlarını sıfırlar (akışın başı)."""
        ...

    def step(self, feat_erb: Array, feat_spec: Array) -> StepOutputs:
        """feat_erb: (32,); feat_spec: (2, 96) [gerçek, sanal]. Çıktı t-LOOKAHEAD. kareye aittir."""
        ...


def verify_step_model(model_dir: Path) -> bytes:
    """``dfn3_step.onnx`` dosyasını okur ve sabitlenmiş sha256'ya karşı doğrular.

    Doğrulanan baytlar doğrudan oturuma verilir (doğrulama ile yükleme arasında dosya değişemez).
    Model pakete dahildir; dosya yoksa paket bozuktur (indirme adımı yoktur).
    """
    path = model_dir / STEP_MODEL_NAME
    try:
        data = path.read_bytes()
    except OSError as error:
        raise OSError(
            f"Gürültü engelleme model dosyası okunamadı: {path}. Jarvis'i yeniden kurun "
            "(model pakete dahildir) ya da `voice.noise_model_dir` ayarını kaldırın."
        ) from error
    actual = hashlib.sha256(data).hexdigest()
    if actual != STEP_MODEL_SHA256:
        raise ValueError(
            f"Gürültü engelleme model dosyasının sha256 değeri beklenenle eşleşmiyor: {path} "
            f"(beklenen {STEP_MODEL_SHA256[:12]}…, bulunan {actual[:12]}…). "
            "Dosyayı paketle gelen sürümle değiştirin."
        )
    return data


class StatefulOnnxRunner:
    """Adım modelini onnxruntime CPU (tek iş parçacığı) ile çalıştırır; durumu hop'lar arası tutar.
    """

    def __init__(self, model_dir: Path) -> None:
        blob = verify_step_model(model_dir)
        try:
            ort = importlib.import_module("onnxruntime")
        except ImportError as error:
            raise DenoiseDependencyError(
                "Gürültü engelleme için onnxruntime gerekli: `pip install .[voice]`."
            ) from error
        self._errors = _ort_error_types(ort)
        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        options.log_severity_level = 3
        try:
            self._session = ort.InferenceSession(
                blob, sess_options=options, providers=["CPUExecutionProvider"]
            )
        except self._errors as error:
            raise RuntimeError(f"Gürültü engelleme modeli yüklenemedi: {error}") from error
        self.reset()

    def reset(self) -> None:
        np = _np()
        self._state = {
            name: np.zeros(shape, dtype="float32") for name, shape in STATE_SHAPES.items()
        }

    def step(self, feat_erb: Array, feat_spec: Array) -> StepOutputs:
        np = _np()
        feeds = {
            "feat_erb": np.ascontiguousarray(feat_erb, dtype="float32").reshape(1, 1, 1, NB_ERB),
            "feat_spec": np.ascontiguousarray(feat_spec, dtype="float32").reshape(1, 2, 1, NB_DF),
            **self._state,
        }
        try:
            mask, coefs, lsnr, *states = self._session.run(_STEP_OUTPUTS, feeds)
        except self._errors as error:
            raise RuntimeError(f"Gürültü engelleme çıkarımı başarısız: {error}") from error
        self._state = dict(zip(STATE_SHAPES, states, strict=True))
        return StepOutputs(
            mask.reshape(NB_ERB), coefs.reshape(NB_DF, DF_ORDER, 2), float(lsnr.reshape(()))
        )


def enhance_fullseq(
    signal: Array, runner: GraphRunner, params: RuntimeParams | None = None
) -> Array:
    """Resmi TAM DİZİ grafiklerle çevrimdışı iyileştirme (parite referansı; üretimde kullanılmaz).

    Çıktı girdiyle hizalıdır (STFT + ileri bakış gecikmesi telafi edilir) ve aynı uzunluktadır.
    Sinyalin ardından sessizlik geliyormuş gibi 3 hop eklenir (akıştaki davranış). Altın çıktıyla
    (``deep-filter -D``) ölçülen SNR: tests/test_voice_denoise.py. Durumlu yol buna eşlenir.
    """
    np = _np()
    params = params or RuntimeParams()
    widths, alpha = erb_widths(), norm_alpha()
    signal = np.asarray(signal, dtype="float32")
    real_hops = -(-signal.size // HOP_SIZE)
    total = real_hops + DELAY_HOPS
    padded = np.zeros(total * HOP_SIZE, dtype="float32")
    padded[: signal.size] = signal
    stft, istft, features = Stft(), Istft(), FeatureExtractor(widths, alpha)
    spec = stft.analyze(padded)
    erb, cplx = features.push(spec)
    outputs = runner.infer(erb, np.stack([cplx.real, cplx.imag]))
    zeros = np.zeros((LOOKAHEAD, FREQ_SIZE), dtype="complex64")
    # Grafiğin t. çıktısı t-LOOKAHEAD. kareye aittir (ileri bakış girdi kaydırmasıyla sağlanır).
    enhanced = enhance_spectra(
        np.concatenate([zeros, spec]),
        outputs.mask[LOOKAHEAD:], outputs.coefs[LOOKAHEAD:], outputs.lsnr[LOOKAHEAD:],
        widths, params,
    )
    audio = istft.synthesize(enhanced)
    return audio[HOP_SIZE : HOP_SIZE + signal.size].copy()  # kare t, hop t-1'i tamamlar


_WARN_INTERVAL_SECONDS = 30.0


# --- Denoiser -------------------------------------------------------------------------


class DeepFilterNetDenoiser:
    """16 kHz int16 akış (``process_frame``) ve 48 kHz çevrimdışı (``enhance_48k``) yolları."""

    def __init__(
        self,
        model_dir: Path | None = None,
        *,
        runner: StepRunner | None = None,
        atten_lim_db: float = 100.0,
        frame_samples: int = 320,
        params: RuntimeParams | None = None,
    ) -> None:
        per_hop = HOP_SIZE // RESAMPLE_FACTOR  # 160 örnek (16 kHz) = 10 ms
        if frame_samples <= 0 or frame_samples % per_hop:
            raise ValueError(f"frame_samples {per_hop}'in katı olmalı")
        if runner is None:
            if model_dir is None:
                raise ValueError("model_dir veya runner verilmeli")
            runner = StatefulOnnxRunner(model_dir)
        self._runner = runner
        base = params or RuntimeParams()
        self._params = RuntimeParams(
            atten_lim_db, base.min_db_thresh, base.max_db_erb_thresh, base.max_db_df_thresh
        )
        self.frame_samples = frame_samples
        # Sabit gecikme: t. hop geldiğinde t-2. kare biter, ISTFT bir hop daha geriden gelir.
        self.delay_hops = DELAY_HOPS
        self.delay_samples = DELAY_HOPS * per_hop + RESAMPLE_DELAY_16K
        self.underruns = 0
        self.late_frames = 0
        self._last_warned: dict[str, float] = {}
        self._widths = erb_widths()
        self._alpha = norm_alpha()
        self._stft = Stft()
        self._istft = Istft()
        self._features = FeatureExtractor(self._widths, self._alpha)
        self._up = Upsampler3()
        self._down = Downsampler3()
        self.reset()

    def _warn_limited(self, key: str, message: str, *args: object) -> None:
        """İlk oluşta hemen, sonra anahtar başına en çok 30 sn'de bir uyarır."""
        now = time.monotonic()
        last = self._last_warned.get(key)
        if last is None or now - last >= _WARN_INTERVAL_SECONDS:
            self._last_warned[key] = now
            logger.warning(message, *args)

    @property
    def atten_lim_db(self) -> float:
        return self._params.atten_lim_db

    def reset(self) -> None:
        """Tüm akış durumunu (STFT, normalizasyon, model GRU/evrişim durumu, FIR) sıfırlar."""
        np = _np()
        self._stft.reset()
        self._istft.reset()
        self._features.reset()
        self._up.reset()
        self._down.reset()
        self._runner.reset()
        # DF için j-2..j+2 kareleri: son 4 gürültülü spektrum (akış başında sıfır).
        self._spec_tail = np.zeros((2 * LOOKAHEAD, FREQ_SIZE), dtype="complex64")
        self._warmup = LOOKAHEAD  # kare -2 ve -1 çıktıları atılır
        self._first_frame_done = False
        self._fifo = np.zeros(self.delay_hops * HOP_SIZE, dtype="float32")

    def _advance(self, spec: Array, erb: Array, cplx: Array) -> Array:
        """Yeni hop'ları modele verir; tamamlanan 48 kHz örnekleri döner (boş olabilir)."""
        np = _np()
        hops = spec.shape[0]
        feat_spec = np.stack([cplx.real, cplx.imag], axis=1)  # (hops, 2, 96)
        masks = np.empty((hops, NB_ERB), dtype="float32")
        coefs = np.empty((hops, NB_DF, DF_ORDER, 2), dtype="float32")
        lsnr = np.empty(hops, dtype="float32")
        for hop in range(hops):  # durum sırayla ilerler; her hop için model bir kez çalışır
            out = self._runner.step(erb[hop], feat_spec[hop])
            masks[hop], coefs[hop], lsnr[hop] = out.mask, out.coefs, out.lsnr
        # Çıktı i, ctx[i + 2] karesine karşılık gelir; DF için ctx[i : i + 5] kullanılır.
        ctx = np.concatenate([self._spec_tail, spec])
        self._spec_tail = ctx[-2 * LOOKAHEAD :].copy()
        drop = min(self._warmup, hops)
        self._warmup -= drop
        if drop == hops:
            return np.zeros(0, dtype="float32")
        enhanced = enhance_spectra(
            ctx[drop:], masks[drop:], coefs[drop:], lsnr[drop:], self._widths, self._params
        )
        audio = self._istft.synthesize(enhanced)
        if not self._first_frame_done:
            self._first_frame_done = True
            audio = audio[HOP_SIZE:]  # kare 0 "hop -1"i üretir; girdide karşılığı yok
        return audio

    # -- Çevrimdışı yol --

    def enhance_48k(self, signal: Array) -> Array:
        """Tüm sinyali (48 kHz float32) durumlu adım modeliyle iyileştirir.

        Çıktı girdiyle hizalıdır ve aynı uzunluktadır (``enhance_fullseq`` ile aynı sözleşme).
        Akış durumunu sıfırlar: sürmekte olan bir ``process_frame`` akışıyla birlikte kullanmayın.
        """
        np = _np()
        signal = np.asarray(signal, dtype="float32")
        real_hops = -(-signal.size // HOP_SIZE)
        padded = np.zeros((real_hops + self.delay_hops) * HOP_SIZE, dtype="float32")
        padded[: signal.size] = signal
        self.reset()
        try:
            spec = self._stft.analyze(padded)
            erb, cplx = self._features.push(spec)
            audio = self._advance(spec, erb, cplx)
        finally:
            self.reset()
        return audio[: signal.size].copy()

    # -- Akış yolu --

    def process_frame(self, pcm: bytes) -> bytes:
        """16 kHz int16 mono bir kare alır, aynı uzunlukta kare döner (``delay_samples`` gecikmeli).

        Isınma süresince (ilk ``delay_samples`` örnek) çıktı sessizliktir; sesin kendisi kaybolmaz,
        yalnız gecikir. Çıkış her zaman girişle 1:1'dir.
        """
        np = _np()
        raw = np.frombuffer(pcm, dtype="<i2")
        if raw.size != self.frame_samples:
            raise ValueError(
                f"Kare {self.frame_samples} örnek olmalı, {raw.size} örnek geldi"
            )
        began = time.perf_counter()
        signal48 = self._up.process(raw.astype("float32") / 32768.0)
        spec = self._stft.analyze(signal48)
        erb, cplx = self._features.push(spec)
        self._fifo = np.concatenate([self._fifo, self._advance(spec, erb, cplx)])
        need = signal48.size
        if self._fifo.size < need:
            self.underruns += 1
            self._warn_limited(
                "underrun",
                "Gürültü engelleme çıktı tamponu boş kaldı (%d kez); ses boşlukları olabilir.",
                self.underruns,
            )
            self._fifo = np.concatenate([self._fifo, np.zeros(need - self._fifo.size, "float32")])
        chunk, self._fifo = self._fifo[:need], self._fifo[need:]
        out16 = self._down.process(chunk)
        if not np.isfinite(out16).all():
            raise ValueError("Gürültü engelleme sonlu olmayan (NaN/inf) örnek üretti")
        pcm_out: bytes = np.clip(np.rint(out16 * 32768.0), -32768, 32767).astype("<i2").tobytes()
        # Kare, gerçek zamandan uzun sürerse çalışan iş parçacığı yetişemez (akış gecikir).
        budget = self.frame_samples / IN_SR
        if time.perf_counter() - began > budget:
            self.late_frames += 1
            self._warn_limited(
                "late",
                "Gürültü engelleme gerçek zamandan yavaş (kare %.0f ms'den uzun sürdü); "
                "ses gecikebilir. Kapatmayı düşünün.",
                budget * 1000,
            )
        return pcm_out


# --- Mikrofon sarmalayıcı -------------------------------------------------------------


class FrameDenoiser(Protocol):
    frame_samples: int
    delay_samples: int

    def process_frame(self, pcm: bytes) -> bytes: ...

    def reset(self) -> None: ...


# Denoiser kendi hatalarını RuntimeError/ValueError olarak yükseltir; numpy kısıtları ValueError,
# bellek/dosya sorunları OSError olabilir. Kör `except Exception` kullanılmaz.
_DENOISE_ERRORS = (RuntimeError, ValueError, OSError, ArithmeticError)


class DenoisedMicrophone:
    """``Microphone`` protokolünü bir iç mikrofonun üstünde gürültü engelleyerek uygular.

    ``pause/resume/discard_pending`` başka bir iş parçacığından (onay akışı) gelebilir. Bu
    yüzden paylaşılan durum yalnız ``_state_lock`` altında değişir; damga ve ham kare
    tamponlarına yalnız ``frames()`` (olay döngüsü) dokunur, sıfırlama onlara nesil sayacıyla
    iletilir. Sıfırlama anından ÖNCE yakalanmış kareler (iç mikrofon geç teslim etse bile)
    atılır: onay/duraklatma sırasındaki ses sonradan komut olarak kullanılamaz.
    """

    def __init__(
        self,
        inner: Microphone,
        denoiser: FrameDenoiser,
        on_state: Callable[[str], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._inner = inner
        self._denoiser = denoiser
        self._on_state = on_state or (lambda _state: None)
        self._clock = clock  # AudioFrame.timestamp ile aynı saat (monotonic)
        self._passthrough = False
        self._state_lock = threading.Lock()
        self._generation = 0
        self._reset_time = float("-inf")
        self._reset_pending = False
        # İptal edilen frames()'in uçuştaki çağrısı ile yenisi çakışmasın.
        self._lock = threading.Lock()
        self._seen_generation = 0
        self._last_stamp = float("-inf")
        delay_frames = round(denoiser.delay_samples / denoiser.frame_samples)
        self._stamps: deque[float] = deque(maxlen=delay_frames + 1)
        # Henüz çıktıya yansımamış (gecikme içindeki) ham kareler; hata anında ham olarak akıtılır.
        self._frame_samples = denoiser.frame_samples
        self._delay_samples = denoiser.delay_samples
        self._raw: deque[AudioFrame] = deque(
            maxlen=-(-denoiser.delay_samples // denoiser.frame_samples)
        )

    @property
    def passthrough(self) -> bool:
        return self._passthrough

    @property
    def underruns(self) -> int:
        return int(getattr(self._denoiser, "underruns", 0))

    def _process(self, pcm: bytes, generation: int) -> bytes | None:
        """İş parçacığında çalışır; bekleyen sıfırlama çıkarımdan önce uygulanır."""
        with self._lock:
            with self._state_lock:
                if generation != self._generation:
                    return None  # kare, sıfırlamadan önceki nesle ait
                reset, self._reset_pending = self._reset_pending, False
            if reset:
                self._denoiser.reset()
            return self._denoiser.process_frame(pcm)

    def _unemitted_part(self, pending: AudioFrame, index: int) -> AudioFrame:
        """Tampon doluysa ilk karenin çıktıya zaten yansımış başını (gecikme taşması) keser."""
        if index or len(self._raw) < (self._raw.maxlen or 0):
            return pending
        extra = len(self._raw) * self._frame_samples - self._delay_samples
        return AudioFrame(pending.pcm[2 * extra :], pending.timestamp) if extra > 0 else pending

    def _fail(self, error: Exception) -> None:
        if self._passthrough:
            return
        self._passthrough = True
        logger.warning(
            "Gürültü engelleme çalışma anında başarısız oldu (%s); bu oturumda kapatıldı, "
            "ses işlenmeden iletiliyor.",
            type(error).__name__,
        )
        self._on_state("denoise:off")

    def _request_reset(self) -> None:
        with self._state_lock:
            self._reset_pending = True
            self._generation += 1
            self._reset_time = self._clock()

    def _snapshot(self) -> tuple[int, float]:
        with self._state_lock:
            return self._generation, self._reset_time

    async def frames(self) -> AsyncIterator[AudioFrame]:
        async for frame in self._inner.frames():
            generation, reset_time = self._snapshot()
            if self._passthrough:
                # Ham geçişte de sıfırlama öncesi kare atılır; iç mikrofonun süzgecine güvenilmez.
                if frame.timestamp > reset_time:
                    yield frame
                continue
            if generation != self._seen_generation:
                self._seen_generation = generation
                self._stamps.clear()
                self._raw.clear()
            if frame.timestamp <= reset_time:
                continue  # sıfırlamadan önce yakalanmış kare (iç kuyrukta kalmış olabilir)
            try:
                pcm = await asyncio.to_thread(self._process, frame.pcm, generation)
            except _DENOISE_ERRORS as error:
                self._fail(error)
                if self._snapshot()[0] == generation:
                    # Gecikme içinde kalan ham sesi kaybetme: sırayla akıt, sonra bu kareyi.
                    for index, pending in enumerate(self._raw):
                        yield self._unemitted_part(pending, index)
                    yield frame
                self._raw.clear()
                continue
            if pcm is None or self._snapshot()[0] != generation:
                continue  # işlenirken sıfırlama geldi; bayat çıktı atılır
            self._stamps.append(frame.timestamp)
            self._raw.append(frame)
            # Çıktı, gecikme kadar önce yakalanan sesi taşır; zaman damgası da onunki olur.
            # Isınmada (sıfırlamadan sonra) en eski bekleyen damga kullanılır; damgalar geriye
            # gitmez. max(), sıfırlama sonrası saat farkına karşı ek güvencedir.
            stamp = max(self._stamps[0], self._last_stamp)
            self._last_stamp = stamp
            yield AudioFrame(pcm, stamp)

    def discard_pending(self) -> None:
        self._inner.discard_pending()
        self._request_reset()

    def pause(self) -> None:
        self._inner.pause()
        self._request_reset()

    def resume(self) -> None:
        self._inner.resume()
        self._request_reset()

    async def close(self) -> None:
        await self._inner.close()
