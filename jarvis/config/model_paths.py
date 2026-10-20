"""Yerel model konumları; yalnız standart kütüphane."""

from pathlib import Path

# DeepFilterNet3 durumlu adım modeli (ADR 0037) pakete dahildir; `voice.noise_model_dir` verilmezse
# runtime buradan okur (indirme adımı yoktur). Paket konumuna bağlıdır: config dosyasının
# konumundan ve cwd'den bağımsızdır, normal ve editable kurulumda aynı dosyaya işaret eder.
DEFAULT_NOISE_MODEL_DIR = (
    Path(__file__).resolve().parents[1] / "channels" / "voice" / "models" / "dfn3"
)
