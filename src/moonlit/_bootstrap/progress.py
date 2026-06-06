"""Transient extraction-progress indicator for the bootstrap (spec 03 §14).

When a ``.pyz`` runs cold, :func:`moonlit._bootstrap.extract.materialize`
unpacks the bundled ``site-packages/`` into the cache. For a large bundle
that takes seconds, and without feedback the terminal looks frozen. This
module draws a moving ``⠋ unpacking <name> 42%`` line on stderr while the
files are written, then clears it.

It is the stdlib-only cousin of :mod:`moonlit._progress` (the build-time
``Step`` reporter): same Braille frames and ``\\r\\x1b[2K`` clear sequence,
but call-driven rather than thread-driven — the extraction loop calls
:meth:`ExtractProgress.update` as bytes land, and that is the only place a
frame is painted. The bootstrap must import only the stdlib (D7), so the
build-time reporter cannot be reused here.

The contract (spec 03 §10, §14) is preserved by three rules:

* **stderr only** — user stdout is never touched.
* **TTY-gated** — nothing is written unless ``stderr.isatty()``; pipes, CI
  logs and redirects stay silent.
* **transient with a delayed start** — the first paint waits ~200 ms, so a
  fast extraction stays silent, and the line is cleared on exit, so nothing
  is left in the scrollback.
"""

import sys
import time
from typing import TextIO

# ---------- public API ----------


class ExtractProgress:
    """A transient unpacking indicator. Use as a context manager.

    ``with ExtractProgress(label, total_bytes) as p:`` then call
    ``p.update(bytes_done)`` as each file is written. On a non-TTY stream,
    before the delayed-start threshold, or between throttled frames, this is
    a cheap no-op.
    """

    def __init__(
        self,
        label: str,
        total_bytes: int,
        *,
        stream: TextIO | None = None,
    ) -> None:
        self.label = label
        self._total_bytes = total_bytes
        self._stream = stream if stream is not None else sys.stderr
        # Defensive isatty: tests pass StringIO subclasses without isatty;
        # captured streams report False (correct: not a real TTY).
        isatty = getattr(self._stream, "isatty", None)
        self._tty = bool(isatty() if callable(isatty) else False)
        self._start = 0.0
        self._last_paint = 0.0
        self._frame = 0
        self._painted = False

    def __enter__(self) -> "ExtractProgress":
        # Delayed start is enforced in update(); __enter__ paints nothing.
        self._start = time.monotonic()
        return self

    def update(self, bytes_done: int) -> None:
        """Paint a frame for ``bytes_done`` if the throttle gates all pass."""
        if not self._tty:
            return
        now = time.monotonic()
        if now - self._start < _DELAY_S:
            return  # too soon — fast extractions never paint
        if self._painted and now - self._last_paint < _FRAME_INTERVAL:
            return  # throttle repaints to the frame interval
        frame = _FRAMES[self._frame % len(_FRAMES)]
        print(
            f"\r{frame} {self.label} {_format_percent(bytes_done, self._total_bytes)}",
            end="",
            file=self._stream,
            flush=True,
        )
        self._frame += 1
        self._last_paint = now
        self._painted = True

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        # Clear the line we drew so the first run leaves no moonlit chatter.
        # Nothing was drawn unless we painted, so non-TTY/fast paths stay silent.
        if self._painted:
            print("\r\x1b[2K", end="", file=self._stream, flush=True)


# ---------- private helpers ----------

# Braille 8-dot frames, mirroring moonlit._progress for visual consistency.
_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_FRAME_INTERVAL = 0.08  # seconds between repaints
_DELAY_S = 0.2  # wait this long before the first paint


def _format_percent(done: int, total: int) -> str:
    """``<N>%`` of the extraction, or an ellipsis when the total is unknown."""
    if total <= 0:
        return "…"
    return f"{min(done * 100 // total, 100)}%"
