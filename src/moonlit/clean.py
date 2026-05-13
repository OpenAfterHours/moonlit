"""``moonlit clean`` — reap stale cache entries (specs/01-cli.md §2.4).

Policy spec: specs/04-cache-layout.md §12.1 and D23. The CLI wiring lives in
:mod:`moonlit.cli`; this module is the implementation. It is build-time code
(may use third-party deps), but uses the same cache-root resolver as the
stdlib-only bootstrap so the two consumers cannot drift.
"""

from __future__ import annotations

import enum
import fnmatch
import os
import re
import shutil
import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from . import errors
from ._bootstrap import environment as _bootstrap_env
from ._bootstrap import locking as _bootstrap_locking
from .builder import humanize_bytes

# ---------- public API ----------


def clean(config: CleanConfig) -> int:
    """Run one ``moonlit clean`` invocation per ``config``.

    Returns the process exit code: ``0`` on success, ``14`` when at least one
    target was skipped because its lock was held (``--force`` unset),
    ``15`` on an I/O failure during deletion (specs/01-cli.md §2.4).

    Output: table to stderr, trailer to stdout. ``--quiet`` suppresses the
    table only.
    """
    cache_root = config.cache_root
    if not cache_root.is_dir():
        _print_trailer(config, deleted_count=0, freed=0)
        return 0

    entries = _scan_cache_root(cache_root)
    now = time.time()
    keep_set, delete_set = _plan_filters(entries, config, now=now)
    delete_keys = {e.name for e in delete_set}
    orphans = _select_orphans(entries, deletion_set=delete_keys)

    rows: list[_Row] = []
    deleted_count = 0
    freed = 0
    refused = False
    io_failure = False

    for entry in delete_set:
        result = _process_delete(entry, config, now=now)
        rows.append(result.row)
        if result.io_failure:
            io_failure = True
            break
        if result.skipped:
            refused = True
        else:
            deleted_count += 1
            freed += result.freed

    if not io_failure:
        for entry in keep_set:
            rows.append(_make_keep_row(entry, config, now=now))

        for orphan in orphans:
            result = _process_orphan(orphan, config, now=now)
            rows.append(result.row)
            if result.io_failure:
                io_failure = True
                break
            if not result.skipped:
                deleted_count += 1
                freed += result.freed

    if config.verbosity >= 0:
        _print_table(rows, verbose=config.verbosity > 0)
    _print_trailer(config, deleted_count=deleted_count, freed=freed)

    if io_failure:
        return errors.CleanIOError.exit_code
    if refused:
        return errors.CleanRefusedError.exit_code
    return 0


@dataclass(frozen=True)
class CleanConfig:
    """Parsed flag state for one ``moonlit clean`` invocation.

    The CLI builds this and hands it to :func:`clean`; tests build it directly
    to bypass argparse. Mirrors :class:`moonlit.builder.BuildConfig` style.
    """

    cache_root: Path
    all_: bool
    older_than: timedelta | None
    keep_latest: int | None
    name_pattern: str | None
    force: bool
    dry_run: bool
    show_sizes: bool
    # -1 quiet, 0 normal, +1 verbose. Mirrors BuildConfig.verbosity.
    verbosity: int


# ---------- types ----------


class _Kind(enum.Enum):
    """Per-directory-entry classification within ``<cache_root>/``."""

    CACHE_ENTRY = "cache_entry"
    TMP = "tmp"
    OLD = "old"
    LOCK = "lock"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class _Classified:
    """Result of :func:`_classify`."""

    kind: _Kind
    # Owning ``<cache_key>`` for {CACHE_ENTRY, TMP, OLD, LOCK}; None for UNKNOWN.
    cache_key: str | None
    # Pid the orphan was tagged with, for {TMP, OLD}; None otherwise.
    pid: int | None


