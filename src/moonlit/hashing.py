"""Deterministic hashes over staging trees.

Two hashes live here, both used as cache keys:

* :func:`compute_build_id` — the *app* cache key (D6, spec 02 §4). SHA-256
  over sorted (relpath, content) pairs. Drives the bootstrap's site-packages
  extract cache.
* :func:`compute_python_fingerprint` — the *bundled-Python* cache key
  (D21/D22, spec 02 §4a). SHA-256 over sorted (arcname, CRC32) pairs, where
  CRC32 matches what ``zipfile`` records in the central directory. Drives
  the Rust launcher's bundled-Python extract cache; the launcher derives the
  same value by walking the produced .exe's central directory.

The two hashes are intentionally orthogonal so an app rebuild does not
invalidate the on-disk Python cache, and a uv-shipped CPython patch bump does
not invalidate the on-disk app cache.
"""

import hashlib
import zlib
from collections.abc import Iterator
from pathlib import Path


def compute_build_id(site_packages_root: Path) -> str:
    """Return the 64-char lowercase hex sha256 digest of the staging tree (D6)."""
    h = hashlib.sha256()
    for relpath in sorted(_iter_hashable_relpaths(site_packages_root)):
        h.update(relpath.encode("utf-8"))
        h.update(b"\0")
        h.update((site_packages_root / relpath).read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def compute_python_fingerprint(
    python_root: Path,
    *,
    arcname_prefix: str = "_python/",
) -> str:
    """Cross-language fingerprint over the bundled-Python tree (spec 02 §4a).

    The producer (this function) and the Rust launcher MUST agree on the
    output byte-for-byte. The launcher derives the same value by walking the
    `_python/*` entries it finds in the central directory; this function
    predicts what those entries will be from the source tree on disk.

    Args:
        python_root: directory containing the bundled Python tree (the dist
            root uv produced; ``python.exe`` lives at the top).
        arcname_prefix: prefix the zip writer will prepend when packing
            ``python_root`` files. Default ``"_python/"`` is what the build
            pipeline uses; exposed for tests.

    Returns:
        64-char lowercase hex SHA-256 digest.
    """
    h = hashlib.sha256()
    for arcname_bytes, crc in sorted(_iter_python_fingerprint_pairs(python_root, arcname_prefix)):
        h.update(arcname_bytes)
        h.update(b"\0")
        h.update(crc.to_bytes(4, "little"))
        h.update(b"\0")
    return h.hexdigest()


def _iter_hashable_relpaths(root: Path) -> Iterator[str]:
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if any(part == "__pycache__" for part in rel.parts):
            continue
        if rel.suffix == ".pyc":
            continue
        yield rel.as_posix()


def _iter_python_fingerprint_pairs(
    python_root: Path, arcname_prefix: str
) -> Iterator[tuple[bytes, int]]:
    for p in python_root.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(python_root).as_posix()
        arcname = (arcname_prefix + rel).encode("utf-8")
        crc = zlib.crc32(p.read_bytes()) & 0xFFFFFFFF
        yield arcname, crc
