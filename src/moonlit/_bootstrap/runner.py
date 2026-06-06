"""sys.path setup, entry-point resolution, and return-value coercion.

Implements specs/03-bootstrap-runtime.md §7 (collision check, addsitedir)
and §8 (entry-point parsing, import, getattr walk, invocation, return-value
coercion). Failures from this module raise either CollisionError (exit 1)
or EntryPointError (exit 2) per the D3 runtime enumeration.
"""

import importlib
import os
import site
from pathlib import Path
from typing import Any, cast

from .environment import Environment
from .errors import CollisionError, EntryPointError


def run(env: Environment, site_dir: Path) -> int:
    """Set up sys.path, resolve and invoke the entry point, coerce its return.

    Returns an int in [0, 255] suitable for ``sys.exit``.
    """
    _check_no_bootstrap_collision(site_dir)
    site.addsitedir(str(site_dir))
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
