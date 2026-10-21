"""PNG üretimi için NumPy tabanlı yardımcı işlevler."""

import struct
import zlib

import numpy as np
from numpy.typing import NDArray

__all__ = [
    "bgra_to_rgb",
    "downscale",
    "encode_png",
    "screenshot_png",
]


def bgra_to_rgb(buffer: bytes, width: int, height: int) -> NDArray[np.uint8]:
    """Üstten alta sıralı 32-bit BGRA tamponunu RGB dizisine dönüştürür."""
    if width < 0 or height < 0:
        raise ValueError("Genişlik ve yükseklik negatif olamaz.")

    expected_size = width * height * 4
    if len(buffer) != expected_size:
        raise ValueError(
            f"BGRA arabelleğinin uzunluğu {expected_size} bayt olmalıdır; "
            f"{len(buffer)} bayt verildi."
        )

    source = np.frombuffer(buffer, dtype=np.uint8).reshape((height, width, 4))
    rgb = np.empty((height, width, 3), dtype=np.uint8)
    rgb[..., 0] = source[..., 2]
    rgb[..., 1] = source[..., 1]
    rgb[..., 2] = source[..., 0]
    return rgb


def downscale(rgb: NDArray[np.uint8], max_width: int) -> NDArray[np.uint8]:
    """Görüntüyü kutu ortalamasıyla istenen genişliğe küçültür."""
    if max_width <= 0:
        raise ValueError("max_width pozitif bir tam sayı olmalıdır.")

    height = int(rgb.shape[0])
    width = int(rgb.shape[1])
    if width <= max_width:
        return rgb
    if height == 0:
        raise ValueError("Küçültülecek RGB görüntüsü boş olamaz.")

    new_width = max_width
    new_height = max(1, round(height * new_width / width))

    row_bounds = (
        np.arange(new_height + 1, dtype=np.int64) * height // new_height
    )
    column_bounds = (
        np.arange(new_width + 1, dtype=np.int64) * width // new_width
    )

    values = np.asarray(rgb, dtype=np.uint64)
    column_sums = np.add.reduceat(values, column_bounds[:-1], axis=1)
    block_sums = np.add.reduceat(column_sums, row_bounds[:-1], axis=0)

    row_counts = np.diff(row_bounds).astype(np.uint64)
    column_counts = np.diff(column_bounds).astype(np.uint64)
    block_counts = row_counts[:, None, None] * column_counts[None, :, None]

    rounded = (block_sums + block_counts // 2) // block_counts
    return np.ascontiguousarray(rounded.astype(np.uint8))


def encode_png(rgb: NDArray[np.uint8]) -> bytes:
    """RGB dizisini filtre tipi sıfır olan tek IDATlı bir PNG olarak kodlar."""
    if not isinstance(rgb, np.ndarray) or rgb.ndim != 3 or rgb.shape[2] != 3:
        raise ValueError("RGB dizisi (yükseklik, genişlik, 3) biçiminde olmalıdır.")
    if rgb.dtype != np.dtype(np.uint8):
        raise ValueError("RGB dizisi uint8 türünde olmalıdır.")

    height = int(rgb.shape[0])
    width = int(rgb.shape[1])
    if height == 0 or width == 0:
        raise ValueError("PNG görüntüsü yükseklik ve genişlik bakımından boş olamaz.")

    raw = b"".join(b"\x00" + row.tobytes() for row in rgb)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    idat = zlib.compress(raw, level=6)

    return b"".join(
        (
            b"\x89PNG\r\n\x1a\n",
            _chunk(b"IHDR", ihdr),
            _chunk(b"IDAT", idat),
            _chunk(b"IEND", b""),
        )
    )


def screenshot_png(
    buffer: bytes,
    width: int,
    height: int,
    max_width: int,
) -> tuple[bytes, tuple[int, int]]:
    """Ekran tamponunu küçültür ve PNG ile çıktı boyutunu birlikte döndürür."""
    rgb = bgra_to_rgb(buffer, width, height)
    scaled = downscale(rgb, max_width)
    png_bytes = encode_png(scaled)
    out_width = int(scaled.shape[1])
    out_height = int(scaled.shape[0])
    return png_bytes, (out_width, out_height)


def _chunk(chunk_type: bytes, data: bytes) -> bytes:
    """PNG yükünü uzunluğu, türü, verisi ve CRC değeriyle oluşturur."""
    checksum = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
    return (
        struct.pack(">I", len(data))
        + chunk_type
        + data
        + struct.pack(">I", checksum)
    )