@dataclass(frozen=True)
class _Entry:
    """One classified entry under ``<cache_root>/``."""

    path: Path
    # Directory-entry basename, as it appears on disk.
    name: str
    classified: _Classified
    # For CACHE_ENTRY: site-packages/ mtime (or cache_key dir mtime if absent).
    # For TMP/OLD/LOCK: the entry's own mtime.
    mtime: float
    # Lazy. Populated on demand by _walk_size during deletion / --show-sizes.
    size: int | None = None


# ---------- private helpers ----------


def _resolve_cache_root() -> Path:
    """Shared resolver — delegates to :func:`bootstrap.environment.resolve_cache_root`.

    The bootstrap and ``moonlit clean`` MUST agree on which directory to
    operate against; pinning the algorithm in one place is the only way to
    prevent silent drift (D23).
    """
    return _bootstrap_env.resolve_cache_root()


_BUILD_ID_RE = re.compile(r"^[0-9a-f]{64}$")
_TMP_OLD_RE = re.compile(r"^\.(?P<key>.+?)\.(?P<kind>tmp|old)\.(?P<pid>\d+)$")
_DURATION_RE = re.compile(r"^(\d+)(s|m|h|d)$")
_UNIT_SECONDS: dict[str, int] = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def _parse_cache_key(name: str) -> tuple[str, str] | None:
    """Split ``<normalized_name>_<build_id>`` on the last underscore and validate.

    Returns ``(normalized_name, build_id_hex)`` on success, ``None`` if the
    name is not a well-formed cache key. The on-disk name is already PEP-503
    normalized by the bootstrap (D5); we round-trip what's on disk.
    """
    idx = name.rfind("_")
    if idx <= 0:
        return None
    name_part, hex_part = name[:idx], name[idx + 1 :]
    if not name_part:
        return None
    if not _BUILD_ID_RE.fullmatch(hex_part):
        return None
    return (name_part, hex_part)


def _classify(name: str) -> _Classified:
    """Map a top-level directory-entry name under ``<cache_root>/`` to a kind.

    Recognized shapes (specs/04-cache-layout.md §4):

    - ``<cache_key>``                → CACHE_ENTRY
    - ``<cache_key>.lock``           → LOCK
    - ``.<cache_key>.tmp.<pid>``     → TMP
    - ``.<cache_key>.old.<pid>``     → OLD

    Anything else (including a future ``v2/`` reserved subdir per §14)
    classifies as UNKNOWN and is left strictly alone.
    """
    if name.startswith("."):
        m = _TMP_OLD_RE.fullmatch(name)
        if m is None:
            return _Classified(_Kind.UNKNOWN, None, None)
        key = m.group("key")
        if _parse_cache_key(key) is None:
            return _Classified(_Kind.UNKNOWN, None, None)
        kind = _Kind.TMP if m.group("kind") == "tmp" else _Kind.OLD
        return _Classified(kind, key, int(m.group("pid")))

    if name.endswith(".lock"):
        key = name[: -len(".lock")]
        if _parse_cache_key(key) is None:
            return _Classified(_Kind.UNKNOWN, None, None)
        return _Classified(_Kind.LOCK, key, None)

    if _parse_cache_key(name) is not None:
        return _Classified(_Kind.CACHE_ENTRY, name, None)

    return _Classified(_Kind.UNKNOWN, None, None)


def _parse_duration(value: str) -> timedelta:
    """Parse ``<positive-int><s|m|h|d>`` into a :class:`timedelta`.

    Zero, negative, fractional, and compound (``1h30m``) forms are rejected.
    Whitespace is rejected so we never silently accept a user typo.
    """
    m = _DURATION_RE.fullmatch(value)
    if m is None:
        raise ValueError(
            f"invalid duration {value!r}; expected <positive-int><s|m|h|d> (e.g. 30m, 7d)"
        )
    n = int(m.group(1))
    if n <= 0:
        raise ValueError(f"duration must be positive, got {value!r}")
    return timedelta(seconds=n * _UNIT_SECONDS[m.group(2)])


