"""Pin _bootstrap/locking to specs/03-bootstrap-runtime.md §5 and D13.

D13 mandates OS-managed advisory locks: fcntl.flock on POSIX,
msvcrt.locking on Windows. The kernel releases the lock on process death,
so the lock file at <cache_root>/<cache_key>.lock persists across releases.

NB on test mode: same caveat as test_environment.py — these unit tests
exercise the locking primitives via direct import as a development-time TDD
harness; the e2e suite is the contract.
"""

import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from moonlit._bootstrap import locking
from moonlit._bootstrap.errors import LockTimeoutError

# ---------- acquire / release happy path ----------


def test_acquire_returns_int_fd_and_creates_file(tmp_path: Path) -> None:
    lock_path = tmp_path / "x.lock"
    fd = locking.acquire(lock_path)
    try:
        assert isinstance(fd, int)
        assert fd >= 0
        assert lock_path.exists()
        assert os.fstat(fd).st_size == 0
    finally:
        locking.release(fd, lock_path)


def test_release_closes_fd_but_keeps_lock_file(tmp_path: Path) -> None:
    # D13 (post-v0.2): the lock file persists across releases. Unlinking it
    # would race against a concurrent opener since flock is per open file
    # description.
    lock_path = tmp_path / "x.lock"
    fd = locking.acquire(lock_path)
    locking.release(fd, lock_path)
    assert lock_path.exists()
    assert lock_path.stat().st_size == 0
    with pytest.raises(OSError):
        os.fstat(fd)


def test_acquire_after_release_succeeds_with_persistent_lockfile(tmp_path: Path) -> None:
    # The lock file is reused across acquire/release cycles.
    lock_path = tmp_path / "x.lock"
    fd1 = locking.acquire(lock_path)
    locking.release(fd1, lock_path)
    assert lock_path.exists()
    fd2 = locking.acquire(lock_path)
    locking.release(fd2, lock_path)
    assert lock_path.exists()


# ---------- contention / timeout ----------


def test_acquire_contended_times_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(locking, "_TIMEOUT_S", 0.1)
    lock_path = tmp_path / "x.lock"
    held_fd = locking.acquire(lock_path)
    try:
        with pytest.raises(LockTimeoutError) as excinfo:
            locking.acquire(lock_path)
        msg = str(excinfo.value)
        assert "lock acquisition timed out" in msg
        assert "0.1s" in msg
        assert str(lock_path) in msg
        assert "MOONLIT_FORCE_EXTRACT" in msg
    finally:
        locking.release(held_fd, lock_path)


def test_acquire_failure_does_not_leak_fd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # If acquisition times out, the fd opened on lock_path is closed before
    # raising. Otherwise we'd leak fds across repeated misses.
    monkeypatch.setattr(locking, "_TIMEOUT_S", 0.05)
    lock_path = tmp_path / "x.lock"
    held_fd = locking.acquire(lock_path)
    try:
        opened_for_lock: list[int] = []
        real_open = os.open
        target = str(lock_path)

        def tracking_open(path: Any, *args: Any, **kwargs: Any) -> int:
            fd = real_open(path, *args, **kwargs)
            if str(path) == target:
                opened_for_lock.append(fd)
            return fd

        monkeypatch.setattr(locking.os, "open", tracking_open)
        with pytest.raises(LockTimeoutError):
            locking.acquire(lock_path)
        # Exactly one fd was opened on lock_path during the failed acquisition,
        # and the failure path must have closed it.
        assert len(opened_for_lock) == 1
        with pytest.raises(OSError):
            os.fstat(opened_for_lock[0])
    finally:
        locking.release(held_fd, lock_path)


def test_lock_timeout_error_exit_code_is_3() -> None:
    err = LockTimeoutError("anything")
    assert err.exit_code == 3


def test_default_timeout_is_60s_per_spec() -> None:
    # Spec 03 §5 / D13 pin the timeout at 60s wall clock.
    assert locking._TIMEOUT_S == 60.0


def test_default_poll_interval_is_50ms_per_spec() -> None:
    # Spec 03 §5 / D13 pin the poll interval at 50ms.
    assert locking._POLL_INTERVAL_S == 0.050


# ---------- polling behavior ----------


