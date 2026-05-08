"""Sentinel-file locking for the bootstrap (D13).

A lock acquired via O_CREAT|O_EXCL|O_RDWR with polling. Timeout is 60 s
wall clock; poll interval is 50 ms; the first attempt has no preceding
sleep (specs/03-bootstrap-runtime.md §5).

Stale-lock recovery is manual: ``rm <lock_path>``. A real flock /
msvcrt.LK_NBLCK implementation is deferred to v0.2.

``sleep`` and ``monotonic`` are imported as bare names so tests can
monkeypatch them on this module without affecting the rest of the process.
"""

import contextlib
import os
from collections.abc import Iterator
from pathlib import Path
from time import monotonic, sleep

from .errors import LockTimeoutError


_TIMEOUT_S: float = 60.0
_POLL_INTERVAL_S: float = 0.050


def acquire(lock_path: str | Path) -> int:
    """Acquire the sentinel lock at ``lock_path``; return the open fd.

    Polls with O_CREAT|O_EXCL until the file can be created or the timeout
    elapses. Raises :class:`LockTimeoutError` after the wall-clock timeout.
    """
    flags = os.O_CREAT | os.O_EXCL | os.O_RDWR
    deadline = monotonic() + _TIMEOUT_S
    while True:
        try:
            return os.open(lock_path, flags, 0o600)
        except FileExistsError:
            if monotonic() >= deadline:
                raise LockTimeoutError(
                    f"lock acquisition timed out ({_TIMEOUT_S:g}s) at {lock_path}; "
                    f"remove this file or set MOONLIT_FORCE_EXTRACT=1"
                )
            sleep(_POLL_INTERVAL_S)


def release(fd: int, lock_path: str | Path) -> None:
    """Close ``fd`` and unlink ``lock_path``; tolerate an already-removed file."""
    os.close(fd)
    try:
        os.unlink(lock_path)
    except FileNotFoundError:
        pass


@contextlib.contextmanager
def lock(lock_path: str | Path) -> Iterator[int]:
    """Context-manager wrapping :func:`acquire` / :func:`release`."""
    fd = acquire(lock_path)
    try:
        yield fd
    finally:
        release(fd, lock_path)