def _scan_cache_root(cache_root: Path) -> list[_Entry]:
    """Walk top-level entries of ``cache_root`` and classify each.

    Returns an empty list when the directory does not exist (treated as an
    empty cache, not as an error — specs/01-cli.md §2.4 step 2). UNKNOWN
    entries are excluded from the result; callers wanting to log them under
    ``--verbose`` walk ``cache_root.iterdir()`` themselves.
    """
    if not cache_root.is_dir():
        return []
    out: list[_Entry] = []
    for child in sorted(cache_root.iterdir(), key=lambda p: p.name):
        classified = _classify(child.name)
        if classified.kind == _Kind.UNKNOWN:
            continue
        mtime = _entry_mtime(child, classified)
        out.append(_Entry(path=child, name=child.name, classified=classified, mtime=mtime))
    return out


def _entry_mtime(path: Path, classified: _Classified) -> float:
    """Mtime to use for filter / display purposes."""
    if classified.kind == _Kind.CACHE_ENTRY:
        sp = path / "site-packages"
        try:
            return sp.stat().st_mtime
        except OSError:
            # site-packages missing or unreadable; fall back to the cache_key dir.
            try:
                return path.stat().st_mtime
            except OSError:
                return 0.0
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _apply_filters(
    entries: list[_Entry],
    config: CleanConfig,
    *,
    now: float | None = None,
) -> list[_Entry]:
    """Return the cache entries selected for deletion by ``config``'s filters.

    Thin wrapper around :func:`_plan_filters` that drops the kept-set; kept
    for callers that only want the deletion list (e.g. unit tests).
    """
    _keep, delete = _plan_filters(entries, config, now=now)
    return delete


def _plan_filters(
    entries: list[_Entry],
    config: CleanConfig,
    *,
    now: float | None = None,
) -> tuple[list[_Entry], list[_Entry]]:
    """Return ``(keep, delete)`` cache-entry partitions selected by ``config``.

    Filter composition is the intersection of ``--older-than`` and ``--name``,
    after which ``--keep-latest`` partitions the survivors. ``--all`` is a
    no-op narrowing pass (it just enables the loop). When no filter is
    active, ``([], [])`` is returned defensively — the CLI rejects no-flag
    invocations at parse time, but a stray call here should not mass-delete.
    """
    has_any_filter = (
        config.all_
        or config.older_than is not None
        or config.name_pattern is not None
        or config.keep_latest is not None
    )
    if not has_any_filter:
        return ([], [])

    candidates = [e for e in entries if e.classified.kind == _Kind.CACHE_ENTRY]
    if config.older_than is not None:
        cutoff = (time.time() if now is None else now) - config.older_than.total_seconds()
        candidates = [e for e in candidates if e.mtime < cutoff]
    if config.name_pattern is not None:
        candidates = [e for e in candidates if _matches_name(e, config.name_pattern)]
    if config.keep_latest is None:
        return ([], candidates)
    return _split_keep_latest(candidates, config.keep_latest)


def _split_keep_latest(
    candidates: list[_Entry], keep: int
) -> tuple[list[_Entry], list[_Entry]]:
    """Group by normalized name, keep ``keep`` newest per group, delete the rest."""
    grouped: dict[str, list[_Entry]] = {}
    for e in candidates:
        parsed = _parse_cache_key(e.name)
        if parsed is None:
            continue  # defensive; CACHE_ENTRY implies parse-able
        grouped.setdefault(parsed[0], []).append(e)
    keep_set: list[_Entry] = []
    delete_set: list[_Entry] = []
    for group in grouped.values():
        group.sort(key=lambda e: e.mtime, reverse=True)
        keep_set.extend(group[:keep])
        delete_set.extend(group[keep:])
    return (keep_set, delete_set)


def _matches_name(entry: _Entry, pattern: str) -> bool:
    parsed = _parse_cache_key(entry.name)
    if parsed is None:
        return False
    return fnmatch.fnmatch(parsed[0], pattern)


