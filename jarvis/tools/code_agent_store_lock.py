"""Tek yerel iş servisi sahibi: başka runtime açılışta canlı işleri kesinti sayamaz."""

from __future__ import annotations

import importlib
import os
from pathlib import Path
from typing import BinaryIO


class StoreOwnership:
    def __init__(self, path: Path) -> None:
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        self._stream: BinaryIO = os.fdopen(descriptor, "r+b", buffering=0)
        try:
            if os.name == "nt":
                msvcrt = importlib.import_module("msvcrt")

                if self._stream.seek(0, os.SEEK_END) == 0:
                    self._stream.write(b"0")
                self._stream.seek(0)
                msvcrt.locking(self._stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl = importlib.import_module("fcntl")

                fcntl.flock(self._stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._stream.close()
            raise

    def close(self) -> None:
        if self._stream.closed:
            return
        if os.name == "nt":
            msvcrt = importlib.import_module("msvcrt")

            self._stream.seek(0)
            msvcrt.locking(self._stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl = importlib.import_module("fcntl")

            fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
        self._stream.close()
