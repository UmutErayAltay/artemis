"""JSONL kayıtları, sırları diske ulaşmadan önce temizler."""

from __future__ import annotations

import json
import os
import re
import threading
from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

_SECRET_NAME = re.compile(
    r"password|passwd|secret|token|apikey|authorization|credential|cookie|accesskey|privatekey",
    re.IGNORECASE,
)
_BEARER = re.compile(r"(?i)\b(Bearer\s+)[^\s\"',;]+")
_REDACTED = "[GİZLENDİ]"


def _is_secret_key(key: str) -> bool:
    normalized = re.sub(r"[^a-zA-Z0-9]", "", key).lower()
    if normalized in {
        "inputtokens",
        "outputtokens",
        "totaltokens",
        "prompttokens",
        "completiontokens",
        "maxtokens",
        "maxoutputtokens",
        "tokencount",
        "cachedinputtokens",
        # Bağlam ölçüsü (ADR 0012) bir sayıdır, sır değildir.
        "contextestimatedtokens",
    }:
        return False
    return bool(_SECRET_NAME.search(normalized))


def redact(value: Any) -> Any:
    """Ortam her kayıtta okunur; sonradan eklenen sırlar da korunur."""
    secrets = sorted(
        {item for key, item in os.environ.items() if item and _is_secret_key(key)},
        key=len,
        reverse=True,
    )

    def clean_string(text: str) -> str:
        for secret in secrets:
            text = text.replace(secret, _REDACTED)
        return _BEARER.sub(r"\1" + _REDACTED, text)

    def visit(item: Any) -> Any:
        if isinstance(item, str):
            return clean_string(item)
        if isinstance(item, dict):
            return {
                clean_string(str(key)): _REDACTED if _is_secret_key(str(key)) else visit(child)
                for key, child in item.items()
            }
        if isinstance(item, (list, tuple)):
            return [visit(child) for child in item]
        if item is None or isinstance(item, (int, float, bool)):
            return item
        if isinstance(item, Enum):
            return visit(item.value)
        if isinstance(item, (Decimal, Path, datetime)):
            return clean_string(str(item))
        # Bilinmeyen nesnenin repr'i sır taşıyabileceğinden hiç çağrılmaz.
        return f"<{type(item).__name__}>"

    return visit(value)


# Tek dosya sınırsız büyümesin; döndürülen dosyalar SİLİNMEZ (saklama kararı kullanıcınındır).
DEFAULT_MAX_BYTES = 20 * 1024 * 1024


class AuditLog:
    def __init__(
        self,
        path: Path,
        on_record: Callable[[dict[str, Any]], None] | None = None,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        self.path = path
        self.on_record = on_record
        self.max_bytes = max_bytes
        self._lock = threading.Lock()

    def _rotate_if_full(self) -> None:
        """Dolu dosyayı `audit-<zaman>.jsonl` adına taşır; başarısızsa eski dosyaya devam eder.

        Aynı yola yazan başka AuditLog örnekleri (Telegram, otomatik başlatma) ortak kilit
        paylaşmaz: yarışta en kötü sonuç küçük bir ek döndürülmüş dosyadır. O_APPEND ile açık
        tanıtıcı taşınan dosyaya yazar; kayıt kaybolmaz. Windows'ta dosya başka süreçte
        açıksa yeniden adlandırma reddedilir; denetim yazımı bu yüzden durmaz.
        """
        try:
            if self.path.stat().st_size < self.max_bytes:
                return
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
            target = self.path.with_name(f"{self.path.stem}-{stamp}-{os.getpid()}.jsonl")
            os.rename(self.path, target)
        except OSError:
            return

    def emit(self, event: str, **fields: Any) -> None:
        record = redact({**fields, "timestamp": datetime.now(UTC).isoformat(), "event": event})
        line = json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n"
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._rotate_if_full()
            # İzinler ilk oluşturmada dar tutulur; Windows kendi ACL'ini kullanır.
            descriptor = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
                stream.write(line)
        if self.on_record is not None:
            self.on_record(deepcopy(record))
