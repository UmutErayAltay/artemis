"""Kullanıcı başına config ve paketlenmiş örnek dosya yolları (ADR 0075)."""

import os
import shutil
import sys
from collections.abc import Mapping
from pathlib import Path


def user_config_dir(
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
    windows: bool | None = None,
) -> Path:
    """Paketlenmiş kurulumda config ve göreli durum dosyalarının tutulduğu kullanıcı klasörü."""
    actual_env = os.environ if env is None else env
    actual_home = Path.home() if home is None else Path(home)
    is_windows = (sys.platform == "win32") if windows is None else windows
    if is_windows:
        appdata = actual_env.get("APPDATA")
        base = Path(appdata) if appdata else actual_home / "AppData" / "Roaming"
        return base / "Artemis"
    xdg = actual_env.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else actual_home / ".config"
    return base / "artemis"


def bundled_example_config() -> Path:
    """Depo kökündeki veya dondurulmuş paketin içindeki `config.example.yaml` yolu."""
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass is not None:
            return Path(meipass) / "config" / "config.example.yaml"
    return Path(__file__).resolve().parents[2] / "config" / "config.example.yaml"


def default_config_path(
    cwd: Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
    windows: bool | None = None,
) -> Path:
    """Depo içinden çalışırken `./config/config.yaml`, değilse kullanıcı config yolu."""
    local = (
        Path("config") / "config.yaml"
        if cwd is None
        else Path(cwd) / "config" / "config.yaml"
    )
    if local.exists():
        return local
    return user_config_dir(env=env, home=home, windows=windows) / "config.yaml"


def ensure_config(path: Path) -> bool:
    """Hedef config yoksa örnek dosyayı kopyalar; var olan dosyanın üzerine asla yazmaz."""
    if path.exists():
        return False
    example = bundled_example_config()
    if not example.is_file():
        raise FileNotFoundError(f"Örnek ayar dosyası bulunamadı: {example}")
    path.parent.mkdir(parents=True, exist_ok=True)
    # Önce geçici dosyaya tam kopya, sonra üzerine yazmayan bağla yerine koyma: kopya yarıda
    # kesilirse 0 baytlık config kalıp sonraki açılışta "var" sayılmaz.
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        shutil.copyfile(example, temp)
        try:
            os.link(temp, path)
        except FileExistsError:
            return False
    finally:
        temp.unlink(missing_ok=True)
    return True
