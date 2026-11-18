import errno
from pathlib import Path

import pytest

from artemis.config import paths


def test_link_unsupported_falls_back_and_copies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Bugünkü kodda os.link EPERM hatası yakalanmadığı için OSError fırlatıp başarısız olur.
    example = tmp_path / "bundled" / "config.example.yaml"
    example.parent.mkdir(parents=True)
    example_bytes = "config-örnek\n".encode()
    example.write_bytes(example_bytes)
    monkeypatch.setattr(paths, "bundled_example_config", lambda: example)

    def fake_link(_src: Path | str, _dst: Path | str) -> None:
        raise OSError(errno.EPERM, "hard link unsupported")

    monkeypatch.setattr(paths.os, "link", fake_link)

    target = tmp_path / "cfg" / "config.yaml"
    assert paths.ensure_config(target) is True
    assert target.read_bytes() == example_bytes


def test_fallback_never_overwrites_existing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Bugünkü kodda os.link EPERM yakalanmadığından hata fırlatır; düzeltmede üzerine yazmaz.
    example = tmp_path / "bundled" / "config.example.yaml"
    example.parent.mkdir(parents=True)
    example.write_bytes("config-örnek\n".encode())
    monkeypatch.setattr(paths, "bundled_example_config", lambda: example)

    def fake_link(_src: Path | str, dst: Path | str) -> None:
        # Yarış koşulunu taklit et: hedef EPERM öncesinde başka süreççe yazılmış olsun.
        Path(dst).write_bytes(b"KULLANICI")
        raise OSError(errno.EPERM, "hard link unsupported")

    monkeypatch.setattr(paths.os, "link", fake_link)

    target = tmp_path / "cfg" / "config.yaml"
    assert paths.ensure_config(target) is False
    assert target.read_bytes() == b"KULLANICI"
