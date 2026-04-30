from __future__ import annotations

import contextlib
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

logger = logging.getLogger(__name__)


class RuntimeModeLockError(RuntimeError):
    """Raised when a runtime mode is already active in the same workspace."""


@dataclass(slots=True)
class RuntimeModeLock:
    mode: str
    lock_path: Path
    current_logger: logging.Logger
    _handle: BinaryIO | None = None

    def acquire(self) -> None:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.lock_path.open("a+b")
        try:
            self._prepare_lock_file(handle)
            self._try_lock(handle)
            self._write_lock_metadata(handle)
        except Exception:
            handle.close()
            raise

        self._handle = handle
        self.current_logger.info("Acquired runtime mode lock mode=%s path=%s", self.mode, self.lock_path)

    def release(self) -> None:
        if self._handle is None:
            return

        try:
            self._unlock(self._handle)
        finally:
            self._handle.close()
            self._handle = None
            with contextlib.suppress(OSError):
                self.lock_path.unlink()
            self.current_logger.info("Released runtime mode lock mode=%s path=%s", self.mode, self.lock_path)

    def __enter__(self) -> RuntimeModeLock:
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()

    def _prepare_lock_file(self, handle: BinaryIO) -> None:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)

    def _write_lock_metadata(self, handle: BinaryIO) -> None:
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n".encode("utf-8"))
        handle.flush()
        handle.seek(0)

    def _try_lock(self, handle: BinaryIO) -> None:
        try:
            if sys.platform.startswith("win"):
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                return

            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.current_logger.warning("Runtime mode lock is already held mode=%s path=%s", self.mode, self.lock_path)
            raise RuntimeModeLockError(
                f"Режим '{self.mode}' уже запущен из этой папки. Закройте старое окно и попробуйте снова."
            ) from exc

    def _unlock(self, handle: BinaryIO) -> None:
        handle.seek(0)
        if sys.platform.startswith("win"):
            import msvcrt

            with contextlib.suppress(OSError):
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            return

        import fcntl

        with contextlib.suppress(OSError):
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def acquire_runtime_mode_lock(
    mode: str,
    lock_root: Path,
    current_logger: logging.Logger | None = None,
) -> RuntimeModeLock:
    active_logger = current_logger or logger
    lock_path = lock_root / f".{mode}.lock"
    return RuntimeModeLock(mode=mode, lock_path=lock_path, current_logger=active_logger)
