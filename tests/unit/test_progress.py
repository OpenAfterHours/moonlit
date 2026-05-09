"""Pin moonlit._progress — the build-pipeline progress reporter.

The Step context manager wraps each pipeline step and emits to stderr per
specs/01-cli.md §8: progress lines in default mode, nothing in --quiet,
and a `+ uv <argv>` echo helper used by the resolver in --verbose. Both
TTY (with Braille spinner) and non-TTY (plain `→`/`✓` lines, safe for
CI logs and file redirects) flavors are exercised here.
"""

import io
import time
from typing import Any

import pytest

from moonlit import _progress
from moonlit._progress import Step, emit_aside

# ---------- helpers ----------


class _FakeStream(io.StringIO):
    """StringIO with a controllable isatty() — lets tests exercise both modes."""

    def __init__(self, *, tty: bool) -> None:
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


@pytest.fixture
def stream_tty() -> _FakeStream:
    return _FakeStream(tty=True)


@pytest.fixture
def stream_plain() -> _FakeStream:
    return _FakeStream(tty=False)


@pytest.fixture(autouse=True)
def _reset_active_step() -> Any:
    """Clear the module-global active-step pointer before every test."""
    _progress._active = None
    yield
    _progress._active = None


# ---------- quiet mode (verbosity == -1) ----------


def test_quiet_step_writes_nothing(stream_tty: _FakeStream) -> None:
    with Step("hashing staged tree", verbosity=-1, stream=stream_tty) as step:
        step.set_result("hashed 1247 files")
    assert stream_tty.getvalue() == ""


def test_quiet_emit_aside_with_no_active_step_still_prints(stream_plain: _FakeStream) -> None:
    # emit_aside is used for the verbose `+ uv ...` echo. It is independent
    # of the Step verbosity check — the resolver only calls it when
    # verbosity >= 1, and quiet mode is verbosity == -1, so they never coexist.
    emit_aside("+ uv export --frozen", file=stream_plain)
    assert stream_plain.getvalue() == "+ uv export --frozen\n"


# ---------- non-TTY (plain) mode ----------


def test_plain_mode_emits_arrow_then_check(stream_plain: _FakeStream) -> None:
    with Step("resolving uv workspace", verbosity=0, stream=stream_plain) as step:
        step.set_result("resolved · 12 members")
    out = stream_plain.getvalue()
    lines = out.splitlines()
    assert lines[0] == "→ resolving uv workspace"
    # Final line: `✓ resolved · 12 members · <duration>`. Duration format
    # depends on perf_counter timing — assert the prefix and suffix shape.
    assert lines[1].startswith("✓ resolved · 12 members · ")
    assert lines[1].endswith("ms") or lines[1].endswith("s")


def test_plain_mode_no_escape_codes(stream_plain: _FakeStream) -> None:
    with Step("writing archive", verbosity=0, stream=stream_plain) as step:
        step.set_result("wrote app.pyz")
    out = stream_plain.getvalue()
    assert "\x1b[" not in out  # no ANSI escapes
    assert "\r" not in out  # no carriage returns


def test_plain_mode_default_label_when_no_result(stream_plain: _FakeStream) -> None:
    with Step("computing hashes", verbosity=0, stream=stream_plain):
        pass
    lines = stream_plain.getvalue().splitlines()
    assert lines[0] == "→ computing hashes"
    assert lines[1].startswith("✓ computing hashes · ")


def test_plain_mode_show_duration_false_omits_duration(stream_plain: _FakeStream) -> None:
    with Step("hashing", verbosity=0, stream=stream_plain) as step:
        step.set_result("build id a4f9…b32c · 1247 files", show_duration=False)
    out = stream_plain.getvalue()
    assert out.splitlines()[1] == "✓ build id a4f9…b32c · 1247 files"


def test_plain_mode_exception_emits_x_marker(stream_plain: _FakeStream) -> None:
    with (
        pytest.raises(RuntimeError),
        Step("freezing dependencies", verbosity=0, stream=stream_plain),
    ):
        raise RuntimeError("uv export failed")
    lines = stream_plain.getvalue().splitlines()
    assert lines[0] == "→ freezing dependencies"
    assert lines[1] == "✗ freezing dependencies"


# ---------- TTY (spinner) mode ----------


def test_tty_mode_spinner_writes_frames(stream_tty: _FakeStream) -> None:
    # Hold the step open long enough for the spinner thread to draw a few
    # frames; then assert the final result line and that we saw the escape.
    with Step("freezing dependencies", verbosity=0, stream=stream_tty) as step:
        # Poll until we see a Braille frame; bound to keep the test fast.
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if any(c in stream_tty.getvalue() for c in _progress._FRAMES):
                break
            time.sleep(0.02)
        step.set_result("frozen · 87 packages")
    out = stream_tty.getvalue()
    # At least one Braille frame must have been written by the spinner thread.
    assert any(c in out for c in _progress._FRAMES), out
    # Final line should be present, prefixed by the line-clear escape.
    assert "\x1b[2K" in out
    assert "✓ frozen · 87 packages · " in out


def test_tty_mode_spinner_stops_on_exit(stream_tty: _FakeStream) -> None:
    # After __exit__ returns, the spinner thread must have joined cleanly.
    with Step("hashing", verbosity=0, stream=stream_tty) as step:
        step.set_result("hashed")
    assert step._thread is None or not step._thread.is_alive()


def test_tty_mode_exception_emits_x_after_clear(stream_tty: _FakeStream) -> None:
    with pytest.raises(ValueError), Step("packing", verbosity=0, stream=stream_tty):
        raise ValueError("boom")
    out = stream_tty.getvalue()
    assert "\x1b[2K" in out
    assert "✗ packing" in out
    assert "✓" not in out


# ---------- emit_aside + active spinner ----------


def test_emit_aside_clears_line_when_spinner_active(stream_tty: _FakeStream) -> None:
    with Step("freezing dependencies", verbosity=0, stream=stream_tty) as step:
        emit_aside("+ uv export --frozen", file=stream_tty)
        step.set_result("frozen")
    out = stream_tty.getvalue()
    # The aside should appear with the line-clear escape and a newline.
    assert "\r\x1b[2K+ uv export --frozen\n" in out


def test_emit_aside_without_active_step_writes_plain_line(stream_plain: _FakeStream) -> None:
    emit_aside("+ uv build", file=stream_plain)
    assert stream_plain.getvalue() == "+ uv build\n"


# ---------- duration formatting ----------


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0.0, "0ms"),
        (0.04, "40ms"),
        (0.999, "999ms"),
        (1.0, "1.0s"),
        (1.234, "1.2s"),
        (4.27, "4.3s"),
        (60.0, "60.0s"),
    ],
)
def test_format_duration(seconds: float, expected: str) -> None:
    assert _progress._format_duration(seconds) == expected