def _delete_cache_entry(entry: _Entry, force: bool) -> tuple[int, str | None]:
    """Delete one cache-entry directory plus its sibling ``.lock``.

    Returns ``(bytes_freed, skip_reason)``. ``skip_reason`` is ``None`` on
    success, ``"locked"`` when the try-lock failed and ``force`` was False.

    D23 contract: we hold the lock during deletion so a concurrent extractor
    serializes against us. On ``force=True`` the lock is bypassed and the
    lock file is left alone (a live holder still owns the byte range and we
    do not want to pull the rug). On the normal path the lock file is
    unlinked after release because the cache_key it guarded is gone.
    """
    cache_root = entry.path.parent
    cache_key = entry.classified.cache_key
    lock_path = cache_root / f"{cache_key}.lock"

    fd: int | None = None
    if not force:
        fd = _bootstrap_locking.try_acquire_nonblocking(lock_path)
        if fd is None:
            return (0, "locked")
    try:
        freed = _walk_size(entry.path)
        shutil.rmtree(entry.path)
    finally:
        if fd is not None:
            _bootstrap_locking.release(fd, lock_path)
            # Lock guarded a now-deleted cache_key; safe to unlink (D23). On
            # contention with a concurrent opener we already won the lock and
            # released after our writes, so the next opener gets a fresh inode.
            try:
                lock_path.unlink(missing_ok=True)
            except OSError:
                pass  # leaving the file behind is harmless per spec 04 §11
    return (freed, None)


def _select_orphans(entries: list[_Entry], deletion_set: set[str]) -> list[_Entry]:
    """Pick orphan ``.tmp.<pid>`` / ``.old.<pid>`` / dangling ``.lock`` entries.

    An orphan is reaped when its owning ``<cache_key>`` is either in the
    deletion set for this run or no longer present on disk. Per spec 04 §12.1
    no per-pid liveness check is performed for ``.tmp`` / ``.old`` — pid reuse
    makes it racy and the user's "no .pyz is extracting" assertion is the
    contract.
    """
    cache_entry_names = {e.name for e in entries if e.classified.kind == _Kind.CACHE_ENTRY}
    out: list[_Entry] = []
    for e in entries:
        if e.classified.kind not in (_Kind.TMP, _Kind.OLD, _Kind.LOCK):
            continue
        owning_key = e.classified.cache_key
        if owning_key in deletion_set or owning_key not in cache_entry_names:
            out.append(e)
    return out


def _delete_orphan(entry: _Entry) -> tuple[int, str | None]:
    """Reap one orphan. Returns ``(bytes_freed, skip_reason)``.

    For TMP/OLD: rmtree directly; no lock is involved.
    For LOCK: try-lock first to confirm no live holder. If held, skip with
    reason ``"locked"`` — ``--force`` does NOT override (we never pull the rug
    under a live holder; spec 04 §12.1).
    """
    if entry.classified.kind == _Kind.LOCK:
        fd = _bootstrap_locking.try_acquire_nonblocking(entry.path)
        if fd is None:
            return (0, "locked")
        try:
            size = _file_size(entry.path)
        finally:
            _bootstrap_locking.release(fd, entry.path)
        try:
            entry.path.unlink(missing_ok=True)
        except OSError:
            pass
        return (size, None)

    freed = _walk_size(entry.path)
    shutil.rmtree(entry.path)
    return (freed, None)


def _walk_size(path: Path) -> int:
    """Sum ``st_size`` over every regular file under ``path``. 0 if absent."""
    if not path.exists():
        return 0
    total = 0
    for root, _dirs, files in os.walk(path):
        for fname in files:
            try:
                total += os.path.getsize(os.path.join(root, fname))
            except OSError:
                pass
    return total


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


# ---------- row / output helpers (step 7) ----------


