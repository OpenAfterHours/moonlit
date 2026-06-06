"""Extract archive site-packages into the per-build cache.

Implements the D14 fast-path / slow-path protocol, the D4 atomic-replace
directory swap, the D5 cache-key derivation, and the D1 arcname rule
(specs/03-bootstrap-runtime.md §6).

The single function that calls os.rename is named ``atomic_replace_dir``;
the D7 stdlib-only gate's allowlist is keyed on that exact name.
"""

import os
import posixpath
import re
import shutil
import zipfile
from pathlib import Path
from time import sleep

from . import progress
from .environment import Environment
from .errors import ExtractionError
from .locking import lock

_OS_REPLACE_RETRIES = 3
_OS_REPLACE_BACKOFF_S = 0.1


def materialize(
    env: Environment,
    cache_root: Path,
    archive_path: str | Path,
) -> Path:
    """Ensure the cache for ``env`` is populated; return the site-packages dir.

    Implements the D14 protocol: an unsynchronized fast-path read for the
    common cache-hit case, falling back to a locked slow path that extracts
    under the D4 atomic-replace dance and sweeps stale ``.old.<pid>`` siblings.
    """
    cache_key = _cache_key(env)
    site_parent = cache_root / cache_key
    site_dir = site_parent / "site-packages"

    # D14 fast path: no lock if cache hit and FORCE_EXTRACT unset.
    if site_dir.is_dir() and not _force_extract():
        return site_dir

    lock_path = cache_root / f"{cache_key}.lock"
    with lock(lock_path):
        # Re-check inside the lock; a sibling may have just won the race.
        if site_dir.is_dir() and not _force_extract():
            return site_dir

        tmp_dir = cache_root / f".{cache_key}.tmp.{os.getpid()}"
        reporter = progress.ExtractProgress(
            f"unpacking {env.name}", _total_extract_bytes(archive_path)
        )
        try:
            with reporter:
                _extract_to(archive_path, tmp_dir, reporter)
            atomic_replace_dir(tmp_dir, site_parent, os.getpid())
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

        _sweep_old_siblings(site_parent)

    return site_dir


def atomic_replace_dir(src: Path, dst: Path, pid: int) -> None:
    """D4 protocol: rename existing ``dst`` aside, replace, clean up.

    The ``os.rename`` calls in this function are intentional and audited; the
    bootstrap stdlib gate (test_bootstrap_stdlib_only.py) keys its allowlist
    on this exact function name.
    """
    old_path: Path | None = None
    if dst.exists():
        old_path = dst.with_name(f"{dst.name}.old.{pid}")
        os.rename(dst, old_path)
    try:
        _replace_with_retry(src, dst)
    except Exception:
        if old_path is not None and old_path.exists():
            os.rename(old_path, dst)
        raise
    if old_path is not None:
        shutil.rmtree(old_path, ignore_errors=True)


# ---------- private helpers, in materialize()'s call order ----------


def _cache_key(env: Environment) -> str:
    # D5: PEP 503 normalize the raw env.json name; build_id is already
    # lowercase hex per spec 05 §3.3.
    normalized = re.sub(r"[-_.]+", "-", env.name).lower()
    return f"{normalized}_{env.build_id}"


def _force_extract() -> bool:
    # D16: present and non-empty after os.environ.get is truthy.
    # MOONLIT_FORCE_EXTRACT="0" is non-empty hence truthy (spec 03 §9).
    return bool(os.environ.get("MOONLIT_FORCE_EXTRACT", ""))


def _total_extract_bytes(archive_path: str | Path) -> int:
    """Sum the uncompressed bytes of the ``site-packages/`` files (D1).

    Drives the progress percentage; matches what ``_extract_to`` will write.
    Directory markers and non-prefixed entries (``_bootstrap/``, env.json)
    contribute nothing.
    """
    with zipfile.ZipFile(archive_path, "r") as zf:
        return sum(
            info.file_size
            for info in zf.infolist()
            if info.filename.startswith("site-packages/") and not info.is_dir()
        )


