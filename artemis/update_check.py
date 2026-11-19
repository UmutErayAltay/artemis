"""Sürüm sorgusu, güncelleme denetimi ve kurucu indirme (ADR 0075, ADR 0081).

İndirme yalnızca kullanıcının açık tıklamasıyla başlar ve GitHub API'sinden gelen
SHA-256 özetiyle doğrulanır; henüz Authenticode imzası yoktur (SignPath bekleniyor).
Telemetri içermez; GitHub API'sine salt GET isteği atılır.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.metadata
import os
import re
import tempfile
import tomllib
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import httpx

_DIGITS_RE = re.compile(r"^(\d+)")
_INSTALLER_NAME_RE = re.compile(r"^Artemis-Setup-\d+(\.\d+)*\.exe$")
_INSTALLER_URL_PREFIX = "https://github.com/UmutErayAltay/artemis/releases/download/"
_MAX_INSTALLER_SIZE = 1_500_000_000
_DIGEST_RE = re.compile(r"^sha256:([0-9a-fA-F]{64})$")
_SHA256_HEX_RE = re.compile(r"^[0-9a-fA-F]{64}$")

FetchCallable = Callable[[str, dict[str, str], float], tuple[int, object]]
StreamCallable = Callable[[str, float], Iterable[bytes]]


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
    installer_url: str = ""
    installer_name: str = ""
    installer_size: int = 0
    installer_sha256: str = ""


class UpdateDownloadError(Exception):
    """Kurulum dosyası indirme veya SHA-256 doğrulama hatası."""


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
        installer_url, installer_name, installer_size, installer_sha256 = _pick_installer_asset(
            body.get("assets")
        )
        return UpdateResult(
            status="newer",
            latest=tag_name,
            url=html_url,
            message=f"Yeni sürüm var: {tag_name}. Sürüm sayfasını aç ile indirebilirsin.",
            installer_url=installer_url,
            installer_name=installer_name,
            installer_size=installer_size,
            installer_sha256=installer_sha256,
        )

    return UpdateResult(
        status="current",
        latest=tag_name,
        url=html_url,
        message=f"Güncelsiniz (sürüm {current}).",
    )


def _pick_installer_asset(assets: object) -> tuple[str, str, int, str]:
    """Sürüm varlıkları içinden doğrulanmış Windows kurucusunu seçer."""
    if not isinstance(assets, list):
        return "", "", 0, ""
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = asset.get("name")
        url = asset.get("browser_download_url")
        size = asset.get("size")
        digest = asset.get("digest")
        if not isinstance(name, str) or not _INSTALLER_NAME_RE.match(name):
            continue
        if not isinstance(url, str) or not url.startswith(_INSTALLER_URL_PREFIX):
            continue
        if isinstance(size, bool) or not isinstance(size, int):
            continue
        if not (1 <= size <= _MAX_INSTALLER_SIZE):
            continue
        if not isinstance(digest, str):
            continue
        digest_match = _DIGEST_RE.match(digest)
        if not digest_match:
            continue
        return url, name, size, digest_match.group(1).lower()
    return "", "", 0, ""


def _default_stream(url: str, timeout: float = 30.0) -> Iterable[bytes]:
    """Kurulum dosyasını HTTPS üzerinden parça parça indirir."""
    # Yönlendirmeler yalnızca HTTPS bağlantısında izlenir.
    follow = url.startswith("https://")
    with httpx.stream("GET", url, follow_redirects=follow, timeout=timeout) as response:
        response.raise_for_status()
        yield from response.iter_bytes()


def download_installer(
    result: UpdateResult,
    dest_dir: Path | None = None,
    *,
    stream: StreamCallable | None = None,
    progress: Callable[[int, int], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> Path:
    """Kurulum dosyasını geçici parçaya indirir, boyut ve SHA-256 doğrulayıp taşır."""
    if (
        not result.installer_name
        or not _INSTALLER_NAME_RE.match(result.installer_name)
        or not result.installer_url.startswith(_INSTALLER_URL_PREFIX)
        or isinstance(result.installer_size, bool)
        or not isinstance(result.installer_size, int)
        or not (1 <= result.installer_size <= _MAX_INSTALLER_SIZE)
        or not _SHA256_HEX_RE.match(result.installer_sha256)
    ):
        raise UpdateDownloadError("Kurulum dosyası bilgisi eksik.")

    target_dir = (
        dest_dir if dest_dir is not None else Path(tempfile.gettempdir()) / "artemis-update"
    )
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise UpdateDownloadError("İndirme başarısız: dosya yazılamadı.") from exc

    part_path = target_dir / f"{result.installer_name}.part"
    final_path = target_dir / result.installer_name
    actual_stream = stream if stream is not None else _default_stream

    hasher = hashlib.sha256()
    downloaded = 0
    completed = False

    try:
        try:
            with open(part_path, "wb") as handle:
                for chunk in actual_stream(result.installer_url, 30.0):
                    if cancelled is not None and cancelled():
                        raise UpdateDownloadError("İndirme iptal edildi.")
                    if not chunk:
                        continue
                    if downloaded + len(chunk) > result.installer_size:
                        raise UpdateDownloadError(
                            "İndirme başarısız: dosya boyutu beklenenden farklı."
                        )
                    handle.write(chunk)
                    hasher.update(chunk)
                    downloaded += len(chunk)
                    if progress is not None:
                        progress(downloaded, result.installer_size)
        except httpx.HTTPError as exc:
            raise UpdateDownloadError("İndirme başarısız: bağlantı hatası.") from exc
        except OSError as exc:
            raise UpdateDownloadError("İndirme başarısız: dosya yazılamadı.") from exc

        if downloaded != result.installer_size:
            raise UpdateDownloadError(
                "İndirme başarısız: dosya boyutu beklenenden farklı."
            )
        if hasher.hexdigest().lower() != result.installer_sha256.lower():
            raise UpdateDownloadError(
                "Kurulum dosyası doğrulanamadı (SHA-256 uyuşmuyor)."
            )

        try:
            os.replace(part_path, final_path)
        except OSError as exc:
            raise UpdateDownloadError("İndirme başarısız: dosya yazılamadı.") from exc

        completed = True
        return final_path
    finally:
        # Yarım veya doğrulanmamış dosya asla diskte bırakılmaz.
        if not completed:
            with contextlib.suppress(OSError):
                part_path.unlink(missing_ok=True)
