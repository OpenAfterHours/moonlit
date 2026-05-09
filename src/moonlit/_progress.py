"""Step-progress reporter for the build pipeline (specs/01-cli.md §8).

The :class:`Step` context manager wraps each pipeline step so the user
gets visible feedback on stderr while long ``uv`` subprocesses run.

Usage from :mod:`moonlit.builder`::

    with Step("resolving uv workspace", verbosity=cfg.verbosity) as step:
        ws = workspace.detect(...)
        step.set_result(f"resolved · {len(ws.members)} members")

Behavior matrix:

* ``verbosity == -1`` (``--quiet``) — nothing is written.
* stderr is a TTY — a Braille spinner cycles next to the label; on exit
  the line is rewritten to ``✓ <result> · <duration>`` (or ``✗ <label>``
  on exception).
* stderr is not a TTY (CI logs, file redirect, pipe) — a plain
  ``→ <label>`` line precedes the body and ``✓ <result> · <duration>``
  follows it. No carriage returns or escape sequences in the output.

:func:`emit_aside` is the cross-module hook used by
:func:`moonlit.resolver._run_uv` to print ``+ uv <argv>`` lines under
``--verbose`` without smearing the spinner output.
"""

from __future__ import annotations

import sys
import threading
import time
from typing import TextIO

# ---------- public API ----------


def emit_aside(msg: str, *, file: TextIO | None = None) -> None:
    """Print ``msg`` to ``file`` (default: stderr), suspending the active spinner.

    When a TTY-mode :class:`Step` is currently running, the spinner line is
    overwritten with ``msg`` (carriage-return + clear-to-end-of-line, then
    ``msg`` + newline). The next spinner tick draws on the line below, so
    the verbose echo and the spinner remain visually coherent.

    When no spinner is active (or the active one is in plain mode), this is
    just an ordinary ``print(msg, file=file)``.
    """
    target = file if file is not None else sys.stderr
    with _print_lock:
        if _active is not None and _active._tty:
            print("\r\x1b[2K" + msg, file=target, flush=True)
        else:
            print(msg, file=target, flush=True)


class Step:
    """One pipeline step. Use as a context manager; see module docstring."""

    def __init__(
        self,
        label: str,
        *,
        verbosity: int = 0,
        stream: TextIO | None = None,
    ) -> None:
        self.label = label
        self._verbosity = verbosity
        self._stream = stream if stream is not None else sys.stderr
        # Defensive isatty: tests pass StringIO subclasses without isatty;
        # captured pytest streams report False (correct: not a real TTY).
        isatty = getattr(self._stream, "isatty", None)
        self._tty = bool(isatty() if callable(isatty) else False)
        self._enabled = verbosity >= 0
        self._result: str | None = None
        self._show_duration: bool = True
        self._start: float = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def set_result(self, text: str, *, show_duration: bool = True) -> None:
        """Replace the in-progress label with ``text`` on successful exit.

        If ``show_duration`` is False, the trailing ``· <ms|s>`` chunk is
        omitted (used by the hashing step which already includes a count).
        """
        self._result = text
        self._show_duration = show_duration

    # ----- context manager -----

    def __enter__(self) -> Step:
        if not self._enabled:
            return self
        global _active
        _active = self
        self._start = time.perf_counter()
        if self._tty:
            self._stop.clear()
            self._thread = threading.Thread(target=self._spin, daemon=True)
            self._thread.start()
        else:
            with _print_lock:
                print(f"→ {self.label}", file=self._stream, flush=True)
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        if not self._enabled:
            return
        if self._thread is not None:
            self._stop.set()
            self._thread.join()
        elapsed = time.perf_counter() - self._start
        with _print_lock:
            if self._tty:
                print("\r\x1b[2K", end="", file=self._stream, flush=True)
            if exc_type is None:
                result = self._result if self._result is not None else self.label
                line = f"✓ {result}"
                if self._show_duration:
                    line += f" · {_format_duration(elapsed)}"
            else:
                line = f"✗ {self.label}"
            print(line, file=self._stream, flush=True)
        global _active
        if _active is self:
            _active = None

    # ----- spinner thread body -----

    def _spin(self) -> None:
        i = 0
        while not self._stop.wait(_FRAME_INTERVAL):
            with _print_lock:
                # \r returns to the start of the current line; the previous
                # frame is overwritten in place. emit_aside() may have moved
                # us down a line — that's fine, we just continue spinning on
                # whatever line we're now on.
                print(
                    f"\r{_FRAMES[i % len(_FRAMES)]} {self.label}",
                    end="",
                    file=self._stream,
                    flush=True,
                )
            i += 1


# ---------- private helpers ----------

# Braille 8-dot frames cycled by the spinner thread.
_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_FRAME_INTERVAL = 0.08  # seconds

# Module-globals coordinating the spinner with cross-module asides.
_active: Step | None = None
_print_lock = threading.Lock()


def _format_duration(seconds: float) -> str:
    """``<N>ms`` under one second, otherwise ``<N.N>s`` (one decimal)."""
    if seconds < 1.0:
        return f"{round(seconds * 1000)}ms"
    return f"{seconds:.1f}s"
