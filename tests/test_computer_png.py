import struct
import zlib

import pytest

np = pytest.importorskip("numpy")

from jarvis.computer.png import (  # noqa: E402
    bgra_to_rgb,
    downscale,
    encode_png,
    screenshot_png,
)


def _decode_png(data: bytes) -> tuple[int, int, np.ndarray]:
    """Küçük bir PNG çözücüyle parçaları, CRC'leri ve pikselleri doğrular."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n"

    position = 8
    chunk_types: list[bytes] = []
    idat_parts: list[bytes] = []
    width = 0
    height = 0

    while position < len(data):
        assert position + 12 <= len(data)
        (length,) = struct.unpack_from(">I", data, position)
        chunk_type = data[position + 4 : position + 8]
        payload_start = position + 8
        payload_end = payload_start + length
        assert payload_end + 4 <= len(data)

        payload = data[payload_start:payload_end]
        (stored_crc,) = struct.unpack_from(">I", data, payload_end)
        calculated_crc = zlib.crc32(chunk_type + payload) & 0xFFFFFFFF
        assert stored_crc == calculated_crc
        chunk_types.append(chunk_type)

        if chunk_type == b"IHDR":
            assert length == 13
            (
                width,
                height,
                bit_depth,
                color_type,
                compression,
                filter_method,
                interlace,
            ) = struct.unpack(">IIBBBBB", payload)
            assert (bit_depth, color_type, compression, filter_method, interlace) == (
                8,
                2,
                0,
                0,
                0,
            )
        elif chunk_type == b"IDAT":
            idat_parts.append(payload)

        position = payload_end + 4

    assert position == len(data)
    assert chunk_types == [b"IHDR", b"IDAT", b"IEND"]

    raw = zlib.decompress(b"".join(idat_parts))
    row_size = width * 3
    assert len(raw) == height * (row_size + 1)

    pixels = np.empty((height, width, 3), dtype=np.uint8)
    offset = 0
    for row_index in range(height):
        assert raw[offset] == 0
        offset += 1
        row = np.frombuffer(
            raw,
            dtype=np.uint8,
            count=row_size,
            offset=offset,
        ).reshape(width, 3)
        pixels[row_index] = row
        offset += row_size

    assert offset == len(raw)
    return width, height, pixels


def test_bgra_to_rgb_uses_rgb_channel_order() -> None:
    """BGRA sırasının RGB olarak değiştirildiğini doğrular."""
    buffer = bytes((10, 20, 30, 40, 50, 60, 70, 80))

    rgb = bgra_to_rgb(buffer, 2, 1)

    expected = np.array([[[30, 20, 10], [70, 60, 50]]], dtype=np.uint8)
    np.testing.assert_array_equal(rgb, expected)
    assert rgb.dtype == np.uint8
    assert rgb.flags.c_contiguous


def test_bgra_to_rgb_rejects_wrong_buffer_length() -> None:
    """Yanlış arabellek uzunluğunda Türkçe hata verir."""
    with pytest.raises(ValueError, match="BGRA"):
        bgra_to_rgb(b"\x00\x00\x00", 1, 1)


def test_downscale_leaves_small_image_unchanged() -> None:
    """Genişliği zaten yeterli görüntüyü değiştirmez."""
    rgb = np.arange(18, dtype=np.uint8).reshape(2, 3, 3)

    result = downscale(rgb, 3)

    assert result is rgb


def test_downscale_averages_each_two_by_two_block() -> None:
    """4x2 görüntünün 2x2 blok ortalamalarını doğrular."""
    rgb = np.array(
        [
            [
                [0, 10, 20],
                [30, 40, 50],
                [60, 70, 80],
                [90, 100, 110],
            ],
            [
                [0, 10, 20],
                [30, 40, 50],
                [60, 70, 80],
                [90, 100, 110],
            ],
        ],
        dtype=np.uint8,
    )

    result = downscale(rgb, 2)

    expected = np.array([[[15, 25, 35], [75, 85, 95]]], dtype=np.uint8)
    assert result.shape == (1, 2, 3)
    np.testing.assert_array_equal(result, expected)


def test_downscale_non_integer_ratio_has_expected_shape() -> None:
    """Tamsayı olmayan oranın beklenen çıktı boyutunu doğrular."""
    rgb = np.zeros((3, 5, 3), dtype=np.uint8)

    result = downscale(rgb, 2)

    assert result.shape == (1, 2, 3)
    assert result.dtype == np.uint8


def test_encode_png_round_trip() -> None:
    """PNG parçalarını, CRC'lerini ve sıkıştırılmış pikselleri çözer."""
    rgb = np.array(
        [
            [[255, 0, 0], [0, 255, 0]],
            [[0, 0, 255], [12, 34, 56]],
        ],
        dtype=np.uint8,
    )

    encoded = encode_png(rgb)

    width, height, decoded = _decode_png(encoded)
    assert (width, height) == (2, 2)
    np.testing.assert_array_equal(decoded, rgb)


def test_screenshot_png_returns_downscaled_size() -> None:
    """Ekran görüntüsü işleminin küçültülmüş boyutu döndürdüğünü doğrular."""
    png_bytes, (out_width, out_height) = screenshot_png(
        bytes(range(32)),
        4,
        2,
        2,
    )

    assert (out_width, out_height) == (2, 1)
    width, height, decoded = _decode_png(png_bytes)
    assert (width, height) == (out_width, out_height)
    assert decoded.shape == (out_height, out_width, 3)
