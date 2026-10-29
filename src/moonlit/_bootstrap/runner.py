"""sys.path setup, entry-point resolution, and return-value coercion.

Implements specs/03-bootstrap-runtime.md §7 (collision check, addsitedir,
D26 placement ahead of host site dirs) and §8 (entry-point parsing, import,
getattr walk, invocation, return-value coercion). Failures from this module
raise either CollisionError (exit 1) or EntryPointError (exit 2) per the D3
runtime enumeration.
"""

import importlib
import os
import site
import sys
from pathlib import Path
from typing import Any, cast

from .environment import Environment
from .errors import CollisionError, EntryPointError


def run(env: Environment, site_dir: Path) -> int:
    """Set up sys.path, resolve and invoke the entry point, coerce its return.

    Returns an int in [0, 255] suitable for ``sys.exit``.
    """
    _check_no_bootstrap_collision(site_dir)
    _activate_site_dir(site_dir)
    entry_point_str = _resolve_entry_point_string(env)
    module_name, attr = _parse_entry_point(entry_point_str)
    obj = _import_and_resolve(module_name, attr)
    # spec §8: invoke the resolved object raw — no callable pre-check. It is
    # dynamic by nature (a getattr walk), so cast away `object` for the call.
    return _coerce_return(cast(Any, obj)())


# ---------- private helpers, in run()'s call order ----------


def _check_no_bootstrap_collision(site_dir: Path) -> None:
    # spec §7: case-fold compare for Windows / HFS+ case-insensitive filesystems.
    for entry in os.listdir(site_dir):
        if entry.casefold() == "_bootstrap":
            raise CollisionError("_bootstrap collision in staged tree")


def _activate_site_dir(site_dir: Path) -> None:
    # D26: addsitedir appends, which lets any host site dir shadow a bundled
    # distribution. Seat site_dir in its slot first, so the bundle's own .pth
    # `import` lines already resolve against it (addsitedir won't re-add a dir
    # already on sys.path, but still processes its .pth files), then move what
    # those .pth files added into the same block.
    before = list(sys.path)
    sys.path.insert(_bundle_slot(before), str(site_dir))
    site.addsitedir(str(site_dir))
    sys.path[:] = _place_ahead_of_host_sites(before, sys.path)


def _bundle_slot(entries: list[str]) -> int:
    # Just ahead of the first host site dir AFTER the stdlib. One reached via
    # PYTHONPATH sits ahead of the stdlib and must not lift the bundle above it.
    # Host startup code can leave non-str junk (e.g. None) here; it matches nothing.
    normalized = [_normalize(entry) if isinstance(entry, str) else None for entry in entries]
    host_dirs = _host_site_dirs()
    for index in range(_stdlib_index(normalized) + 1, len(normalized)):
        if normalized[index] in host_dirs:
            return index
    # No host site dir after the stdlib (e.g. `python -S`): nothing to shadow.
    return len(normalized)


def _host_site_dirs() -> set[str]:
    # getusersitepackages() is None on platforms with no home dir (WASI, iOS, …).
    candidates = [*site.getsitepackages(), site.getusersitepackages()]
    return {_normalize(path) for path in candidates if path}


def _stdlib_index(normalized: list[str | None]) -> int:
    # os.py lives in the stdlib dir (inside the zip, for a zipped stdlib). When
    # that can't be located on sys.path, -1 makes the host search start at 0.
    os_file = getattr(os, "__file__", None)
    if os_file is None:
        return -1
    stdlib = _normalize(os.path.dirname(os_file))
    if stdlib not in normalized:
        return -1
    return normalized.index(stdlib)


def _normalize(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _place_ahead_of_host_sites(before: list[str], after: list[str]) -> list[str]:
    # A bundled .pth `import` line can insert anywhere, so diff rather than
    # slicing the tail; both groups keep their relative order.
    previous = set(before)
    added = [entry for entry in after if entry not in previous]
    kept = [entry for entry in after if entry in previous]
    anchor = _bundle_slot(kept)
    return [*kept[:anchor], *added, *kept[anchor:]]


def _resolve_entry_point_string(env: Environment) -> str:
    # D16: empty (after os.environ.get default) is treated as unset.
    override = os.environ.get("MOONLIT_ENTRY_POINT", "")
    return override if override else env.entry_point


def _parse_entry_point(value: str) -> tuple[str, str]:
    parts = value.split(":")
    if len(parts) != 2:
        raise EntryPointError(f"invalid entry point: {value}")
    module, attr = parts
    if not module or not attr:
        raise EntryPointError(f"invalid entry point: {value}")
    if "" in attr.split("."):
        raise EntryPointError(f"invalid entry point: {value}")
    return module, attr


def _import_and_resolve(module_name: str, attr: str) -> object:
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise EntryPointError(f"cannot import {module_name}: {exc}") from exc
    obj: object = module
    for part in attr.split("."):
        try:
            obj = getattr(obj, part)
        except AttributeError as exc:
            raise EntryPointError(f"attribute {attr} not found on {module_name}") from exc
    return obj


def _coerce_return(result: object) -> int:
    if result is None:
        return 0
    # bool is int in Python; True → 1, False → 0 fall through here (spec §8).
    if isinstance(result, int):
        return result & 0xFF
    try:
        return int(cast(Any, result)) & 0xFF
    except (TypeError, ValueError) as exc:
        raise EntryPointError(
            f"entry point returned uncoercible value: {type(result).__name__}"
        ) from exc
