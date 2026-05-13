"""Deterministic hash over the staging tree.

:func:`compute_build_id` is the app's cache key (D6, spec 02 §4). SHA-256
over sorted (relpath, content) pairs; drives the bootstrap's site-packages
extract cache.

A second hash, ``compute_python_fingerprint``, lived here in v0.3.0 to drive
the launcher's runtime extract of a bundled Python tree. That feature is gone
(D21 redesign moved the bundled Python to a sibling directory next to the
launcher — no runtime extraction, no fingerprint), so the function is gone
too.
"""

import hashlib
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
