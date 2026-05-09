"""OS-managed advisory locking for the bootstrap (D13).

POSIX uses ``fcntl.flock(LOCK_EX | LOCK_NB)``; Windows uses
``msvcrt.locking(LK_NBLCK, 1)``. Both forms are non-blocking; the poll loop
drives retries (50 ms interval, 60 s wall clock per specs/03-bootstrap-runtime.md
§5). The kernel releases the lock on process death — crashed holders no
longer wedge the cache.

The lock file at ``<cache_root>/<cache_key>.lock`` is opened with
``O_CREAT | O_RDWR`` (no ``O_EXCL`` — the file is shared; only the OS-managed
lock on the open file description is exclusive). It persists across releases;
unlinking would race a concurrent opener since ``flock`` is per open file
description.

``sleep`` and ``monotonic`` are imported as bare names so tests can
monkeypatch them on this module without affecting the rest of the process.
"""

import contextlib
import errno
import os
from collections.abc import Iterator
from pathlib import Path
from time import monotonic, sleep

from .errors import LockTimeoutError

_TIMEOUT_S: float = 60.0
_POLL_INTERVAL_S: float = 0.050


def acquire(lock_path: str | Path) -> int:
    """Acquire an exclusive OS lock at ``lock_path``; return the open fd.

    Polls ``_try_lock`` until it succeeds or the wall-clock timeout elapses.
    On timeout the fd is closed before raising :class:`LockTimeoutError`, so
    a contended cache does not leak file descriptors across retries.
    """
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        deadline = monotonic() + _TIMEOUT_S
        while True:
            if _try_lock(fd):
                return fd
            if monotonic() >= deadline:
                raise LockTimeoutError(
                    f"lock acquisition timed out ({_TIMEOUT_S:g}s) at {lock_path}; "
                    f"remove this file or set MOONLIT_FORCE_EXTRACT=1"
                )
            sleep(_POLL_INTERVAL_S)
    except BaseException:
        os.close(fd)
        raise


def release(fd: int, lock_path: str | Path) -> None:  # noqa: ARG001
    """Release the OS lock and close ``fd``; the lock file is left in place.

    ``lock_path`` is accepted for symmetry with :func:`acquire`; it is not
    unlinked because doing so races a concurrent opener (D13).
    """
    _unlock(fd)
    os.close(fd)


@contextlib.contextmanager
def lock(lock_path: str | Path) -> Iterator[int]:
    """Context-manager wrapping :func:`acquire` / :func:`release`."""
    fd = acquire(lock_path)
    try:
        yield fd
    finally:
        release(fd, lock_path)


# ---------- platform dispatch (defined below callers per stepdown rule) ----------

if os.name == "nt":
    import msvcrt

    # Windows ``msvcrt.locking`` reports a held byte range as OSError with one
    # of these errnos depending on Python/CRT version. Anything else is fatal.
    _LOCK_HELD_ERRNOS = frozenset({errno.EACCES, errno.EAGAIN, errno.EDEADLK})

    def _try_lock(fd: int) -> bool:
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError as exc:
            if exc.errno in _LOCK_HELD_ERRNOS:
                return False
            raise

    def _unlock(fd: int) -> None:
        # Closing the fd releases the lock unconditionally; the explicit
        # LK_UNLCK is best-effort and its failure is benign.
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass

else:
    import fcntl

    def _try_lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False

    def _unlock(fd: int) -> None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
