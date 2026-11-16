"""Sürüm sorgusu ve güncelleme denetimi (ADR 0075).

Yalnızca en son sürüm bilgisini sorgular; otomatik indirme veya telemetri içermez.
GitHub API'sine salt GET isteği atılır.
"""

from __future__ import annotations

import importlib.metadata
import re
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import httpx

_DIGITS_RE = re.compile(r"^(\d+)")
FetchCallable = Callable[[str, dict[str, str], float], tuple[int, object]]


def current_version() -> str:
    """Kurulu paket ya da pyproject.toml sürümünü döndürür; hata durumunda '0.0.0'."""
    try:
        ver = importlib.metadata.version("artemis-tr")
        if ver and ver.strip():
            return ver.strip()
    except importlib.metadata.PackageNotFoundError:
        pass
    except (OSError, ValueError):
        pass

    try:
        pyproject_path = Path(__file__).resolve().parent.parent / "pyproject.toml"
        if pyproject_path.is_file():
            data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
            version = data.get("project", {}).get("version") or data.get("version")
            if isinstance(version, str) and version.strip():
                return version.strip()
    except (OSError, tomllib.TOMLDecodeError, KeyError, TypeError, ValueError):
        pass

    return "0.0.0"


def parse_version(text: str) -> tuple[int, ...]:
    """Sürüm dizgesini karşılaştırılabilir tamsayı demetine çevirir.

    Örnekler:
    - 'v1.2.3' -> (1, 2, 3)
    - '1.2.3rc1' -> (1, 2, 3)
    - 'v0.1.0' -> (0, 1, 0)
    - '1.2.x' -> (1, 2)
    - '' -> ()
    """
    s = text.strip()
    if s.startswith(("v", "V")):
        s = s[1:].strip()
    if not s:
        return ()
    parts: list[int] = []
    for part in s.split("."):
        match = _DIGITS_RE.match(part)
        if match:
            parts.append(int(match.group(1)))
        else:
            break
    return tuple(parts)


@dataclass(frozen=True)
class UpdateResult:
    status: Literal["newer", "current", "error"]
    latest: str
    url: str
    message: str


def _default_fetch(url: str, headers: dict[str, str], timeout: float) -> tuple[int, object]:
    """GitHub API'sine GET isteği atar; (durum_kodu, json_nesnesi) döner."""
    with httpx.Client(follow_redirects=False, timeout=timeout) as client:
        response = client.get(url, headers=headers)
        try:
            body: object = response.json()
        except (ValueError, TypeError):
            body = None
        return response.status_code, body


def check_latest(
    fetch: FetchCallable | None = None,
    repo: str = "UmutErayAltay/artemis",
    timeout: float = 8.0,
) -> UpdateResult:
    """En son GitHub sürümünü sorgular ve kurulu sürümle karşılaştırır.

    fetch: (url, headers, timeout) -> (status_code, json_body) çağrılabilir nesnesi.
    """
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    headers = {"Accept": "application/vnd.github+json"}
    actual_fetch = fetch if fetch is not None else _default_fetch

    try:
        status_code, body = actual_fetch(url, headers, timeout)
    except httpx.HTTPError:
        return UpdateResult(
            status="error",
            latest="",
            url="",
            message="Güncelleme denetlenemedi: bağlantı yok ya da zaman aşımı.",
        )
    except ValueError:
        return UpdateResult(
            status="error",
            latest="",
            url="",
            message="Güncelleme denetlenemedi: yanıt biçimi geçersiz.",
        )

    if status_code == 404:
        return UpdateResult(
            status="current",
            latest="",
            url="",
            message="Henüz yayımlanmış sürüm yok.",
        )

    if status_code != 200:
        return UpdateResult(
            status="error",
            latest="",
            url="",
            message=f"Güncelleme denetlenemedi (HTTP {status_code}).",
        )

    if not isinstance(body, dict):
        return UpdateResult(
            status="error",
            latest="",
            url="",
            message="Güncelleme denetlenemedi: yanıt biçimi geçersiz.",
        )

    tag_name = body.get("tag_name")
    html_url = body.get("html_url")
    if not isinstance(tag_name, str) or not isinstance(html_url, str):
        return UpdateResult(
            status="error",
            latest="",
            url="",
            message="Güncelleme denetlenemedi: yanıt biçimi geçersiz.",
        )

    current = current_version()
    if parse_version(tag_name) > parse_version(current):
        return UpdateResult(
            status="newer",
            latest=tag_name,
            url=html_url,
            message=f"Yeni sürüm var: {tag_name}. Sürüm sayfasını aç ile indirebilirsin.",
        )

    return UpdateResult(
        status="current",
        latest=tag_name,
        url=html_url,
        message=f"Güncelsiniz (sürüm {current}).",
    )
