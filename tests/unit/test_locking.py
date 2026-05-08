"""Pin _bootstrap/locking to specs/03-bootstrap-runtime.md §5 and D13.

NB on test mode: same caveat as test_environment.py — these unit tests
exercise the locking primitives via direct import as a development-time TDD
harness; the e2e suite (built once the full bootstrap exists) is the contract.
"""

import os
import threading
import time
from pathlib import Path

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
        # FD is real and refers to the lock file (zero size).
        assert os.fstat(fd).st_size == 0
    finally:
        locking.release(fd, lock_path)


def test_release_closes_fd_and_unlinks_file(tmp_path: Path) -> None:
    lock_path = tmp_path / "x.lock"
    fd = locking.acquire(lock_path)
    locking.release(fd, lock_path)
    assert not lock_path.exists()
    with pytest.raises(OSError):
        os.fstat(fd)


def test_release_tolerates_missing_lock_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Spec §5: release catches FileNotFoundError on unlink (stale-lock recovery).
    # Simulated via monkeypatch because the real-fs scenario (rm a file held
    # open by an fd) is POSIX-only — Windows refuses with PermissionError.
    lock_path = tmp_path / "x.lock"
    fd = locking.acquire(lock_path)

    def fake_unlink(path: object) -> None:
        raise FileNotFoundError(2, "No such file", str(path))

    monkeypatch.setattr(os, "unlink", fake_unlink)
    locking.release(fd, lock_path)  # must not raise


def test_acquire_after_release_succeeds(tmp_path: Path) -> None:
    lock_path = tmp_path / "x.lock"
    fd1 = locking.acquire(lock_path)
    locking.release(fd1, lock_path)
    fd2 = locking.acquire(lock_path)
    locking.release(fd2, lock_path)


# ---------- contention / timeout ----------


def test_acquire_contended_times_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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


def test_acquire_retries_until_lock_disappears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # If a holder releases mid-poll, the waiter wins on the next attempt.
    monkeypatch.setattr(locking, "_TIMEOUT_S", 5.0)
    monkeypatch.setattr(locking, "_POLL_INTERVAL_S", 0.005)
    lock_path = tmp_path / "x.lock"
    lock_path.touch()  # pre-existing, simulating an external holder

    def remove_after_delay() -> None:
        time.sleep(0.05)
        lock_path.unlink()

    t = threading.Thread(target=remove_after_delay)
    t.start()
    fd = locking.acquire(lock_path)
    t.join()
    try:
        assert lock_path.exists()
    finally:
        locking.release(fd, lock_path)


# ---------- context manager ----------


def test_lock_context_manager_yields_fd(tmp_path: Path) -> None:
    lock_path = tmp_path / "x.lock"
    with locking.lock(lock_path) as fd:
        assert isinstance(fd, int)
        assert lock_path.exists()
    assert not lock_path.exists()


def test_lock_releases_on_exception(tmp_path: Path) -> None:
    lock_path = tmp_path / "x.lock"
    with pytest.raises(RuntimeError, match="boom"):
        with locking.lock(lock_path):
            raise RuntimeError("boom")
    assert not lock_path.exists()


def test_lock_propagates_timeout_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(locking, "_TIMEOUT_S", 0.05)
    lock_path = tmp_path / "x.lock"
    held_fd = locking.acquire(lock_path)
    try:
        with pytest.raises(LockTimeoutError):
            with locking.lock(lock_path):
                pytest.fail("body must not run when acquire times out")  # pragma: no cover
    finally:
        locking.release(held_fd, lock_path)