@dataclass(frozen=True)
class _Row:
    """One row of the action-plan table."""

    action: str  # "keep" | "delete" | "skip" | "orphan"
    name: str  # display name (normalized for entries; "<key>.<kind>.<pid>" for orphans)
    build_id: str  # full 64-char hex for cache entries, "—" for orphans
    age: str  # humanized
    size: int | None  # bytes; None = render as "—"
    path: str
    reason: str | None  # appended to PATH for skip rows


@dataclass(frozen=True)
class _ProcessResult:
    """One iteration of the deletion / orphan-reap loop."""

    row: _Row
    skipped: bool
    io_failure: bool
    freed: int


def _process_delete(entry: _Entry, config: CleanConfig, *, now: float) -> _ProcessResult:
    """Either delete ``entry`` (or predict in dry-run) and produce a row."""
    parsed = _parse_cache_key(entry.name)
    name = parsed[0] if parsed else entry.name
    build_id = parsed[1] if parsed else ""

    if config.dry_run:
        if not config.force and _is_lock_held(_lock_path_for(entry)):
            row = _Row("skip", name, build_id, _humanize_age(now - entry.mtime), None,
                       str(entry.path), "locked")
            return _ProcessResult(row=row, skipped=True, io_failure=False, freed=0)
        size = _walk_size(entry.path)
        row = _Row("delete", name, build_id, _humanize_age(now - entry.mtime), size,
                   str(entry.path), None)
        return _ProcessResult(row=row, skipped=False, io_failure=False, freed=size)

    try:
        freed, reason = _delete_cache_entry(entry, force=config.force)
    except OSError as exc:
        row = _Row("skip", name, build_id, _humanize_age(now - entry.mtime), None,
                   str(entry.path), f"io error: {exc}")
        return _ProcessResult(row=row, skipped=False, io_failure=True, freed=0)
    if reason is not None:
        row = _Row("skip", name, build_id, _humanize_age(now - entry.mtime), None,
                   str(entry.path), reason)
        return _ProcessResult(row=row, skipped=True, io_failure=False, freed=0)
    row = _Row("delete", name, build_id, _humanize_age(now - entry.mtime), freed,
               str(entry.path), None)
    return _ProcessResult(row=row, skipped=False, io_failure=False, freed=freed)


def _process_orphan(entry: _Entry, config: CleanConfig, *, now: float) -> _ProcessResult:
    """Either delete the orphan (or predict in dry-run) and produce a row."""
    display_name = _orphan_display_name(entry)
    age = _humanize_age(now - entry.mtime)

    if config.dry_run:
        if entry.classified.kind == _Kind.LOCK:
            held = _is_lock_held(entry.path)
            if held:
                row = _Row("skip", display_name, "—", age, None, str(entry.path), "locked")
                return _ProcessResult(row=row, skipped=True, io_failure=False, freed=0)
            size = _file_size(entry.path)
        else:
            size = _walk_size(entry.path)
        row = _Row("orphan", display_name, "—", age, size, str(entry.path), None)
        return _ProcessResult(row=row, skipped=False, io_failure=False, freed=size)

    try:
        freed, reason = _delete_orphan(entry)
    except OSError as exc:
        row = _Row("skip", display_name, "—", age, None, str(entry.path), f"io error: {exc}")
        return _ProcessResult(row=row, skipped=False, io_failure=True, freed=0)
    if reason is not None:
        row = _Row("skip", display_name, "—", age, None, str(entry.path), reason)
        return _ProcessResult(row=row, skipped=True, io_failure=False, freed=0)
    row = _Row("orphan", display_name, "—", age, freed, str(entry.path), None)
    return _ProcessResult(row=row, skipped=False, io_failure=False, freed=freed)


def _make_keep_row(entry: _Entry, config: CleanConfig, *, now: float) -> _Row:
    parsed = _parse_cache_key(entry.name)
    name = parsed[0] if parsed else entry.name
    build_id = parsed[1] if parsed else ""
    size = _walk_size(entry.path) if config.show_sizes else None
    return _Row("keep", name, build_id, _humanize_age(now - entry.mtime), size,
                str(entry.path), None)


