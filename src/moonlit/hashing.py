"""Deterministic build_id over a staging tree.

build_id ties a build to its runtime cache key (D6 / specs/02-build-pipeline.md §4).
It is sha256 over sorted (relpath, content) pairs separated by null bytes.
__pycache__ segments and .pyc files are excluded so transient bytecode never
perturbs the cache key.
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
