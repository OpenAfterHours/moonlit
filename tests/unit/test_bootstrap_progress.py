"""Pin _bootstrap/progress.ExtractProgress to spec 03 §14.

Development-time TDD harness for the transient unpacking indicator; the
e2e suite asserts the silent-under-pipe contract end to end.
"""

import io

import pytest

from moonlit._bootstrap import progress
from moonlit._bootstrap.progress import _FRAMES, ExtractProgress

# ---------- helpers / fixtures ----------


class _FakeTTY(io.StringIO):
    """A StringIO that claims to be a terminal."""

    def isatty(self) -> bool:
        return True


class _Clock:
    """Monkeypatchable monotonic clock; advance() moves it forward."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    c = _Clock()
    monkeypatch.setattr(progress.time, "monotonic", c)
    return c


_PAST_DELAY = progress._DELAY_S + 0.01


# ---------- TTY gating ----------


def test_non_tty_stream_writes_nothing(clock: _Clock) -> None:
    stream = io.StringIO()
    with ExtractProgress("unpacking app", 100, stream=stream) as p:
        clock.advance(_PAST_DELAY)
        p.update(50)
        clock.advance(_PAST_DELAY)
        p.update(100)
    assert stream.getvalue() == ""


# ---------- delayed start ----------


def test_delayed_start_suppresses_fast_extraction(clock: _Clock) -> None:
    stream = _FakeTTY()
    with ExtractProgress("unpacking app", 100, stream=stream) as p:
        clock.advance(progress._DELAY_S - 0.05)  # still inside the delay window
        p.update(50)
        p.update(100)
    assert stream.getvalue() == ""


def test_exit_writes_nothing_when_never_painted(clock: _Clock) -> None:
    stream = _FakeTTY()
    with ExtractProgress("unpacking app", 100, stream=stream):
        pass  # never updated past the delay
    assert stream.getvalue() == ""


# ---------- painting ----------


def test_paints_percentage_on_tty_after_delay(clock: _Clock) -> None:
    stream = _FakeTTY()
    with ExtractProgress("unpacking app", 100, stream=stream) as p:
        clock.advance(_PAST_DELAY)
        p.update(50)
    out = stream.getvalue()
    assert "\r" in out
    assert any(frame in out for frame in _FRAMES)
    assert "unpacking app" in out
    assert "50%" in out


def test_repaint_is_throttled(clock: _Clock) -> None:
    stream = _FakeTTY()
    with ExtractProgress("unpacking app", 100, stream=stream) as p:
        clock.advance(_PAST_DELAY)
        p.update(10)  # first paint
        clock.advance(progress._FRAME_INTERVAL / 2)  # too soon
        p.update(20)
    # Only the first update painted; one frame char emitted.
    painted = sum(out_char in _FRAMES for out_char in stream.getvalue())
    assert painted == 1


def test_frame_advances_across_paints(clock: _Clock) -> None:
    stream = _FakeTTY()
    with ExtractProgress("unpacking app", 100, stream=stream) as p:
        clock.advance(_PAST_DELAY)
        p.update(10)
        clock.advance(progress._FRAME_INTERVAL + 0.01)
        p.update(20)
    frames_seen = [ch for ch in stream.getvalue() if ch in _FRAMES]
    assert len(frames_seen) == 2
    assert frames_seen[0] != frames_seen[1]


# ---------- line clearing ----------


def test_exit_clears_line_when_painted(clock: _Clock) -> None:
    stream = _FakeTTY()
    with ExtractProgress("unpacking app", 100, stream=stream) as p:
        clock.advance(_PAST_DELAY)
        p.update(50)
    out = stream.getvalue()
    assert out.endswith("\r\x1b[2K")
    assert "✓" not in out
    assert not out.endswith("\n")


# ---------- edge cases ----------


def test_total_bytes_zero_does_not_divide_by_zero(clock: _Clock) -> None:
    stream = _FakeTTY()
    with ExtractProgress("unpacking app", 0, stream=stream) as p:
        clock.advance(_PAST_DELAY)
        p.update(0)
    out = stream.getvalue()
    assert "0%" not in out
    assert any(frame in out for frame in _FRAMES)
