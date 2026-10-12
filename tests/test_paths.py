from pathlib import Path

import pytest

from jarvis.safety.paths import safe_join


@pytest.mark.parametrize(
    "target",
    [
        "",
        ".",
        "./",
        "folder/..",
        "..",
        "../outside",
        "folder/../file",
        "/tmp/file",
        "C:\\Windows\\file",
        "C:relative",
        "\\\\server\\share\\file",
        "\\rooted",
        "..\\outside",
        "folder\\..\\file",
        "file:stream",
        "CON",
        "con.txt",
        "folder/NUL.log",
        "PRN.anything",
        "AUX",
        "COM1",
        "com9.txt",
        "LPT1",
        "lpt9.txt",
        "COM¹.txt",
        "LPT³",
        "trailing.",
        "trailing ",
        "a\x00b",
    ],
)
def test_path_escape_rejected(tmp_path: Path, target: str) -> None:
    with pytest.raises(ValueError):
        safe_join(tmp_path, target)


def test_nested_relative_paths_and_windows_separators(tmp_path: Path) -> None:
    assert safe_join(tmp_path, "nested/file.txt") == tmp_path / "nested" / "file.txt"
    assert safe_join(tmp_path, "nested\\file.txt") == tmp_path / "nested" / "file.txt"


def test_symlink_escape_and_root_alias_rejected(tmp_path: Path) -> None:
    base = tmp_path / "desktop"
    base.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (base / "escape").symlink_to(outside, target_is_directory=True)
        (base / "self").symlink_to(base, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"Bu platformda symlink yetkisi yok: {error}")
    with pytest.raises(ValueError):
        safe_join(base, "escape/new.txt")
    with pytest.raises(ValueError):
        safe_join(base, "self")


def test_symlink_within_workspace_allowed(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    try:
        (tmp_path / "alias").symlink_to(real, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"Bu platformda symlink yetkisi yok: {error}")
    assert safe_join(tmp_path, "alias/file") == real / "file"


def test_symlinks_are_rejected_for_mutation_paths(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    real_dir = root / "real"
    real_dir.mkdir()
    real_file = real_dir / "source.txt"
    real_file.write_text("koru", encoding="utf-8")
    try:
        (root / "dir-link").symlink_to(real_dir, target_is_directory=True)
        (root / "file-link").symlink_to(real_file)
        (real_dir / "nested-link").symlink_to(real_file)
    except OSError as error:
        pytest.skip(f"Bu platformda symlink yetkisi yok: {error}")

    for target in ("dir-link/new.txt", "file-link", "real/nested-link"):
        with pytest.raises(ValueError, match="sembolik bağlantı"):
            safe_join(root, target, allow_symlinks=False)
    assert real_file.read_text(encoding="utf-8") == "koru"
