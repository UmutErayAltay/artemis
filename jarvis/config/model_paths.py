"""Yerel model konumları; yalnız standart kütüphane (fetch betiği de bunu içe aktarır)."""

from pathlib import Path

# DeepFilterNet3 (ADR 0037): betik bu dizine yazar, `voice.noise_model_dir` verilmezse
# runtime buradan okur. Depo köküne bağlıdır; config dosyasının konumundan bağımsızdır.
DEFAULT_NOISE_MODEL_DIR = Path(__file__).resolve().parents[2] / "models" / "deepfilternet3"
