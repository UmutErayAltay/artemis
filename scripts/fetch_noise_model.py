"""DeepFilterNet3 ONNX modelini indirir ve doğrular (ADR 0037); yalnız standart kütüphane.

Kullanım: python scripts/fetch_noise_model.py [--dest models/deepfilternet3]
Jarvis çalışma anında indirme yapmaz; modeli bir kez bu betikle kurun ve
`voice.noise_model_dir` ile gösterin.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import sys
import tarfile
import urllib.request
from collections.abc import Mapping
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DEST = ROOT / "models" / "deepfilternet3"
SOURCE_URL = (
    "https://github.com/Rikorose/DeepFilterNet/raw/"
    "d375b2d8309e0935d165700c91da9de862a99c31/models/DeepFilterNet3_onnx.tar.gz"
)
TARBALL_SHA256 = "c94d91f70911001c946e0fabb4aa9adc37045f45a03b56008cb0c8244cb63616"
# Tarball içindeki yol (tmp/export/*) -> hedef dosya adı ve sha256.
MEMBER_SHA256: Mapping[str, str] = {
    "enc.onnx": "7c5399d3da8a50ebef1c1a0ae421b33376aa5e45d0e92df16da7e83c9c131916",
    "erb_dec.onnx": "ab669a1d10afe20911728b33053a452071042317a90581092b325da7b2f9d895",
    "df_dec.onnx": "23114ce3b0f6464b763ee62f7bb8aab6b2a129a21eabd5bcfe59413db05f278a",
    "config.ini": "415eb925d44990d938fb739f514aa3662c1ec0ea836cff044fa1291b82cb4290",
}
_MEMBER_PREFIX = PurePosixPath("tmp/export")
_MAX_MEMBER_BYTES = 64 * 1024 * 1024
NOTICE = """DeepFilterNet3 (ONNX dışa aktarımı)
Yazar: Hendrik Schröter. Lisans: MIT veya Apache-2.0 (çift lisans).
Kaynak: https://github.com/Rikorose/DeepFilterNet (models/DeepFilterNet3_onnx.tar.gz)
İndirilen adres: {url}
Arşiv sha256: {digest}
Jarvis bu dosyaları değiştirmez; bütünlük denoise.py'de her yüklemede sha256 ile doğrulanır.
"""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def extract_model(
    tarball: bytes,
    dest: Path,
    *,
    tarball_sha256: str = TARBALL_SHA256,
    member_sha256: Mapping[str, str] = MEMBER_SHA256,
    url: str = SOURCE_URL,
) -> list[Path]:
    """Arşivi doğrular, yalnız beklenen üyeleri `dest` içine yazar. Hata: ValueError."""
    digest = _sha256(tarball)
    if digest != tarball_sha256:
        raise ValueError(f"Arşiv sha256 değeri eşleşmiyor: {digest[:12]}… ≠ {tarball_sha256[:12]}…")
    blobs: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as archive:
        for member in archive.getmembers():
            path = PurePosixPath(member.name)
            if member.isdir():
                continue
            ok = (
                member.isfile()
                and not path.is_absolute()
                and ".." not in path.parts
                and path.parent == _MEMBER_PREFIX
                and path.name in member_sha256
                and member.size <= _MAX_MEMBER_BYTES
            )
            if not ok:
                raise ValueError(f"Arşivde beklenmeyen üye reddedildi: {member.name!r}")
            handle = archive.extractfile(member)
            if handle is None:
                raise ValueError(f"Arşiv üyesi okunamadı: {member.name!r}")
            blobs[path.name] = handle.read()
    for name, expected in member_sha256.items():
        data = blobs.get(name)
        if data is None:
            raise ValueError(f"Arşivde beklenen dosya yok: {name}")
        if _sha256(data) != expected:
            raise ValueError(f"{name} sha256 değeri eşleşmiyor")
    dest.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, data in blobs.items():
        target = dest / name
        target.write_bytes(data)
        written.append(target)
    notice = dest / "NOTICE.txt"
    notice.write_text(NOTICE.format(url=url, digest=digest), encoding="utf-8")
    written.append(notice)
    return written


def download(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=60) as response:
        data: bytes = response.read()
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dest", type=Path, default=DEFAULT_DEST)
    parser.add_argument("--tarball", type=Path, help="İndirmek yerine yerel arşivi kullan.")
    args = parser.parse_args(argv)
    try:
        data = args.tarball.read_bytes() if args.tarball else download(SOURCE_URL)
        written = extract_model(data, args.dest)
    except (OSError, ValueError, tarfile.TarError) as error:
        print(f"Model kurulamadı: {error}", file=sys.stderr)
        return 1
    print(f"{len(written)} dosya yazıldı: {args.dest}")
    print(f'config.yaml → voice.noise_model_dir: "{args.dest}"')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