def _lock_path_for(entry: _Entry) -> Path:
    return entry.path.parent / f"{entry.classified.cache_key}.lock"


def _is_lock_held(lock_path: Path) -> bool:
    fd = _bootstrap_locking.try_acquire_nonblocking(lock_path)
    if fd is None:
        return True
    _bootstrap_locking.release(fd, lock_path)
    return False


def _orphan_display_name(entry: _Entry) -> str:
    parsed = _parse_cache_key(entry.classified.cache_key or "")
    base = parsed[0] if parsed else (entry.classified.cache_key or entry.name)
    if entry.classified.kind == _Kind.TMP:
        return f"{base}.tmp.{entry.classified.pid}"
    if entry.classified.kind == _Kind.OLD:
        return f"{base}.old.{entry.classified.pid}"
    if entry.classified.kind == _Kind.LOCK:
        return f"{base}.lock"
    return base


def _humanize_age(seconds: float) -> str:
    """Compact age string: ``45s``, ``9m``, ``2h``, ``9d`` (no padding)."""
    s = int(max(seconds, 0))
    if s < 60:
        return f"{s}s"
    m = s // 60
    if m < 60:
        return f"{m}m"
    h = m // 60
    if h < 24:
        return f"{h}h"
    d = h // 24
    return f"{d}d"


def _print_table(rows: list[_Row], *, verbose: bool) -> None:
    """Render rows as a fixed-column table on stderr."""
    if not rows:
        return
    sys_stderr = _stderr()
    cols = _table_columns(rows, verbose=verbose)
    widths = _column_widths(cols)
    sys_stderr.write(_format_row(("ACTION", "NAME", "BUILD_ID", "AGE", "SIZE", "PATH"), widths))
    sys_stderr.write("\n")
    for row in rows:
        size_col = "—" if row.size is None else humanize_bytes(row.size)
        build_id_col = "—" if row.build_id == "—" else (row.build_id if verbose else row.build_id[:8])
        path_col = row.path if row.reason is None else f"{row.path} ({row.reason})"
        sys_stderr.write(_format_row((row.action, row.name, build_id_col, row.age, size_col, path_col), widths))
        sys_stderr.write("\n")


def _table_columns(rows: list[_Row], *, verbose: bool) -> list[tuple[str, str, str, str, str, str]]:
    out = []
    for r in rows:
        size_col = "—" if r.size is None else humanize_bytes(r.size)
        build_id_col = "—" if r.build_id == "—" else (r.build_id if verbose else r.build_id[:8])
        path_col = r.path if r.reason is None else f"{r.path} ({r.reason})"
        out.append((r.action, r.name, build_id_col, r.age, size_col, path_col))
    return out


def _column_widths(cols: list[tuple[str, str, str, str, str, str]]) -> tuple[int, ...]:
    headers = ("ACTION", "NAME", "BUILD_ID", "AGE", "SIZE", "PATH")
    widths = [len(h) for h in headers]
    for row in cols:
        for i, v in enumerate(row):
            widths[i] = max(widths[i], len(v))
    return tuple(widths)


def _format_row(fields: tuple[str, ...], widths: tuple[int, ...]) -> str:
    # Two-space gutter between columns; final column un-padded.
    parts = []
    for i, (v, w) in enumerate(zip(fields, widths, strict=True)):
        if i == len(fields) - 1:
            parts.append(v)
        else:
            parts.append(v.ljust(w))
    return "  ".join(parts)


def _print_trailer(config: CleanConfig, *, deleted_count: int, freed: int) -> None:
    """Trailer goes to stdout regardless of ``--quiet`` (spec 01 §2.4)."""
    if config.dry_run:
        msg = f"would delete {deleted_count} entries, would free {humanize_bytes(freed)}"
    else:
        msg = f"deleted {deleted_count} entries, freed {humanize_bytes(freed)}"
    _stdout().write(msg + "\n")


def _stdout():
    import sys
    return sys.stdout


def _stderr():
    import sys
    return sys.stderr
