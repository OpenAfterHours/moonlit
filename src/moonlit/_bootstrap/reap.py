"""Runtime cache self-GC (stdlib-only, D24).

After a fresh slow-path extraction, the bootstrap calls :func:`reap` to trim
OLDER cache entries of the SAME app on the recipient machine, keeping the
newest ``keep_latest`` and only those past an age grace. Recipients of a .pyz
usually do not have moonlit installed, so the build-time ``moonlit clean`` is
unavailable to them — this is the only automatic reclaim they get.

Contract: specs/04-cache-layout.md §12.2, specs/03-bootstrap-runtime.md §2/§9,
D24. Mirrors — but MUST NOT import — :mod:`moonlit.clean` (D7 forbids importing
build-time code into the stdlib-only bootstrap); the classifier is pinned to
``clean``'s by ``tests/unit/test_reap.py::test_classifier_matches_clean_parse_cache_key``.

Safety model (the crux): a D14 cache-hit fast-path reader of an older build
holds no lock and is invisible here. The hazard is BOUNDED — never eliminated —
by (a) keeping ``keep_latest`` builds, (b) the age grace, (c) same-app scope,
and (d) a per-victim cooperative try-lock. The whole pass is best-effort:
:func:`reap` never raises and never changes the caller's exit code.
"""

import os
import re
import shutil
import sys
import time
from pathlib import Path

from . import locking
from .environment import Environment

# Built-in defaults, applied when env.json carries no (or a partial) gc object.
# Identical to builder.BuildConfig's gc_* defaults (pinned by spec 05 §3.10).
_DEFAULT_ENABLED = True
_DEFAULT_KEEP_LATEST = 2
_DEFAULT_GRACE_SECONDS = 86400  # 24h

_BUILD_ID_RE = re.compile(r"^[0-9a-f]{64}$")


def reap(env: Environment, cache_root: Path, *, now: float | None = None) -> None:
    """Best-effort prune of this app's stale cache entries. Never raises.

    Resolves the policy from ``env.gc`` overlaid with the ``MOONLIT_*`` runtime
    overrides, then — when enabled — reaps same-app cache entries beyond the
    newest ``keep_latest`` that are also older than the age grace, skipping the
    just-installed key and any entry whose lock is currently held.
    """
    try:
        enabled, keep_latest, grace_seconds = _resolve_policy(env)
        if not enabled:
            return
        normalized = _normalize(env.name)
        current_key = f"{normalized}_{env.build_id}"
        clock = time.time() if now is None else now
        victims = _select_victims(
            cache_root,
            normalized_name=normalized,
            current_key=current_key,
            keep_latest=keep_latest,
            grace_seconds=grace_seconds,
            now=clock,
        )
        for victim in victims:
            _reap_one(cache_root, victim)
    except Exception as exc:  # GC must never break the user's app run; swallow all.
        _debug(f"reap aborted: {type(exc).__name__}: {exc}")


# ---------- policy resolution, in reap()'s call order ----------


def _resolve_policy(env: Environment) -> tuple[bool, int, int]:
    """Resolve (enabled, keep_latest, grace_seconds) from env.json + env vars.

    env.json gc (or built-in defaults when absent/partial) is the base; the
    ``MOONLIT_*`` runtime overrides win. A malformed override is ignored — the
    bootstrap must not fail on a bad knob.
    """
    base = env.gc if isinstance(env.gc, dict) else {}
    enabled = base.get("enabled", _DEFAULT_ENABLED)
    keep_latest = base.get("keep_latest", _DEFAULT_KEEP_LATEST)
    grace_seconds = base.get("grace_seconds", _DEFAULT_GRACE_SECONDS)

    # MOONLIT_NO_GC (truthy = present and non-empty, per D16) hard-disables.
    if os.environ.get("MOONLIT_NO_GC", ""):
        enabled = False

    keep_latest = _override_int(
        os.environ.get("MOONLIT_GC_KEEP_LATEST", ""), keep_latest, minimum=1
    )
    grace_seconds = _override_int(os.environ.get("MOONLIT_GC_GRACE", ""), grace_seconds, minimum=0)
    return bool(enabled), int(keep_latest), int(grace_seconds)


