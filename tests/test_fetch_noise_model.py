"""scripts/fetch_noise_model.py: sahte yerel arşivle; ağ kullanılmaz."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import sys
import tarfile
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "fetch_noise_model.py"


@pytest.fixture(scope="module")
def fetch() -> ModuleType:
    spec = importlib.util.spec_from_file_location("fetch_noise_model", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["fetch_noise_model"] = module
    spec.loader.exec_module(module)
    return module


FILES = {
    "enc.onnx": b"E" * 10, "erb_dec.onnx": b"R" * 11,
    "df_dec.onnx": b"D" * 12, "config.ini": b"[df]\n",
}


def _tar(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _hashes(files: dict[str, bytes]) -> dict[str, str]:
    return {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}


def _good() -> dict[str, bytes]:
    return {f"tmp/export/{name}": data for name, data in FILES.items()}


def test_extracts_only_expected_members_and_writes_notice(
    fetch: ModuleType, tmp_path: Path
) -> None:
    blob = _tar(_good())
    written = fetch.extract_model(
        blob, tmp_path / "m", tarball_sha256=hashlib.sha256(blob).hexdigest(),
        member_sha256=_hashes(FILES),
    )
    assert {p.name for p in written} == {*FILES, "NOTICE.txt"}
    assert (tmp_path / "m" / "enc.onnx").read_bytes() == FILES["enc.onnx"]
    notice = (tmp_path / "m" / "NOTICE.txt").read_text(encoding="utf-8")
    assert "MIT" in notice and "Schröter" in notice and "github.com/Rikorose" in notice


def test_rejects_bad_tarball_hash_without_writing(fetch: ModuleType, tmp_path: Path) -> None:
    blob = _tar(_good())
    with pytest.raises(ValueError, match="Arşiv sha256"):
        fetch.extract_model(
            blob, tmp_path / "m", tarball_sha256="0" * 64, member_sha256=_hashes(FILES)
        )
    assert not (tmp_path / "m").exists()


def test_rejects_member_hash_mismatch(fetch: ModuleType, tmp_path: Path) -> None:
    members = _good()
    blob = _tar(members)
    hashes = _hashes(FILES)
    hashes["enc.onnx"] = "1" * 64
    with pytest.raises(ValueError, match="enc.onnx"):
        fetch.extract_model(
            blob, tmp_path / "m", tarball_sha256=hashlib.sha256(blob).hexdigest(),
            member_sha256=hashes,
        )
    assert not (tmp_path / "m").exists()


@pytest.mark.parametrize(
    "bad",
    ["tmp/export/../../evil.onnx", "/etc/passwd", "tmp/export/extra.bin", "enc.onnx",
     "x/tmp/export/enc.onnx"],
)
def test_rejects_traversal_and_unexpected_paths(
    fetch: ModuleType, tmp_path: Path, bad: str
) -> None:
    members = _good()
    members[bad] = b"kotu"
    blob = _tar(members)
    with pytest.raises(ValueError, match="beklenmeyen üye"):
        fetch.extract_model(
            blob, tmp_path / "m", tarball_sha256=hashlib.sha256(blob).hexdigest(),
            member_sha256=_hashes(FILES),
        )
    assert not (tmp_path / "m").exists() and not (tmp_path.parent / "evil.onnx").exists()


def test_rejects_missing_member(fetch: ModuleType, tmp_path: Path) -> None:
    members = _good()
    del members["tmp/export/df_dec.onnx"]
    blob = _tar(members)
    with pytest.raises(ValueError, match="df_dec.onnx"):
        fetch.extract_model(
            blob, tmp_path / "m", tarball_sha256=hashlib.sha256(blob).hexdigest(),
            member_sha256=_hashes(FILES),
        )


def test_default_dest_is_the_shared_runtime_default(fetch: ModuleType) -> None:
    from jarvis.config.model_paths import DEFAULT_NOISE_MODEL_DIR

    assert fetch.DEFAULT_DEST == DEFAULT_NOISE_MODEL_DIR and fetch.DEFAULT_DEST.is_absolute()


def test_main_prints_resolved_absolute_path(
    fetch: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blob = _tar(_good())
    monkeypatch.setattr(fetch, "TARBALL_SHA256", hashlib.sha256(blob).hexdigest())
    monkeypatch.setattr(fetch, "MEMBER_SHA256", _hashes(FILES))
    monkeypatch.setattr(fetch.extract_model, "__kwdefaults__", {
        "tarball_sha256": fetch.TARBALL_SHA256, "member_sha256": fetch.MEMBER_SHA256,
        "url": fetch.SOURCE_URL,
    })
    local = tmp_path / "ok.tar.gz"
    local.write_bytes(blob)
    monkeypatch.chdir(tmp_path)
    assert fetch.main(["--tarball", str(local), "--dest", "rel/m"]) == 0
    assert f'"{tmp_path / "rel" / "m"}"' in capsys.readouterr().out


def test_pinned_hashes_match_denoiser_and_main_reports_failure(
    fetch: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from jarvis.channels.voice.denoise import MODEL_SHA256

    assert {k: v for k, v in fetch.MEMBER_SHA256.items() if k.endswith(".onnx")} == MODEL_SHA256
    local = tmp_path / "a.tar.gz"
    local.write_bytes(_tar(_good()))
    assert fetch.main(["--tarball", str(local), "--dest", str(tmp_path / "m")]) == 1
    assert "Model kurulamadı" in capsys.readouterr().err