def test_first_acquire_attempt_has_no_preceding_sleep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Spec 03 §5: 'First attempt has no preceding sleep; the 50ms sleep is
    # between retries only.' Uncontended acquire must call sleep zero times.
    sleep_calls: list[float] = []

    def fake_sleep(s: float) -> None:
        sleep_calls.append(s)

    monkeypatch.setattr(locking, "sleep", fake_sleep)
    lock_path = tmp_path / "x.lock"
    fd = locking.acquire(lock_path)
    try:
        assert sleep_calls == []
    finally:
        locking.release(fd, lock_path)


def test_contended_acquire_sleeps_at_poll_interval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sleep_calls: list[float] = []

    def fake_sleep(s: float) -> None:
        sleep_calls.append(s)

    monkeypatch.setattr(locking, "sleep", fake_sleep)
    monkeypatch.setattr(locking, "_TIMEOUT_S", 0.05)
    lock_path = tmp_path / "x.lock"
    held_fd = locking.acquire(lock_path)
    try:
        with pytest.raises(LockTimeoutError):
            locking.acquire(lock_path)
        assert sleep_calls, "expected at least one poll-interval sleep"
        assert all(s == 0.050 for s in sleep_calls)
    finally:
        locking.release(held_fd, lock_path)


def test_acquire_retries_until_holder_releases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # If a holder releases mid-poll, the waiter wins on the next attempt.
    monkeypatch.setattr(locking, "_TIMEOUT_S", 5.0)
    monkeypatch.setattr(locking, "_POLL_INTERVAL_S", 0.005)
    lock_path = tmp_path / "x.lock"
    held_fd = locking.acquire(lock_path)

    def release_after_delay() -> None:
        time.sleep(0.05)
        locking.release(held_fd, lock_path)

    t = threading.Thread(target=release_after_delay)
    t.start()
    fd = locking.acquire(lock_path)
    t.join()
    try:
        assert lock_path.exists()  # file persists
    finally:
        locking.release(fd, lock_path)


# ---------- context manager ----------


def test_lock_context_manager_yields_fd(tmp_path: Path) -> None:
    lock_path = tmp_path / "x.lock"
    with locking.lock(lock_path) as fd:
        assert isinstance(fd, int)
        assert lock_path.exists()
    assert lock_path.exists()  # file persists after release


def test_lock_releases_on_exception(tmp_path: Path) -> None:
    lock_path = tmp_path / "x.lock"
    with pytest.raises(RuntimeError, match="boom"):
        with locking.lock(lock_path):
            raise RuntimeError("boom")
    # After the body raised, the lock must have been released — re-acquire to confirm.
    fd = locking.acquire(lock_path)
    locking.release(fd, lock_path)


def test_lock_propagates_timeout_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(locking, "_TIMEOUT_S", 0.05)
    lock_path = tmp_path / "x.lock"
    held_fd = locking.acquire(lock_path)
    try:
        with pytest.raises(LockTimeoutError):
            with locking.lock(lock_path):
                pytest.fail("body must not run when acquire times out")  # pragma: no cover
    finally:
        locking.release(held_fd, lock_path)


# ---------- cross-process: kernel releases on crash ----------


def test_lock_released_when_holder_process_is_killed(tmp_path: Path) -> None:
    # The headline win of OS-managed locking over the old O_CREAT|O_EXCL
    # sentinel: a holder that gets SIGKILLed (or TerminateProcessed) does NOT
    # leave the cache permanently jammed. The kernel releases the lock at
    # process exit; the next acquirer succeeds promptly.
    lock_path = tmp_path / "x.lock"
    src_root = Path(__file__).resolve().parents[2] / "src"
    holder_script = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {str(src_root)!r})
        from moonlit._bootstrap import locking
        fd = locking.acquire({str(lock_path)!r})
        print("LOCKED", flush=True)
        import time
        time.sleep(60)
        """
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", holder_script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        # Wait for the child to confirm it holds the lock.
        assert proc.stdout is not None
        line = proc.stdout.readline()
        assert line.strip() == "LOCKED", f"holder did not lock: {line!r}"
        proc.kill()
        proc.wait(timeout=5)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
    # After the holder is dead, we must be able to acquire promptly.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            fd = locking.acquire(lock_path)
            locking.release(fd, lock_path)
            break
        except LockTimeoutError:  # pragma: no cover - retry until the OS reaps
            time.sleep(0.05)
    else:  # pragma: no cover
        pytest.fail("kernel did not release the lock after holder was killed")