def _override_int(raw: str, fallback: int, *, minimum: int) -> int:
    """Parse a runtime-override int; fall back on anything malformed or < minimum."""
    if not raw:
        return fallback
    try:
        value = int(raw)
    except ValueError:
        return fallback
    return value if value >= minimum else fallback


def _normalize(name: str) -> str:
    # D5 PEP 503 normalization — identical to extract._cache_key. Collapses every
    # run of [-_.] to '-', so a well-formed cache_key has the separator '_' once.
    return re.sub(r"[-_.]+", "-", name).lower()


def _select_victims(
    cache_root: Path,
    *,
    normalized_name: str,
    current_key: str,
    keep_latest: int,
    grace_seconds: int,
    now: float,
) -> list[Path]:
    """Pick the same-app cache dirs to reap: beyond keep_latest AND past grace.

    Ranks the whole same-app group (including the current key) by mtime so the
    just-installed key — newest by construction — is always retained; it is also
    excluded explicitly as a belt-and-suspenders guard.
    """
    if not cache_root.is_dir():
        return []
    group = _same_app_entries(cache_root, normalized_name)
    group.sort(key=lambda pm: pm[1], reverse=True)  # newest first
    out: list[Path] = []
    for path, mtime in group[keep_latest:]:
        if path.name == current_key:
            continue  # never reap the tree we just installed / are about to run
        if grace_seconds > 0 and mtime >= now - grace_seconds:
            continue  # too recent — a predecessor might still be running
        out.append(path)
    return out


def _same_app_entries(cache_root: Path, normalized_name: str) -> list[tuple[Path, float]]:
    """[(cache_entry_dir, site-packages mtime)] for every well-formed cache key
    of this app. UNKNOWN names and .tmp/.old/.lock siblings are ignored.
    """
    out: list[tuple[Path, float]] = []
    for child in cache_root.iterdir():
        if child.name.startswith("."):
            continue  # .<key>.tmp/.old siblings — not the reaper's concern (D24)
        parsed = _parse_cache_key(child.name)
        if parsed is None or parsed[0] != normalized_name:
            continue
        if not child.is_dir():
            continue
        out.append((child, _entry_mtime(child)))
    return out


def _parse_cache_key(name: str) -> tuple[str, str] | None:
    """Split ``<normalized_name>_<build_id>`` on the last underscore and validate.

    Mirrors :func:`moonlit.clean._parse_cache_key` exactly (pinned by a parity
    test). Returns ``(name_part, build_id_hex)`` or ``None`` if not a key.
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


def _entry_mtime(entry: Path) -> float:
    """site-packages/ mtime (stable against .pyc writes); fall back to the key dir."""
    sp = entry / "site-packages"
    try:
        return sp.stat().st_mtime
    except OSError:
        try:
            return entry.stat().st_mtime
        except OSError:
            return 0.0


def _reap_one(cache_root: Path, entry: Path) -> None:
    """Delete one victim under its own lock, then unlink the orphaned lock.

    Cooperative (D23-style): if the lock is held by a live extractor of that
    exact key, skip. Any OSError during deletion is swallowed — the entry is
    left intact and the pass continues.
    """
    lock_path = cache_root / f"{entry.name}.lock"
    fd = locking.try_acquire_nonblocking(lock_path)
    if fd is None:
        _debug(f"skipped {entry.name} (locked)")
        return
    try:
        shutil.rmtree(entry)
    except OSError as exc:
        _debug(f"could not reap {entry.name}: {exc}")
        return
    finally:
        locking.release(fd, lock_path)
    # The lock guarded a now-deleted key; unlink it. Harmless to leave on error.
    try:
        lock_path.unlink()
    except OSError:
        pass
    _debug(f"pruned {entry.name}")


def _debug(message: str) -> None:
    # D16: present and non-empty after os.environ.get is truthy. stderr only,
    # preserving the §14 silent-on-success contract.
    if os.environ.get("MOONLIT_DEBUG", ""):
        print(f"moonlit: {message}", file=sys.stderr)
