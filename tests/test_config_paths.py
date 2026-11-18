import sys
from pathlib import Path

import pytest

from artemis.channels import cli
from artemis.config import paths


def test_user_config_dir_windows_and_posix(tmp_path: Path) -> None:
    appdata = tmp_path / "RoamingCustom"
    win_dir = paths.user_config_dir(
        env={"APPDATA": str(appdata)}, home=tmp_path, windows=True
    )
    assert win_dir == appdata / "Artemis"

    win_fallback = paths.user_config_dir(env={}, home=tmp_path, windows=True)
    assert win_fallback == tmp_path / "AppData" / "Roaming" / "Artemis"

    xdg = tmp_path / "xdg"
    posix_dir = paths.user_config_dir(
        env={"XDG_CONFIG_HOME": str(xdg)}, home=tmp_path, windows=False
    )
    assert posix_dir == xdg / "artemis"

    posix_fallback = paths.user_config_dir(env={}, home=tmp_path, windows=False)
    assert posix_fallback == tmp_path / ".config" / "artemis"


def test_default_config_path_prefers_dev_checkout(tmp_path: Path) -> None:
    dev_cfg = tmp_path / "config" / "config.yaml"
    dev_cfg.parent.mkdir(parents=True)
    dev_cfg.write_text("primary_model: ''\n", encoding="utf-8")

    chosen = paths.default_config_path(
        cwd=tmp_path, env={"APPDATA": str(tmp_path / "appdata")}, home=tmp_path, windows=True
    )
    assert chosen == dev_cfg

    empty_cwd = tmp_path / "empty"
    empty_cwd.mkdir()
    user_chosen = paths.default_config_path(
        cwd=empty_cwd,
        env={"APPDATA": str(tmp_path / "appdata")},
        home=tmp_path,
        windows=True,
    )
    assert user_chosen == tmp_path / "appdata" / "Artemis" / "config.yaml"


def test_ensure_config_copies_once_and_never_overwrites(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    example = tmp_path / "bundled" / "config.example.yaml"
    example.parent.mkdir(parents=True)
    example.write_text("# ornek\nprimary_model: ''\n", encoding="utf-8")
    monkeypatch.setattr(paths, "bundled_example_config", lambda: example)

    target = tmp_path / "user" / "Artemis" / "config.yaml"
    assert paths.ensure_config(target) is True
    assert target.read_text(encoding="utf-8") == "# ornek\nprimary_model: ''\n"

    target.write_text("# kullanici degistirdi\n", encoding="utf-8")
    assert paths.ensure_config(target) is False
    assert target.read_text(encoding="utf-8") == "# kullanici degistirdi\n"


def test_ensure_config_missing_example_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "yok" / "config.example.yaml"
    monkeypatch.setattr(paths, "bundled_example_config", lambda: missing)

    with pytest.raises(FileNotFoundError, match="Örnek ayar dosyası bulunamadı"):
        paths.ensure_config(tmp_path / "out" / "config.yaml")


def test_bundled_example_config_frozen_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert paths.bundled_example_config() == tmp_path / "config" / "config.example.yaml"


def test_cli_default_config_flows_through_and_prints_first_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    default_cfg = tmp_path / "Artemis" / "config.yaml"
    ensured: list[Path] = []
    seen: list[ tuple[Path, str] ] = []

    monkeypatch.setattr(cli, "default_config_path", lambda: default_cfg)
    monkeypatch.setattr(
        cli,
        "ensure_config",
        lambda p: ensured.append(p) is None and True,
    )
    monkeypatch.setattr("artemis.tools.credentials.load_into_environment", lambda: None)
    monkeypatch.setattr(
        cli,
        "autostart",
        lambda cfg, action: seen.append((cfg, action)) is None and 0,
    )
    monkeypatch.setattr(sys, "argv", ["artemis", "autostart", "durum"])

    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert ensured == [default_cfg]
    assert seen == [(default_cfg, "durum")]
    err = capsys.readouterr().err
    assert f"İlk açılış: ayarlar {default_cfg} konumunda oluşturuldu;" in err


def test_cli_explicit_missing_config_does_not_call_ensure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing_cfg = tmp_path / "olmayan.yaml"
    ensured: list[Path] = []

    monkeypatch.setattr(cli, "ensure_config", lambda p: ensured.append(p) is None and True)
    monkeypatch.setattr("artemis.tools.credentials.load_into_environment", lambda: None)
    monkeypatch.setattr(
        sys, "argv", ["artemis", "autostart", "durum", "--config", str(missing_cfg)]
    )

    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    assert ensured == []


def test_frozen_app_never_loads_config_from_working_directory(tmp_path: Path) -> None:
    # Paketli uygulama bulunduğu klasördeki config/config.yaml'ı yüklememeli (MCP komutu vb.).
    planted = tmp_path / "config" / "config.yaml"
    planted.parent.mkdir(parents=True)
    planted.write_text("primary_model: ''\n", encoding="utf-8")
    chosen = paths.default_config_path(
        cwd=tmp_path,
        env={"APPDATA": str(tmp_path / "appdata")},
        home=tmp_path,
        windows=True,
        frozen=True,
    )
    assert chosen != planted
    assert chosen.parent.parent == tmp_path / "appdata"