def _extract_to(
    archive_path: str | Path,
    tmp_dir: Path,
    reporter: progress.ExtractProgress,
) -> None:
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir, ignore_errors=True)
    tmp_site_packages = tmp_dir / "site-packages"
    tmp_site_packages.mkdir(parents=True, exist_ok=False)

    bytes_done = 0
    with zipfile.ZipFile(archive_path, "r") as zf:
        for info in zf.infolist():
            arcname = info.filename
            if not arcname.startswith("site-packages/"):
                continue  # D1: only the site-packages/ prefix is extracted.
            rel = arcname[len("site-packages/") :]
            if not rel:
                continue  # bare directory marker
            normalized = posixpath.normpath(rel)
            _reject_unsafe_path(arcname, normalized)
            dest = tmp_site_packages / normalized
            _extract_one(zf, info, dest)
            bytes_done += info.file_size
            reporter.update(bytes_done)


def _reject_unsafe_path(arcname: str, normalized: str) -> None:
    if normalized.startswith("/") or ".." in normalized.split("/"):
        raise ExtractionError(
            f"archive entry has unsafe path after normalization: {arcname!r} -> {normalized!r}"
        )


def _extract_one(zf: zipfile.ZipFile, info: zipfile.ZipInfo, dest: Path) -> None:
    if info.is_dir():
        dest.mkdir(parents=True, exist_ok=True)
        return

    dest.parent.mkdir(parents=True, exist_ok=True)
    mode = info.external_attr >> 16
    is_symlink = (mode & 0o170000) == 0o120000

    if is_symlink:
        _extract_symlink(zf, info, dest)
        return

    with zf.open(info, "r") as src, open(dest, "wb") as out:
        shutil.copyfileobj(src, out)
    if mode and os.name != "nt":
        try:
            os.chmod(dest, mode & 0o7777)
        except OSError:
            pass  # best-effort per spec §11


def _extract_symlink(zf: zipfile.ZipFile, info: zipfile.ZipInfo, dest: Path) -> None:
    target = zf.read(info).decode("utf-8")
    if os.name != "nt":
        os.symlink(target, dest)
        return
    # Windows: follow the link target inside the archive and write resolved
    # bytes as a regular file (spec 03 §6 step 2).
    resolved = _resolve_archive_path(info.filename, target)
    try:
        target_info = zf.getinfo(resolved)
    except KeyError as exc:
        raise ExtractionError(
            f"symlink target not in archive: {info.filename!r} -> {target!r}"
        ) from exc
    with zf.open(target_info, "r") as src, open(dest, "wb") as out:
        shutil.copyfileobj(src, out)


def _resolve_archive_path(src_filename: str, target: str) -> str:
    if posixpath.isabs(target):
        return target.lstrip("/")
    return posixpath.normpath(posixpath.join(posixpath.dirname(src_filename), target))


def _replace_with_retry(src: Path, dst: Path) -> None:
    """3-attempt retry around ``os.replace`` to ride out transient holds.

    On Windows, AV/EDR may briefly hold extracted DLLs open (spec §6 step 4).
    """
    last: OSError | None = None
    for attempt in range(_OS_REPLACE_RETRIES):
        try:
            os.replace(src, dst)
            return
        except OSError as exc:
            last = exc
            if attempt < _OS_REPLACE_RETRIES - 1:
                sleep(_OS_REPLACE_BACKOFF_S)
    raise ExtractionError(
        f"os.replace({src} -> {dst}) failed after {_OS_REPLACE_RETRIES} "
        f"attempts: errno={last.errno if last else '?'} {last}"
    ) from last


def _sweep_old_siblings(site_parent: Path) -> None:
    """Best-effort cleanup of stale ``.old.<pid>`` siblings (D4)."""
    parent = site_parent.parent
    if not parent.is_dir():
        return
    prefix = f"{site_parent.name}.old."
    for entry in parent.iterdir():
        if entry.name.startswith(prefix):
            shutil.rmtree(entry, ignore_errors=True)
