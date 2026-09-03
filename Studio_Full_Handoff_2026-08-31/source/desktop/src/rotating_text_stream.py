"""Bounded text stream used for desktop stdout/stderr capture."""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Optional


class RotatingTextStream:
    """A small file-like stream with size-based rotation.

    The desktop app assigns one instance to both ``sys.stdout`` and
    ``sys.stderr``.  Rotation is protected by a lock so concurrent worker
    messages cannot race while the file is being replaced.
    """

    def __init__(
        self,
        path: str,
        *,
        max_bytes: int = 50 * 1024 * 1024,
        backup_count: int = 2,
        encoding: str = "utf-8",
    ) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        if backup_count < 1:
            raise ValueError("backup_count must be at least one")
        self.path = Path(path)
        self.max_bytes = int(max_bytes)
        self.backup_count = int(backup_count)
        self._encoding = encoding
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = None
        if self.path.exists() and self.path.stat().st_size >= self.max_bytes:
            self._rotate_closed_file()
        self._open()

    def _open(self) -> None:
        self._file = self.path.open("a", encoding=self._encoding, buffering=1)

    def _rotate_closed_file(self) -> None:
        oldest = Path(f"{self.path}.{self.backup_count}")
        if oldest.exists():
            oldest.unlink()
        for index in range(self.backup_count - 1, 0, -1):
            source = Path(f"{self.path}.{index}")
            if source.exists():
                os.replace(source, Path(f"{self.path}.{index + 1}"))
        if self.path.exists():
            os.replace(self.path, Path(f"{self.path}.1"))

    def _rotate(self) -> None:
        if self._file is not None:
            self._file.flush()
            self._file.close()
        self._rotate_closed_file()
        self._open()

    @property
    def encoding(self) -> str:
        return self._encoding

    @property
    def closed(self) -> bool:
        return self._file is None or self._file.closed

    def writable(self) -> bool:
        return True

    def isatty(self) -> bool:
        return False

    def fileno(self) -> int:
        return self._file.fileno()

    def write(self, value: object) -> int:
        text = value if isinstance(value, str) else str(value)
        with self._lock:
            encoded_size = len(text.encode(self._encoding, errors="replace"))
            current_size = self.path.stat().st_size if self.path.exists() else 0
            if current_size and current_size + encoded_size > self.max_bytes:
                self._rotate()
            return self._file.write(text)

    def flush(self) -> None:
        with self._lock:
            if self._file is not None and not self._file.closed:
                self._file.flush()

    def close(self) -> None:
        with self._lock:
            if self._file is not None and not self._file.closed:
                self._file.close()

    def __getattr__(self, name: str):
        file_obj: Optional[object] = self.__dict__.get("_file")
        if file_obj is None:
            raise AttributeError(name)
        return getattr(file_obj, name)

