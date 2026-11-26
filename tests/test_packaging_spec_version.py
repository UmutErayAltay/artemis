"""PyInstaller reçetesi exe sürüm kaynağını pyproject sürümünden üretir (Karne 6 P6)."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_spec_derives_version_from_pyproject():
    spec = (ROOT / "packaging" / "artemis.spec").read_text(encoding="utf-8")
    assert 'ROOT / "pyproject.toml"' in spec and "tomllib" in spec
    assert "StringStruct(\"ProductVersion\", text)" in spec
    # Hem pencereli exe hem konsol kardeşi sürüm kaynağı taşır.
    assert 'version=_version_info("Artemis.exe")' in spec
    assert 'version=_version_info("artemis-cli.exe")' in spec


def test_spec_helper_builds_with_pyproject_version():
    spec = (ROOT / "packaging" / "artemis.spec").read_text(encoding="utf-8")
    start = spec.index("APP_VERSION =")
    end = spec.index("datas = ")
    ns = {"tomllib": __import__("tomllib"), "Path": Path, "ROOT": ROOT}
    exec(spec[start:end], ns)  # noqa: S102 - yalnız depodaki sabit reçete parçası
    import tomllib

    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    expected = data["project"]["version"]
    assert ns["APP_VERSION"] == expected
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        return
    assert str(ns["_version_info"]("Artemis.exe")).count(expected) >= 2
