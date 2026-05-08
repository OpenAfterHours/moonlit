"""moonlit zipapp bootstrap (stdlib-only, D7).

Shipped verbatim into every produced .pyz; runs before staged site-packages
reaches sys.path. The :func:`bootstrap` orchestrator below is invoked by the
generated ``__main__.py`` inside the .pyz:

    import sys
    from _bootstrap import bootstrap
    sys.exit(bootstrap())

Contract: specs/03-bootstrap-runtime.md §2 (operations), §3 (cache root),
§10 (error model). Exit codes per D3 runtime enumeration.
"""

import os
import sys
import traceback
import zipfile
from pathlib import Path

from . import environment, extract, runner
from .errors import ArchiveError, BootstrapError


def bootstrap() -> int:
    """Bootstrap entry point. Returns an int in [0, 255] for ``sys.exit``."""
    try:
        return _do_bootstrap()
    except BootstrapError as exc:
        print(f"moonlit: {exc}", file=sys.stderr)
        if _debug():
            traceback.print_exc(file=sys.stderr)
        return exc.exit_code


# ---------- internal orchestration, in bootstrap()'s call order ----------


def _do_bootstrap() -> int:
    archive = _resolve_archive()
    env = environment.load(archive)
    cache_root = _resolve_cache_root()
    _ensure_cache_root_exists(cache_root)
    site_dir = extract.materialize(env, cache_root, archive)
    return runner.run(env, site_dir)


def _resolve_archive() -> str:
    if not sys.argv or not sys.argv[0]:
        raise ArchiveError("cannot locate zipapp (sys.argv[0] is empty)")
    archive = os.path.abspath(sys.argv[0])
    if not zipfile.is_zipfile(archive):
        raise ArchiveError(f"not a moonlit zipapp: {archive}")
    return archive


def _resolve_cache_root() -> Path:
    # D16: present and non-empty after os.environ.get is truthy.
    override = os.environ.get("MOONLIT_ROOT", "")
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        if local_app_data:
            return Path(local_app_data) / "moonlit"
    return Path.home() / ".moonlit"


def _ensure_cache_root_exists(cache_root: Path) -> None:
    try:
        os.makedirs(cache_root, exist_ok=True)
    except OSError as exc:
        raise BootstrapError(
            f"cannot create cache root {cache_root}: {exc}"
        ) from exc


def _debug() -> bool:
    # D16: present and non-empty after os.environ.get is truthy.
    return bool(os.environ.get("MOONLIT_DEBUG", ""))
