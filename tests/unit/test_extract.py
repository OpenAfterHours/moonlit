"""Pin _bootstrap/extract.materialize to specs/03-bootstrap-runtime.md §6, D1/D4/D5/D14.

NB on test mode: same caveat as test_environment.py — these unit tests
exercise the extraction logic via direct import as a development-time TDD
harness; the e2e suite is the contract.
"""

import os
import zipfile
from pathlib import Path

import pytest

from moonlit._bootstrap import extract, locking
from moonlit._bootstrap.environment import Environment
from moonlit._bootstrap.errors import ExtractionError


# ---------- helpers / fixtures ----------


@pytest.fixture(autouse=True)
def _clean_force_extract(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests start with MOONLIT_FORCE_EXTRACT unset; opt in via setenv."""
    monkeypatch.delenv("MOONLIT_FORCE_EXTRACT", raising=False)


def make_pyz(path: Path, files: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for arcname, content in files.items():
            zf.writestr(arcname, content)
    return path


def env_with(name: str = "myapp", build_id: str | None = None) -> Environment:
    return Environment(
        schema_version=1,
        name=name,
        build_id=build_id or ("a" * 64),
        entry_point="myapp.cli:main",
        built_at="2026-05-08T15:23:01Z",
        moonlit_version="0.1.0",
        python_shebang="/usr/bin/env python3",
    )


# ---------- happy-path materialize ----------


def test_returns_populated_site_dir(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    archive = make_pyz(
        tmp_path / "app.pyz",
        {
            "site-packages/foo/__init__.py": b"# foo\n",
            "site-packages/foo/bar.py": b"x = 1\n",
        },
    )
    site_dir = extract.materialize(env_with(), cache_root, archive)
    assert site_dir.is_dir()
    assert site_dir.name == "site-packages"
    assert (site_dir / "foo" / "__init__.py").read_bytes() == b"# foo\n"
    assert (site_dir / "foo" / "bar.py").read_bytes() == b"x = 1\n"


def test_only_site_packages_prefix_is_extracted(tmp_path: Path) -> None:
    # D1: top-level zip entries other than site-packages/ stay in the archive.
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    archive = make_pyz(
        tmp_path / "app.pyz",
        {
            "site-packages/pkg/file.py": b"",
            "_bootstrap/__init__.py": b"# bootstrap\n",
            "__main__.py": b"",
            "env.json": b"{}",
        },
    )
    site_dir = extract.materialize(env_with(), cache_root, archive)
    assert (site_dir / "pkg" / "file.py").exists()
    assert not (site_dir / "_bootstrap").exists()
    assert not (site_dir.parent / "_bootstrap").exists()
    assert not (site_dir / "__main__.py").exists()
    assert not (site_dir / "env.json").exists()


def test_directory_markers_are_skipped(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    archive_path = tmp_path / "app.pyz"
    with zipfile.ZipFile(archive_path, "w") as zf:
        zf.writestr(zipfile.ZipInfo("site-packages/"), b"")
        zf.writestr(zipfile.ZipInfo("site-packages/sub/"), b"")
        zf.writestr("site-packages/sub/x.py", b"data\n")
    site_dir = extract.materialize(env_with(), cache_root, archive_path)
    assert (site_dir / "sub" / "x.py").read_bytes() == b"data\n"


def test_returned_path_is_under_cache_root(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    archive = make_pyz(tmp_path / "app.pyz", {"site-packages/x.py": b""})
    site_dir = extract.materialize(env_with(), cache_root, archive)
    assert site_dir.is_relative_to(cache_root)


# ---------- D5: cache-key derivation ----------


def test_cache_key_uses_pep503_normalized_name(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    archive = make_pyz(tmp_path / "app.pyz", {"site-packages/x.py": b""})
    env = env_with(name="My_App.Name", build_id="b" * 64)
    site_dir = extract.materialize(env, cache_root, archive)
    assert site_dir.parent.name == f"my-app-name_{'b' * 64}"


def test_cache_key_appends_build_id(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    archive = make_pyz(tmp_path / "app.pyz", {"site-packages/x.py": b""})
    env = env_with(name="myapp", build_id="c" * 64)
    site_dir = extract.materialize(env, cache_root, archive)
    assert site_dir.parent.name == f"myapp_{'c' * 64}"


# ---------- D14: fast path ----------


def test_fast_path_does_not_re_extract(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    archive = make_pyz(tmp_path / "app.pyz", {"site-packages/foo.py": b"original\n"})
    env = env_with()

    site_dir = extract.materialize(env, cache_root, archive)
    (site_dir / "foo.py").write_bytes(b"mutated\n")

    site_dir2 = extract.materialize(env, cache_root, archive)
    assert site_dir2 == site_dir
    assert (site_dir / "foo.py").read_bytes() == b"mutated\n"


def test_fast_path_does_not_acquire_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # arch §10 falsifier: the fast path must take no lock when cache is hit.
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    archive = make_pyz(tmp_path / "app.pyz", {"site-packages/foo.py": b"x\n"})
    env = env_with()
    extract.materialize(env, cache_root, archive)  # cold cache → extracts

    def must_not_acquire(_path: object) -> None:
        raise AssertionError("fast path acquired lock; D14 violated")

    monkeypatch.setattr(extract, "lock", must_not_acquire)
    site_dir = extract.materialize(env, cache_root, archive)
    assert site_dir.is_dir()


# ---------- MOONLIT_FORCE_EXTRACT (D16, spec 03 §9) ----------


def test_force_extract_re_extracts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    archive = make_pyz(tmp_path / "app.pyz", {"site-packages/foo.py": b"original\n"})
    env = env_with()
    site_dir = extract.materialize(env, cache_root, archive)
    (site_dir / "foo.py").write_bytes(b"mutated\n")

    monkeypatch.setenv("MOONLIT_FORCE_EXTRACT", "1")
    site_dir = extract.materialize(env, cache_root, archive)
    assert (site_dir / "foo.py").read_bytes() == b"original\n"


def test_force_extract_empty_string_is_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # D16: empty after os.environ.get is treated as unset.
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    archive = make_pyz(tmp_path / "app.pyz", {"site-packages/foo.py": b"x\n"})
    env = env_with()

    monkeypatch.setenv("MOONLIT_FORCE_EXTRACT", "")
    site_dir = extract.materialize(env, cache_root, archive)
    (site_dir / "foo.py").write_bytes(b"mutated\n")
    site_dir = extract.materialize(env, cache_root, archive)
    assert (site_dir / "foo.py").read_bytes() == b"mutated\n"


def test_force_extract_zero_is_truthy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # spec 03 §9: '0' is non-empty hence truthy.
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    archive = make_pyz(tmp_path / "app.pyz", {"site-packages/foo.py": b"x\n"})
    env = env_with()
    site_dir = extract.materialize(env, cache_root, archive)
    (site_dir / "foo.py").write_bytes(b"mutated\n")

    monkeypatch.setenv("MOONLIT_FORCE_EXTRACT", "0")
    site_dir = extract.materialize(env, cache_root, archive)
    assert (site_dir / "foo.py").read_bytes() == b"x\n"


# ---------- path-traversal defense (spec §6 step 2) ----------


def test_dotdot_in_arcname_raises(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    archive_path = tmp_path / "evil.pyz"
    with zipfile.ZipFile(archive_path, "w") as zf:
        info = zipfile.ZipInfo("site-packages/../escape.py")
        zf.writestr(info, b"pwned\n")
    with pytest.raises(ExtractionError):
        extract.materialize(env_with(), cache_root, archive_path)


def test_deep_dotdot_in_arcname_raises(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    archive_path = tmp_path / "evil.pyz"
    with zipfile.ZipFile(archive_path, "w") as zf:
        info = zipfile.ZipInfo("site-packages/foo/../../escape.py")
        zf.writestr(info, b"pwned\n")
    with pytest.raises(ExtractionError):
        extract.materialize(env_with(), cache_root, archive_path)


def test_internal_dotdot_that_normalizes_safely_is_accepted(tmp_path: Path) -> None:
    # site-packages/foo/../bar.py normalizes to bar.py — safe.
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    archive_path = tmp_path / "app.pyz"
    with zipfile.ZipFile(archive_path, "w") as zf:
        info = zipfile.ZipInfo("site-packages/foo/../bar.py")
        zf.writestr(info, b"safe\n")
    site_dir = extract.materialize(env_with(), cache_root, archive_path)
    assert (site_dir / "bar.py").read_bytes() == b"safe\n"


# ---------- D4: atomic_replace_dir ----------


def test_atomic_replace_dir_handles_nonexistent_destination(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "f").write_text("x", encoding="utf-8")
    dst = tmp_path / "dst"

    extract.atomic_replace_dir(src, dst, pid=12345)
    assert dst.is_dir()
    assert (dst / "f").read_text(encoding="utf-8") == "x"
    assert not src.exists()


def test_atomic_replace_dir_replaces_populated_destination(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "new").write_text("new", encoding="utf-8")

    dst = tmp_path / "dst"
    dst.mkdir()
    (dst / "old").write_text("old", encoding="utf-8")

    extract.atomic_replace_dir(src, dst, pid=12345)
    assert (dst / "new").read_text(encoding="utf-8") == "new"
    assert not (dst / "old").exists()
    assert not src.exists()
    # The .old.<pid> sibling was best-effort cleaned.
    assert not dst.with_name(f"{dst.name}.old.12345").exists()


def test_force_extract_replaces_populated_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end D4 via the materialize path: re-extraction over a populated cache."""
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    archive = make_pyz(
        tmp_path / "app.pyz",
        {
            "site-packages/foo.py": b"original\n",
            "site-packages/sub/bar.py": b"baz\n",
        },
    )
    env = env_with()
    site_dir = extract.materialize(env, cache_root, archive)
    assert (site_dir / "foo.py").read_bytes() == b"original\n"
    assert (site_dir / "sub" / "bar.py").read_bytes() == b"baz\n"

    monkeypatch.setenv("MOONLIT_FORCE_EXTRACT", "1")
    site_dir = extract.materialize(env, cache_root, archive)
    assert (site_dir / "foo.py").read_bytes() == b"original\n"
    assert (site_dir / "sub" / "bar.py").read_bytes() == b"baz\n"


# ---------- tmp_dir lifecycle ----------


def test_tmp_dir_is_cleaned_after_success(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    archive = make_pyz(tmp_path / "app.pyz", {"site-packages/foo.py": b"x\n"})
    extract.materialize(env_with(), cache_root, archive)
    leftovers = [p.name for p in cache_root.iterdir() if p.name.startswith(".")]
    assert leftovers == [], f"unexpected tmp/dot entries left in cache: {leftovers}"


def test_tmp_dir_is_cleaned_after_extraction_error(
    tmp_path: Path,
) -> None:
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    archive_path = tmp_path / "evil.pyz"
    with zipfile.ZipFile(archive_path, "w") as zf:
        info = zipfile.ZipInfo("site-packages/../escape.py")
        zf.writestr(info, b"pwned\n")
    with pytest.raises(ExtractionError):
        extract.materialize(env_with(), cache_root, archive_path)
    # No partially-extracted tmp dir should remain.
    leftovers = [p.name for p in cache_root.iterdir() if p.name.startswith(".")]
    assert leftovers == []


# ---------- stale .old.<pid> sweep ----------


def test_stale_old_siblings_are_swept(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    archive = make_pyz(tmp_path / "app.pyz", {"site-packages/foo.py": b"x\n"})
    env = env_with()
    site_dir = extract.materialize(env, cache_root, archive)

    # Manually plant a stale .old.<otherpid> sibling.
    site_parent = site_dir.parent
    stale = site_parent.with_name(f"{site_parent.name}.old.99999")
    stale.mkdir()
    (stale / "stale.py").write_text("stale", encoding="utf-8")

    os.environ["MOONLIT_FORCE_EXTRACT"] = "1"
    try:
        extract.materialize(env, cache_root, archive)
    finally:
        os.environ.pop("MOONLIT_FORCE_EXTRACT", None)
    assert not stale.exists()


# ---------- lock is acquired on slow path ----------


def test_slow_path_acquires_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    archive = make_pyz(tmp_path / "app.pyz", {"site-packages/foo.py": b"x\n"})
    env = env_with()

    real_acquire = locking.acquire
    acquired: list[Path] = []

    def tracking_acquire(lock_path: object) -> int:
        acquired.append(Path(str(lock_path)))
        return real_acquire(lock_path)

    monkeypatch.setattr(locking, "acquire", tracking_acquire)
    extract.materialize(env, cache_root, archive)  # cold cache → slow path
    assert len(acquired) == 1
    expected_lock = cache_root / f"myapp_{'a' * 64}.lock"
    assert acquired[0] == expected_lock
